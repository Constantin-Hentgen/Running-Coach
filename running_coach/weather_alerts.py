"""Alertes météo : prévient sur Telegram quand une séance planifiée tombe sous de la pluie, une forte
chaleur, de fortes rafales ou un risque de verglas, et quand la menace disparaît (« bonne nouvelle »).

- Séances lues dans l'agenda sur les RAIN_ALERT_HOURS_AHEAD prochaines heures (48 h par défaut : l'alerte
  part dès que la prévision annonce le risque, donc normalement 24 à 48 h avant la séance).
  On ignore les journées entières, les événements > 5 h et ceux dont le titre contient un mot
  de RAIN_ALERT_SKIP_KEYWORDS (ex. « repos »).
- Lieu de la séance : champ LOCATION de l'événement, sinon un nom de lieu cité dans le
  titre/la description, sinon le premier lieu de WEATHER_LOCATIONS.
- Prévision HORAIRE sur le créneau exact :
  ☔ pluie : probabilité max ≥ RAIN_ALERT_PROBABILITY % ET cumul ≥ RAIN_ALERT_MIN_MM mm ;
  🥵 chaleur : température ressentie max ≥ WEATHER_ALERT_HEAT_C ;
  💨 vent : rafales max ≥ WEATHER_ALERT_GUST_KMH ;
  🧊 verglas : température min ≤ WEATHER_ALERT_FREEZE_C avec neige / pluie verglaçante, ou sol mouillé
     (précipitations dans les 12 h avant la séance).
- Une alerte par nouveau risque et par séance (un risque déjà signalé ne l'est pas deux fois ; si la
  séance est déplacée, le nouveau créneau est réévalué). Si tous les risques signalés disparaissent,
  un message « bonne nouvelle » part une fois (au plus tard 1 h avant la séance).
- Messages générés par le code (aucun appel au modèle), avec la prévision des autres lieux pour
  comparer ; ajoutés à l'historique pour que « déplace-la » fonctionne en réponse.
"""
import datetime
import logging
import re
from dataclasses import dataclass, field

from .config import FR_DAYS, TZ, UTC, settings
from .formatting import md_escape

logger = logging.getLogger(__name__)

MAX_SESSION = datetime.timedelta(hours=5)
WET_LOOKBACK = datetime.timedelta(hours=12)
CLEAR_MIN_LEAD = datetime.timedelta(hours=1)
ICY_CODES = {56, 57, 66, 67, 71, 73, 75, 77, 85, 86}   # pluie/bruine verglaçante, neige


@dataclass
class Hour:
    time: datetime.datetime
    probability: int | None = None
    mm: float = 0.0
    temp: float | None = None
    feels: float | None = None
    gust: float | None = None
    code: int | None = None


ICONS = {"pluie": "☔", "chaleur": "🥵", "vent": "💨", "verglas": "🧊"}


@dataclass
class Conditions:
    probability: int | None
    mm: float
    feels_max: float | None = None
    gust_max: float | None = None
    temp_min: float | None = None
    icy: bool = False

    def hazards(self) -> dict[str, str]:
        """Risques présents sur le créneau : nom → détail lisible."""
        out = {}
        prob_ok = self.probability is None or self.probability >= settings.rain_probability
        if prob_ok and self.mm >= settings.rain_min_mm:
            prob = f"{self.probability} %" if self.probability is not None else "? %"
            out["pluie"] = f"{prob}, {self.mm:.1f} mm"
        if self.feels_max is not None and self.feels_max >= settings.heat_alert_c:
            out["chaleur"] = f"{self.feels_max:.0f}°C ressentis"
        if self.gust_max is not None and self.gust_max >= settings.gust_alert_kmh:
            out["vent"] = f"rafales {self.gust_max:.0f} km/h"
        if self.icy:
            out["verglas"] = f"{self.temp_min:.0f}°C, sol glissant possible"
        return out


def conditions(hours: list[Hour], start: datetime.datetime, end: datetime.datetime) -> Conditions | None:
    """Agrège les heures qui chevauchent [start, end[ (une heure H couvre [H, H+1h[)."""
    window = [h for h in hours if h.time < end and h.time + datetime.timedelta(hours=1) > start]
    if not window:
        return None
    probs = [h.probability for h in window if h.probability is not None]
    feels = [h.feels for h in window if h.feels is not None]
    gusts = [h.gust for h in window if h.gust is not None]
    temps = [h.temp for h in window if h.temp is not None]
    temp_min = min(temps) if temps else None
    icy = False
    if temp_min is not None and temp_min <= settings.freeze_alert_c:
        wet = sum(h.mm for h in hours if start - WET_LOOKBACK <= h.time < end)
        icy = wet >= 0.2 or any(h.code in ICY_CODES for h in window)
    return Conditions(
        probability=max(probs) if probs else None,
        mm=round(sum(h.mm for h in window), 2),
        feels_max=max(feels) if feels else None,
        gust_max=max(gusts) if gusts else None,
        temp_min=temp_min,
        icy=icy,
    )


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


