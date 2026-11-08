"""Configuration : TOUT vient de l'environnement (.env). Aucune valeur personnelle dans le code.

Voir `.env.example` pour la liste commentée des variables.
"""
import datetime
import logging
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

UTC = datetime.timezone.utc
logger = logging.getLogger(__name__)


def _str(name: str, default: str | None = None) -> str | None:
    """Valeur nettoyée ; vide = non définie. Retire des guillemets englobants (l'UI Portainer les conserve)."""
    value = os.getenv(name, "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value or default


def _int(name: str, default: int) -> int:
    value = _str(name)
    try:
        return int(value) if value is not None else default
    except ValueError:
        raise ValueError(f"{name} doit être un entier (reçu : {value!r})") from None


def _float(name: str) -> float | None:
    value = _str(name)
    try:
        return float(value) if value is not None else None
    except ValueError:
        raise ValueError(f"{name} doit être un nombre (reçu : {value!r})") from None


def _instance_root(url: str | None) -> str | None:
    # Accepte la racine de l'instance ou une URL d'API / MCP complète : on ne garde que la racine.
    if not url:
        return None
    return url.split("/mcp")[0].split("/api")[0].rstrip("/")


def parse_locations(raw: str | None) -> list[tuple[str, float, float]]:
    """WEATHER_LOCATIONS : « Nom: lat, lon | Nom: lat, lon » (ex. « Maison: 48.85, 2.35 | Travail: 48.89, 2.24 »)."""
    locations = []
    for part in (raw or "").split("|"):
        part = part.strip()
        if not part:
            continue
        name, sep, coords = part.rpartition(":")
        try:
            lat, lon = (float(x) for x in coords.split(","))
        except ValueError:
            raise ValueError(f"WEATHER_LOCATIONS : « {part} » doit être au format « Nom: latitude, longitude »") from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"WEATHER_LOCATIONS : coordonnées hors limites pour « {part} »")
        locations.append((name.strip() or f"Lieu {len(locations) + 1}", lat, lon))
    return locations


def _briefing_time(value: str | None) -> tuple[int, int] | None:
    if not value or value.lower() in ("off", "non", "no", "none", "0"):
        return None
    try:
        hour, minute = (int(x) for x in value.split(":"))
        datetime.time(hour, minute)
        return hour, minute
    except ValueError:
        raise ValueError(f"BRIEFING_TIME doit être au format HH:MM ou « off » (reçu : {value!r})") from None


def _weather_locations() -> list[tuple[str, float, float]]:
    locations = parse_locations(_str("WEATHER_LOCATIONS"))
    if not locations:   # ancien format, un seul lieu
        lat, lon = _float("WEATHER_LAT"), _float("WEATHER_LON")
        if lat is not None and lon is not None:
            locations = [("Domicile", lat, lon)]
    return locations


