"""Radicale / CalDAV : lecture et écritures vérifiées.

- tout est identifié par UID (pas de recherche floue par titre) ;
- chaque écriture est vérifiée par RELECTURE sur le serveur ;
- le parsing/édition se fait avec `icalendar` sur le texte brut (`ev.data`).
  On n'utilise plus vobject : il ne sait pas sérialiser `datetime.timezone.utc`
  ("Unable to guess TZID for tzinfo UTC"), ce qui cassait toute modification.
"""
import datetime
import logging
import re
import threading
import uuid

import caldav
from caldav.lib.error import NotFoundError
from icalendar import Calendar, Event

from .config import FR_DAYS, TZ, UTC, settings

logger = logging.getLogger(__name__)

DEFAULT_DESCRIPTION = "👟 Séance planifiée par ton coach."
TOLERANCE_S = 60


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def now_utc() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def parse_dt(value: str) -> datetime.datetime:
    """ISO -> datetime aware UTC. Sans fuseau, on suppose le fuseau local du coach."""
    dt = datetime.datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(UTC)


def parse_range(start: str, end: str) -> tuple[datetime.datetime, datetime.datetime]:
    """Une date seule en fin de plage inclut toute la journée."""
    s, e = parse_dt(start), parse_dt(end)
    if len(end.strip()) == 10:
        e += datetime.timedelta(days=1)
    return s, e


def to_aware(value) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=TZ)
    return datetime.datetime.combine(value, datetime.time.min, tzinfo=TZ)  # date = journée entière


def fmt_when(start: datetime.datetime, end: datetime.datetime) -> str:
    s, e = start.astimezone(TZ), end.astimezone(TZ)
    return f"{FR_DAYS[s.weekday()]} {s:%d/%m} {s:%H:%M}–{e:%H:%M}"


def _close(a: datetime.datetime, b: datetime.datetime) -> bool:
    return abs((a - b).total_seconds()) <= TOLERANCE_S


# ---------------------------------------------------------------------------
# iCalendar
# ---------------------------------------------------------------------------

def parse_vevent(ics: str):
    """Retourne (Calendar, VEVENT principal)."""
    cal = Calendar.from_ical(ics)
    for comp in cal.walk("VEVENT"):
        if "RECURRENCE-ID" not in comp:
            return cal, comp
    raise ValueError("aucun VEVENT dans la ressource")


def event_bounds(comp) -> tuple[datetime.datetime, datetime.datetime]:
    start_raw = comp.decoded("DTSTART")
    start = to_aware(start_raw)
    if "DTEND" in comp:
        end = to_aware(comp.decoded("DTEND"))
    elif "DURATION" in comp:
        end = start + comp.decoded("DURATION")
    elif not isinstance(start_raw, datetime.datetime):
        end = start + datetime.timedelta(days=1)   # RFC 5545 : journée entière par défaut
    else:
        end = start
    return start, end


def is_all_day(comp) -> bool:
    return not isinstance(comp.decoded("DTSTART"), datetime.datetime)


def _text(comp, key: str, default: str = "") -> str:
    return str(comp.get(key, default))


def serialize(comp) -> dict:
    start, end = event_bounds(comp)
    item = {
        "uid": _text(comp, "UID"),
        "summary": _text(comp, "SUMMARY", "(sans titre)"),
        "start": start.astimezone(TZ).isoformat(timespec="minutes"),
        "end": end.astimezone(TZ).isoformat(timespec="minutes"),
        "description": _text(comp, "DESCRIPTION"),
    }
    if comp.get("LOCATION"):
        item["location"] = _text(comp, "LOCATION")
    if is_all_day(comp):
        item["all_day"] = True
    return item


def _set(comp, key: str, value) -> None:
    """Remplace une propriété en repartant de zéro (supprime TZID/VALUE=DATE obsolètes)."""
    comp.pop(key, None)
    comp.add(key, value)


