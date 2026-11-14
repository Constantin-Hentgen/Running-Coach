"""Tâches planifiées : briefing matinal, bilan hebdomadaire, alertes météo."""
import asyncio
import datetime
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .. import agent, coaching, dreeve, weather, weather_alerts
from .. import calendar_store as cs
from ..config import TZ, settings
from ..memory import store

logger = logging.getLogger(__name__)


MORNING_PROMPT = (
    "Réalise le briefing matinal. Analyse l'agenda Radicale, la météo, la charge, mon historique Dreeve, mes ressentis et "
    "le compte à rebours de mes objectifs, en appliquant tout ce que tu sais de moi. "
    f"Met à jour / planifie les {settings.planning_horizon_days} prochains jours dans Radicale, en cohérence avec la "
    "périodisation vers mon objectif principal.\n"
    "Fais-le en silence : ne me parle pas des semaines à venir ni de l'état du plan.\n"
    "Puis envoie-moi un petit mot du matin, comme un pote : 1 ou 2 messages d'une ligne (séparés par une ligne vide), "
    "seulement sur aujourd'hui : la séance du jour (ou le repos) et LE truc à ne pas oublier, plus ce qui a changé "
    "aujourd'hui ou demain dans l'agenda s'il y a lieu. La météo seulement si elle change quelque chose aujourd'hui. "
    "Pas de titres, pas de chiffres inutiles, pas de proposition au-delà de demain."
)
REVIEW_PROMPT = (
    "Fais le bilan de ma semaine. Chiffres calculés par le système (fiables, ne les recalcule pas) :\n{adherence}\n\n"
    "Dis-le comme un pote : 2 messages d'une ou deux lignes (séparés par une ligne vide), sans titres : ce que je "
    "retiens de la semaine (course, alimentation, forme) ; le cap pour la semaine prochaine. Si le programme mérite un "
    "ajustement, enregistre-le avec propose_change sans le faire. Ne modifie pas l'agenda."
)


class JobsMixin:
    """Méthodes de CoachBot déclenchées par le planificateur ou par leur commande."""

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
