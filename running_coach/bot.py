"""Telegram : messages (texte et vocaux), commandes, boutons, briefing matinal, bilan hebdo,
détection de nouveaux runs avec ressenti, alertes météo."""
import asyncio
import contextlib
import datetime
import functools
import logging

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import agent, commands, dreeve, voice  # noqa: F401  (dreeve : réexporté)
from . import calendar_store as cs  # noqa: F401  (réexporté)
from .config import TZ, settings  # noqa: F401  (TZ : réexporté)
from .memory import store
from .handlers.info import InfoMixin
from .handlers.jobs import JobsMixin
from .handlers.kitchen import KitchenMixin
from .handlers.meals import MealsMixin
from .handlers.runs import ACTIVITY_STATUS, RunsMixin  # noqa: F401  (ACTIVITY_STATUS : réexporté, lu par app.py)
from .handlers.ui import (
    BUBBLE_GAP_S,  # noqa: F401
    BUBBLE_MAX_GAP_S,  # noqa: F401
    MAX_MESSAGES_IN_A_ROW,
    PROPOSAL_OUTCOME,
    bubble_gap,  # noqa: F401
    meal_hint,
    meal_rows,
    merge_messages,
    proposal_bubble,
    proposal_markup,
    send_text,
    shopping_markup,
    shopping_text,
    without_button,  # noqa: F401
    without_proposal,
)

logger = logging.getLogger(__name__)

BOT_NAME = settings.bot_name   # BOT_NAME : nom affiché dans Telegram et personnage du coach (voir agent.SYSTEM_PROMPT)
APPLY_PROMPT = "Applique maintenant cette proposition que tu m'as faite et que je valide : {text}"
MESSAGE_DEBOUNCE_S = 4.0   # silence attendu avant de répondre, pour regrouper les messages envoyés à la suite


class CoachBot(InfoMixin, KitchenMixin, MealsMixin, RunsMixin, JobsMixin):
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
                try:
                    await self._ask_and_reply(self._pending_update, merge_messages(texts))
                except Exception:   # un envoi raté ne doit pas laisser les messages suivants sans réponse
                    logger.exception("Réponse au message en échec")
                    with contextlib.suppress(Exception):
                        await self._push("Oups, mon message n'est pas parti 🥲 Réessaie ?")
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
        duration = media.duration
        seconds = duration.total_seconds() if isinstance(duration, datetime.timedelta) else duration   # PTB v23 : timedelta
        if seconds and seconds > voice.MAX_SECONDS:
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

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Exception non gérée d'un handler : on la journalise, et on s'excuse auprès du chat autorisé. Ne lève jamais."""
        try:
            logger.error("Exception non gérée dans un handler", exc_info=context.error, extra={"update": str(update)})
            chat = update.effective_chat if isinstance(update, Update) else None
            if chat is not None and self.chat_id and str(chat.id) == self.chat_id:
                await self._push("Oups, une erreur de mon côté 🥲 Réessaie dans un instant ?")
        except Exception:
            logger.exception("Gestionnaire d'erreurs en échec")

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
            elif parts[0] == "e":
                await self._on_eat_again(update)
            elif parts[0] == "k":
                await self._on_pantry_check(update, parts[1] == "y", parts[2])
            elif parts[0] == "r":
                await self._on_route_button(update, parts[1], parts[2], parts[3])
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


def build_bot() -> CoachBot:
    # Handlers concurrents : un message reçu pendant que le coach réfléchit est mis en attente (et regroupé)
    # au lieu de bloquer la file ; l'agent sérialise lui-même ses exécutions.
    app = Application.builder().token(settings.telegram_bot_token).concurrent_updates(True).build()
    bot = CoachBot(app)
    handlers = {
        "plan": bot.on_agenda, "load": bot.on_form, "runs": bot.on_last_runs, "weather": bot.on_weather,
        "goals": bot.on_goals, "memory": bot.on_memory, "list": bot.on_list, "recipes": bot.on_recipes, "eat": bot.on_eat, "stock": bot.on_stock, "meme": bot.on_meme, "nutrition": bot.on_nutrition,
        "routes": bot.on_routes, "newroute": bot.on_new_route, "debrief": bot.on_debrief, "review": bot.on_review,
        "shopping": bot.on_shopping, "briefing": bot.on_briefing, "help": bot.on_help,
    }
    for name, aliases, _ in commands.COMMANDS:
        app.add_handler(CommandHandler([name, *aliases], handlers[name]))
    app.add_handler(CallbackQueryHandler(bot.on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, bot.on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_message))
    app.add_error_handler(bot.on_error)
    return bot
