"""Telegram : messages (texte et vocaux), commandes, boutons, briefing matinal, bilan hebdo,
détection de nouveaux runs avec ressenti, alertes météo."""
import asyncio
import contextlib
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
from .formatting import bubbles, md_escape, md_unescape, split_message
from .memory import LEGS, PANTRY_ASK_GRACE_DAYS, store

logger = logging.getLogger(__name__)

BOT_NAME = "Bob"   # nom affiché du bot dans Telegram, et son personnage (voir agent.SYSTEM_PROMPT)

MORNING_PROMPT = (
    "Réalise le briefing matinal. Analyse l'agenda Radicale, la météo, la charge, mon historique Dreeve, mes ressentis et "
    "le compte à rebours de mes objectifs, en appliquant tout ce que tu sais de moi. "
    f"Met à jour / planifie les {settings.planning_horizon_days} prochains jours dans Radicale, en cohérence avec la "
    "périodisation vers mon objectif principal.\n"
    "Puis envoie-moi un petit mot du matin, comme un pote : 1 ou 2 messages d'une ligne (séparés par une ligne vide) : "
    "la séance du jour (ou le repos) et LE truc à ne pas oublier (météo, récup, repas…), plus ce qui a changé dans "
    "l'agenda s'il y a lieu. Pas de titres, pas de chiffres inutiles."
)

DEBRIEF_PROMPT = (
    "{intro} :\n{runs}\n\n"
    "Réagis comme un pote qui suit mes courses : 1 ou 2 messages très courts (une ligne chacun, séparés par une ligne "
    "vide), sans titres ni liste de chiffres. Un mot sincère sur la séance (par rapport à ce qui était prévu et à mon "
    "historique : relis l'agenda du jour avec list_calendar_events si besoin), et seulement si ça compte : la chose à "
    "retenir pour la suite (récup, repas, prochaine séance), en gardant mes objectifs en tête. Si un ajustement du programme est vraiment conseillé, "
    "enregistre-le avec propose_change sans le faire. Ne modifie pas l'agenda."
)

REVIEW_PROMPT = (
    "Fais le bilan de ma semaine. Chiffres calculés par le système (fiables, ne les recalcule pas) :\n{adherence}\n\n"
    "Dis-le comme un pote : 2 messages d'une ou deux lignes (séparés par une ligne vide), sans titres : ce que je "
    "retiens de la semaine (course, alimentation, forme) ; le cap pour la semaine prochaine. Si le programme mérite un "
    "ajustement, enregistre-le avec propose_change sans le faire. Ne modifie pas l'agenda."
)

APPLY_PROMPT = "Applique maintenant cette proposition que tu m'as faite et que je valide : {text}"
DEBRIEF_TIMEOUT_S = 300   # au-delà, récap brut : un débrief bloqué ne doit jamais empêcher les vérifications suivantes
# État de la dernière vérification Dreeve, exposé par /health pour diagnostiquer un débrief manquant.
ACTIVITY_STATUS: dict = {"checked_at": None, "result": None, "error": None, "debriefed_at": None}
MESSAGE_DEBOUNCE_S = 4.0   # silence attendu avant de répondre, pour regrouper les messages envoyés à la suite


