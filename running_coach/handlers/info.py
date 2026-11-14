"""Commandes instantanées ou simples : aide, mémoire, objectifs, agenda, forme, météo, nutrition, boucles, mème."""
import asyncio
import dataclasses
import datetime
import logging

from telegram import InputMediaDocument, InputMediaPhoto, Update
from telegram.ext import ContextTypes

from .. import agent, commands, dreeve, routes, weather
from .. import calendar_store as cs
from ..config import TZ, settings
from ..formatting import md_escape
from ..memory import store
from .ui import send_text

logger = logging.getLogger(__name__)


class InfoMixin:
    """Méthodes de CoachBot pour les commandes d'information."""

    async def _command(self, update: Update, build) -> None:
        """Commande instantanée : `build()` (synchrone, exécuté hors boucle) renvoie le texte Markdown."""
        if not update.message or not await self._authorized(update):
            return
        try:
            text = await asyncio.to_thread(build)
        except Exception as e:
            logger.exception("Commande en échec")
            text = f"❌ Indisponible : {md_escape(str(e))}"
        await send_text(update.message.reply_text, text)

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command(update, lambda: commands.HELP_TEXT)

    async def on_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/memory : ce que le coach a retenu (pour corriger : « oublie le #3 », « en fait je préfère… »)."""
        await self._command(update, lambda: (
            "*🧠 Ce que je sais de toi*\n" + md_escape(store.render())
            + "\n\n_Pour corriger : « oublie le #3 » ou dis-moi simplement ce qui a changé._"))

    async def on_meme(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/meme : un GIF ou mème qui résume la journée d'entraînement, pour rire."""
        if not update.message or not await self._authorized(update):
            return
        if not settings.giphy_api_key:
            await self._push("Pour les mèmes, il me faut une clé GIPHY (GIPHY_API_KEY dans la config) 😅")
            return
        typing = asyncio.create_task(self._keep_typing(update))
        try:
            reply = await agent.ask(commands.MEME_PROMPT, chat_id=str(update.effective_chat.id), allow_writes=False,
                                    gif_on_demand=True)
        except Exception:
            logger.exception("/meme en échec")
            reply = agent.AgentReply("J'ai pas trouvé de mème à la hauteur de ta journée 🥲 Réessaie ?")
        finally:
            typing.cancel()
        if reply.gif:
            reply = dataclasses.replace(reply, gif_moment="ouverture")   # le mème d'abord, la légende ensuite
        await self._deliver(reply, update=update)

    async def on_goals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command(update, lambda: commands.format_goals_reply(store.goals, datetime.datetime.now(TZ).date()))

    async def on_routes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send_routes(update, context.args or [], never_done=False)

    async def on_new_route(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send_routes(update, context.args or [], never_done=True)

    async def _send_routes(self, update: Update, args: list[str], never_done: bool) -> None:
        """Boucles autour du lieu de la séance : un album de cartes, puis un album de fichiers GPX."""
        if not update.message or not await self._authorized(update):
            return
        if not settings.weather_locations:
            await self._push("Il me faut tes lieux (WEATHER_LOCATIONS, ex. maison et travail) pour trouver des boucles 😅")
            return
        typing = asyncio.create_task(self._keep_typing(update))
        try:
            now = datetime.datetime.now(TZ)
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            try:
                events = (await asyncio.to_thread(cs.list_events, day, day + datetime.timedelta(days=1)))[0]
            except Exception:
                events = []
            name, start, km = commands.route_request(args, settings.weather_locations, events)
            runs = await asyncio.to_thread(dreeve.get_all_runs) if never_done and settings.dreeve_enabled else []
            loops = await asyncio.to_thread(routes.find_loops, start, km, 1 if never_done else 3, runs, never_done)
            maps = [await asyncio.to_thread(routes.to_map_png, lp, start) for lp in loops]
        except Exception:
            logger.exception("Boucles en échec")
            loops = []
        finally:
            typing.cancel()
        if not loops:
            await self._push("J'ai pas réussi à tracer de boucle là, réessaie dans un moment 🥲")
            return
        if never_done and loops[0].novelty is not None and loops[0].novelty < routes.NEVER_DONE_MIN:
            intro = f"Pas trouvé de boucle 100 % inédite vers {name}, voilà la plus nouvelle 👇"
        else:
            intro = (f"Une boucle jamais faite depuis {name} 👀" if never_done
                     else f"{len(loops)} boucles de ~{km:g} km depuis {name} 👇").replace(".", ",")
        await self._push(intro)
        photos = [InputMediaPhoto(img, caption=f"{i}. {routes.describe(lp)}" + (" · © OpenStreetMap" if i == 1 else ""))
                  for i, (lp, img) in enumerate(zip(loops, maps), 1)]
        docs = [InputMediaDocument(routes.to_gpx(lp, f"Boucle {i} · {lp.km:.1f} km · {name}"),
                                   filename=f"boucle-{i}-{lp.km:.1f}km.gpx".replace(".", ",", 1).replace(",gpx", ".gpx"))
                for i, lp in enumerate(loops, 1)]
        try:
            if len(photos) == 1:
                await update.message.reply_photo(maps[0], caption=photos[0].caption)
                await update.message.reply_document(docs[0].media, filename=docs[0].filename)
            else:
                await update.message.reply_media_group(photos)
                await update.message.reply_media_group(docs)
        except Exception:
            logger.exception("Envoi des boucles en échec")
            await self._push("Les boucles sont prêtes mais Telegram n'a pas voulu les prendre 🥲 Réessaie ?")

    async def on_nutrition(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def build() -> str:
            today = datetime.datetime.now(TZ).date()
            runs = dreeve.get_all_runs() if settings.dreeve_enabled else []
            return commands.format_nutrition(store.meals_since(today - datetime.timedelta(days=8)), runs, store.profile, today)
        await self._command(update, build)

    async def on_agenda(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        days = commands.parse_days(context.args or [])

        def build():
            today = datetime.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            events, _ = cs.list_events(today, today + datetime.timedelta(days=days))
            return commands.format_agenda(events, days)
        await self._command(update, build)

    async def on_form(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def build():
            if not settings.dreeve_enabled:
                return "Dreeve n'est pas configuré."
            runs, today = dreeve.get_runs_since_weeks(4)
            return commands.format_form(dreeve.get_load(), runs, today)
        await self._command(update, build)

    async def on_last_runs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def build():
            if not settings.dreeve_enabled:
                return "Dreeve n'est pas configuré."
            return commands.format_last_runs(dreeve.get_recent_runs(5))
        await self._command(update, build)

    async def on_weather(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command(update, lambda: commands.format_weather(weather.get_forecast_text()))