def build_event(uid: str, summary: str, start, end, description: str, location: str | None = None) -> str:
    cal = Calendar()
    cal.add("prodid", "-//running-coach-agent//FR")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", uid)
    ev.add("dtstamp", now_utc())
    ev.add("dtstart", start)
    ev.add("dtend", end)
    ev.add("summary", summary)
    ev.add("description", description)
    if location:
        ev.add("location", location)
    cal.add_component(ev)
    return cal.to_ical().decode("utf-8")


def apply_update(ics: str, *, summary=None, start=None, end=None, description=None, location=None) -> str:
    """Applique les modifications au VEVENT et renvoie le nouvel iCalendar.

    Les dates sont écrites en UTC (suffixe Z) : aucun VTIMEZONE requis, compris par tous les clients.
    Un événement « journée entière » déplacé à une heure précise devient un événement horodaté.
    """
    cal, comp = parse_vevent(ics)
    if summary:
        _set(comp, "SUMMARY", summary)
    if start:
        _set(comp, "DTSTART", start.astimezone(UTC))
    if end:
        comp.pop("DURATION", None)
        _set(comp, "DTEND", end.astimezone(UTC))
    if description is not None:
        _set(comp, "DESCRIPTION", description)
    if location:
        _set(comp, "LOCATION", location)
    _set(comp, "DTSTAMP", now_utc())
    _set(comp, "LAST-MODIFIED", now_utc())
    _set(comp, "SEQUENCE", int(comp.get("SEQUENCE", 0)) + 1)
    return cal.to_ical().decode("utf-8")


# ---------------------------------------------------------------------------
# Accès serveur
# ---------------------------------------------------------------------------

_local = threading.local()   # un client (donc une session HTTP) par thread : asyncio.to_thread en lance plusieurs


def get_calendar():
    """Client CalDAV réutilisé par thread (keep-alive) au lieu d'un nouveau par appel, jamais partagé entre threads."""
    if getattr(_local, "calendar", None) is None:
        client = caldav.DAVClient(url=settings.radicale_url, username=settings.radicale_user, password=settings.radicale_pass)
        _local.calendar = client.calendar(url=settings.radicale_url)
    return _local.calendar


def list_events(start: datetime.datetime, end: datetime.datetime) -> tuple[list[dict], int]:
    """Retourne (événements triés, nb d'événements illisibles ignorés)."""
    items, skipped = [], 0
    for ev in get_calendar().search(start=start, end=end, event=True):
        try:
            items.append(serialize(parse_vevent(ev.data)[1]))
        except Exception as e:
            skipped += 1
            logger.warning(f"Événement illisible ignoré : {e}")
    items.sort(key=lambda x: x["start"])
    return items, skipped


def _fetch(uid: str):
    """Retourne (ressource, VEVENT) ou lève LookupError."""
    try:
        ev = get_calendar().get_event_by_uid(uid)
    except NotFoundError:
        raise LookupError(f"aucun événement avec l'UID {uid}") from None
    return ev, parse_vevent(ev.data)[1]


def create_event(summary: str, start, end, description: str | None, location: str | None = None) -> dict:
    if end <= start:
        raise ValueError("end_time doit être postérieur à start_time")
    if end < now_utc():
        raise ValueError("événement dans le passé : refusé")

    uid = f"{uuid.uuid4()}@running-coach"
    get_calendar().add_event(build_event(uid, summary, start, end, description or DEFAULT_DESCRIPTION, location))

    c_start, c_end = event_bounds(_fetch(uid)[1])
    if not (_close(c_start, start) and _close(c_end, end)):
        raise RuntimeError("l'événement relu sur Radicale ne correspond pas aux horaires demandés")
    return {"uid": uid, "summary": summary, "start": c_start, "end": c_end}