@dataclass(frozen=True)
class Settings:
    # Anthropic
    anthropic_api_key: str | None
    model: str
    max_output_tokens: int
    max_tool_rounds: int
    # Telegram
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    # Radicale (CalDAV)
    radicale_url: str | None
    radicale_user: str | None
    radicale_pass: str | None
    # Dreeve
    dreeve_url: str | None
    dreeve_api_key: str | None
    dreeve_mcp_key: str | None
    dreeve_mcp_url: str | None
    dreeve_run_types: str
    # Météo
    weather_locations: list[tuple[str, float, float]]
    weather_api_url: str
    # Alerte pluie
    rain_check_minutes: int
    rain_hours_ahead: int
    rain_probability: int
    rain_min_mm: float
    rain_skip_keywords: list[str]
    # Coach
    tz: ZoneInfo
    briefing_time: tuple[int, int] | None
    activity_poll_minutes: int
    planning_horizon_days: int
    context_days_ahead: int
    history_messages: int
    seed_memory: str | None
    data_dir: str
    http_timeout: int

    @classmethod
    def from_env(cls) -> "Settings":
        # DREEVE_URL est la variable recommandée ; DREEVE_API_URL reste acceptée (ancien nom).
        dreeve_url = _instance_root(_str("DREEVE_URL") or _str("DREEVE_API_URL"))
        return cls(
            anthropic_api_key=_str("ANTHROPIC_API_KEY"),
            model=_str("CLAUDE_MODEL", "claude-haiku-5-5"),
            max_output_tokens=_int("CLAUDE_MAX_TOKENS", 16000),
            max_tool_rounds=_int("CLAUDE_MAX_TOOL_ROUNDS", 25),
            telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
            radicale_url=_str("RADICALE_URL"),
            radicale_user=_str("RADICALE_USER"),
            radicale_pass=_str("RADICALE_PASS"),
            dreeve_url=dreeve_url,
            dreeve_api_key=_str("DREEVE_API_KEY"),
            dreeve_mcp_key=_str("DREEVE_MCP_KEY"),
            dreeve_mcp_url=_str("DREEVE_MCP_URL") or (f"{dreeve_url}/mcp" if dreeve_url else None),
            dreeve_run_types=_str("DREEVE_RUN_TYPES", "Run,TrailRun,VirtualRun"),
            weather_locations=_weather_locations(),
            weather_api_url=_str("WEATHER_API_URL", "https://api.open-meteo.com/v1/forecast"),
            rain_check_minutes=_int("RAIN_ALERT_CHECK_MINUTES", 60),
            rain_hours_ahead=_int("RAIN_ALERT_HOURS_AHEAD", 48),
            rain_probability=_int("RAIN_ALERT_PROBABILITY", 60),
            rain_min_mm=_float("RAIN_ALERT_MIN_MM") if _str("RAIN_ALERT_MIN_MM") else 0.3,
            rain_skip_keywords=[k.strip().lower() for k in _str("RAIN_ALERT_SKIP_KEYWORDS", "repos,rest,off").split(",")
                                if k.strip()],
            tz=ZoneInfo(_str("COACH_TZ") or _str("TZ") or "Europe/Paris"),
            briefing_time=_briefing_time(_str("BRIEFING_TIME", "08:00")),
            activity_poll_minutes=_int("ACTIVITY_POLL_MINUTES", 1),
            planning_horizon_days=_int("PLANNING_HORIZON_DAYS", 30),
            context_days_ahead=_int("CALENDAR_CONTEXT_DAYS", 45),
            history_messages=_int("HISTORY_MESSAGES", 6),
            seed_memory=_str("COACH_SEED_MEMORY"),
            data_dir=_str("COACH_DATA_DIR", "data"),
            http_timeout=_int("HTTP_TIMEOUT", 8),
        )

    @property
    def dreeve_enabled(self) -> bool:
        return bool(self.dreeve_url and self.dreeve_api_key)

    @property
    def weather_enabled(self) -> bool:
        return bool(self.weather_locations)

    def report(self) -> None:
        """Journalise au démarrage ce qui est configuré (sans jamais afficher de secret)."""
        required = {
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "RADICALE_URL": self.radicale_url,
        }
        for name, value in required.items():
            if not value:
                logger.error(f"[CONFIG] {name} manquante : le coach ne peut pas fonctionner (voir .env.example).")
        optional = {
            "TELEGRAM_CHAT_ID (sécurité + briefing + débriefs)": self.telegram_chat_id,
            "Dreeve REST (DREEVE_URL + DREEVE_API_KEY)": self.dreeve_enabled,
            "Dreeve MCP (DREEVE_MCP_KEY)": self.dreeve_mcp_key and self.dreeve_mcp_url,
            f"Météo ({', '.join(n for n, _, _ in self.weather_locations) or 'WEATHER_LOCATIONS'})": self.weather_enabled,
        }
        for name, ok in optional.items():
            logger.info(f"[CONFIG] {name} : {'activé' if ok else 'désactivé'}")
        logger.info(f"[CONFIG] modèle={self.model}, fuseau={self.tz.key}, briefing={self.briefing_time}, "
                    f"détection runs={self.activity_poll_minutes} min")


settings = Settings.from_env()
TZ = settings.tz
TZ_LABEL = TZ.key   # ex. « Europe/Paris », affiché au modèle

FR_DAYS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
FR_DAYS_LONG = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
