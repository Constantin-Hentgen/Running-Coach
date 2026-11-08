"""Alerte pluie : prévient sur Telegram quand il va pleuvoir pendant une séance planifiée.

- Séances lues dans l'agenda sur les RAIN_ALERT_HOURS_AHEAD prochaines heures
  (on ignore les journées entières, les événements > 5 h et ceux dont le titre contient
  un mot de RAIN_ALERT_SKIP_KEYWORDS, ex. « repos »).
- Lieu de la séance : champ LOCATION de l'événement, sinon un nom de lieu cité dans le
  titre/la description, sinon le premier lieu de WEATHER_LOCATIONS.
- Prévision HORAIRE sur le créneau exact : alerte si probabilité max ≥ RAIN_ALERT_PROBABILITY %
  ET cumul ≥ RAIN_ALERT_MIN_MM mm.
- Une seule alerte par séance (si elle est déplacée, le nouveau créneau est réévalué).
- Message généré par le code (aucun appel au modèle), avec la prévision de l'autre lieu
  pour comparer ; il est ajouté à l'historique pour que « déplace-la » fonctionne en réponse.
"""
import datetime
import logging
import re
from dataclasses import dataclass

from .config import FR_DAYS, TZ, UTC, settings
from .formatting import md_escape

logger = logging.getLogger(__name__)

MAX_SESSION = datetime.timedelta(hours=5)

Hourly = list[tuple[datetime.datetime, int | None, float]]


@dataclass
class Risk:
    probability: int | None
    mm: float

    def is_rainy(self) -> bool:
        prob_ok = self.probability is None or self.probability >= settings.rain_probability
        return prob_ok and self.mm >= settings.rain_min_mm

    def __str__(self) -> str:
        prob = f"{self.probability} %" if self.probability is not None else "? %"
        return f"{prob}, {self.mm:.1f} mm"


@dataclass
class Alert:
    key: str
    summary: str
    start: datetime.datetime
    end: datetime.datetime
    place: str
    risk: Risk
    others: dict[str, Risk]


def window_risk(hourly: Hourly, start: datetime.datetime, end: datetime.datetime) -> Risk | None:
    """Agrège les heures qui chevauchent [start, end[ (une heure H couvre [H, H+1h[)."""
    hours = [(p, mm) for t, p, mm in hourly if t < end and t + datetime.timedelta(hours=1) > start]
    if not hours:
        return None
    probs = [p for p, _ in hours if p is not None]
    return Risk(max(probs) if probs else None, round(sum(mm for _, mm in hours), 2))


def resolve_place(event: dict, places: list[str]) -> str:
    location = (event.get("location") or "").casefold()
    for name in places:
        if name.casefold() == location or (location and name.casefold() in location):
            return name
    text = f"{event.get('summary', '')} {event.get('description', '')}".casefold()
    for name in places:
        if name.casefold() in text:
            return name
    return places[0]


def is_training(event: dict, start: datetime.datetime, end: datetime.datetime) -> bool:
    if event.get("all_day") or end - start > MAX_SESSION:
        return False
    words = set(re.findall(r"\w+", (event.get("summary") or "").casefold()))
    return not words & set(settings.rain_skip_keywords)


def find_alerts(events: list[dict], forecasts: dict[str, Hourly], now: datetime.datetime,
                already_alerted) -> list[Alert]:
    places = list(forecasts)
    if not places:
        return []
    horizon = now + datetime.timedelta(hours=settings.rain_hours_ahead)
    alerts = []
    for ev in events:
        start, end = datetime.datetime.fromisoformat(ev["start"]), datetime.datetime.fromisoformat(ev["end"])
        if end <= now or start > horizon or not is_training(ev, start, end):
            continue
        key = f"{ev['uid']}@{start.astimezone(UTC).isoformat()}"
        if already_alerted(key):
            continue
        place = resolve_place(ev, places)
        risk = window_risk(forecasts[place], start, end)
        if not risk or not risk.is_rainy():
            continue
        others = {p: r for p in places if p != place and (r := window_risk(forecasts[p], start, end))}
        alerts.append(Alert(key, ev.get("summary") or "Séance", start, end, place, risk, others))
    return alerts


def format_alert(alert: Alert) -> str:
    s, e = alert.start.astimezone(TZ), alert.end.astimezone(TZ)
    lines = [
        f"☔ *Pluie prévue sur ta séance* « {md_escape(alert.summary)} »",
        f"- 📅 {FR_DAYS[s.weekday()]} {s:%d/%m} {s:%H:%M}–{e:%H:%M}",
        f"- 📍 {md_escape(alert.place)} : {alert.risk}",
    ]
    for place, risk in alert.others.items():
        dry = " ✅ sec, bonne alternative" if not risk.is_rainy() else ""
        lines.append(f"- 📍 {md_escape(place)} : {risk}{dry}")
    lines.append("_Réponds-moi si tu veux la déplacer, changer de lieu ou l'adapter._")
    return "\n".join(lines)
