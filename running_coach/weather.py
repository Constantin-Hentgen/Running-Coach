"""Prévisions Open-Meteo (sans clé) pour chaque lieu d'entraînement, en un seul appel.

Les lieux viennent de WEATHER_LOCATIONS (ex. « Maison: 48.85, 2.35 | Travail: 48.89, 2.24 »).
La sortie est groupée par jour, pour que le coach choisisse la météo du lieu où tombe la séance.
"""
import datetime
import logging

import requests

from .config import FR_DAYS, settings

logger = logging.getLogger(__name__)

DAILY_FIELDS = ("weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                "precipitation_probability_max,windspeed_10m_max")

# Codes météo WMO regroupés
_WMO = [
    ((0,), "☀️ dégagé"),
    ((1, 2), "🌤️ peu nuageux"),
    ((3,), "☁️ couvert"),
    ((45, 48), "🌫️ brouillard"),
    ((51, 53, 55, 56, 57), "🌦️ bruine"),
    ((61, 63, 65, 66, 67, 80, 81, 82), "🌧️ pluie"),
    ((71, 73, 75, 77, 85, 86), "🌨️ neige"),
    ((95, 96, 99), "⛈️ orage"),
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
            "latitude": ",".join(str(lat) for _, lat, _ in locations),
            "longitude": ",".join(str(lon) for _, _, lon in locations),
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
            "latitude": ",".join(str(lat) for _, lat, _ in locations),
            "longitude": ",".join(str(lon) for _, _, lon in locations),
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
    for (name, _, _), item in zip(locations, items):
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


def _day_summary(daily: dict, i: int) -> str:
    def col(name):
        values = daily.get(name) or []
        return values[i] if i < len(values) else None

    rain = f"pluie {col('precipitation_sum')} mm"
    if col("precipitation_probability_max") is not None:
        rain += f" ({col('precipitation_probability_max')} %)"
    return (f"{describe_code(col('weathercode'))}, {col('temperature_2m_min')}–{col('temperature_2m_max')}°C, "
            f"{rain}, vent {col('windspeed_10m_max')} km/h")


def format_forecast(dailies: list[dict], names: list[str]) -> str:
    """Groupé par jour ; une ligne par lieu (ou une seule ligne s'il n'y a qu'un lieu)."""
    days = next((d.get("time") for d in dailies if d.get("time")), None)
    if not days:
        return "indisponible"
    lines = []
    for i, day in enumerate(days):
        d = datetime.date.fromisoformat(day)
        label = f"{FR_DAYS[d.weekday()]} {d:%d/%m}"
        if len(dailies) == 1:
            lines.append(f"  {label} : {_day_summary(dailies[0], i)}")
        else:
            lines.append(f"  {label} :")
            lines += [f"    {name} : {_day_summary(daily, i)}" for name, daily in zip(names, dailies)]
    return "\n" + "\n".join(lines)


def get_forecast_text() -> str:
    if not settings.weather_enabled:
        return "non configurée"
    try:
        names = [name for name, _, _ in settings.weather_locations]
        return f" (lieux : {', '.join(names)})" + format_forecast(fetch_daily(), names)
    except Exception as e:
        logger.warning(f"Open-Meteo indisponible : {e}")
        return "indisponible"