def merge_messages(texts: list[str]) -> str:
    if len(texts) == 1:
        return texts[0]
    return ("Plusieurs messages envoyés à la suite, à traiter ensemble en une seule réponse :\n"
            + "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1)))


PAIN_FOLLOW_UP = "T'as mal où, et depuis quand ? Ça gêne à la marche ?"


BUBBLE_GAP_S = 1.0          # pause minimale avant chaque message suivant d'une réponse
BUBBLE_CHARS_PER_S = 35     # « vitesse de frappe » : un message plus long se fait attendre un peu plus
BUBBLE_MAX_GAP_S = 4.0
MAX_MESSAGES_IN_A_ROW = 4   # au-delà, c'est trop à lire : les messages les plus courts sont regroupés


def bubble_gap(chunk: str) -> float:
    return 0.0 if not BUBBLE_GAP_S else min(BUBBLE_MAX_GAP_S, BUBBLE_GAP_S + len(chunk) / BUBBLE_CHARS_PER_S)


async def send_text(send, text: str, reply_markup=None, as_bubbles: bool = False, typing=None,
                    max_bubbles: int = MAX_MESSAGES_IN_A_ROW) -> None:
    """send : coroutine(text=…, **kwargs), texte toujours nommé (send_message a chat_id lié par partial, reply_text non). Markdown d'abord, texte brut si Telegram refuse le balisage.
    Les boutons éventuels sont attachés au dernier morceau."""
    chunks = bubbles(text, max_bubbles) if as_bubbles else split_message(text)
    for i, chunk in enumerate(chunks):
        if as_bubbles and i:   # « écrit… » puis une pause à la mesure du message, comme quelqu'un qui tape
            if typing is not None:
                with contextlib.suppress(Exception):
                    await typing()
            await asyncio.sleep(bubble_gap(chunk))
        extra = {"reply_markup": reply_markup} if reply_markup is not None and i == len(chunks) - 1 else {}
        try:
            await send(text=chunk, parse_mode=ParseMode.MARKDOWN, **extra)
        except BadRequest:
            await send(text=md_unescape(chunk), **extra)


def proposal_bubble(summary: str) -> str:
    return f"💡 {md_escape(summary)}"


def proposal_markup(pid) -> InlineKeyboardMarkup:
    """Une proposition = une bulle à part, avec ses propres boutons : répondre à l'une ne touche pas aux autres."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Oui, vas-y", callback_data=f"p:a:{pid}"),
                                  InlineKeyboardButton("✏️ Modifier", callback_data=f"p:e:{pid}"),
                                  InlineKeyboardButton("❌ Non", callback_data=f"p:x:{pid}")]])


PROPOSAL_OUTCOME = {"a": "C'est parti 👌", "e": "Ok, dis-moi ce que tu veux changer 👂", "x": "Ok, on garde comme ça 👍"}


def without_proposal(markup: InlineKeyboardMarkup | None, pid: str) -> InlineKeyboardMarkup | None:
    """Clavier sans la ligne de cette proposition (anciens messages à plusieurs propositions)."""
    rows = [list(row) for row in (markup.inline_keyboard if markup else ())
            if not any((b.callback_data or "").startswith("p:") and b.callback_data.split(":")[2] == str(pid) for b in row)]
    return InlineKeyboardMarkup(rows) if rows else None


MEAL_HINT = "_Touche un plat une fois mangé ou cuisiné : ses ingrédients sortent du stock._"


CHOOSE_HINT = "_Choisis ton plat : je t'envoie la recette et je retire ses ingrédients du stock._"


def has_recipe(mid) -> bool:
    """Plat proposé par /eat : choisi par bouton, sa recette est envoyée à ce moment-là."""
    return bool((item := store.proposal(mid)) and item.get("recipe"))


def meal_rows(meals: list[tuple[int, str]]) -> list[list[InlineKeyboardButton]]:
    """Un bouton par plat proposé : « choisir » (/eat, envoie la recette) ou « mangé / cuisiné » ; retire ses
    ingrédients du stock, sans IA."""
    return [[InlineKeyboardButton(f"{'🍽️' if has_recipe(mid) else '✅'} {title}"[:60], callback_data=f"m:{mid}")]
            for mid, title in meals]


def meal_hint(meals: list[tuple[int, str]]) -> str:
    return CHOOSE_HINT if any(has_recipe(mid) for mid, _ in meals) else MEAL_HINT


MEAL_GUESS_HOURS = 36   # un plat proposé il y a plus longtemps n'est plus un candidat crédible
MAX_MEAL_GUESSES = 3
MEAL_SLOT_WHEN = {"petit-déjeuner": "Ce matin", "déjeuner": "Ce midi", "collation": "Cet après-midi", "dîner": "Ce soir"}


def meal_guesses(now: datetime.datetime) -> list[dict]:
    """Ce que l'athlète a probablement mangé : ses boîtes prêtes (la plus ancienne d'abord), puis les plats que le coach
    lui a proposés récemment (le plus récent d'abord). [{text, uses, macros, box, source}]"""
    guesses = []
    for p in sorted((p for p in store.pantry if p.get("boxes")), key=lambda p: p.get("added") or ""):
        n = coaching.box_count(p.get("qty"))
        use = {"name": p["name"], **({"qty_left": f"{n - 1} boîte{'s' if n > 2 else ''}"} if n > 1 else {})}
        guesses.append({"text": p["name"], "uses": [use], "macros": {}, "box": True, "source": None})
    cutoff = now - datetime.timedelta(hours=MEAL_GUESS_HOURS)
    recent = [(pid, item) for pid, item in store.state["proposals"].items()
              if item.get("kind") == "meal" and item["status"] == "open" and not item.get("makes")
              and not item.get("check") and datetime.datetime.fromisoformat(item["created"]) > cutoff]
    for pid, item in sorted(recent, key=lambda x: x[1]["created"], reverse=True):
        guesses.append({"text": item["text"], "uses": item.get("uses") or [], "macros": item.get("macros") or {},
                        "box": False, "source": pid})
    seen, out = set(), []
    for g in guesses:
        if g["text"].casefold() not in seen:
            seen.add(g["text"].casefold())
            out.append(g)
    return out[:MAX_MEAL_GUESSES]


def close_meal_guesses(markup: InlineKeyboardMarkup | None) -> None:
    """La question « tu as mangé quoi ? » a sa réponse : ses autres plats candidats ne valent plus rien."""
    for row in (markup.inline_keyboard if markup else ()):
        for btn in row:
            if (btn.callback_data or "").startswith("m:"):
                store.close_proposal(btn.callback_data[2:], "ignored")


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
        self._pending: list[str] = []          # messages reçus pas encore envoyés au coach
        self._pending_update: Update | None = None
        self._draining = False
        self._last_message = 0.0

    # --- Envoi ---------------------------------------------------------------

    def _sender(self, update: Update | None = None):
        if update is not None and update.effective_message is not None:
            return update.effective_message.reply_text
        return functools.partial(self.app.bot.send_message, chat_id=self.chat_id)

    async def _push(self, text: str, reply_markup=None) -> None:
        await send_text(self._sender(), text, reply_markup)

    async def _deliver(self, reply: agent.AgentReply, header: str = "", update: Update | None = None) -> None:
        """Réponse de l'agent, puis chaque proposition dans sa propre bulle avec ses boutons."""
        footer, markup = "", None
        if reply.meals:
            footer, markup = meal_hint(reply.meals), InlineKeyboardMarkup(meal_rows(reply.meals))
        body = "\n".join(part for part in (header, reply.text) if part)   # l'en-tête reste sur le premier message
        text = "\n\n".join(part for part in (body, footer) if part)
        if text:   # un message par paragraphe : une conversation, pas un pavé
            if reply.gif and reply.gif_moment == "ouverture":   # le GIF ouvre l'échange, avant le premier message
                await self._send_gif(reply.gif, update)
            await send_text(self._sender(update), text, markup, as_bubbles=True, typing=self._typing(update),
                            max_bubbles=MAX_MESSAGES_IN_A_ROW - len(reply.proposals))   # propositions comprises
        if reply.gif and (reply.gif_moment != "ouverture" or not text):   # clôture : après le dernier message
            await self._send_gif(reply.gif, update)
        for pid, summary in reply.proposals:
            await send_text(self._sender(update), proposal_bubble(summary), proposal_markup(pid))
        if reply.shopping_list is not None and (lst := store.shopping_list) and lst["id"] == reply.shopping_list:
            await send_text(self._sender(update), shopping_text(lst), shopping_markup(lst))

    def _typing(self, update: Update | None = None):
        """Coroutine qui affiche « écrit… » dans le chat (le temps de la pause entre deux messages)."""
        async def typing() -> None:
            if update is not None and update.effective_chat is not None:
                await update.effective_chat.send_action(ChatAction.TYPING)
            else:
                await self.app.bot.send_chat_action(chat_id=self.chat_id, action=ChatAction.TYPING)
        return typing

    async def _send_gif(self, url: str, update: Update | None = None) -> None:
        """Le GIF choisi par le coach, juste après son message ; un échec ne bloque jamais la réponse."""
        try:
            if update is not None and update.effective_message is not None:
                await update.effective_message.reply_animation(url)
            else:
                await self.app.bot.send_animation(chat_id=self.chat_id, animation=url)
        except Exception as e:
            logger.warning(f"GIF non envoyé ({url}) : {e}")

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
        await self._enqueue(update, update.message.text)

    async def _enqueue(self, update: Update, text: str) -> None:
        """Messages envoyés coup sur coup → une seule réponse du coach.

        On attend MESSAGE_DEBOUNCE_S de silence avant de lancer le coach ; ce qui arrive pendant qu'il réfléchit
        part ensemble au tour suivant. Le premier message reçu fait le travail, les suivants s'ajoutent et rendent la main
        (handlers concurrents, voir build_bot)."""
        loop = asyncio.get_running_loop()
        self._pending.append(text)
        self._pending_update = update
        self._last_message = loop.time()
        if self._draining:
            return
        self._draining = True
        try:
            while self._pending:
                while (wait := self._last_message + MESSAGE_DEBOUNCE_S - loop.time()) > 0:
                    await asyncio.sleep(wait)
                texts, self._pending = self._pending, []
                await self._ask_and_reply(self._pending_update, merge_messages(texts))
        finally:
            self._draining = False

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
        await self._enqueue(update, text)

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
        remaining = without_proposal(query.message.reply_markup, pid)
        if remaining is None and item["kind"] != "weather":   # sa propre bulle : elle garde la trace du choix
            await query.edit_message_text(f"{proposal_bubble(item.get('summary') or item['text'])}\n{PROPOSAL_OUTCOME[action]}",
                                          parse_mode=ParseMode.MARKDOWN)
        else:   # seuls ses boutons partent : les autres propositions du message restent utilisables
            await query.edit_message_reply_markup(remaining)
        if action == "a":
            prompt = item["text"] if item["kind"] == "weather" else APPLY_PROMPT.format(text=item["text"])
            await self._ask_and_reply(update, prompt)
        elif action == "e":
            await agent.add_to_history(self.chat_id, f"[L'athlète veut ajuster ta proposition : {item['text']}]",
                                       "Dis-moi ce que tu veux changer.")
            if item["kind"] == "weather":
                await self._push("Ok, dis-moi ce que tu veux changer 👂")
        else:
            await agent.add_to_history(self.chat_id, f"[L'athlète a ignoré ta proposition : {item['text']}]",
                                       "D'accord, on garde le programme tel quel.")
            if item["kind"] == "weather":
                await self._push("Ok, on garde comme ça 👍")

    async def _on_meal(self, update: Update, mid: str) -> None:
        """Plat mangé / cuisiné : ses ingrédients sortent du stock (ou passent à ce qu'il en reste)."""
        query = update.callback_query
        item = store.proposal(mid)
        if not item or item.get("kind") != "meal" or store.close_proposal(mid, "applied") is None:
            await query.answer("Déjà noté.")
            return
        r = store.update_pantry(remove=item.get("uses") or [])
        await query.answer()
        if item.get("check"):   # réponse à « tu as mangé quoi ? » : une ligne, et la question est close
            await self._on_meal_guess(query, item, r)
            return
        markup = without_button(query.message.reply_markup, f"m:{mid}")
        if item.get("recipe"):   # /eat : un seul plat choisi, les autres propositions tombent
            for row in (markup.inline_keyboard if markup else ()):
                for btn in row:
                    if (btn.callback_data or "").startswith("m:") and has_recipe(other := btn.callback_data[2:]):
                        store.close_proposal(other, "ignored")
                        markup = without_button(markup, btn.callback_data)
        await query.edit_message_reply_markup(markup)
        changes = [f"−{x}" for x in r["removed"]] + r["updated"]
        if item.get("makes"):   # batch cooking : les boîtes entrent au stock, rien n'est encore mangé
            store.update_pantry(add=[{"name": item["text"], "qty": item["makes"], "keeps_days": 4, "boxes": True}])
            changes.append(f"+{item['text']} ({item['makes']})")
            text = f"Top, {md_escape(item['makes'])} de côté pour les prochains jours 🙌"
        else:                   # mangé : entre dans le journal alimentaire
            macros = item.get("macros") or {}
            store.log_meal(item["text"], datetime.datetime.now(TZ), source="bouton", **macros)
            text = "Bon app' 😋"
        if item.get("recipe"):   # plat choisi : la recette, simplement
            text = f"Bon choix 😋 *{md_escape(item['text'])}*, voilà comment faire :\n\n{item['recipe']}"
        await self._push(text)
        await agent.add_to_history(self.chat_id, f"[L'athlète a confirmé avoir mangé / cuisiné : {item['text']}]", md_unescape(text))

    async def _on_meal_guess(self, query, item: dict, r: dict) -> None:
        store.set_meal_check(item["check"], "answered")
        if item.get("source"):   # le plat proposé à l'origine est mangé : plus un candidat pour le prochain repas
            store.close_proposal(item["source"], "applied")
        close_meal_guesses(query.message.reply_markup)
        macros = item.get("macros") or {}
        store.log_meal(item["text"], datetime.datetime.fromisoformat(item["at"]), source="bouton", **macros)
        box = next((u for u in item.get("uses") or [] if u.get("name", "").casefold() == item["text"].casefold()), None)
        if box and box.get("qty_left"):
            text = f"😋 Il t'en reste {md_escape(box['qty_left'])}."
        elif box and r["removed"]:
            text = "😋 C'était la dernière boîte !"
        else:
            text = "Bon app' 😋"
        await query.edit_message_reply_markup(None)
        await self._push(text)
        await agent.add_to_history(self.chat_id, f"[L'athlète a répondu avoir mangé : {item['text']}]", md_unescape(text))

    async def _on_meal_question(self, update: Update, action: str, day: str, index: int) -> None:
        """« Qu'as-tu mangé ? » : Sauté (noté comme repas vide, à la fin de sa plage) ou Passer (on n'en parle plus)."""
        query = update.callback_query
        await query.answer()
        _, end, slot = coaching.meal_window(settings.meal_check_times, index)
        key = f"{day} {slot}"
        close_meal_guesses(query.message.reply_markup)
        if action == "s":
            at = datetime.datetime.combine(datetime.date.fromisoformat(day), end, tzinfo=TZ)
            store.log_meal(f"rien ({slot} sauté)", at, source="bouton", kcal=0, protein_g=0, carbs_g=0)
            store.set_meal_check(key, "answered")
            text = "Ça arrive 😅 Pense à bien manger au prochain repas."
        else:
            store.set_meal_check(key, "skipped")
            text = "Ok 👌"
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
            await query.edit_message_text("Merci 🙏")
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
                f"{rpe}/10, ok 👌 Et les jambes ?",
                parse_mode=ParseMode.MARKDOWN, reply_markup=legs_markup(activity_id))
        elif step == "l" and value in LEGS:
            item = store.set_checkin(activity_id, legs=value)
            rpe = f"effort {item['rpe']}/10, " if item.get("rpe") else ""
            await query.edit_message_text("Merci 🙏" if value != "douleur" else "Aïe 😬")
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
        await self._push("Alors, t'as senti comment ? Effort de 1 (tranquille) à 10 (à bloc) :", rpe_markup(activity_id))

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
            # on part de ce qu'on sait (boîtes prêtes, plats proposés) : un geste suffit dans le cas courant
            at = datetime.datetime.combine(now.date(), end, tzinfo=TZ).isoformat()
            rows = []
            for g in meal_guesses(now):
                gid = store.add_proposal(g["text"], kind="meal", uses=g["uses"], macros=g["macros"], at=at,
                                         check=key, source=g["source"])
                rows.append([InlineKeyboardButton(f"{'🍱' if g['box'] else '🍽️'} {g['text']}"[:60], callback_data=f"m:{gid}")])
            hint = "Touche le plat, ou dis-moi ce que c'était." if rows else "Dis-le-moi en texte ou en vocal, je m'occupe du stock."
            text = f"🍽️ *{MEAL_SLOT_WHEN[slot]}, tu as mangé quoi ?*\n{hint}"
            rows.append([InlineKeyboardButton("🙅 Sauté", callback_data=f"q:s:{now.date().isoformat()}:{index}")])
            markup = InlineKeyboardMarkup(rows)
            await self._push(text, markup)
            store.set_meal_check(key, "asked")
            await agent.add_to_history(self.chat_id, f"[Question automatique du système : repas ({slot}) non noté]",
                                       md_unescape(text))
        except Exception:
            logger.exception("Question repas en échec")

    async def suggest_meal(self) -> None:
        """Juste avant un repas (EAT_SUGGEST_TIMES) : des plats au choix, comme /eat, sans qu'on les demande.
        Rien si /eat vient d'être demandé, si le repas est déjà noté, ou si le coach sait qu'il se prend ailleurs."""
        now = datetime.datetime.now(TZ)
        recent = now - datetime.timedelta(hours=2)
        try:
            if any(item.get("kind") == "meal" and item.get("recipe") and item["status"] == "open"
                   and datetime.datetime.fromisoformat(item["created"]) > recent for item in store.state["proposals"].values()):
                return
            if coaching.has_meal_between(store.meals_since(now.date()), now.date(),
                                         (now - datetime.timedelta(minutes=90)).time(), now.time()):
                return
            reply = await asyncio.wait_for(agent.ask(commands.eat_prompt(now, spontaneous=True), chat_id=self.chat_id,
                                                     allow_writes=False, skip_marker=commands.NO_SUGGESTION),
                                           DEBRIEF_TIMEOUT_S)
            if reply.text or reply.meals:
                await self._deliver(reply)
        except Exception:
            logger.exception("Suggestion de repas en échec")

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
            ACTIVITY_STATUS.update(checked_at=datetime.datetime.now(TZ).isoformat(timespec="seconds"), error=None)
            try:
                runs = await asyncio.to_thread(dreeve.get_recent_runs)
            except Exception as e:
                logger.error(f"Erreur vérification Dreeve ({source}) : {e}")
                ACTIVITY_STATUS.update(result="erreur Dreeve", error=str(e))
                return False
            if not runs:
                ACTIVITY_STATUS["result"] = "aucun run"
                return False
            previous = store.last_activity_id   # persistant : un run importé pendant un redémarrage n'est pas perdu
            fresh = dreeve.new_runs_since(runs, previous)
            if not fresh:
                ACTIVITY_STATUS["result"] = f"rien de nouveau (dernier vu : {previous})"
                logger.debug(f"Dreeve ({source}) : rien de nouveau (dernier vu : {previous})")
                return False
            if previous is None:   # tout premier passage : simple référence, pas de notification
                store.last_activity_id = runs[0].get("id")
                ACTIVITY_STATUS["result"] = f"référence initiale : {runs[0].get('id')}"
                logger.info(f"Dreeve : référence initiale {runs[0].get('id')} (pas de débrief)")
                return False

            logger.info(f"{len(fresh)} nouveau(x) run(s) détecté(s) ({source}) : {[r.get('id') for r in fresh]}")
            what = "une nouvelle séance" if len(fresh) == 1 else f"{len(fresh)} nouvelles séances (la dernière compte le plus)"
            runs_text = "\n".join(dreeve.format_activity(r) for r in fresh)
            prompt = DEBRIEF_PROMPT.format(intro=f"Je viens de terminer et d'uploader {what}", runs=runs_text)
            try:
                reply = await asyncio.wait_for(agent.ask(prompt, chat_id=self.chat_id, allow_writes=False), DEBRIEF_TIMEOUT_S)
            except Exception:   # l'agent (en échec ou bloqué) ne doit pas faire perdre le récap : au moins les chiffres
                logger.exception("Débrief de séance : agent en échec, envoi du récap brut")
                reply = agent.AgentReply(f"J'ai vu passer ta sortie 🏃 mais j'ai pas réussi à la regarder en détail 🥲 "
                                         f"Tape /debrief pour réessayer.\n{runs_text}")
            try:
                await self._deliver(reply)   # Bob réagit lui-même à la sortie, sans en-tête de notification
            except Exception as e:   # run non marqué comme vu : nouvel essai au prochain passage
                logger.exception("Débrief de séance : envoi Telegram en échec, nouvel essai au prochain passage")
                ACTIVITY_STATUS.update(result="envoi Telegram en échec", error=str(e))
                return False
            # marqué comme vu seulement une fois le récap livré, pour qu'un échec ne fasse pas perdre le run
            store.last_activity_id = runs[0].get("id")
            ACTIVITY_STATUS.update(result=f"débrief envoyé : {runs[0].get('id')}",
                                   debriefed_at=datetime.datetime.now(TZ).isoformat(timespec="seconds"))
            logger.info(f"Débrief de séance envoyé ({runs[0].get('id')})")
            try:
                await self._ask_checkin(fresh[-1])
            except Exception:
                logger.exception("Demande de ressenti en échec")
            return True


def build_bot() -> CoachBot:
    # Handlers concurrents : un message reçu pendant que le coach réfléchit est mis en attente (et regroupé)
    # au lieu de bloquer la file ; l'agent sérialise lui-même ses exécutions.
    app = Application.builder().token(settings.telegram_bot_token).concurrent_updates(True).build()
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
