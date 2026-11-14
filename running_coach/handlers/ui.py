"""Aides Telegram sans état : envoi en bulles, claviers, textes des boutons (propositions, repas, courses, ressenti)."""
import asyncio
import contextlib
import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

from .. import coaching
from .. import calendar_store as cs
from ..formatting import bubbles, md_escape, md_unescape, split_message
from ..memory import LEGS, store


def merge_messages(texts: list[str]) -> str:
    if len(texts) == 1:
        return texts[0]
    return ("Plusieurs messages envoyés à la suite, à traiter ensemble en une seule réponse :\n"
            + "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1)))


PAIN_FOLLOW_UP = "T'as mal où, et depuis quand ? Ça gêne à la marche ?"
BUBBLE_GAP_S = 1.0          # pause minimale avant chaque message suivant d'une réponse
BUBBLE_CHARS_PER_S = 35     # « vitesse de frappe » : un message plus long se fait attendre un peu plus
BUBBLE_MAX_GAP_S = 4.0
MAX_MESSAGES_IN_A_ROW = 4   # au-delà, c'est trop à lire : les messages les plus courts sont regroupés


def bubble_gap(chunk: str) -> float:
    return 0.0 if not BUBBLE_GAP_S else min(BUBBLE_MAX_GAP_S, BUBBLE_GAP_S + len(chunk) / BUBBLE_CHARS_PER_S)


async def send_text(send, text: str, reply_markup=None, as_bubbles: bool = False, typing=None,
                    max_bubbles: int = MAX_MESSAGES_IN_A_ROW) -> None:
    """send : coroutine(text=…, **kwargs), texte toujours nommé (send_message a chat_id lié par partial, reply_text non). Markdown d'abord, texte brut si Telegram refuse le balisage.
    Les boutons éventuels sont attachés au dernier morceau."""
    chunks = bubbles(text, max_bubbles) if as_bubbles else split_message(text)
    for i, chunk in enumerate(chunks):
        if as_bubbles and i:   # « écrit… » puis une pause à la mesure du message, comme quelqu'un qui tape
            if typing is not None:
                with contextlib.suppress(Exception):
                    await typing()
            await asyncio.sleep(bubble_gap(chunk))
        extra = {"reply_markup": reply_markup} if reply_markup is not None and i == len(chunks) - 1 else {}
        try:
            await send(text=chunk, parse_mode=ParseMode.MARKDOWN, **extra)
        except BadRequest:
            await send(text=md_unescape(chunk), **extra)


def proposal_bubble(summary: str) -> str:
    return f"💡 {md_escape(summary)}"


def proposal_markup(pid) -> InlineKeyboardMarkup:
    """Une proposition = une bulle à part, avec ses propres boutons : répondre à l'une ne touche pas aux autres."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Oui, vas-y", callback_data=f"p:a:{pid}"),
                                  InlineKeyboardButton("✏️ Modifier", callback_data=f"p:e:{pid}"),
                                  InlineKeyboardButton("❌ Non", callback_data=f"p:x:{pid}")]])


PROPOSAL_OUTCOME = {"a": "C'est parti 👌", "e": "Ok, dis-moi ce que tu veux changer 👂", "x": "Ok, on garde comme ça 👍"}


def without_proposal(markup: InlineKeyboardMarkup | None, pid: str) -> InlineKeyboardMarkup | None:
    """Clavier sans la ligne de cette proposition (anciens messages à plusieurs propositions)."""
    rows = [list(row) for row in (markup.inline_keyboard if markup else ())
            if not any((b.callback_data or "").startswith("p:") and b.callback_data.split(":")[2] == str(pid) for b in row)]
    return InlineKeyboardMarkup(rows) if rows else None


MEAL_HINT = "Touche un plat une fois mangé ou cuisiné 👇"
CHOOSE_HINT = "Ça te dit quoi ? 👇"


def has_recipe(mid) -> bool:
    """Plat proposé par /eat : choisi par bouton, sa recette est envoyée à ce moment-là."""
    return bool((item := store.proposal(mid)) and item.get("recipe"))


def meal_rows(meals: list[tuple[int, str]]) -> list[list[InlineKeyboardButton]]:
    """Un bouton par plat proposé : « choisir » (/eat, envoie la recette) ou « mangé / cuisiné » ; retire ses
    ingrédients du stock, sans IA."""
    def icon(mid) -> str:
        item = store.proposal(mid) or {}
        if item.get("mood"):   # /eat : le plat porte déjà son émoji, pas d'émoji d'envie devant
            return ""
        return "🍽️ " if has_recipe(mid) else "✅ "

    def order(meal) -> int:
        mood = (store.proposal(meal[0]) or {}).get("mood")
        return list(MOOD_ICON).index(mood) if mood in MOOD_ICON else len(MOOD_ICON)

    return [[InlineKeyboardButton(f"{icon(mid)}{title}"[:60], callback_data=f"m:{mid}")]
            for mid, title in sorted(meals, key=order)]


MOOD_ICON = {"express": "⚡", "chill": "😌", "gourmand": "🤤"}   # /eat : ordre des plats (rapide → gourmand) ; icônes non affichées
EAT_REPEAT_HOURS = 2   # /eat redemandé avant : on remontre les mêmes plats plutôt que d'en inventer d'autres


def recent_eat_options(now: datetime.datetime) -> list[tuple[int, str]]:
    """Plats /eat encore ouverts proposés il y a moins de EAT_REPEAT_HOURS heures, du plus récent lot."""
    cutoff = now - datetime.timedelta(hours=EAT_REPEAT_HOURS)
    items = [(int(pid), item) for pid, item in store.state["proposals"].items()
             if item.get("kind") == "meal" and item.get("recipe") and item["status"] == "open"
             and datetime.datetime.fromisoformat(item["created"]) > cutoff]
    return [(pid, item["text"]) for pid, item in sorted(items, key=lambda x: x[0])][-3:]


def meal_hint(meals: list[tuple[int, str]]) -> str:
    return CHOOSE_HINT if any(has_recipe(mid) for mid, _ in meals) else MEAL_HINT


MEAL_GUESS_HOURS = 36   # un plat proposé il y a plus longtemps n'est plus un candidat crédible
MAX_BOX_GUESSES = 4    # toutes les boîtes du stock d'abord (jusqu'à 4)
MAX_DISH_GUESSES = 2   # puis les derniers plats proposés
MEAL_SLOT_WHEN = {"petit-déjeuner": "Ce matin", "déjeuner": "Ce midi", "collation": "Cet après-midi", "dîner": "Ce soir"}


def meal_guesses(now: datetime.datetime) -> list[dict]:
    """Ce que l'athlète a probablement mangé : ses boîtes prêtes (la plus ancienne d'abord), puis les plats que le coach
    lui a proposés récemment (le plus récent d'abord). [{text, uses, macros, box, source}]"""
    guesses = []
    for p in sorted((p for p in store.pantry if p.get("boxes")), key=lambda p: p.get("added") or ""):
        n = coaching.box_count(p.get("qty"))
        use = {"name": p["name"], **({"qty_left": f"{n - 1} boîte{'s' if n > 2 else ''}"} if n > 1 else {})}
        guesses.append({"text": p["name"], "uses": [use], "macros": {}, "box": True, "source": None})
    cutoff = now - datetime.timedelta(hours=MEAL_GUESS_HOURS)
    recent = [(pid, item) for pid, item in store.state["proposals"].items()
              if item.get("kind") == "meal" and item["status"] == "open" and not item.get("makes")
              and not item.get("check") and datetime.datetime.fromisoformat(item["created"]) > cutoff]
    for pid, item in sorted(recent, key=lambda x: x[1]["created"], reverse=True):
        guesses.append({"text": item["text"], "uses": item.get("uses") or [], "macros": item.get("macros") or {},
                        "box": False, "source": pid})
    seen, out, counts = set(), [], {True: 0, False: 0}
    for g in guesses:
        if g["text"].casefold() in seen or counts[g["box"]] >= (MAX_BOX_GUESSES if g["box"] else MAX_DISH_GUESSES):
            continue
        seen.add(g["text"].casefold())
        counts[g["box"]] += 1
        out.append(g)
    return out


def close_meal_guesses(markup: InlineKeyboardMarkup | None) -> None:
    """La question « tu as mangé quoi ? » a sa réponse : ses autres plats candidats ne valent plus rien."""
    for row in (markup.inline_keyboard if markup else ()):
        for btn in row:
            if (btn.callback_data or "").startswith("m:"):
                store.close_proposal(btn.callback_data[2:], "ignored")


def open_meals(ids: list[int]) -> list[tuple[int, str]]:
    """Plats encore à confirmer parmi `ids` (déjà touchés ou expirés : plus de bouton)."""
    return [(mid, item["text"]) for mid in ids
            if (item := store.proposal(mid)) and item.get("kind") == "meal" and item["status"] == "open"]


def without_button(markup: InlineKeyboardMarkup | None, data: str) -> InlineKeyboardMarkup | None:
    """Le même clavier sans la ligne du bouton `data` ; None s'il ne reste rien."""
    rows = [list(row) for row in (markup.inline_keyboard if markup else ()) if all(b.callback_data != data for b in row)]
    return InlineKeyboardMarkup(rows) if rows else None


def pantry_check_markup(items: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ {p['name']}"[:40], callback_data=f"k:y:{p['id']}"),
                                  InlineKeyboardButton("🗑️ Fini", callback_data=f"k:n:{p['id']}")] for p in items])


