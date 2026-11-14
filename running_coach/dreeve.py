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

VOLUME_WEEKS = 12          # volume hebdo détaillé (semaine en cours incluse)
DETAILED_RUNS = 150        # runs listés un par un ; au-delà, les plus anciens ne sont résumés que par mois
HISTORY_CACHE_S = 600      # l'historique complet est relu au plus toutes les 10 min (ou dès qu'un nouveau run arrive)


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
    note = " ".join(str(a.get("description") or "").split())
    note = f" « {note[:100]}{'…' if len(note) > 100 else ''} »" if note else ""
    return f"- {day} : {a.get('name', 'Sortie')}{sport}{note} — {', '.join(parts)} (id {a.get('id')})"


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


def _pace_s(a: dict) -> float | None:
    km, secs = distance_km(a), a.get("movingTimeInSeconds")
    return secs / km if km and secs else None


def _fmt_pace(p: float) -> str:
    return f"{int(p // 60)}'{int(p % 60):02d}/km"


def monthly_volume(activities: list[dict]) -> list[tuple[str, float, int, float, float | None]]:
    """(AAAA-MM, km, nb de sorties, plus longue, allure moyenne en s/km) par mois, le plus ancien d'abord."""
    months = defaultdict(list)
    for a in activities:
        if (a.get("startDateLocal") or "")[:7]:
            months[a["startDateLocal"][:7]].append(a)
    out = []
    for month in sorted(months):
        runs = months[month]
        km = sum(distance_km(a) for a in runs)
        secs = sum(a.get("movingTimeInSeconds") or 0 for a in runs if distance_km(a))
        out.append((month, km, len(runs), max(distance_km(a) for a in runs), secs / km if km and secs else None))
    return out


def records(activities: list[dict]) -> list[str]:
    lines = []
    if activities:
        lines.append(f"plus longue sortie : {format_activity(max(activities, key=distance_km))[2:]}")
    for min_km in (5, 10, 21.1):
        timed = [a for a in activities if distance_km(a) >= min_km and _pace_s(a)]
        if timed:
            best = min(timed, key=_pace_s)
            lines.append(f"meilleure allure sur {min_km:g} km ou plus : {_fmt_pace(_pace_s(best))} ({format_activity(best)[2:]})")
    return lines


def format_runs(activities: list[dict], today: datetime.date) -> str:
    """Tout l'historique : totaux et records, volume par mois et par semaine, puis chaque run (les plus récents)."""
    if not activities:
        return "Historique de course : aucun run dans Dreeve."
    first = min((a.get("startDateLocal") or "")[:10] for a in activities)
    total = sum(distance_km(a) for a in activities)
    lines = [f"HISTORIQUE COMPLET DE COURSE (Dreeve) : {len(activities)} runs depuis le {first}, {total:.0f} km au total.",
             "Records :"] + [f"- {r}" for r in records(activities)]
    lines.append("Volume par mois (tout l'historique) :")
    for month, km, n, longest, pace in monthly_volume(activities):
        lines.append(f"- {month} : {km:.1f} km en {n} sortie(s), plus longue {longest:.1f} km"
                     + (f", allure moyenne {_fmt_pace(pace)}" if pace else ""))
    lines.append(f"Volume par semaine ({VOLUME_WEEKS} dernières, semaine en cours incluse) :")
    for monday, km, n in weekly_volume(activities, today, VOLUME_WEEKS):
        lines.append(f"- semaine du {monday:%d/%m} : {km:.1f} km en {n} sortie(s)")
    shown = activities[:DETAILED_RUNS]
    older = len(activities) - len(shown)
    lines.append("Tous les runs, du plus récent au plus ancien" + (f" ({len(shown)} derniers ; les {older} plus anciens "
                 "sont dans le volume par mois, détail via les outils Dreeve)" if older else "") + " :")
    lines += [format_activity(a) for a in shown]
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


_history_cache: tuple[float, str | None, list[dict]] = (0.0, None, [])


def get_all_runs() -> list[dict]:
    """Tous les runs de Dreeve (pages de 100), du plus récent au plus ancien. Relu au plus toutes les HISTORY_CACHE_S
    secondes, ou dès que le run le plus récent change."""
    global _history_cache
    import time
    latest = get_recent_runs(1)
    latest_id = latest[0].get("id") if latest else None
    fetched_at, cached_id, runs = _history_cache
    if runs and cached_id == latest_id and time.monotonic() - fetched_at < HISTORY_CACHE_S:
        return runs
    runs, page = [], 1
    while True:
        data = _get_json("/activities", **{"filters[sportType]": settings.dreeve_run_types,
                                            "pagination[size]": 100, "pagination[page]": page})
        runs += data.get("activities") or []
        if page >= ((data.get("pagination") or {}).get("totalPages") or 1):
            break
        page += 1
    _history_cache = (time.monotonic(), latest_id, runs)
    return runs


def get_runs_text() -> str:
    return format_runs(get_all_runs(), datetime.datetime.now(TZ).date())


def get_load() -> dict:
    return _get_json("/metrics/training-load")


def get_summary(runs: list[dict] | None = None, runs_error: Exception | None = None) -> str:
    """Contexte Dreeve complet ; chaque bloc échoue indépendamment. `runs` : runs déjà lus (évite de les relire)."""
    if not settings.dreeve_enabled:
        return "Dreeve non configuré."

    def runs_text() -> str:
        if runs_error is not None:
            raise runs_error
        if runs is None:
            return get_runs_text()
        return format_runs(runs, datetime.datetime.now(TZ).date())

    parts = []
    for name, fn in (("get_load_text", get_load_text), ("get_runs_text", runs_text)):
        try:
            parts.append(fn())
        except Exception as e:
            logger.error(f"Erreur Dreeve ({name}) : {e}")
            parts.append(f"(indisponible : {e})")
    return "\n".join(parts)


def get_context() -> tuple[str, list[dict]]:
    """(texte de contexte, tous les runs) en ne lisant les runs qu'une fois."""
    if not settings.dreeve_enabled:
        return get_summary(), []
    try:
        runs = get_all_runs()
    except Exception as e:
        logger.error(f"Erreur Dreeve (get_all_runs) : {e}")
        return get_summary(runs_error=e), []
    return get_summary(runs), runs


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