def update_event(uid: str, *, summary=None, start=None, end=None, description=None, location=None) -> dict:
    ev, comp = _fetch(uid)
    old_start, old_end = event_bounds(comp)
    old_summary = _text(comp, "SUMMARY", uid)
    if old_end < now_utc():
        raise PermissionError("événement passé : modification refusée")
    if start and not end:
        end = start + (old_end - old_start)   # on conserve la durée
    if start and start < now_utc():
        raise PermissionError("nouvelle date dans le passé : refusée")
    if end and end <= (start or old_start):
        raise ValueError("la fin doit être postérieure au début")

    ev.data = apply_update(ev.data, summary=summary, start=start, end=end,
                           description=description, location=location)
    ev.save()

    c = _fetch(uid)[1]
    c_start, c_end = event_bounds(c)
    if start and not _close(c_start, start):
        raise RuntimeError("relecture : la date de début n'a pas été modifiée sur Radicale")
    if end and not _close(c_end, end):
        raise RuntimeError("relecture : la date de fin n'a pas été modifiée sur Radicale")
    if summary and _text(c, "SUMMARY") != summary:
        raise RuntimeError("relecture : le titre n'a pas été modifié sur Radicale")
    return {"uid": uid, "summary": summary or old_summary, "start": c_start, "end": c_end}


def describe(uid: str) -> str:
    """Libellé lisible d'un événement (pour les comptes-rendus d'échec)."""
    try:
        comp = _fetch(uid)[1]
        return f"{_text(comp, 'SUMMARY', uid)} ({fmt_when(*event_bounds(comp))})"
    except Exception:
        return uid


def delete_event(uid: str) -> dict:
    ev, comp = _fetch(uid)
    start, end = event_bounds(comp)
    summary = _text(comp, "SUMMARY", uid)
    if end < now_utc():
        raise PermissionError("événement passé : suppression refusée")

    ev.delete()
    try:
        _fetch(uid)
    except LookupError:
        return {"uid": uid, "summary": summary, "start": start, "end": end}
    raise RuntimeError("l'événement est toujours présent sur Radicale après suppression")


# ---------------------------------------------------------------------------
# Liste de courses : UN événement, à l'heure voulue, dans l'agenda courses dédié (RADICALE_SHOPPING_URL),
# avec la checklist par rayon dans la description (visible dans n'importe quelle app de calendrier).
# ---------------------------------------------------------------------------

SHOPPING_UID_SUFFIX = "@running-coach-shopping"


def get_shopping_calendar():
    """Comme get_calendar : un client par thread."""
    if getattr(_local, "shopping", None) is None:
        client = caldav.DAVClient(url=settings.radicale_shopping_url, username=settings.radicale_user,
                                  password=settings.radicale_pass)
        _local.shopping = client.calendar(url=settings.radicale_shopping_url)
    return _local.shopping


def item_label(item: dict) -> str:
    return item["name"] + (f" — {item['qty']}" if item.get("qty") else "")


def group_by_aisle(items: list[dict]) -> dict[str, list[tuple[int, dict]]]:
    """Rayon → [(index, article)], dans l'ordre d'apparition des rayons."""
    groups: dict[str, list[tuple[int, dict]]] = {}
    for i, item in enumerate(items):
        groups.setdefault(item.get("aisle") or "🛒 Divers", []).append((i, item))
    return groups


def format_cost(lst: dict) -> str | None:
    lo, hi = lst.get("cost_min"), lst.get("cost_max")
    if lo is None and hi is None:
        return None
    if lo is None or hi is None or round(lo) == round(hi):
        return f"~{round(lo if lo is not None else hi)} €"
    return f"{round(lo)}–{round(hi)} €"


def shopping_summary_line(lst: dict) -> str:
    """« 7 jours · ~55–70 € » : ce que couvre la liste et son coût estimé."""
    parts = []
    if lst.get("days"):
        parts.append(f"{lst['days']} jour{'s' if lst['days'] > 1 else ''}")
    if cost := format_cost(lst):
        parts.append(f"{cost} estimés")
    return " · ".join(parts)


def plain_text(markdown: str) -> str:
    """Retire le balisage Telegram (*gras*, _italique_, `code`, échappements) pour une description d'agenda."""
    return re.sub(r"\\([_*`\[])", r"\1", re.sub(r"(?<!\\)[*_`]", "", markdown)).strip()


