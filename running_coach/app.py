"""Point d'entrée FastAPI : démarre le bot Telegram (polling) et le planificateur."""
import asyncio
import contextlib
import datetime
import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from . import calendar_store as cs
from . import dreeve, dreeve_mcp, weather
from .bot import build_bot
from .commands import BOT_COMMANDS
from .config import UTC, settings

logging.basicConfig(level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # sinon une ligne par long-polling Telegram
logger = logging.getLogger("running_coach")


def verify_services_connectivity() -> None:
    logger.info("=== TEST DE CONNECTIVITÉ DES SERVICES ===")
    settings.report()
    if settings.dreeve_enabled:
        try:
            logger.info(f"[DREEVE] API REST OK : {dreeve.ping()}")
        except Exception as e:
            logger.error(f"[DREEVE] Erreur : {e}")

    if settings.weather_enabled:
        try:
            weather.fetch_daily("temperature_2m_max")
            logger.info("[METEO] Open-Meteo OK.")
        except Exception as e:
            logger.error(f"[METEO] Erreur Open-Meteo : {e}")

    if settings.radicale_url:
        try:
            now = datetime.datetime.now(UTC)
            events, skipped = cs.list_events(now, now + datetime.timedelta(days=7))
            logger.info(f"[RADICALE] CalDAV OK : {len(events)} événement(s) sur 7 jours ({skipped} illisible(s)).")
        except Exception as e:
            logger.error(f"[RADICALE] Échec de connexion CalDAV : {e}")
    logger.info("=== FIN DU TEST DE CONNECTIVITÉ ===")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(verify_services_connectivity)
    if dreeve_mcp.enabled():
        await dreeve_mcp.load_tools()   # journalise le catalogue ou l'erreur de connexion

    tg_app, scheduler = None, None
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN non défini : bot désactivé.")
    else:
        bot = build_bot()
        tg_app = bot.app
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()
        try:
            await tg_app.bot.set_my_commands(BOT_COMMANDS)   # menu « / » dans Telegram
        except Exception as e:
            logger.warning(f"Menu des commandes Telegram non publié : {e}")

        if settings.telegram_chat_id:
            scheduler = AsyncIOScheduler(timezone=settings.tz)
            if settings.briefing_time:
                hour, minute = settings.briefing_time
                scheduler.add_job(bot.morning_briefing, "cron", hour=hour, minute=minute,
                                  misfire_grace_time=3600, coalesce=True)
            if settings.weather_enabled and settings.radicale_url and settings.rain_check_minutes > 0:
                scheduler.add_job(bot.check_rain, "interval", minutes=settings.rain_check_minutes, coalesce=True,
                                  next_run_time=datetime.datetime.now(settings.tz) + datetime.timedelta(minutes=1))
            if settings.dreeve_enabled and settings.activity_poll_minutes > 0:
                scheduler.add_job(bot.check_new_activity, "interval", minutes=settings.activity_poll_minutes, coalesce=True,
                                  next_run_time=datetime.datetime.now(settings.tz) + datetime.timedelta(minutes=1))
            scheduler.start()
        else:
            logger.warning("TELEGRAM_CHAT_ID non défini : mode configuration. Envoie un message au bot, "
                           "il te répondra avec l'id à mettre dans .env.")

    app.state.telegram_running = tg_app is not None
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        if tg_app:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health_check():
    return {"status": "running", "telegram": getattr(app.state, "telegram_running", False)}

