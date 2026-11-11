"""Telegram : messages (texte et vocaux), commandes, boutons, briefing matinal, bilan hebdo,
détection de nouveaux runs avec ressenti, alertes météo."""
import asyncio
import datetime
import functools
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import agent, coaching, commands, dreeve, voice, weather, weather_alerts
from . import calendar_store as cs
from .config import TZ, settings
from .formatting import md_escape, md_unescape, split_message
from .memory import LEGS, PANTRY_ASK_GRACE_DAYS, store

logger = logging.getLogger(__name__)

MORNING_PROMPT = (
    "Réalise le briefing matinal. Analyse l'agenda Radicale, la météo, la charge, le volume hebdo, les derniers runs Dreeve, "
    "mes ressentis et le compte à rebours de mes objectifs, en appliquant tout ce que tu sais de moi. "
    f"Met à jour / planifie les {settings.planning_horizon_days} prochains jours dans Radicale, en cohérence avec la "
    "périodisation vers mon objectif principal. "
    "Fais un résumé Telegram ultra-concis, sobre, avec des descriptions structurées et des émojis."
)

DEBRIEF_PROMPT = (
    "{intro} :\n{runs}\n\n"
    "Fais-moi spontanément un retour de coach, concis et factuel, en 4 blocs courts :\n"
    "1. 🏃 *La séance* : ce qui était prévu dans l'agenda vs ce que j'ai fait. Regarde tours/splits via Dreeve si utile. "
    "Si la séance prévue était un jour passé, relis l'agenda de ce jour-là avec list_calendar_events.\n"
    "2. 📈 *Par rapport à mes performances passées* : compare avec mes séances similaires récentes "
    "(allure, FC, dénivelé) et la tendance des dernières semaines, en t'appuyant sur les métriques Dreeve ; "
    "signale un progrès net ou un signal d'alerte.\n"
    "3. 🔋 *Forme et prochaines séances* : avec la charge (TSB, ratio A/C), mes ressentis, la météo et ce qu'il y a "
    "dans l'agenda les prochains jours, dis si le programme reste adapté. Si un ajustement est conseillé, "
    "enregistre-le avec propose_change sans le faire.\n"
    "4. 🎯 *Objectifs* : où j'en suis par rapport à mes objectifs (compte à rebours, phase de préparation) et le point clé "
    "à travailler ensuite. Si tu ne connais aucun objectif, demande-le-moi en une phrase.\n"
    "Ne modifie pas l'agenda."
)

REVIEW_PROMPT = (
    "Fais le bilan de ma semaine. Chiffres calculés par le système (fiables, ne les recalcule pas) :\n{adherence}\n\n"
    "En 4 blocs courts et factuels :\n"
    "1. 📊 *La semaine* : régularité (prévu vs fait), volume, intensité, ce qui a manqué et pourquoi c'est important ou non.\n"
    "2. 🔋 *Forme* : charge Dreeve et ressentis (effort perçu, jambes) : fatigue qui s'accumule ou bonne assimilation ?\n"
    "3. 🎯 *Objectifs* : où j'en suis (compte à rebours, phase de préparation) et ce que la semaine change.\n"
    "4. 📅 *Semaine prochaine* : les 2-3 séances clés prévues dans l'agenda et l'intention de la semaine. "
    "Si le programme mérite un ajustement, enregistre-le avec propose_change sans le faire.\n"
    "Ne modifie pas l'agenda."
)

APPLY_PROMPT = "Applique maintenant cette proposition que tu m'as faite et que je valide : {text}"
PAIN_FOLLOW_UP = ("😬 Où as-tu mal, depuis quand, et est-ce que ça gêne à la marche ? "
                  "Réponds-moi (texte ou vocal), j'adapterai la suite.")


async def send_text(send, text: str, reply_markup=None) -> None:
    """send : coroutine(text, **kwargs). Markdown d'abord, texte brut si Telegram refuse le balisage.
    Les boutons éventuels sont attachés au dernier morceau."""
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        extra = {"reply_markup": reply_markup} if reply_markup is not None and i == len(chunks) - 1 else {}
        try:
            await send(chunk, parse_mode=ParseMode.MARKDOWN, **extra)
        except BadRequest:
            await send(md_unescape(chunk), **extra)