def shopping_text(lst: dict) -> str:
    done = sum(i["done"] for i in lst["items"])
    total = len(lst["items"])
    status = "tout est dans le panier 🎉" if done == total else f"{done}/{total} ✅"
    lines = [f"🛒 *Liste de courses* — {status}"]
    if summary := cs.shopping_summary_line(lst):
        lines.append(f"📆 {md_escape(summary)}")
    if lst.get("coverage"):
        lines.append(f"🍽️ {md_escape(lst['coverage'])}")
    lines.append("_Touche un article quand il est dans le panier. Recettes : /recipes_")
    return "\n".join(lines)


def shopping_markup(lst: dict) -> InlineKeyboardMarkup:
    """Un bouton par article (☐ → ✅), précédé d'un bouton-titre par rayon ; « Tout décocher » en bas."""
    lid, rows = lst["id"], []
    for aisle, entries in cs.group_by_aisle(lst["items"]).items():
        rows.append([InlineKeyboardButton(f"— {aisle} —", callback_data="s:n")])
        rows += [[InlineKeyboardButton(f"{'✅' if item['done'] else '☐'} {cs.item_label(item)}", callback_data=f"s:t:{lid}:{i}")]
                 for i, item in entries]
    rows.append([InlineKeyboardButton("🧹 Tout décocher", callback_data=f"s:r:{lid}")])
    return InlineKeyboardMarkup(rows)


def rpe_markup(activity_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(str(n), callback_data=f"c:r:{activity_id}:{n}") for n in row]
                                 for row in ((1, 2, 3, 4, 5), (6, 7, 8, 9, 10))])


def legs_markup(activity_id) -> InlineKeyboardMarkup:
    icons = {"fraiches": "🟢", "normales": "🟡", "lourdes": "🟠", "douleur": "🔴"}
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{icons[k]} {label.capitalize()}", callback_data=f"c:l:{activity_id}:{k}")
                                  for k, label in LEGS.items()]])
