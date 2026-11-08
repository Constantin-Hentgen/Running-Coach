"""Client REST Dreeve (lecture seule) — https://docs.dreeve.app/api/

Sert au contexte pré-chargé à chaque message (charge + volume + derniers runs) et
à la détection de nouvelles séances. Le détail à la demande (tours, splits, zones
cardio, streams…) passe par les outils MCP (voir dreeve_mcp.py).
"""
import datetime
import functools
import logging
from collections import defaultdict

import requests

from .config import FR_DAYS, TZ, settings

logger = logging.getLogger(__name__)

VOLUME_WEEKS = 6
RECENT_RUNS = 10


@functools.cache
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {settings.dreeve_api_key}", "Accept": "application/json"})
    return s


def _get_json(path: str, timeout: float | None = None, **params):
    res = _session().get(f"{settings.dreeve_url}/api/v1{path}", params=params or None,
                         timeout=timeout or settings.http_timeout)
    if not res.ok:
        try:
            err = res.json()
            detail = f"{err.get('error')}: {err.get('message')}"
        except ValueError:
            detail = res.text[:200]
        raise RuntimeError(f"Dreeve {path} HTTP {res.status_code} ({detail})")
    return res.json()


# ---------------------------------------------------------------------------
# Mise en forme
# ---------------------------------------------------------------------------

def distance_km(activity: dict) -> float:
    meters = activity.get("distanceInMeter")
    if meters is None:
        meters = activity.get("distance")
    try:
        return float(meters) / 1000.0 if meters else 0.0
    except (TypeError, ValueError):
        return 0.0


def _duration(seconds) -> str:
    if not seconds:
        return "?"
    h, rem = divmod(int(seconds), 3600)
    return f"{h}h{rem // 60:02d}" if h else f"{rem // 60}min"


def _pace(activity: dict) -> str:
    km, secs = distance_km(activity), activity.get("movingTimeInSeconds")
    if not km or not secs:
        return "?"
    p = secs / km
    return f"{int(p // 60)}'{int(p % 60):02d}/km"


def format_activity(a: dict) -> str:
    day = (a.get("startDateLocal") or "")[:10]
    try:
        d = datetime.date.fromisoformat(day)
        day = f"{FR_DAYS[d.weekday()]} {d:%d/%m}"
    except ValueError:
        pass
    parts = [f"{distance_km(a):.1f} km", _duration(a.get("movingTimeInSeconds")), _pace(a)]
    if a.get("elevationInMeter"):
        parts.append(f"D+ {a['elevationInMeter']} m")
    if a.get("averageHeartRate"):
        parts.append(f"FC moy {a['averageHeartRate']}")
    sport = "" if a.get("sportType") == "Run" else f" [{a.get('sportType')}]"
    return f"- {day} : {a.get('name', 'Sortie')}{sport} — {', '.join(parts)} (id {a.get('id')})"


def format_load(d: dict) -> str:
    tsb, ac = d.get("tsb") or {}, d.get("acRatio") or {}
    return (
        f"Charge : CTL (forme) {d.get('ctl')}, ATL (fatigue) {d.get('atl')}, "
        f"TSB {tsb.get('value')} ({tsb.get('status')}), ratio A/C {ac.get('value')} ({ac.get('status')}), "
        f"TRIMP 7j {d.get('weeklyTrimp')}, monotonie {d.get('monotony')}, strain {d.get('strain')}, "
        f"jours de repos sur 7j {d.get('restDaysLast7Days')}"
    )


def weekly_volume(activities: list[dict], today: datetime.date, weeks: int) -> list[tuple[datetime.date, float, int]]:
    """(lundi, km, nb de sorties) pour les `weeks` dernières semaines lun→dim, la plus ancienne d'abord."""
    week0 = today - datetime.timedelta(days=today.weekday())
    totals = defaultdict(lambda: [0.0, 0])
    for a in activities:
        try:
            d = datetime.date.fromisoformat((a.get("startDateLocal") or "")[:10])
        except ValueError:
            continue
        monday = d - datetime.timedelta(days=d.weekday())
        totals[monday][0] += distance_km(a)
        totals[monday][1] += 1
    mondays = [week0 - datetime.timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    return [(m, *totals.get(m, (0.0, 0))) for m in mondays]


def format_runs(activities: list[dict], today: datetime.date) -> str:
    """Volume hebdo (semaines lun→dim) + derniers runs détaillés + plus longue sortie."""
    lines = [f"Volume course par semaine ({VOLUME_WEEKS} dernières, semaine en cours incluse) :"]
    for monday, km, n in weekly_volume(activities, today, VOLUME_WEEKS):
        lines.append(f"- semaine du {monday:%d/%m} : {km:.1f} km en {n} sortie(s)")

    if activities:
        lines.append(f"{min(RECENT_RUNS, len(activities))} derniers runs :")
        lines += [format_activity(a) for a in activities[:RECENT_RUNS]]
        longest = max(activities, key=distance_km)
        lines.append(f"Plus longue sortie sur la période : {format_activity(longest)[2:]}")
    else:
        lines.append(f"Aucun run sur les {VOLUME_WEEKS} dernières semaines.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Appels
# ---------------------------------------------------------------------------

def get_load_text() -> str:
    return format_load(_get_json("/metrics/training-load"))


def get_runs_since_weeks(weeks: int) -> tuple[list[dict], datetime.date]:
    """Runs depuis le lundi d'il y a `weeks - 1` semaines, et la date du jour."""
    today = datetime.datetime.now(TZ).date()
    since = today - datetime.timedelta(days=today.weekday(), weeks=weeks - 1)
    data = _get_json("/activities", **{
        "filters[from]": since.isoformat(),
        "filters[sportType]": settings.dreeve_run_types,
        "pagination[size]": 100,
    })
    return data.get("activities") or [], today


def get_runs_text() -> str:
    return format_runs(*get_runs_since_weeks(VOLUME_WEEKS))


def get_load() -> dict:
    return _get_json("/metrics/training-load")


def get_summary() -> str:
    """Contexte Dreeve complet ; chaque bloc échoue indépendamment."""
    if not settings.dreeve_enabled:
        return "Dreeve non configuré."
    parts = []
    for fn in (get_load_text, get_runs_text):
        try:
            parts.append(fn())
        except Exception as e:
            logger.error(f"Erreur Dreeve ({fn.__name__}) : {e}")
            parts.append(f"(indisponible : {e})")
    return "\n".join(parts)


def get_recent_runs(count: int = 5) -> list[dict]:
    """Derniers runs, du plus récent au plus ancien."""
    data = _get_json("/activities", timeout=5, **{"filters[sportType]": settings.dreeve_run_types, "pagination[size]": count})
    return data.get("activities") or []


def new_runs_since(runs: list[dict], previous_id) -> list[dict]:
    """Runs plus récents que `previous_id`, du plus ancien au plus récent.

    `runs` est trié du plus récent au plus ancien. Si `previous_id` n'y figure plus
    (séance supprimée, ou plus de `len(runs)` imports d'un coup), seul le dernier compte.
    """
    ids = [r.get("id") for r in runs]
    if not runs or ids[0] == previous_id:
        return []
    fresh = runs[:ids.index(previous_id)] if previous_id in ids else runs[:1]
    return list(reversed(fresh))


def ping() -> dict:
    return _get_json("/status", timeout=5)