def shopping_description(lst: dict) -> str:
    """Description de l'événement courses : couverture, coût estimé, checklist, puis les recettes si générées."""
    head = []
    if lst.get("days") or lst.get("coverage"):
        head.append("Couvre : " + ", ".join(x for x in (lst.get("days") and f"{lst['days']} jours", lst.get("coverage")) if x))
    if cost := format_cost(lst):
        head.append(f"Coût estimé : {cost}")
    if not lst.get("recipes"):
        head.append("Recettes : /recipes dans Telegram")
    text = "\n".join(head) + "\n\n" + format_checklist(lst["items"])
    if lst.get("recipes"):
        text += "\n\n🍳 RECETTES\n\n" + plain_text(lst["recipes"])
    return text


def format_checklist(items: list[dict]) -> str:
    """Articles {name, qty, aisle, done} → checklist texte par rayon (✅ acheté, ☐ à acheter)."""
    return "\n\n".join(
        f"{aisle}\n" + "\n".join(f"{'✅' if item.get('done') else '☐'} {item_label(item)}" for _, item in entries)
        for aisle, entries in group_by_aisle(items).items())


def _coach_shopping_items(now: datetime.datetime) -> list:
    """Ce que le coach a déjà créé et qu'une nouvelle liste remplace : ses événements courses à venir
    (les passés restent, c'est l'historique) et les anciennes tâches (ancienne version de la liste)."""
    found = []
    shopping = get_shopping_calendar()
    for obj in [*shopping.events(), *shopping.todos(include_completed=True)]:
        try:
            comp = next(c for c in Calendar.from_ical(obj.data).walk() if c.name in ("VEVENT", "VTODO"))
        except Exception:
            continue
        if not str(comp.get("UID", "")).endswith(SHOPPING_UID_SUFFIX):
            continue
        if comp.name == "VTODO" or event_bounds(comp)[1] > now:
            found.append(obj)
    return found


def save_shopping_event(lst: dict, start: datetime.datetime, end: datetime.datetime) -> dict:
    """Crée l'événement courses (relu sur le serveur) et retire la précédente liste à venir du coach.
    Les événements et tâches que tu crées toi-même dans cet agenda ne sont jamais touchés."""
    items = lst["items"]
    if not items:
        raise ValueError("liste vide")
    if end <= start:
        raise ValueError("l'heure de fin doit être après l'heure de début")
    now = now_utc()
    if end < now:
        raise ValueError("créneau dans le passé : refusé")
    previous = _coach_shopping_items(now)
    uid = f"{uuid.uuid4()}{SHOPPING_UID_SUFFIX}"
    details = ", ".join(x for x in (f"{len(items)} articles", shopping_summary_line(lst)) if x)
    summary = f"🛒 Courses ({details})"
    get_shopping_calendar().add_event(build_event(uid, summary, start, end, shopping_description(lst)))
    try:   # relecture : l'événement doit exister avec le bon créneau avant de supprimer l'ancienne liste
        _, comp = parse_vevent(get_shopping_calendar().get_event_by_uid(uid).data)
    except Exception as e:
        raise RuntimeError(f"relecture impossible après création : {e}") from None
    c_start, c_end = event_bounds(comp)
    if not (_close(c_start, start) and _close(c_end, end)):
        raise RuntimeError("relecture : le créneau enregistré ne correspond pas")
    for obj in previous:
        obj.delete()
    return {"uid": uid, "summary": summary, "start": c_start, "end": c_end, "replaced": len(previous)}


def set_shopping_description(uid: str, description: str) -> None:
    """Met à jour la checklist (articles cochés) dans la description de l'événement courses ; relu sur le serveur."""
    obj = get_shopping_calendar().get_event_by_uid(uid)
    obj.data = apply_update(obj.data, description=description)
    obj.save()
    _, comp = parse_vevent(get_shopping_calendar().get_event_by_uid(uid).data)
    if _text(comp, "DESCRIPTION") != description:
        raise RuntimeError("relecture : la checklist n'a pas été mise à jour sur Radicale")