def proposals_markup(proposals: list[tuple[int, str]]) -> tuple[str, InlineKeyboardMarkup | None]:
    """Pied de message listant les propositions (texte généré par le code) + boutons."""
    if not proposals:
        return "", None
    if len(proposals) == 1:
        pid, text = proposals[0]
        footer = f"*💡 Proposition* : {md_escape(text)}"
        rows = [[InlineKeyboardButton("✅ Appliquer", callback_data=f"p:a:{pid}"),
                 InlineKeyboardButton("✏️ Ajuster", callback_data=f"p:e:{pid}"),
                 InlineKeyboardButton("❌ Ignorer", callback_data=f"p:x:{pid}")]]
        return footer, InlineKeyboardMarkup(rows)
    lines = ["*💡 Propositions*"] + [f"{i}. {md_escape(text)}" for i, (_, text) in enumerate(proposals, 1)]
    rows = [[InlineKeyboardButton(f"✅ {i}", callback_data=f"p:a:{pid}"),
             InlineKeyboardButton(f"✏️ {i}", callback_data=f"p:e:{pid}"),
             InlineKeyboardButton(f"❌ {i}", callback_data=f"p:x:{pid}")] for i, (pid, _) in enumerate(proposals, 1)]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


MEAL_HINT = "_Touche un plat une fois mangé ou cuisiné : ses ingrédients sortent du stock._"


def meal_rows(meals: list[tuple[int, str]]) -> list[list[InlineKeyboardButton]]:
    """Un bouton « mangé / cuisiné » par plat proposé (retire ses ingrédients du stock, sans IA)."""
    return [[InlineKeyboardButton(f"✅ {title}"[:60], callback_data=f"m:{mid}")] for mid, title in meals]


def open_meals(ids: list[int]) -> list[tuple[int, str]]:
    """Plats encore à confirmer parmi `ids` (déjà touchés ou expirés : plus de bouton)."""
    return [(mid, item["text"]) for mid in ids
            if (item := store.proposal(mid)) and item.get("kind") == "meal" and item["status"] == "open"]


def without_button(markup: InlineKeyboardMarkup | None, data: str) -> InlineKeyboardMarkup | None:
    """Le même clavier sans la ligne du bouton `data` ; None s'il ne reste rien."""
    rows = [list(row) for row in (markup.inline_keyboard if markup else ()) if all(b.callback_data != data for b in row)]
    return InlineKeyboardMarkup(rows) if rows else None


def pantry_check_markup(items: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ {p['name']}"[:40], callback_data=f"k:y:{p['id']}"),
                                  InlineKeyboardButton("🗑️ Fini", callback_data=f"k:n:{p['id']}")] for p in items])


def shopping_text(lst: dict) -> str:
    done = sum(i["done"] for i in lst["items"])
    total = len(lst["items"])
    status = "tout est dans le panier 🎉" if done == total else f"{done}/{total} ✅"
    lines = [f"🛒 *Liste de courses* — {status}"]
    if summary := cs.shopping_summary_line(lst):
        lines.append(f"📆 {md_escape(summary)}")
    if lst.get("coverage"):
        lines.append(f"🍽️ {md_escape(lst['coverage'])}")
    lines.append("_Touche un article quand il est dans le panier. Recettes : /recipes_")
    return "\n".join(lines)


def shopping_markup(lst: dict) -> InlineKeyboardMarkup:
    """Un bouton par article (☐ → ✅), précédé d'un bouton-titre par rayon ; « Tout décocher » en bas."""
    lid, rows = lst["id"], []
    for aisle, entries in cs.group_by_aisle(lst["items"]).items():
        rows.append([InlineKeyboardButton(f"— {aisle} —", callback_data="s:n")])
        rows += [[InlineKeyboardButton(f"{'✅' if item['done'] else '☐'} {cs.item_label(item)}", callback_data=f"s:t:{lid}:{i}")]
                 for i, item in entries]
    rows.append([InlineKeyboardButton("🧹 Tout décocher", callback_data=f"s:r:{lid}")])
    return InlineKeyboardMarkup(rows)


def rpe_markup(activity_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(str(n), callback_data=f"c:r:{activity_id}:{n}") for n in row]
                                 for row in ((1, 2, 3, 4, 5), (6, 7, 8, 9, 10))])


def legs_markup(activity_id) -> InlineKeyboardMarkup:
    icons = {"fraiches": "🟢", "normales": "🟡", "lourdes": "🟠", "douleur": "🔴"}
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{icons[k]} {label.capitalize()}", callback_data=f"c:l:{activity_id}:{k}")
                                  for k, label in LEGS.items()]])


