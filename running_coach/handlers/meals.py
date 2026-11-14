"""Repas : /eat, choix d'un plat, questions « qu'as-tu mangé ? », suggestions et contrôle nutrition."""
import asyncio
import datetime
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .. import agent, coaching, commands, dreeve, nutrition
from ..config import TZ, settings
from ..formatting import md_escape, md_unescape
from ..memory import store
from .ui import MEAL_SLOT_WHEN, close_meal_guesses, has_recipe, meal_guesses, meal_rows, recent_eat_options, without_button
from .runs import DEBRIEF_TIMEOUT_S
from ..i18n import _

logger = logging.getLogger(__name__)


class MealsMixin:
    """Méthodes de CoachBot liées aux repas."""

    async def on_eat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/eat : quoi manger maintenant (stock, heure, entraînement), questions si besoin. Haiku, sans écriture d'agenda."""
        if not update.message or not await self._authorized(update):
            return
        if options := recent_eat_options(datetime.datetime.now(datetime.timezone.utc)):   # pas encore choisi : mêmes plats
            rows = [*meal_rows(options), [InlineKeyboardButton(_("🔄 Autre chose"), callback_data="e:n")]]
            await self._push(_("Tes options de tout à l'heure 👇"), InlineKeyboardMarkup(rows))
            return
        await self._ask_and_reply(update, commands.eat_prompt(datetime.datetime.now(TZ)), allow_writes=False)

    async def _on_eat_again(self, update: Update) -> None:
        """« Autre chose » : les plats de ce message tombent, de nouvelles idées arrivent."""
        query = update.callback_query
        await query.answer()
        close_meal_guesses(query.message.reply_markup)
        await query.edit_message_reply_markup(None)
        await self._ask_and_reply(update, commands.eat_prompt(datetime.datetime.now(TZ)) +
                                  "\nJe veux d'autres idées que celles que tu viens de me proposer.", allow_writes=False)

    async def _on_meal(self, update: Update, mid: str) -> None:
        """Plat mangé / cuisiné : ses ingrédients sortent du stock (ou passent à ce qu'il en reste)."""
        query = update.callback_query
        item = store.proposal(mid)
        if not item or item.get("kind") != "meal" or store.close_proposal(mid, "applied") is None:
            await query.answer(_("Déjà noté."))
            return
        logs = item.get("check") or item.get("recipe") or not item.get("makes")   # ce bouton note un repas mangé
        when = datetime.datetime.fromisoformat(item["at"]) if item.get("check") and item.get("at") else datetime.datetime.now(TZ)
        dup = store.similar_meal(item["text"], when) if logs else None   # déjà noté : ni journal ni stock en double
        r = store.update_pantry(remove=[] if dup else (item.get("uses") or []))
        await query.answer()
        if item.get("check"):   # réponse à « tu as mangé quoi ? » : une ligne, et la question est close
            await self._on_meal_guess(query, item, r, dup)
            return
        markup = without_button(query.message.reply_markup, f"m:{mid}")
        if item.get("recipe"):   # /eat : un seul plat choisi, les autres propositions tombent
            for row in (markup.inline_keyboard if markup else ()):
                for btn in row:
                    if (btn.callback_data or "").startswith("m:") and has_recipe(other := btn.callback_data[2:]):
                        store.close_proposal(other, "ignored")
                        markup = without_button(markup, btn.callback_data)
        await query.edit_message_reply_markup(markup)
        changes = [f"−{x}" for x in r["removed"]] + r["updated"]
        text = _("Déjà noté 👌")
        if item.get("makes") and not dup:   # boîtes pour plus tard : elles entrent au stock
            store.update_pantry(add=[{"name": item["text"], "qty": item["makes"], "keeps_days": 4, "boxes": True}])
            changes.append(f"+{item['text']} ({item['makes']})")
            text = _("Top, {boxes} de côté pour les prochains jours 🙌").format(boxes=md_escape(item['makes']))
        if (item.get("recipe") or not item.get("makes")) and not dup:   # /eat (choisi = mangé) ou plat mangé : au journal
            macros = item.get("macros") or {}
            store.log_meal(item["text"], datetime.datetime.now(TZ), source="bouton", **macros)
            text = _("Bon app' 😋") if not item.get("makes") else text
        if item.get("recipe"):   # plat choisi : la recette, simplement
            boxes = _(" (et {boxes} de côté 🙌)").format(boxes=md_escape(item['makes'])) if item.get("makes") else ""
            text = _("Bon choix 😋 *{dish}*{boxes}, voilà comment faire :").format(dish=md_escape(item['text']), boxes=boxes) \
                + f"\n\n{item['recipe']}"
        await self._push(text)
        await agent.add_to_history(self.chat_id, f"[L'athlète a confirmé avoir mangé / cuisiné : {item['text']}]", md_unescape(text))

    async def _on_meal_guess(self, query, item: dict, r: dict, dup: dict | None = None) -> None:
        store.set_meal_check(item["check"], "answered")
        if item.get("source"):   # le plat proposé à l'origine est mangé : plus un candidat pour le prochain repas
            store.close_proposal(item["source"], "applied")
        close_meal_guesses(query.message.reply_markup)
        if dup:
            await query.edit_message_reply_markup(None)
            await self._push(_("C'était déjà noté à {time} 👌").format(time=dup['time']))
            return
        macros = item.get("macros") or {}
        store.log_meal(item["text"], datetime.datetime.fromisoformat(item["at"]), source="bouton", **macros)
        box = next((u for u in item.get("uses") or [] if u.get("name", "").casefold() == item["text"].casefold()), None)
        if box and box.get("qty_left"):
            text = _("😋 Il t'en reste {left}.").format(left=md_escape(box['qty_left']))
        elif box and r["removed"]:
            text = _("😋 C'était la dernière boîte !")
        else:
            text = _("Bon app' 😋")
        await query.edit_message_reply_markup(None)
        await self._push(text)
        await agent.add_to_history(self.chat_id, f"[L'athlète a répondu avoir mangé : {item['text']}]", md_unescape(text))

    async def _on_meal_question(self, update: Update, action: str, day: str, index: int) -> None:
        """« Qu'as-tu mangé ? » : Sauté (noté comme repas vide, à la fin de sa plage) ou Passer (on n'en parle plus)."""
        query = update.callback_query
        await query.answer()
        __, end, slot = coaching.meal_window(settings.meal_check_times, index)
        key = f"{day} {slot}"
        close_meal_guesses(query.message.reply_markup)
        if action == "s":
            at = datetime.datetime.combine(datetime.date.fromisoformat(day), end, tzinfo=TZ)
            store.log_meal(f"rien ({slot} sauté)", at, source="bouton", kcal=0, protein_g=0, carbs_g=0)
            store.set_meal_check(key, "answered")
            text = _("Ça arrive 😅 Pense à bien manger au prochain repas.")
        else:
            store.set_meal_check(key, "skipped")
            text = _("Ok 👌")
        await query.edit_message_reply_markup(None)
        await self._push(text)
        await agent.add_to_history(self.chat_id, f"[Réponse à « qu'as-tu mangé ? » ({slot})]", text)

    async def check_nutrition(self) -> None:
        """Le soir (NUTRITION_CHECK_TIME) : le bilan du jour (plats, calories face aux besoins, macros), calculé par le
        code ; puis, si la journée manque de quelque chose (apports bas, repas léger ou sans trace, doublon dans le
        journal…), un mot bienveillant du coach."""
        now = datetime.datetime.now(TZ)
        today = now.date()
        key = f"{today.isoformat()} nutrition"
        try:
            if store.meal_check(key):
                return
            runs = await asyncio.to_thread(dreeve.get_all_runs) if settings.dreeve_enabled else []
            meals = store.meals_since(today - datetime.timedelta(days=1))
            store.set_meal_check(key, "checked")
            recap = commands.format_day_recap(meals, runs, store.profile, today)
            await self._push(recap)
            await agent.add_to_history(self.chat_id, "[Bilan nutrition du soir envoyé par le système]", md_unescape(recap))
            issues = nutrition.anomalies(meals, runs, store.profile, today)
            issues += [i for i in nutrition.log_issues(meals, today, now)
                       if "aujourd'hui" in i and ("deux fois" in i or "repas complets" in i)]   # doublons du journal
            if not issues:
                return
            logger.info(f"Contrôle nutrition : {issues}")
            reply = await asyncio.wait_for(agent.ask(commands.NUTRITION_CHECK_PROMPT.format(issues=" ; ".join(issues)),
                                                     chat_id=self.chat_id, allow_writes=False), DEBRIEF_TIMEOUT_S)
            await self._deliver(reply)
        except Exception:
            logger.exception("Contrôle nutrition en échec")

    async def new_dish(self) -> None:
        """Une fois par semaine (NEW_DISH_TIME) : un plat nouveau à découvrir, choisi pour casser la monotonie."""
        today = datetime.datetime.now(TZ).date()
        try:
            last = store.discoveries[-1:]
            if last and (today - datetime.date.fromisoformat(last[0]["date"])).days < 6:   # une par semaine au plus
                return
            reply = await asyncio.wait_for(agent.ask(commands.NEW_DISH_PROMPT, chat_id=self.chat_id, allow_writes=False),
                                           DEBRIEF_TIMEOUT_S)
            if reply.text or reply.meals:
                await self._deliver(reply)
        except Exception:
            logger.exception("Plat découverte en échec")

    MEAL_QUESTION_DELAY = datetime.timedelta(hours=1)   # le temps de cuisiner et manger le plat choisi

    async def check_meal(self, index: int) -> None:
        """Pas de question pendant l'heure qui suit le choix d'un plat : elle attend la fin de cette heure."""
        chosen = store.last_dish_chosen_at()
        wait = (chosen + self.MEAL_QUESTION_DELAY - datetime.datetime.now(datetime.timezone.utc)).total_seconds() if chosen else 0
        if wait > 0:
            task = asyncio.create_task(self._ask_meal_later(index, wait))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            return
        await self._ask_meal(index)

    async def _ask_meal_later(self, index: int, wait: float) -> None:
        await asyncio.sleep(wait)
        await self._ask_meal(index)

    async def _ask_meal(self, index: int) -> None:
        """Après un repas (MEAL_CHECK_TIMES) : sans aucune trace de ce repas (bouton « mangé », repas dit au coach),
        demande ce qui a été mangé. La réponse, libre, passe par le coach : journal, stock, avis nutrition."""
        now = datetime.datetime.now(TZ)
        start, end, slot = coaching.meal_window(settings.meal_check_times, index)
        key = f"{now.date().isoformat()} {slot}"
        try:
            if store.meal_check(key) or coaching.has_meal_between(store.meals_since(now.date()), now.date(), start, end):
                return
            # on part de ce qu'on sait (boîtes prêtes, plats proposés) : un geste suffit dans le cas courant
            at = datetime.datetime.combine(now.date(), end, tzinfo=TZ).isoformat()
            rows = []
            for g in meal_guesses(now):
                gid = store.add_proposal(g["text"], kind="meal", uses=g["uses"], macros=g["macros"], at=at,
                                         check=key, source=g["source"])
                rows.append([InlineKeyboardButton(f"{'🍱' if g['box'] else '🍽️'} {g['text']}"[:60], callback_data=f"m:{gid}")])
            hint = (_("Une de tes boîtes, un plat proposé ? Touche-le, ou dis-moi ce que c'était.") if rows
                    else _("Dis-le-moi en texte ou en vocal, je m'occupe du stock."))
            text = _("🍽️ *{when}, tu as mangé quoi ?*").format(when=MEAL_SLOT_WHEN[slot]) + f"\n{hint}"
            markup = InlineKeyboardMarkup(rows) if rows else None   # jamais de bouton « sauté »
            await self._push(text, markup)
            store.set_meal_check(key, "asked")
            await agent.add_to_history(self.chat_id, f"[Question automatique du système : repas ({slot}) non noté]",
                                       md_unescape(text))
        except Exception:
            logger.exception("Question repas en échec")

    async def suggest_meal(self) -> None:
        """Juste avant un repas (EAT_SUGGEST_TIMES) : des plats au choix, comme /eat, sans qu'on les demande.
        Rien si /eat vient d'être demandé, si le repas est déjà noté, ou si le coach sait qu'il se prend ailleurs."""
        now = datetime.datetime.now(TZ)
        try:
            if recent_eat_options(now):   # /eat vient d'être demandé : ses plats sont encore là
                return
            if coaching.has_meal_between(store.meals_since(now.date()), now.date(),
                                         (now - datetime.timedelta(minutes=90)).time(), now.time()):
                return
            reply = await asyncio.wait_for(agent.ask(commands.eat_prompt(now, spontaneous=True), chat_id=self.chat_id,
                                                     allow_writes=False, skip_marker=commands.NO_SUGGESTION),
                                           DEBRIEF_TIMEOUT_S)
            if reply.text or reply.meals:
                await self._deliver(reply)
        except Exception:
            logger.exception("Suggestion de repas en échec")
