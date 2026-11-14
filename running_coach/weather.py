"""Prévisions Open-Meteo (sans clé) pour chaque lieu d'entraînement, en un seul appel.

Les lieux viennent de WEATHER_LOCATIONS (ex. « Maison: 48.85, 2.35 | Travail: 48.89, 2.24 »).
La sortie est groupée par jour, pour que le coach choisisse la météo du lieu où tombe la séance. Chaque jour donne
aussi les heures où il pleut, et chaque séance planifiée sa météo sur son créneau exact : une pluie de nuit ne
compte pas pour une séance du soir.
"""
import datetime
import logging

import requests

from .config import DAYS, settings
from .i18n import _, colon

logger = logging.getLogger(__name__)

RAIN_HOUR_MM = 0.1   # une heure « pluvieuse » : au moins 0,1 mm
DAILY_FIELDS = ("weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                "precipitation_probability_max,windspeed_10m_max")

# Codes météo WMO regroupés
_WMO = [
    ((0,), _("☀️ dégagé")),
    ((1, 2), _("🌤️ peu nuageux")),
    ((3,), _("☁️ couvert")),
    ((45, 48), _("🌫️ brouillard")),
    ((51, 53, 55, 56, 57), _("🌦️ bruine")),
    ((61, 63, 65, 66, 67, 80, 81, 82), _("🌧️ pluie")),
    ((71, 73, 75, 77, 85, 86), _("🌨️ neige")),
    ((95, 96, 99), _("⛈️ orage")),
]


def describe_code(code) -> str:
    for codes, label in _WMO:
        if code in codes:
            return label
    return f"code {code}"


def fetch_daily(fields: str = DAILY_FIELDS) -> list[dict]:
    """Un bloc `daily` par lieu, dans l'ordre de WEATHER_LOCATIONS."""
    locations = settings.weather_locations
    res = requests.get(
        settings.weather_api_url,
        params={
            "latitude": ",".join(str(lat) for __, lat, __ in locations),
            "longitude": ",".join(str(lon) for __, __, lon in locations),
            "daily": fields,
            "timezone": "auto",
        },
        timeout=settings.http_timeout,
    )
    res.raise_for_status()
    data = res.json()
    items = data if isinstance(data, list) else [data]   # un seul lieu → objet, plusieurs → liste
    return [item.get("daily", {}) for item in items]


def fetch_hourly(days: int = 3) -> dict:
    """Par lieu : liste d'`weather_alerts.Hour` (heure locale aware), depuis hier 00:00 (sol mouillé)."""
    from .weather_alerts import Hour   # import local : weather_alerts ne dépend pas de weather

    locations = settings.weather_locations
    res = requests.get(
        settings.weather_api_url,
        params={
            "latitude": ",".join(str(lat) for __, lat, __ in locations),
            "longitude": ",".join(str(lon) for __, __, lon in locations),
            "hourly": "precipitation_probability,precipitation,temperature_2m,apparent_temperature,wind_gusts_10m,weathercode",
            "timezone": settings.tz.key,   # heures renvoyées dans le fuseau du coach
            "forecast_days": days,
            "past_days": 1,
        },
        timeout=settings.http_timeout,
    )
    res.raise_for_status()
    data = res.json()
    items = data if isinstance(data, list) else [data]
    out = {}
    for (name, __, __), item in zip(locations, items):
        h = item.get("hourly", {})

        def col(key, i):
            values = h.get(key) or []
            return values[i] if i < len(values) else None

        out[name] = [
            Hour(
                time=datetime.datetime.fromisoformat(t).replace(tzinfo=settings.tz),
                probability=col("precipitation_probability", i),
                mm=col("precipitation", i) or 0.0,
                temp=col("temperature_2m", i),
                feels=col("apparent_temperature", i),
                gust=col("wind_gusts_10m", i),
                code=col("weathercode", i),
            )
            for i, t in enumerate(h.get("time") or [])
        ]
    return out


def rain_spans(hours: list, day: datetime.date) -> list[tuple[int, int, float]]:
    """Heures de pluie du jour, regroupées : [(heure de début, heure de fin exclue, mm)]."""
    spans: list[list] = []
    for h in sorted((h for h in hours if h.time.date() == day), key=lambda h: h.time):
        if (h.mm or 0) < RAIN_HOUR_MM:
            continue
        if spans and spans[-1][1] == h.time.hour:
            spans[-1][1] += 1
            spans[-1][2] += h.mm
        else:
            spans.append([h.time.hour, h.time.hour + 1, h.mm])
    return [(a, b, round(mm, 1)) for a, b, mm in spans]


def format_spans(spans: list[tuple[int, int, float]]) -> str:
    return ", ".join(f"{a:02d}h–{b:02d}h ({mm:g} mm)" for a, b, mm in spans)


def _day_summary(daily: dict, i: int, hours: list | None = None) -> str:
    def col(name):
        values = daily.get(name) or []
        return values[i] if i < len(values) else None

    rain = _("pluie {mm} mm").format(mm=col('precipitation_sum'))
    if col("precipitation_probability_max") is not None:
        rain += f" ({col('precipitation_probability_max')} %)"
    if hours is not None and col("time"):
        spans = rain_spans(hours, datetime.date.fromisoformat(col("time")))
        rain += f" : {format_spans(spans)}" if spans else _(", sec heure par heure")
    return (f"{describe_code(col('weathercode'))}, {col('temperature_2m_min')}–{col('temperature_2m_max')}°C, "
            + _("{rain}, vent {wind} km/h").format(rain=rain, wind=col('windspeed_10m_max')))


def format_forecast(dailies: list[dict], names: list[str], hourly: dict | None = None) -> str:
    """Groupé par jour ; une ligne par lieu (ou une seule ligne s'il n'y a qu'un lieu).
    hourly (lieu → heures) : ajoute les heures de pluie de chaque jour."""
    days = next((d.get("time") for d in dailies if d.get("time")), None)
    if not days:
        return _("indisponible")
    lines = []
    for i, day in enumerate(days):
        d = datetime.date.fromisoformat(day)
        label = f"{DAYS[d.weekday()]} {d:%d/%m}"
        if len(dailies) == 1:
            lines.append("  " + colon(label, _day_summary(dailies[0], i, _hours(hourly, names[0]))))
        else:
            lines.append("  " + colon(label))
            lines += ["    " + colon(name, _day_summary(daily, i, _hours(hourly, name))) for name, daily in zip(names, dailies)]
    return "\n" + "\n".join(lines)


def _hours(hourly: dict | None, name: str) -> list | None:
    return None if hourly is None else hourly.get(name, [])


def fetch_forecast() -> tuple[list[dict], dict] | None:
    """(blocs `daily` par lieu, heures par lieu) sur 7 jours ; None si aucun lieu n'est configuré."""
    if not settings.weather_enabled:
        return None
    return fetch_daily(), fetch_hourly(7)


def forecast_text(data: tuple[list[dict], dict] | None, events: list[dict] | None = None,
                  now: datetime.datetime | None = None) -> str:
    """Texte pour le coach et /weather : jour par jour avec les heures de pluie, puis la météo de chaque séance
    planifiée sur son créneau exact (calculée par le code)."""
    if data is None:
        return _("non configurée")
    from .weather_alerts import session_lines
    dailies, hourly = data
    names = [name for name, __, __ in settings.weather_locations]
    text = _(" (lieux : {places})").format(places=", ".join(names)) + format_forecast(dailies, names, hourly)
    sessions = session_lines(events or [], hourly, now or datetime.datetime.now(settings.tz))
    if sessions:
        text += "\n  " + _("Sur tes séances (heure par heure, créneau exact) :") + "\n" + "\n".join(f"    {s}" for s in sessions)
    return text


def get_forecast_text(events: list[dict] | None = None) -> str:
    try:
        return forecast_text(fetch_forecast(), events)
    except Exception as e:
        logger.warning(f"Open-Meteo indisponible : {e}")
        return _("indisponible")
