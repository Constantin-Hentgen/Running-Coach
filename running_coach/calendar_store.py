"""Radicale / CalDAV : lecture et écritures vérifiées.

- tout est identifié par UID (pas de recherche floue par titre) ;
- chaque écriture est vérifiée par RELECTURE sur le serveur ;
- le parsing/édition se fait avec `icalendar` sur le texte brut (`ev.data`).
  On n'utilise plus vobject : il ne sait pas sérialiser `datetime.timezone.utc`
  ("Unable to guess TZID for tzinfo UTC"), ce qui cassait toute modification.
"""
import datetime
import functools
import logging
import uuid

import caldav
from caldav.lib.error import NotFoundError
from icalendar import Calendar, Event, Todo

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

@functools.cache
def get_calendar():
    """Client CalDAV réutilisé (connexion HTTP keep-alive) au lieu d'un nouveau par appel."""
    client = caldav.DAVClient(url=settings.radicale_url, username=settings.radicale_user, password=settings.radicale_pass)
    return client.calendar(url=settings.radicale_url)


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
# Liste de courses : tâches (VTODO) dans une liste dédiée (RADICALE_TASKS_URL)
# ---------------------------------------------------------------------------

SHOPPING_UID_SUFFIX = "@running-coach-shopping"


@functools.cache
def get_tasks_list():
    client = caldav.DAVClient(url=settings.radicale_tasks_url, username=settings.radicale_user,
                              password=settings.radicale_pass)
    return client.calendar(url=settings.radicale_tasks_url)


def build_todo(uid: str, summary: str, category: str | None = None) -> str:
    cal = Calendar()
    cal.add("prodid", "-//running-coach-agent//FR")
    cal.add("version", "2.0")
    todo = Todo()
    todo.add("uid", uid)
    todo.add("dtstamp", now_utc())
    todo.add("summary", summary)
    todo.add("status", "NEEDS-ACTION")
    if category:
        todo.add("categories", [category])
    cal.add_component(todo)
    return cal.to_ical().decode("utf-8")


def _coach_todos() -> list:
    found = []
    for obj in get_tasks_list().todos(include_completed=True):
        try:
            uid = next(str(c.get("UID", "")) for c in Calendar.from_ical(obj.data).walk("VTODO"))
        except Exception:
            continue
        if uid.endswith(SHOPPING_UID_SUFFIX):
            found.append(obj)
    return found


def replace_shopping_list(items: list[tuple[str, str | None]]) -> dict:
    """Remplace la liste précédente du coach par `items` [(libellé, rayon)] ; relu sur le serveur.

    Seules les tâches créées par le coach (UID …@running-coach-shopping) sont supprimées :
    ce que tu as ajouté toi-même à la liste n'est jamais touché.
    """
    if not items:
        raise ValueError("liste vide")
    removed = 0
    for obj in _coach_todos():
        obj.delete()
        removed += 1
    tasks = get_tasks_list()
    for label, aisle in items:
        tasks.add_todo(build_todo(f"{uuid.uuid4()}{SHOPPING_UID_SUFFIX}", label, aisle))
    saved = len(_coach_todos())
    if saved != len(items):
        raise RuntimeError(f"relecture : {saved} article(s) sur le serveur au lieu de {len(items)}")
    return {"saved": saved, "removed": removed}
