"""Telegram : messages entrants, briefing matinal, détection de nouveaux runs."""
import asyncio
import datetime
import functools
import logging

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import agent, dreeve, rain_alerts, weather
from . import calendar_store as cs
from .config import TZ, settings
from .formatting import md_escape, md_unescape, split_message
from .memory import store

logger = logging.getLogger(__name__)

MORNING_PROMPT = (
    "Réalise le briefing matinal. Analyse l'agenda Radicale, la météo, la charge, le volume hebdo et les derniers runs Dreeve, "
    f"en appliquant tout ce que tu sais de moi. Met à jour / planifie les {settings.planning_horizon_days} prochains jours dans Radicale. "
    "Fais un résumé Telegram ultra-concis, sobre, avec des descriptions structurées et des émojis."
)


async def send_text(send, text: str) -> None:
    """send : coroutine(text, **kwargs). Markdown d'abord, texte brut si Telegram refuse le balisage."""
    for chunk in split_message(text):
        try:
            await send(chunk, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await send(md_unescape(chunk))


class CoachBot:
    def __init__(self, app: Application):
        self.app = app
        self.chat_id = settings.telegram_chat_id

    async def _push(self, text: str) -> None:
        await send_text(functools.partial(self.app.bot.send_message, chat_id=self.chat_id), text)

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

    async def on_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/memoire : ce que le coach a retenu (pour corriger : « oublie le #3 », « en fait je préfère… »)."""
        if not update.message or not await self._authorized(update):
            return
        text = ("*🧠 Ce que je sais de toi*\n" + md_escape(store.render())
                + "\n\n_Pour corriger : « oublie le #3 » ou dis-moi simplement ce qui a changé._")
        await send_text(update.message.reply_text, text)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text or not await self._authorized(update):
            return
        chat_id = str(update.effective_chat.id)

        typing = asyncio.create_task(self._keep_typing(update))
        try:
            reply = await agent.ask(update.message.text, chat_id=chat_id)
        except Exception as e:
            logger.exception("Erreur non gérée")
            reply = f"❌ Erreur technique : {type(e).__name__}. Rien n'est garanti côté Radicale, vérifie l'agenda."
        finally:
            typing.cancel()
        await send_text(update.message.reply_text, reply)

    @staticmethod
    async def _keep_typing(update: Update) -> None:
        # L'indicateur « écrit… » expire après ~5 s : on le renouvelle pendant que l'agent travaille.
        try:
            while True:
                await update.message.chat.send_action(ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("send_action échoué", exc_info=True)

    async def morning_briefing(self) -> None:
        try:
            reply = await agent.ask(MORNING_PROMPT, chat_id=self.chat_id)
        except Exception as e:
            logger.exception("Briefing matinal en échec")
            reply = f"❌ Briefing matinal impossible : {type(e).__name__}"
        await self._push(reply)

    async def check_rain(self) -> None:
        """Prévient (une fois par séance) s'il va pleuvoir pendant une séance planifiée."""
        try:
            now = datetime.datetime.now(TZ)
            horizon = now + datetime.timedelta(hours=settings.rain_hours_ahead)
            days = min(16, (settings.rain_hours_ahead + now.hour) // 24 + 2)
            (events, _), forecasts = await asyncio.gather(
                asyncio.to_thread(cs.list_events, now, horizon + datetime.timedelta(hours=6)),
                asyncio.to_thread(weather.fetch_hourly, days),
            )
        except Exception as e:
            logger.error(f"Alerte pluie : vérification impossible : {e}")
            return
        for alert in rain_alerts.find_alerts(events, forecasts, now, store.was_alerted):
            text = rain_alerts.format_alert(alert)
            try:
                await self._push(text)
            except Exception:
                logger.exception("Alerte pluie : envoi Telegram en échec")
                continue
            store.mark_alerted(alert.key, alert.end)
            await agent.add_to_history(self.chat_id, "[Alerte météo automatique envoyée par le système]", text)
            logger.info(f"Alerte pluie envoyée : {alert.summary} ({alert.place}, {alert.risk})")

    async def check_new_activity(self) -> None:
        try:
            latest = await asyncio.to_thread(dreeve.get_latest_run)
        except Exception as e:
            logger.error(f"Erreur vérification automatique Dreeve : {e}")
            return
        if not latest:
            return
        latest_id = latest.get("id")
        previous = store.last_activity_id   # persistant : un run importé pendant un redémarrage n'est pas perdu
        if latest_id == previous:
            return
        store.last_activity_id = latest_id
        if previous is None:   # tout premier passage : simple référence, pas de notification
            return

        prompt = (f"Je viens de terminer et d'uploader une nouvelle séance : {dreeve.format_activity(latest)[2:]}. "
                  "Regarde ses tours/splits via Dreeve si utile et compare avec ce qui était prévu dans l'agenda. "
                  "Donne-moi un débrief très court, factuel et concis. Ne modifie pas l'agenda.")
        try:
            reply = await agent.ask(prompt, chat_id=self.chat_id, allow_writes=False)
            await self._push(f"⚡ *Nouveau run détecté !*\n\n{reply}")
        except Exception:
            logger.exception("Débrief de séance en échec")


def build_bot() -> CoachBot:
    app = Application.builder().token(settings.telegram_bot_token).build()
    bot = CoachBot(app)
    app.add_handler(CommandHandler(["memoire", "memory"], bot.on_memory))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_message))
    return bot
