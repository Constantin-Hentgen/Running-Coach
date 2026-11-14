"""Séances : détection des nouveaux runs, débrief et ressenti après la séance."""
import asyncio
import datetime
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .. import agent, dreeve
from ..config import TZ, settings
from ..memory import LEGS, store
from .ui import PAIN_FOLLOW_UP, legs_markup, rpe_markup
from ..i18n import _

logger = logging.getLogger(__name__)


DEBRIEF_PROMPT = (
    "{intro} :\n{runs}\n\n"
    "Réagis comme un pote qui suit mes courses : 1 ou 2 messages très courts (une ligne chacun, séparés par une ligne "
    "vide), sans titres ni liste de chiffres. Un mot sincère sur la séance (par rapport à ce qui était prévu et à mon "
    "historique : relis l'agenda du jour avec list_calendar_events si besoin), et seulement si ça compte : la chose à "
    "retenir pour la suite (récup, repas, prochaine séance), en gardant mes objectifs en tête. Si un ajustement du programme est vraiment conseillé, "
    "enregistre-le avec propose_change sans le faire. Ne modifie pas l'agenda."
)
DEBRIEF_TIMEOUT_S = 300   # au-delà, récap brut : un débrief bloqué ne doit jamais empêcher les vérifications suivantes
# État de la dernière vérification Dreeve, exposé par /health pour diagnostiquer un débrief manquant.
ACTIVITY_STATUS: dict = {"checked_at": None, "result": None, "error": None, "debriefed_at": None}


class RunsMixin:
    """Méthodes de CoachBot liées aux séances de course."""

    async def on_debrief(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not await self._authorized(update):
            return
        try:
            runs = await asyncio.to_thread(dreeve.get_recent_runs, 1) if settings.dreeve_enabled else []
        except Exception as e:
            await update.message.reply_text(_("❌ Dreeve indisponible : {error}").format(error=e))
            return
        if not runs:
            await update.message.reply_text(_("Aucun run trouvé dans Dreeve."))
            return
        prompt = DEBRIEF_PROMPT.format(intro="Débriefe ma dernière séance", runs=dreeve.format_activity(runs[0]))
        await self._ask_and_reply(update, prompt, allow_writes=False)
        await self._ask_checkin(runs[0], only_if_missing=True)

    async def _on_checkin(self, update: Update, step: str, activity_id: str, value: str) -> None:
        query = update.callback_query
        await query.answer()
        if step == "r":
            rpe = int(value)
            store.set_checkin(activity_id, rpe=rpe)
            await query.edit_message_text(
                _("{rpe}/10, ok 👌 Et les jambes ?").format(rpe=rpe),
                parse_mode=ParseMode.MARKDOWN, reply_markup=legs_markup(activity_id))
        elif step == "l" and value in LEGS:
            store.set_checkin(activity_id, legs=value)
            await query.edit_message_text(_("Merci 🙏") if value != "douleur" else _("Aïe 😬"))
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
        await self._push(_("Alors, t'as senti comment ? Effort de 1 (tranquille) à 10 (à bloc) :"), rpe_markup(activity_id))

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
                reply = agent.AgentReply(_("J'ai vu passer ta sortie 🏃 mais j'ai pas réussi à la regarder en détail 🥲 "
                                           "Tape /debrief pour réessayer.") + f"\n{runs_text}")
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
