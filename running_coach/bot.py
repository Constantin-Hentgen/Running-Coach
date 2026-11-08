"""Telegram : messages entrants, commandes, briefing matinal, détection de nouveaux runs, alertes pluie."""
import asyncio
import datetime
import functools
import logging

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import agent, commands, dreeve, rain_alerts, weather
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


DEBRIEF_PROMPT = (
    "{intro} :\n{runs}\n\n"
    "Fais-moi spontanément un retour de coach, concis et factuel, en 4 blocs courts :\n"
    "1. 🏃 *La séance* : ce qui était prévu dans l'agenda vs ce que j'ai fait. Regarde tours/splits via Dreeve si utile. "
    "Si la séance prévue était un jour passé, relis l'agenda de ce jour-là avec list_calendar_events.\n"
    "2. 📈 *Par rapport à mes performances passées* : compare avec mes séances similaires récentes "
    "(allure, FC, dénivelé) et la tendance des dernières semaines ; signale un progrès net ou un signal d'alerte.\n"
    "3. 🔋 *Forme et prochaines séances* : avec la charge (TSB, ratio A/C), la météo et ce qu'il y a dans l'agenda "
    "les prochains jours, dis si le programme reste adapté. Si un ajustement est conseillé, propose-le précisément "
    "(séance, jour, contenu) sans le faire : je validerai en répondant.\n"
    "4. 🎯 *Objectifs* : où j'en suis par rapport à mes objectifs (mémoire, courses dans l'agenda) et le point clé "
    "à travailler ensuite. Si tu ne connais aucun objectif, demande-le-moi en une phrase.\n"
    "Ne modifie pas l'agenda."
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
        self._activity_lock = asyncio.Lock()   # jamais deux détections en parallèle (pas de double débrief)

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
        await self._ask_and_reply(update, update.message.text)

    async def _ask_and_reply(self, update: Update, prompt: str, allow_writes: bool = True) -> None:
        typing = asyncio.create_task(self._keep_typing(update))
        try:
            reply = await agent.ask(prompt, chat_id=str(update.effective_chat.id), allow_writes=allow_writes)
        except Exception as e:
            logger.exception("Erreur non gérée")
            reply = f"❌ Erreur technique : {type(e).__name__}. Rien n'est garanti côté Radicale, vérifie l'agenda."
        finally:
            typing.cancel()
        await send_text(update.message.reply_text, reply)

    # --- Commandes ----------------------------------------------------------

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

    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def build():
            store.set_history(str(update.effective_chat.id), [])
            return "🆕 Conversation remise à zéro. Ta mémoire est intacte (/memoire)."
        await self._command(update, build)

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

    async def on_briefing(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not await self._authorized(update):
            return
        await self._ask_and_reply(update, MORNING_PROMPT.replace("le briefing matinal", "un briefing"))

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
            text = rain_alerts.format_alert(alert, now)
            try:
                await self._push(text)
            except Exception:
                logger.exception("Alerte pluie : envoi Telegram en échec")
                continue
            store.mark_alerted(alert.key, alert.end)
            await agent.add_to_history(self.chat_id, "[Alerte météo automatique envoyée par le système]", text)
            logger.info(f"Alerte pluie envoyée : {alert.summary} ({alert.place}, {alert.risk})")

    async def check_new_activity(self, source: str = "polling") -> bool:
        """Débriefe chaque run apparu dans Dreeve depuis le dernier vu. Retourne True si un run a été trouvé."""
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
                await self._push(f"⚡ *Nouveau run détecté !*\n\n{reply}")
            except Exception:
                logger.exception("Débrief de séance en échec")
            return True


def build_bot() -> CoachBot:
    app = Application.builder().token(settings.telegram_bot_token).build()
    bot = CoachBot(app)
    handlers = {
        "programme": bot.on_agenda, "forme": bot.on_form, "derniers": bot.on_last_runs,
        "meteo": bot.on_weather, "memoire": bot.on_memory, "debrief": bot.on_debrief,
        "briefing": bot.on_briefing, "nouveau": bot.on_reset, "aide": bot.on_help,
    }
    for name, aliases, _ in commands.COMMANDS:
        app.add_handler(CommandHandler([name, *aliases], handlers[name]))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_message))
    return bot