@dataclass
class Notice:
    kind: str                       # "alert" | "clear"
    key: str
    uid: str
    summary: str
    start: datetime.datetime
    end: datetime.datetime
    place: str
    hazards: dict[str, str]         # alerte : risques actuels ; « bonne nouvelle » : vide
    new: set[str] = field(default_factory=set)        # risques jamais signalés pour cette séance
    cleared: list[str] = field(default_factory=list)  # « bonne nouvelle » : risques disparus
    others: dict[str, dict[str, str]] = field(default_factory=dict)
    # Après envoi : risques considérés comme signalés. Pour une alerte, l'union avec ceux déjà signalés
    # (un risque qui oscille d'une prévision à l'autre n'est pas re-signalé) ; vide après « bonne nouvelle »,
    # pour qu'un risque qui revient soit signalé à nouveau.
    signalled: list[str] = field(default_factory=list)


def find_notices(events: list[dict], forecasts: dict[str, list[Hour]], now: datetime.datetime,
                 state_of) -> list[Notice]:
    """`state_of(clé)` → {hazards, cleared} déjà mémorisé pour la séance, ou None."""
    places = list(forecasts)
    if not places:
        return []
    horizon = now + datetime.timedelta(hours=settings.rain_hours_ahead)
    notices = []
    for ev in events:
        start, end = datetime.datetime.fromisoformat(ev["start"]), datetime.datetime.fromisoformat(ev["end"])
        if end <= now or start > horizon or not is_training(ev, start, end):
            continue
        key = f"{ev['uid']}@{start.astimezone(UTC).isoformat()}"
        place = resolve_place(ev, places)
        cond = conditions(forecasts[place], start, end)
        if cond is None:
            continue
        current = cond.hazards()
        previous = state_of(key) or {}
        signalled = set(previous.get("hazards") or [])
        base = dict(key=key, uid=ev.get("uid", ""), summary=ev.get("summary") or "Séance", start=start, end=end, place=place)
        if set(current) - signalled:
            others = {p: c.hazards() for p in places if p != place and (c := conditions(forecasts[p], start, end))}
            notices.append(Notice("alert", hazards=current, new=set(current) - signalled, others=others,
                                  signalled=sorted(signalled | set(current)), **base))
        elif signalled and not current and start - now > CLEAR_MIN_LEAD:
            notices.append(Notice("clear", hazards={}, cleared=sorted(signalled), **base))
    return notices


def _when(n: Notice, now: datetime.datetime | None) -> str:
    s, e = n.start.astimezone(TZ), n.end.astimezone(TZ)
    lead = f" ({lead_time(n.start, now)})" if now else ""
    return f"{FR_DAYS[s.weekday()]} {s:%d/%m} {s:%H:%M}–{e:%H:%M}{lead}"


def lead_time(start: datetime.datetime, now: datetime.datetime) -> str:
    hours = int((start - now).total_seconds() // 3600)
    if hours < 1:
        return "imminent"
    return f"dans {hours} h" if hours < 48 else f"dans {hours // 24} j"


def _hazard_line(hazards: dict[str, str]) -> str:
    return " · ".join(f"{ICONS[h]} {h} {detail}" for h, detail in hazards.items())


def format_notice(n: Notice, now: datetime.datetime | None = None) -> str:
    if n.kind == "clear":
        what = ", ".join(n.cleared)
        return (f"✅ *Bonne nouvelle* : plus de risque ({md_escape(what)}) sur ta séance « {md_escape(n.summary)} »\n"
                f"- 📅 {_when(n, now)}\n- 📍 {md_escape(n.place)}\n_On garde le programme prévu._")
    title = "☔ *Pluie prévue sur ta séance*" if set(n.hazards) == {"pluie"} else "⚠️ *Météo à surveiller sur ta séance*"
    lines = [
        f"{title} « {md_escape(n.summary)} »",
        f"- 📅 {_when(n, now)}",
        f"- 📍 {md_escape(n.place)} : {md_escape(_hazard_line(n.hazards))}",
    ]
    for place, hazards in n.others.items():
        status = md_escape(_hazard_line(hazards)) if hazards else "✅ RAS, bonne alternative"
        lines.append(f"- 📍 {md_escape(place)} : {status}")
    lines.append("_« Adapter » : je te propose un créneau ou un lieu plus favorable. Tu peux aussi me répondre directement._")
    return "\n".join(lines)


def adapt_prompt(n: Notice) -> str:
    """Demande envoyée au coach quand l'athlète clique « Adapter » sous une alerte."""
    others = "; ".join(f"{p} : {_hazard_line(h) or 'RAS'}" for p, h in n.others.items()) or "aucun autre lieu"
    return (f"Alerte météo sur ma séance « {n.summary} » (UID {n.uid}) du {_when(n, None)} à {n.place} : "
            f"{_hazard_line(n.hazards)}. Autres lieux sur ce créneau : {others}. "
            "Adapte-la au mieux (autre créneau proche, autre lieu, contenu ou tenue adaptés à la météo) "
            "en respectant ma mémoire et le reste de la semaine, puis dis-moi ce que tu as changé.")
