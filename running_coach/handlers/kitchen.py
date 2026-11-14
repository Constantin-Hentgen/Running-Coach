"""Cuisine : liste de courses, recettes, stock et vérification du stock."""
import asyncio
import datetime
import logging

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from .. import agent, commands
from .. import calendar_store as cs
from ..config import TZ, settings
from ..formatting import md_escape, md_unescape
from ..memory import PANTRY_ASK_GRACE_DAYS, store
from .ui import MEAL_HINT, meal_rows, open_meals, pantry_check_markup, send_text, shopping_markup, shopping_text, without_button

logger = logging.getLogger(__name__)


class KitchenMixin:
    """Méthodes de CoachBot liées aux courses et au stock."""

    async def on_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/list : réaffiche la liste de courses en cours, à cocher (sans IA)."""
        if not update.message or not await self._authorized(update):
            return
        lst = store.shopping_list
        if lst is None:
            await update.message.reply_text("🛒 Pas de liste de courses en cours. Lance /shopping pour en créer une.")
            return
        await send_text(update.message.reply_text, shopping_text(lst), shopping_markup(lst))

    async def on_recipes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/recipes : recettes de la liste en cours (générées une fois, puis réaffichées sans IA) ; /recipes new : d'autres."""
        if not update.message or not await self._authorized(update):
            return
        lst = store.shopping_list
        if lst is None:
            await update.message.reply_text("🍳 Pas de liste de courses en cours : lance d'abord /shopping.")
            return
        regenerate = bool(context.args) and context.args[0].lower() in ("new", "nouvelles", "autres")
        if lst.get("recipes") and not regenerate:
            meals = open_meals(lst.get("recipe_meals") or [])
            await send_text(update.message.reply_text, lst["recipes"] + (f"\n\n{MEAL_HINT}" if meals else ""),
                            InlineKeyboardMarkup(meal_rows(meals)) if meals else None)
            return
        typing = asyncio.create_task(self._keep_typing(update))
        try:
            reply = await agent.ask(commands.recipes_prompt(lst), chat_id=str(update.effective_chat.id), allow_writes=False)
        except Exception as e:
            logger.exception("Recettes en échec")
            reply = agent.AgentReply(f"❌ Recettes impossibles : {type(e).__name__}")
        finally:
            typing.cancel()
        if not reply.text.startswith("❌"):
            store.set_recipes(lst["id"], reply.text, [mid for mid, _ in reply.meals])
            if lst.get("event_uid"):   # les recettes rejoignent la checklist dans l'événement courses
                await self._sync_shopping_event(lst["id"])
        await self._deliver(reply, update=update)

    async def on_stock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._command(update, lambda: commands.format_stock_reply(store.pantry, datetime.datetime.now(TZ).date()))

    async def on_shopping(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not await self._authorized(update):
            return
        days = commands.parse_days(context.args or [], commands.SHOPPING_DEFAULT_DAYS, commands.SHOPPING_MAX_DAYS)
        prompt = commands.shopping_prompt(days, datetime.datetime.now(TZ).date(), bool(settings.radicale_shopping_url))
        await self._ask_and_reply(update, prompt, allow_writes=False)

    async def _on_pantry_check(self, update: Update, still_there: bool, pantry_id: str) -> None:
        """Réponse à « Encore là ? » : la ligne de l'article disparaît du message."""
        query = update.callback_query
        item = store.answer_pantry_check(int(pantry_id), still_there, datetime.datetime.now(TZ).date())
        if item is None:
            await query.answer("Déjà retiré du stock.")
        else:
            await query.answer(f"{item['name']} : encore là 👍" if still_there else f"{item['name']} : retiré du stock 🗑️")
        markup = without_button(query.message.reply_markup, f"k:y:{pantry_id}")
        if markup is None:
            await query.edit_message_text("Merci 🙏")
        else:
            await query.edit_message_reply_markup(markup)

    async def _on_shopping_button(self, update: Update, parts: list[str]) -> None:
        query = update.callback_query
        if parts[1] == "n":   # titre de rayon : rien à faire
            await query.answer()
            return
        lst = (store.toggle_shopping_item(parts[2], int(parts[3])) if parts[1] == "t"
               else store.reset_shopping(parts[2]))
        if lst is None:
            await query.answer("Cette liste a été remplacée : /list affiche la liste en cours.", show_alert=True)
            return
        await query.answer()
        try:
            await query.edit_message_text(shopping_text(lst), parse_mode=ParseMode.MARKDOWN, reply_markup=shopping_markup(lst))
        except BadRequest as e:
            if "not modified" not in str(e).lower():   # « Tout décocher » sur une liste déjà vierge
                raise
        if lst.get("event_uid"):
            task = asyncio.create_task(self._sync_shopping_event(lst["id"]))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _sync_shopping_event(self, list_id: int) -> None:
        """Recopie les cases cochées dans la description de l'événement courses. Les écritures passent une par une
        et relisent l'état courant : après une série de clics rapides, l'agenda finit sur le dernier état."""
        async with self._shopping_sync_lock:
            lst = store.shopping_list
            if lst is None or lst["id"] != list_id or not lst.get("event_uid"):
                return
            try:
                await asyncio.to_thread(cs.set_shopping_description, lst["event_uid"], cs.shopping_description(lst))
            except Exception as e:
                logger.error(f"Checklist non recopiée dans l'agenda courses : {e}")

    async def check_pantry(self) -> None:
        """Chaque jour : retire ce qui est resté sans réponse, puis demande « Encore là ? » pour les produits frais
        arrivés à échéance (un bouton par article)."""
        today = datetime.datetime.now(TZ).date()
        try:
            expired = store.expire_pantry(today)
            due = store.pantry_to_check(today)[:20]
            if not due and not expired:
                return
            lines = []
            if due:
                lines.append("🥫 *Encore là ?* Ces produits frais arrivent à échéance :")
                lines += [f"- {md_escape(p['name'])}" + (f" ({md_escape(p['qty'])})" if p.get("qty") else "")
                          + f" · acheté il y a {(today - datetime.date.fromisoformat(p['added'])).days} j" for p in due]
                lines.append(f"_Sans réponse sous {PANTRY_ASK_GRACE_DAYS} j, je les retire du stock._")
            if expired:
                lines.append(f"🗑️ Retirés du stock (pas de réponse) : {md_escape(', '.join(expired))}")
            text = "\n".join(lines)
            await self._push(text, pantry_check_markup(due) if due else None)
            store.mark_pantry_asked([p["id"] for p in due], today)
            await agent.add_to_history(self.chat_id, "[Vérification automatique du stock envoyée par le système]", md_unescape(text))
        except Exception:
            logger.exception("Vérification du stock en échec")
