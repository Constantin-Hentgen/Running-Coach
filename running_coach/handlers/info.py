"""Commandes instantanées ou simples : aide, mémoire, objectifs, agenda, forme, météo, nutrition, boucles, mème."""
import asyncio
import dataclasses
import datetime
import logging
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument, InputMediaPhoto, Update
from telegram.ext import ContextTypes

from .. import agent, commands, dreeve, routes, weather
from .. import calendar_store as cs
from ..config import TZ, settings
from ..formatting import md_escape
from ..memory import store
from .ui import send_text
from ..i18n import _, decimal

logger = logging.getLogger(__name__)

ROUTE_KM_CHOICES = (5, 8, 10, 12, 15)
SURFACE_LABELS = {"route": _("🛣️ Route"), "nature": _("🌲 Nature"), "mixte": _("🔀 Mixte")}


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
            text = _("❌ Indisponible : {error}").format(error=md_escape(str(e)))
        await send_text(update.message.reply_text, text)

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command(update, lambda: commands.HELP_TEXT)

    async def on_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/memory : ce que le coach a retenu (pour corriger : « oublie le #3 », « en fait je préfère… »)."""
        await self._command(update, lambda: (
            _("*🧠 Ce que je sais de toi*") + "\n" + md_escape(store.render())
            + "\n\n" + _("_Pour corriger : « oublie le #3 » ou dis-moi simplement ce qui a changé._")))

    async def on_meme(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/meme : un GIF ou mème qui résume la journée d'entraînement, pour rire."""
        if not update.message or not await self._authorized(update):
            return
        if not settings.giphy_api_key:
            await self._push(_("Pour les mèmes, il me faut une clé GIPHY (GIPHY_API_KEY dans la config) 😅"))
            return
        typing = asyncio.create_task(self._keep_typing(update))
        try:
            reply = await agent.ask(commands.MEME_PROMPT, chat_id=str(update.effective_chat.id), allow_writes=False,
                                    gif_on_demand=True)
        except Exception:
            logger.exception("/meme en échec")
            reply = agent.AgentReply(_("J'ai pas trouvé de mème à la hauteur de ta journée 🥲 Réessaie ?"))
        finally:
            typing.cancel()
        if reply.gif:
            reply = dataclasses.replace(reply, gif_moment="ouverture")   # le mème d'abord, la légende ensuite
        await self._deliver(reply, update=update)

    async def on_goals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command(update, lambda: commands.format_goals_reply(store.goals, datetime.datetime.now(TZ).date()))

    async def on_routes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not await self._authorized(update):
            return
        ask = commands.parse_route_args(context.args or [], settings.weather_locations)
        ask.surface = ask.surface or "mixte"
        await self._route_flow(update.message, ask, never_done=False)

    async def on_new_route(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/newroute [km] [route|nature|mixte] [lieu en texte libre] : ce qui manque est demandé par boutons."""
        if not update.message or not await self._authorized(update):
            return
        await self._route_flow(update.message, commands.parse_route_args(context.args or [], settings.weather_locations),
                               never_done=True)

    def _route_asks(self) -> dict:
        return self.__dict__.setdefault("_route_asks_store", {})

    async def _route_flow(self, message, ask: "commands.RouteAsk", never_done: bool, filled: bool = False) -> None:
        """Complète la demande (agenda, puis boutons pour le revêtement et la distance), puis envoie les boucles."""
        if not settings.weather_locations:
            await self._push(_("Il me faut tes lieux (WEATHER_LOCATIONS, ex. maison et travail) pour trouver des boucles 😅"))
            return
        if not filled:
            now = datetime.datetime.now(TZ)
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            try:   # la prochaine séance, aujourd'hui ou demain
                events = (await asyncio.to_thread(cs.list_events, day, day + datetime.timedelta(days=2)))[0]
            except Exception:
                events = []
            ask = commands.fill_from_session(ask, settings.weather_locations, events, now)
        asks = self._route_asks()
        if never_done and (ask.surface is None or ask.km is None):   # /routes garde ses valeurs par défaut
            token = str(max((int(k) for k in asks), default=0) + 1)
            asks[token] = ask
            while len(asks) > 20:   # quelques demandes en attente au plus
                asks.pop(next(iter(asks)))
            if ask.surface is None:
                text, buttons = _("Tu veux courir sur quoi ? 👟"), [
                    InlineKeyboardButton(SURFACE_LABELS["route"], callback_data=f"r:s:{token}:route"),
                    InlineKeyboardButton(SURFACE_LABELS["nature"], callback_data=f"r:s:{token}:nature"),
                    InlineKeyboardButton(SURFACE_LABELS["mixte"], callback_data=f"r:s:{token}:mixte")]
            else:
                text, buttons = _("Combien de km ? 📏"), [InlineKeyboardButton(f"{k} km", callback_data=f"r:k:{token}:{k}")
                                                       for k in ROUTE_KM_CHOICES]
            await message.reply_text(text, reply_markup=InlineKeyboardMarkup([buttons]))
            return
        await self._send_routes(message, ask, never_done)

    async def _on_route_button(self, update: Update, kind: str, token: str, value: str) -> None:
        query = update.callback_query
        ask = self._route_asks().pop(token, None)
        if ask is None:
            await query.answer(_("Demande expirée, relance /newroute"))
            return
        await query.answer()
        if kind == "s" and value in routes.SURFACES:
            ask.surface = value
            label = SURFACE_LABELS[value]
        else:
            ask.km = min(max(float(value), 2.0), 30.0)
            label = f"{value} km"
        try:
            await query.edit_message_text(f"{query.message.text} → {label}")
        except Exception:
            logger.debug("Choix de boucle : message non modifié", exc_info=True)
        await self._route_flow(query.message, ask, never_done=True, filled=True)

    async def _send_routes(self, message, ask: "commands.RouteAsk", never_done: bool) -> None:
        """Boucles autour du lieu demandé : un album de cartes, puis un album de fichiers GPX."""
        typing = asyncio.create_task(self._keep_typing(SimpleNamespace(effective_chat=message.chat)))
        loops, maps = [], []
        try:
            name, start = None, None
            if ask.place:
                name, start = ask.place[0], (ask.place[1], ask.place[2])
            elif ask.query:
                home = settings.weather_locations[0]
                try:
                    hit = await asyncio.to_thread(routes.geocode, ask.query, (home[1], home[2]))
                except Exception as e:
                    logger.warning(f"Géocodage de « {ask.query} » impossible : {e}")
                    hit = None
                if hit is None:
                    await self._push(_("Je trouve pas « {place} » sur la carte 🤔 Essaie avec la ville, "
                                       "genre « bois de vincennes paris »").format(place=ask.query))
                    return
                name, start = hit[0], (hit[1], hit[2])
            else:
                home = settings.weather_locations[0]
                name, start = home[0], (home[1], home[2])
            km = ask.km if ask.km is not None else commands.DEFAULT_ROUTE_KM
            runs = await asyncio.to_thread(dreeve.get_all_runs) if never_done and settings.dreeve_enabled else []
            loops = await asyncio.to_thread(routes.find_loops, start, km, 1 if never_done else 3, runs, never_done,
                                            ask.surface or "mixte")
            maps = [await asyncio.to_thread(routes.to_map_png, lp, start) for lp in loops]
        except Exception:
            logger.exception("Boucles en échec")
            loops = []
        finally:
            typing.cancel()
        if not loops:
            await self._push(_("J'ai pas réussi à tracer de boucle là, réessaie dans un moment 🥲"))
            return
        surface = ask.surface or "mixte"
        kind = {"route": _(" sur route"), "nature": _(" nature"), "mixte": ""}[surface]
        if never_done and loops[0].novelty is not None and loops[0].novelty < routes.NEVER_DONE_MIN:
            intro = _("Pas trouvé de boucle{kind} 100 % inédite vers {place}, voilà la plus nouvelle 👇").format(kind=kind, place=name)
        else:
            intro = (_("Une boucle{kind} jamais faite depuis {place} 👀").format(kind=kind, place=name) if never_done
                     else _("{count} boucles{kind} de ~{km} km depuis {place} 👇").format(
                         count=len(loops), kind=kind, km=decimal(f"{km:g}"), place=name))
        await self._push(intro)
        photos = [InputMediaPhoto(img, caption=f"{i}. {routes.describe(lp, surface)}" + (" · © OpenStreetMap" if i == 1 else ""))
                  for i, (lp, img) in enumerate(zip(loops, maps), 1)]
        gpx = [(routes.to_gpx(lp, _("Boucle {n} · {km} km · {place}").format(n=i, km=decimal(f"{lp.km:.1f}"), place=name)),
                _("boucle-{n}-{km}km.gpx").format(n=i, km=decimal(f"{lp.km:.1f}")))
               for i, lp in enumerate(loops, 1)]
        docs = [InputMediaDocument(data, filename=filename) for data, filename in gpx]
        try:
            if len(photos) == 1:
                await message.reply_photo(maps[0], caption=photos[0].caption)
                await message.reply_document(gpx[0][0], filename=gpx[0][1])   # InputMediaDocument n'expose pas filename
            else:
                await message.reply_media_group(photos)
                await message.reply_media_group(docs)
        except Exception:
            logger.exception("Envoi des boucles en échec")
            await self._push(_("Les boucles sont prêtes mais Telegram n'a pas voulu les prendre 🥲 Réessaie ?"))

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
            events, __ = cs.list_events(today, today + datetime.timedelta(days=days))
            return commands.format_agenda(events, days)
        await self._command(update, build)

    async def on_form(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def build():
            if not settings.dreeve_enabled:
                return _("Dreeve n'est pas configuré.")
            runs, today = dreeve.get_runs_since_weeks(4)
            return commands.format_form(dreeve.get_load(), runs, today)
        await self._command(update, build)

    async def on_last_runs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def build():
            if not settings.dreeve_enabled:
                return _("Dreeve n'est pas configuré.")
            return commands.format_last_runs(dreeve.get_recent_runs(5))
        await self._command(update, build)

    async def on_weather(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def build() -> str:
            now = datetime.datetime.now(TZ)
            try:
                events = cs.list_events(now, now + datetime.timedelta(days=7))[0]
            except Exception as e:
                logger.warning(f"/weather : agenda illisible, météo des séances omise : {e}")
                events = []
            return commands.format_weather(weather.get_forecast_text(events))
        await self._command(update, build)