class CoachBot:
    def __init__(self, app: Application):
        self.app = app
        self.chat_id = settings.telegram_chat_id
        self._activity_lock = asyncio.Lock()   # jamais deux détections en parallèle (pas de double débrief)
        self._shopping_sync_lock = asyncio.Lock()   # écritures de la checklist dans l'agenda courses, une à la fois
        self._background: set[asyncio.Task] = set()

    # --- Envoi ---------------------------------------------------------------

    def _sender(self, update: Update | None = None):
        if update is not None and update.effective_message is not None:
            return update.effective_message.reply_text
        return functools.partial(self.app.bot.send_message, chat_id=self.chat_id)

    async def _push(self, text: str, reply_markup=None) -> None:
        await send_text(self._sender(), text, reply_markup)

    async def _deliver(self, reply: agent.AgentReply, header: str = "", update: Update | None = None) -> None:
        """Réponse de l'agent + propositions (pied de message et boutons)."""
        footer, markup = proposals_markup(reply.proposals)
        if reply.meals:
            footer = "\n\n".join(part for part in (footer, MEAL_HINT) if part)
            markup = InlineKeyboardMarkup([*(markup.inline_keyboard if markup else ()), *meal_rows(reply.meals)])
        text = "\n\n".join(part for part in (header, reply.text, footer) if part)
        await send_text(self._sender(update), text, markup)
        if reply.shopping_list is not None and (lst := store.shopping_list) and lst["id"] == reply.shopping_list:
            await send_text(self._sender(update), shopping_text(lst), shopping_markup(lst))

    async def _authorized(self, update: Update) -> bool:
        """Le bot peut modifier l'agenda : il ne répond qu'au chat TELEGRAM_CHAT_ID.

        Tant que TELEGRAM_CHAT_ID n'est pas défini, il se contente de donner l'id du chat
        (mode configuration), pour que personne ne puisse piloter l'agenda d'un bot fraîchement installé.
        """
        chat_id = str(update.effective_chat.id)
        if not self.chat_id:
            logger.warning(f"Mode configuration : message reçu du chat {chat_id}. Mets TELEGRAM_CHAT_ID={chat_id} dans .env.")
            if update.message:
                await update.message.reply_text(
                    f"👋 Configuration : ajoute TELEGRAM_CHAT_ID={chat_id} dans ton .env puis redémarre le conteneur."
                )
            return False
        if chat_id != self.chat_id:
            logger.warning(f"Message ignoré d'un chat non autorisé : {chat_id}")
            return False
        return True

    # --- Conversation --------------------------------------------------------

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text or not await self._authorized(update):
            return
        await self._ask_and_reply(update, update.message.text)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Vocal → transcription locale (Whisper) → traité comme un message écrit."""
        if not update.message or not await self._authorized(update):
            return
        media = update.message.voice or update.message.audio
        if not voice.enabled():
            await update.message.reply_text("🎙️ Les messages vocaux sont désactivés (WHISPER_MODEL=off). Écris-moi plutôt.")
            return
        if media.duration and media.duration > voice.MAX_SECONDS:
            await update.message.reply_text(f"🎙️ Vocal trop long (max {voice.MAX_SECONDS // 60} min). Fais plus court ou écris-moi.")
            return
        typing = asyncio.create_task(self._keep_typing(update))
        try:
            data = await (await media.get_file()).download_as_bytearray()
            text = await asyncio.to_thread(voice.transcribe, bytes(data))
        except Exception as e:
            logger.exception("Transcription en échec")
            await update.message.reply_text(f"❌ Je n'ai pas pu transcrire ce vocal ({type(e).__name__}). Réessaie ou écris-moi.")
            return
        finally:
            typing.cancel()
        if not text:
            await update.message.reply_text("🎙️ Je n'ai rien entendu de clair. Tu peux réessayer ?")
            return
        await update.message.reply_text(f"🎙️ « {text} »")
        await self._ask_and_reply(update, text)

    async def _ask_and_reply(self, update: Update, prompt: str, allow_writes: bool = True,
                             planning: bool = False, header: str = "") -> None:
        typing = asyncio.create_task(self._keep_typing(update))
        try:
            reply = await agent.ask(prompt, chat_id=str(update.effective_chat.id), allow_writes=allow_writes, planning=planning)
        except Exception as e:
            logger.exception("Erreur non gérée")
            reply = agent.AgentReply(f"❌ Erreur technique : {type(e).__name__}. Rien n'est garanti côté Radicale, vérifie l'agenda.")
        finally:
            typing.cancel()
        await self._deliver(reply, header, update)

    @staticmethod
    async def _keep_typing(update: Update) -> None:
        # L'indicateur « écrit… » expire après ~5 s : on le renouvelle pendant que l'agent travaille.
        try:
            while True:
                await update.effective_chat.send_action(ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("send_action échoué", exc_info=True)

    # --- Commandes -----------------------------------------------------------

    async def _command(self, update: Update, build) -> None:
        """Commande instantanée : `build()` (synchrone, exécuté hors boucle) renvoie le texte Markdown."""
        if not update.message or not await self._authorized(update):
            return
        try:
            text = await asyncio.to_thread(build)
        except Exception as e:
            logger.exception("Commande en échec")
            text = f"❌ Indisponible : {md_escape(str(e))}"
        await send_text(update.message.reply_text, text)

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command(update, lambda: commands.HELP_TEXT)

    async def on_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/memory : ce que le coach a retenu (pour corriger : « oublie le #3 », « en fait je préfère… »)."""
        await self._command(update, lambda: (
            "*🧠 Ce que je sais de toi*\n" + md_escape(store.render())
            + "\n\n_Pour corriger : « oublie le #3 » ou dis-moi simplement ce qui a changé._"))

    async def on_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/list : réaffiche la liste de courses en cours, à cocher (sans IA)."""
        if not update.message or not await self._authorized(update):
            return
        lst = store.shopping_list
        if lst is None:
            await update.message.reply_text("🛒 Pas de liste de courses en cours. Lance /shopping pour en créer une.")
            return
        await send_text(update.message.reply_text, shopping_text(lst), shopping_markup(lst))

    async def on_recipes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/recipes : recettes de la liste en cours (générées une fois, puis réaffichées sans IA) ; /recipes new : d'autres."""
        if not update.message or not await self._authorized(update):
            return
        lst = store.shopping_list
        if lst is None:
            await update.message.reply_text("🍳 Pas de liste de courses en cours : lance d'abord /shopping.")
            return
        regenerate = bool(context.args) and context.args[0].lower() in ("new", "nouvelles", "autres")
        if lst.get("recipes") and not regenerate:
            meals = open_meals(lst.get("recipe_meals") or [])
            await send_text(update.message.reply_text, lst["recipes"] + (f"\n\n{MEAL_HINT}" if meals else ""),
                            InlineKeyboardMarkup(meal_rows(meals)) if meals else None)
            return
        typing = asyncio.create_task(self._keep_typing(update))
        try:
            reply = await agent.ask(commands.recipes_prompt(lst), chat_id=str(update.effective_chat.id), allow_writes=False)
        except Exception as e:
            logger.exception("Recettes en échec")
            reply = agent.AgentReply(f"❌ Recettes impossibles : {type(e).__name__}")
        finally:
            typing.cancel()
        if not reply.text.startswith("❌"):
            store.set_recipes(lst["id"], reply.text, [mid for mid, _ in reply.meals])
            if lst.get("event_uid"):   # les recettes rejoignent la checklist dans l'événement courses
                await self._sync_shopping_event(lst["id"])
        await self._deliver(reply, update=update)

    async def on_eat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/eat : quoi manger maintenant (stock, heure, entraînement), questions si besoin. Haiku, sans écriture d'agenda."""
        if not update.message or not await self._authorized(update):
            return
        await self._ask_and_reply(update, commands.eat_prompt(datetime.datetime.now(TZ)), allow_writes=False)

    async def on_stock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command(update, lambda: commands.format_stock_reply(store.pantry, datetime.datetime.now(TZ).date()))

    async def on_goals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command(update, lambda: commands.format_goals_reply(store.goals, datetime.datetime.now(TZ).date()))

    async def on_agenda(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        days = commands.parse_days(context.args or [])

        def build():
            today = datetime.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            events, _ = cs.list_events(today, today + datetime.timedelta(days=days))
            return commands.format_agenda(events, days)
        await self._command(update, build)

    async def on_form(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def build():
            if not settings.dreeve_enabled:
                return "Dreeve n'est pas configuré."
            runs, today = dreeve.get_runs_since_weeks(4)
            return commands.format_form(dreeve.get_load(), runs, today)
        await self._command(update, build)

    async def on_last_runs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def build():
            if not settings.dreeve_enabled:
                return "Dreeve n'est pas configuré."
            return commands.format_last_runs(dreeve.get_recent_runs(5))
        await self._command(update, build)

    async def on_weather(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command(update, lambda: commands.format_weather(weather.get_forecast_text()))

    async def on_debrief(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not await self._authorized(update):
            return
        try:
            runs = await asyncio.to_thread(dreeve.get_recent_runs, 1) if settings.dreeve_enabled else []
        except Exception as e:
            await update.message.reply_text(f"❌ Dreeve indisponible : {e}")
            return
        if not runs:
            await update.message.reply_text("Aucun run trouvé dans Dreeve.")
            return
        prompt = DEBRIEF_PROMPT.format(intro="Débriefe ma dernière séance", runs=dreeve.format_activity(runs[0]))
        await self._ask_and_reply(update, prompt, allow_writes=False)
        await self._ask_checkin(runs[0], only_if_missing=True)

    async def on_shopping(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not await self._authorized(update):
            return
        days = commands.parse_days(context.args or [], commands.SHOPPING_DEFAULT_DAYS, commands.SHOPPING_MAX_DAYS)
        prompt = commands.shopping_prompt(days, datetime.datetime.now(TZ).date(), bool(settings.radicale_shopping_url))
        await self._ask_and_reply(update, prompt, allow_writes=False)

    async def on_briefing(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not await self._authorized(update):
            return
        await self._ask_and_reply(update, MORNING_PROMPT.replace("le briefing matinal", "un briefing"))

    async def on_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not await self._authorized(update):
            return
        try:
            prompt = await asyncio.to_thread(self._review_prompt)
        except Exception as e:
            await update.message.reply_text(f"❌ Bilan impossible : {e}")
            return
        await self._ask_and_reply(update, prompt, allow_writes=False, planning=True, header="📊 *Bilan de la semaine*")

    # --- Boutons -------------------------------------------------------------

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not await self._authorized(update):
            return
        parts = (query.data or "").split(":")
        try:
            if parts[0] == "p":
                await self._on_proposal(update, parts[1], parts[2])
            elif parts[0] == "c":
                await self._on_checkin(update, parts[1], parts[2], parts[3])
            elif parts[0] == "s":
                await self._on_shopping_button(update, parts)
            elif parts[0] == "m":
                await self._on_meal(update, parts[1])
            elif parts[0] == "q":
                await self._on_meal_question(update, parts[1], parts[2], int(parts[3]))
            elif parts[0] == "k":
                await self._on_pantry_check(update, parts[1] == "y", parts[2])
            else:
                await query.answer()
        except Exception:
            logger.exception(f"Bouton en échec : {query.data}")
            await query.answer("❌ Erreur technique", show_alert=True)

    async def _on_proposal(self, update: Update, action: str, pid: str) -> None:
        query = update.callback_query
        status = {"a": "applied", "e": "adjusting", "x": "ignored"}[action]
        item = store.close_proposal(pid, status)
        if item is None:
            await query.answer("Déjà traité.")
            return
        await query.answer()
        await query.edit_message_reply_markup(None)   # plus de double clic possible
        if action == "a":
            prompt = item["text"] if item["kind"] == "weather" else APPLY_PROMPT.format(text=item["text"])
            await self._ask_and_reply(update, prompt)
        elif action == "e":
            await agent.add_to_history(self.chat_id, f"[L'athlète veut ajuster ta proposition : {item['text']}]",
                                       "Dis-moi ce que tu veux changer.")
            await self._push("✏️ Dis-moi ce que tu veux changer (texte ou vocal), je m'adapte.")
        else:
            await agent.add_to_history(self.chat_id, f"[L'athlète a ignoré ta proposition : {item['text']}]",
                                       "D'accord, on garde le programme tel quel.")
            await self._push("👍 On garde le programme tel quel.")

    async def _on_meal(self, update: Update, mid: str) -> None:
        """Plat mangé / cuisiné : ses ingrédients sortent du stock (ou passent à ce qu'il en reste)."""
        query = update.callback_query
        item = store.proposal(mid)
        if not item or item.get("kind") != "meal" or store.close_proposal(mid, "applied") is None:
            await query.answer("Déjà noté.")
            return
        r = store.update_pantry(remove=item.get("uses") or [])
        await query.answer()
        await query.edit_message_reply_markup(without_button(query.message.reply_markup, f"m:{mid}"))
        changes = [f"−{x}" for x in r["removed"]] + r["updated"]
        if item.get("makes"):   # batch cooking : les boîtes entrent au stock, rien n'est encore mangé
            store.update_pantry(add=[{"name": item["text"], "qty": item["makes"], "keeps_days": 4}])
            changes.append(f"+{item['text']} ({item['makes']})")
            text = f"🍳 *{md_escape(item['text'])}* cuisiné."
        else:                   # mangé : entre dans le journal alimentaire
            macros = item.get("macros") or {}
            store.log_meal(item["text"], datetime.datetime.now(TZ), source="bouton", **macros)
            text = f"🍽️ *{md_escape(item['text'])}* noté."
        if changes:
            text += f"\n🥫 Stock : {md_escape(', '.join(changes))}"
        if r["not_found"]:
            text += f"\n_Pas dans le stock : {md_escape(', '.join(r['not_found']))}_"
        await self._push(text)
        await agent.add_to_history(self.chat_id, f"[L'athlète a confirmé avoir mangé / cuisiné : {item['text']}]", md_unescape(text))

    async def _on_meal_question(self, update: Update, action: str, day: str, index: int) -> None:
        """« Qu'as-tu mangé ? » : Sauté (noté comme repas vide, à la fin de sa plage) ou Passer (on n'en parle plus)."""
        query = update.callback_query
        await query.answer()
        _, end, slot = coaching.meal_window(settings.meal_check_times, index)
        key = f"{day} {slot}"
        if action == "s":
            at = datetime.datetime.combine(datetime.date.fromisoformat(day), end, tzinfo=TZ)
            store.log_meal(f"rien ({slot} sauté)", at, source="bouton", kcal=0, protein_g=0, carbs_g=0)
            store.set_meal_check(key, "answered")
            text = f"🙅 Noté : {slot} sauté."
        else:
            store.set_meal_check(key, "skipped")
            text = "⏭️ D'accord, on passe."
        await query.edit_message_reply_markup(None)
        await self._push(text)
        await agent.add_to_history(self.chat_id, f"[Réponse à « qu'as-tu mangé ? » ({slot})]", text)

    async def _on_pantry_check(self, update: Update, still_there: bool, pantry_id: str) -> None:
        """Réponse à « Encore là ? » : la ligne de l'article disparaît du message."""
        query = update.callback_query
        item = store.answer_pantry_check(int(pantry_id), still_there, datetime.datetime.now(TZ).date())
        if item is None:
            await query.answer("Déjà retiré du stock.")
        else:
            await query.answer(f"{item['name']} : encore là 👍" if still_there else f"{item['name']} : retiré du stock 🗑️")
        markup = without_button(query.message.reply_markup, f"k:y:{pantry_id}")
        if markup is None:
            await query.edit_message_text("🥫 Stock vérifié ✅")
        else:
            await query.edit_message_reply_markup(markup)

    async def _on_shopping_button(self, update: Update, parts: list[str]) -> None:
        query = update.callback_query
        if parts[1] == "n":   # titre de rayon : rien à faire
            await query.answer()
            return
        lst = (store.toggle_shopping_item(parts[2], int(parts[3])) if parts[1] == "t"
               else store.reset_shopping(parts[2]))
        if lst is None:
            await query.answer("Cette liste a été remplacée : /list affiche la liste en cours.", show_alert=True)
            return
        await query.answer()
        try:
            await query.edit_message_text(shopping_text(lst), parse_mode=ParseMode.MARKDOWN, reply_markup=shopping_markup(lst))
        except BadRequest as e:
            if "not modified" not in str(e).lower():   # « Tout décocher » sur une liste déjà vierge
                raise
        if lst.get("event_uid"):
            task = asyncio.create_task(self._sync_shopping_event(lst["id"]))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _sync_shopping_event(self, list_id: int) -> None:
        """Recopie les cases cochées dans la description de l'événement courses. Les écritures passent une par une
        et relisent l'état courant : après une série de clics rapides, l'agenda finit sur le dernier état."""
        async with self._shopping_sync_lock:
            lst = store.shopping_list
            if lst is None or lst["id"] != list_id or not lst.get("event_uid"):
                return
            try:
                await asyncio.to_thread(cs.set_shopping_description, lst["event_uid"], cs.shopping_description(lst))
            except Exception as e:
                logger.error(f"Checklist non recopiée dans l'agenda courses : {e}")

    async def _on_checkin(self, update: Update, step: str, activity_id: str, value: str) -> None:
        query = update.callback_query
        await query.answer()
        if step == "r":
            rpe = int(value)
            store.set_checkin(activity_id, rpe=rpe)
            await query.edit_message_text(
                f"📝 Effort noté : *{rpe}/10* ({coaching.RPE_LABELS[rpe]})\n🦵 Et les jambes ?",
                parse_mode=ParseMode.MARKDOWN, reply_markup=legs_markup(activity_id))
        elif step == "l" and value in LEGS:
            item = store.set_checkin(activity_id, legs=value)
            rpe = f"effort {item['rpe']}/10, " if item.get("rpe") else ""
            await query.edit_message_text(f"📝 Ressenti noté : {rpe}jambes {LEGS[value]} ✅")
            if value == "douleur":
                await agent.add_to_history(self.chat_id, "[Ressenti après séance : douleur signalée]", PAIN_FOLLOW_UP)
                await self._push(PAIN_FOLLOW_UP)

    async def _ask_checkin(self, run: dict, only_if_missing: bool = False) -> None:
        """Demande l'effort perçu (1-10) puis l'état des jambes, par boutons."""
        activity_id = run.get("id")
        if activity_id is None or len(f"c:r:{activity_id}:10") > 64:   # limite Telegram des données de bouton
            return
        existing = store.state["checkins"].get(str(activity_id), {})
        if only_if_missing and existing.get("rpe"):
            return
        store.set_checkin(activity_id, date=(run.get("startDateLocal") or "")[:10] or None,
                          minutes=round((run.get("movingTimeInSeconds") or 0) / 60), name=run.get("name"))
        await self._push(f"📝 *Ton ressenti sur « {md_escape(run.get('name') or 'ta séance')} »*\n"
                         "Effort perçu, de 1 (très facile) à 10 (maximal) :", rpe_markup(activity_id))

    # --- Tâches planifiées ---------------------------------------------------

    async def morning_briefing(self) -> None:
        try:
            reply = await agent.ask(MORNING_PROMPT, chat_id=self.chat_id)
        except Exception as e:
            logger.exception("Briefing matinal en échec")
            reply = agent.AgentReply(f"❌ Briefing matinal impossible : {type(e).__name__}")
        await self._deliver(reply)

    def _review_prompt(self) -> str:
        """Prévu (agenda) vs fait (Dreeve) sur la semaine en cours, calculé par le code."""
        today = datetime.datetime.now(TZ).date()
        monday, sunday = coaching.week_bounds(today)
        start = datetime.datetime.combine(monday, datetime.time.min, tzinfo=TZ)
        events, _ = cs.list_events(start, start + datetime.timedelta(days=7))
        planned = [ev for ev in events if weather_alerts.is_training(
            ev, datetime.datetime.fromisoformat(ev["start"]), datetime.datetime.fromisoformat(ev["end"]))]
        runs = dreeve.get_runs_since_weeks(1)[0] if settings.dreeve_enabled else []
        stats = coaching.adherence(planned, runs, monday)
        return REVIEW_PROMPT.format(adherence=coaching.format_adherence(stats))

    async def weekly_review(self) -> None:
        try:
            prompt = await asyncio.to_thread(self._review_prompt)
            reply = await agent.ask(prompt, chat_id=self.chat_id, allow_writes=False, planning=True)
        except Exception as e:
            logger.exception("Bilan hebdo en échec")
            reply = agent.AgentReply(f"❌ Bilan de la semaine impossible : {type(e).__name__}")
        await self._deliver(reply, header="📊 *Bilan de la semaine*")

    async def check_pantry(self) -> None:
        """Chaque jour : retire ce qui est resté sans réponse, puis demande « Encore là ? » pour les produits frais
        arrivés à échéance (un bouton par article)."""
        today = datetime.datetime.now(TZ).date()
        try:
            expired = store.expire_pantry(today)
            due = store.pantry_to_check(today)[:20]
            if not due and not expired:
                return
            lines = []
            if due:
                lines.append("🥫 *Encore là ?* Ces produits frais arrivent à échéance :")
                lines += [f"- {md_escape(p['name'])}" + (f" ({md_escape(p['qty'])})" if p.get("qty") else "")
                          + f" · acheté il y a {(today - datetime.date.fromisoformat(p['added'])).days} j" for p in due]
                lines.append(f"_Sans réponse sous {PANTRY_ASK_GRACE_DAYS} j, je les retire du stock._")
            if expired:
                lines.append(f"🗑️ Retirés du stock (pas de réponse) : {md_escape(', '.join(expired))}")
            text = "\n".join(lines)
            await self._push(text, pantry_check_markup(due) if due else None)
            store.mark_pantry_asked([p["id"] for p in due], today)
            await agent.add_to_history(self.chat_id, "[Vérification automatique du stock envoyée par le système]", md_unescape(text))
        except Exception:
            logger.exception("Vérification du stock en échec")

    async def check_meal(self, index: int) -> None:
        """Après un repas (MEAL_CHECK_TIMES) : sans aucune trace de ce repas (bouton « mangé », repas dit au coach),
        demande ce qui a été mangé. La réponse, libre, passe par le coach : journal, stock, avis nutrition."""
        now = datetime.datetime.now(TZ)
        start, end, slot = coaching.meal_window(settings.meal_check_times, index)
        key = f"{now.date().isoformat()} {slot}"
        try:
            if store.meal_check(key) or coaching.has_meal_between(store.meals_since(now.date()), now.date(), start, end):
                return
            text = (f"🍽️ *Qu'as-tu mangé {coaching.MEAL_SLOT_LABEL[slot]} ?*\n"
                    "Réponds en texte ou en vocal (« pâtes au thon et une pomme ») : je mets ton stock à jour et je te "
                    "dis si ça colle avec ton entraînement.")
            data = f"{now.date().isoformat()}:{index}"
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🙅 Sauté", callback_data=f"q:s:{data}"),
                                            InlineKeyboardButton("⏭️ Passer", callback_data=f"q:x:{data}")]])
            await self._push(text, markup)
            store.set_meal_check(key, "asked")
            await agent.add_to_history(self.chat_id, f"[Question automatique du système : repas ({slot}) non noté]",
                                       md_unescape(text))
        except Exception:
            logger.exception("Question repas en échec")

    async def check_weather(self) -> None:
        """Prévient (une fois par risque et par séance) en cas de pluie, chaleur, vent ou verglas, et quand ça s'arrange."""
        try:
            now = datetime.datetime.now(TZ)
            horizon = now + datetime.timedelta(hours=settings.rain_hours_ahead)
            days = min(16, (settings.rain_hours_ahead + now.hour) // 24 + 2)
            (events, _), forecasts = await asyncio.gather(
                asyncio.to_thread(cs.list_events, now, horizon + datetime.timedelta(hours=6)),
                asyncio.to_thread(weather.fetch_hourly, days),
            )
        except Exception as e:
            logger.error(f"Alerte météo : vérification impossible : {e}")
            return
        for notice in weather_alerts.find_notices(events, forecasts, now, store.alert_state):
            text = weather_alerts.format_notice(notice, now)
            markup = None
            if notice.kind == "alert":
                pid = store.add_proposal(weather_alerts.adapt_prompt(notice), kind="weather")
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Adapter", callback_data=f"p:a:{pid}"),
                                                InlineKeyboardButton("👍 Je garde", callback_data=f"p:x:{pid}")]])
            try:
                await self._push(text, markup)
            except Exception:
                logger.exception("Alerte météo : envoi Telegram en échec")
                continue
            store.save_alert_state(notice.key, notice.end, notice.signalled, cleared=notice.kind == "clear")
            await agent.add_to_history(self.chat_id, "[Alerte météo automatique envoyée par le système]", text)
            logger.info(f"Alerte météo ({notice.kind}) : {notice.summary} ({notice.place}, {sorted(notice.hazards) or notice.cleared})")

    async def check_new_activity(self, source: str = "polling") -> bool:
        """Débriefe chaque run apparu dans Dreeve depuis le dernier vu, puis demande le ressenti.
        Retourne True si un run a été trouvé."""
        async with self._activity_lock:
            try:
                runs = await asyncio.to_thread(dreeve.get_recent_runs)
            except Exception as e:
                logger.error(f"Erreur vérification Dreeve ({source}) : {e}")
                return False
            if not runs:
                return False
            previous = store.last_activity_id   # persistant : un run importé pendant un redémarrage n'est pas perdu
            fresh = dreeve.new_runs_since(runs, previous)
            if not fresh:
                return False
            store.last_activity_id = runs[0].get("id")
            if previous is None:   # tout premier passage : simple référence, pas de notification
                return False

            logger.info(f"{len(fresh)} nouveau(x) run(s) détecté(s) ({source}) : {[r.get('id') for r in fresh]}")
            what = "une nouvelle séance" if len(fresh) == 1 else f"{len(fresh)} nouvelles séances (la dernière compte le plus)"
            prompt = DEBRIEF_PROMPT.format(intro=f"Je viens de terminer et d'uploader {what}",
                                           runs="\n".join(dreeve.format_activity(r) for r in fresh))
            try:
                reply = await agent.ask(prompt, chat_id=self.chat_id, allow_writes=False)
                await self._deliver(reply, header="⚡ *Nouveau run détecté !*")
                await self._ask_checkin(fresh[-1])
            except Exception:
                logger.exception("Débrief de séance en échec")
            return True


def build_bot() -> CoachBot:
    app = Application.builder().token(settings.telegram_bot_token).build()
    bot = CoachBot(app)
    handlers = {
        "plan": bot.on_agenda, "load": bot.on_form, "runs": bot.on_last_runs, "weather": bot.on_weather,
        "goals": bot.on_goals, "memory": bot.on_memory, "list": bot.on_list, "recipes": bot.on_recipes, "eat": bot.on_eat, "stock": bot.on_stock, "debrief": bot.on_debrief, "review": bot.on_review,
        "shopping": bot.on_shopping, "briefing": bot.on_briefing, "help": bot.on_help,
    }
    for name, aliases, _ in commands.COMMANDS:
        app.add_handler(CommandHandler([name, *aliases], handlers[name]))
    app.add_handler(CallbackQueryHandler(bot.on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, bot.on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_message))
    return bot
