"""Outils exposés au modèle + registre (ledger) des actions réellement effectuées."""
import datetime
import logging

import requests
from dataclasses import dataclass, field

from . import calendar_store as cs
from .coaching import box_count
from .config import settings
from .formatting import md_escape
from .memory import CATEGORIES, GOAL_KINDS, GOAL_PRIORITIES, format_goal, store

logger = logging.getLogger(__name__)


@dataclass
class Action:
    kind: str       # create | update | delete | remember | forget | goal | goal_remove | proposal | meal | shopping | shopping_event
    label: str
    ok: bool
    detail: str = ""


@dataclass
class Ledger:
    actions: list[Action] = field(default_factory=list)

    def record(self, kind: str, label: str, ok: bool, detail: str = "") -> None:
        self.actions.append(Action(kind, label, ok, detail))

    @property
    def ok_count(self) -> int:
        return sum(a.ok for a in self.actions)

    def __len__(self) -> int:
        return len(self.actions)


# ---------------------------------------------------------------------------
# Implémentations : (args, ledger) -> dict {"ok": bool, ...}
# ---------------------------------------------------------------------------

def _clean_description(description):
    return None if description is None else description.replace("\\n", "\n")


def _opt_dt(value):
    return cs.parse_dt(value) if value else None


def tool_list(args: dict, ledger: Ledger) -> dict:
    start, end = cs.parse_range(args["start_date"], args["end_date"])
    events, skipped = cs.list_events(start, end)
    return {"ok": True, "count": len(events), "unreadable_skipped": skipped, "events": events}


def tool_create(args: dict, ledger: Ledger) -> dict:
    summary = args.get("summary") or "(sans titre)"
    try:
        r = cs.create_event(summary, cs.parse_dt(args["start_time"]), cs.parse_dt(args["end_time"]),
                            _clean_description(args.get("description")), args.get("location") or None)
    except Exception as e:
        ledger.record("create", summary, False, str(e))
        raise
    ledger.record("create", f"{summary} ({cs.fmt_when(r['start'], r['end'])})", True)
    return {"ok": True, "uid": r["uid"], "summary": summary, "start": r["start"].astimezone(cs.TZ).isoformat(timespec="minutes")}


def tool_update(args: dict, ledger: Ledger) -> dict:
    uid = args.get("uid", "")
    try:
        r = cs.update_event(
            uid,
            summary=args.get("new_summary") or None,
            start=_opt_dt(args.get("new_start_time")),
            end=_opt_dt(args.get("new_end_time")),
            description=_clean_description(args.get("description")),
            location=args.get("location") or None,
        )
    except Exception as e:
        ledger.record("update", cs.describe(uid), False, str(e))
        raise
    ledger.record("update", f"{r['summary']} ({cs.fmt_when(r['start'], r['end'])})", True)
    return {"ok": True, "uid": uid, "summary": r["summary"], "start": r["start"].astimezone(cs.TZ).isoformat(timespec="minutes")}


def tool_delete(args: dict, ledger: Ledger) -> dict:
    uid = args.get("uid", "")
    try:
        r = cs.delete_event(uid)
    except Exception as e:
        ledger.record("delete", cs.describe(uid), False, str(e))
        raise
    ledger.record("delete", f"{r['summary']} ({cs.fmt_when(r['start'], r['end'])})", True)
    return {"ok": True, "uid": uid, "deleted": r["summary"]}


def tool_remember(args: dict, ledger: Ledger) -> dict:
    replaces = args.get("replaces_id")
    item = store.remember(args.get("category", "autre"), args.get("text", ""),
                          source=args.get("source") or "conversation",
                          replaces=int(replaces) if replaces not in (None, "") else None,
                          priority=args.get("priority"))
    ledger.record("remember", item["text"] + (" (règle)" if item["priority"] == 1 else ""), True)
    return {"ok": True, "id": item["id"]}


def tool_forget(args: dict, ledger: Ledger) -> dict:
    item = store.forget(int(args["id"]))
    ledger.record("forget", item["text"], True)
    return {"ok": True, "forgotten": item["text"]}


def tool_set_goal(args: dict, ledger: Ledger) -> dict:
    goal_id = args.get("goal_id")
    item = store.set_goal(args.get("title", ""), kind=args.get("kind", "course"), date=args.get("date") or None,
                          target=args.get("target"), priority=args.get("priority"), notes=args.get("notes"),
                          goal_id=int(goal_id) if goal_id not in (None, "") else None)
    ledger.record("goal", format_goal(item)[2:], True)
    return {"ok": True, "goal": item}


def tool_remove_goal(args: dict, ledger: Ledger) -> dict:
    item = store.remove_goal(int(args["id"]))
    ledger.record("goal_remove", item["title"], True)
    return {"ok": True, "removed": item["title"]}


MAX_PROPOSAL_CHARS = 8000   # le détail est renvoyé tel quel à l'application : ne jamais le couper en route
MAX_SUMMARY_CHARS = 200


def tool_propose_change(args: dict, ledger: Ledger) -> dict:
    text = (args.get("change") or "").strip()
    if not text:
        raise ValueError("proposition vide")
    if len(text) > MAX_PROPOSAL_CHARS:
        raise ValueError(f"proposition trop longue ({len(text)} > {MAX_PROPOSAL_CHARS} caractères) : découpe-la en "
                         "plusieurs propositions (une par semaine par exemple)")
    summary = " ".join((args.get("summary") or "").split())
    if not summary:
        raise ValueError("summary manquant : une ligne courte et lisible pour l'athlète")
    if len(summary) > MAX_SUMMARY_CHARS:
        raise ValueError(f"summary trop long ({len(summary)} > {MAX_SUMMARY_CHARS}) : une ligne, sans UID ni détail")
    pid = store.add_proposal(text, summary=summary)
    ledger.record("proposal", summary, True, detail=str(pid))
    return {"ok": True, "proposal_id": pid,
            "note": "L'athlète voit le summary avec des boutons Appliquer / Ajuster / Ignorer sous ta réponse ; ne "
                    "recopie pas le détail dans ta réponse."}


def tool_update_stock(args: dict, ledger: Ledger) -> dict:
    r = store.update_pantry(add=args.get("add"), remove=args.get("remove"))
    changes = [f"+{x}" for x in r["added"]] + [f"−{x}" for x in r["removed"]] + [f"{x}" for x in r["updated"]]
    if changes:
        ledger.record("stock", ", ".join(changes), True)
    return {"ok": True, **r}


MAX_RECIPE_CHARS = 2000


def _uses(args: dict) -> list[dict]:
    uses = []
    for u in args.get("uses") or []:
        name = " ".join(str(u.get("name") or "").split())
        if name:
            left = " ".join(str(u.get("qty_left") or "").split())[:40]
            uses.append({"name": name, **({"qty_left": left} if left else {})})
    return uses


def tool_propose_meal(args: dict, ledger: Ledger) -> dict:
    title = " ".join((args.get("title") or "").split())[:80]
    if not title:
        raise ValueError("titre vide")
    uses = _uses(args)
    recipe = (args.get("recipe") or "").strip()[:MAX_RECIPE_CHARS] or None
    if not uses and not recipe:
        raise ValueError("aucun ingrédient du stock : pas de bouton pour ce plat")
    makes = " ".join(str(args.get("makes") or "").split())[:40] or None
    if makes and box_count(makes) > settings.meal_boxes:
        raise ValueError(f"l'athlète n'a que {settings.meal_boxes} boîtes : prévois-en {settings.meal_boxes} au plus "
                         "(ou une autre session de cuisine)")
    pid = store.add_proposal(title, kind="meal", uses=uses, makes=makes, recipe=recipe,
                             macros={k: args.get(k) for k in ("kcal", "protein_g", "carbs_g")})
    ledger.record("meal", title, True, detail=str(pid))
    note = ("Un bouton « choisir » est affiché sous ta réponse : la recette sera envoyée quand l'athlète choisira ce plat, "
            "ne la recopie pas dans ta réponse." if recipe else "Un bouton « mangé / cuisiné » est affiché sous ta réponse.")
    return {"ok": True, "meal_id": pid, "note": note + " Ne mets pas le stock à jour toi-même."}


def tool_log_meal(args: dict, ledger: Ledger) -> dict:
    at = cs.parse_dt(args["time"]) if args.get("time") else cs.now_utc()
    if at > cs.now_utc() + datetime.timedelta(minutes=5):
        raise ValueError("repas dans le futur : n'enregistre que ce qui a été mangé")
    item = store.log_meal(args.get("description", ""), at, kcal=args.get("kcal"),
                          protein_g=args.get("protein_g"), carbs_g=args.get("carbs_g"))
    ledger.record("meal_log", f"{item['time']} {item['text']}", True)
    result = {"ok": True, "logged": item}
    if uses := _uses(args):
        r = store.update_pantry(remove=uses)
        changes = [f"−{x}" for x in r["removed"]] + r["updated"]
        if changes:
            ledger.record("stock", ", ".join(changes), True)
        result["stock"] = r
    return result


def tool_edit_shopping_list(args: dict, ledger: Ledger) -> dict:
    lst, r = store.edit_shopping_list(check=args.get("check"), uncheck=args.get("uncheck"),
                                      remove=args.get("remove"), add=args.get("add"))
    changes = ([f"✅ {x}" for x in r["checked"]] + [f"☐ {x}" for x in r["unchecked"]]
               + [f"−{x}" for x in r["removed"]] + [f"+{x}" for x in r["added"]])
    ledger.record("shopping", f"{len(lst['items'])} article(s)", True, detail=str(lst["id"]))
    if changes:
        ledger.record("shopping_edit", ", ".join(changes), True)
    result = {"ok": True, **r, "items": len(lst["items"]),
              "checklist": "la checklist à jour est envoyée sous ta réponse"}
    if lst.get("event_uid") and changes:
        try:
            cs.set_shopping_description(lst["event_uid"], cs.shopping_description(lst))
        except Exception as e:
            ledger.record("shopping_event", "agenda courses", False, str(e))
            return {**result, "calendar": f"échec : {type(e).__name__}: {e}"}
        result["calendar"] = "événement de l'agenda courses mis à jour"
    return result


def tool_save_shopping_list(args: dict, ledger: Ledger) -> dict:
    """Checklist Telegram à cocher (toujours) + événement dans l'agenda courses (si configuré et heure fournie)."""
    items = []
    for it in args.get("items") or []:
        name = " ".join(str(it.get("name") or "").split())
        if not name:
            continue
        qty = " ".join(str(it.get("quantity") or "").split()) or None
        items.append({"name": name, "qty": qty, "aisle": (it.get("aisle") or "").strip() or None,
                      "keeps": it.get("keeps_days")})
    if not items:
        raise ValueError("liste vide")
    def number(key):
        try:
            return float(args[key]) if args.get(key) not in (None, "") else None
        except (TypeError, ValueError):
            return None

    # Agenda courses : créneau demandé, sinon celui de la liste en cours (liste modifiée → même événement mis à jour).
    slot = None
    if settings.radicale_shopping_url:
        previous = store.shopping_list
        if args.get("start_time"):
            start = cs.parse_dt(args["start_time"])
            slot = (start, start + datetime.timedelta(minutes=int(args.get("duration_minutes") or 30)))
        elif previous and previous.get("event_start") and previous.get("event_end"):
            slot = (datetime.datetime.fromisoformat(previous["event_start"]),
                    datetime.datetime.fromisoformat(previous["event_end"]))
            if slot[1] <= cs.now_utc():
                slot = None
        if slot is None:
            raise ValueError("start_time requis : à quelle heure l'athlète fait-il ses courses ou sa commande ? "
                             "(mémoire, sinon propose un créneau et dis-le)")

    days = number("days_covered")
    lst = store.set_shopping_list(items, days=int(days) if days else None,
                                  coverage=" ".join(str(args.get("coverage") or "").split())[:200] or None,
                                  cost_min=number("estimated_cost_min_eur"), cost_max=number("estimated_cost_max_eur"))
    ledger.record("shopping", f"{len(lst['items'])} article(s)", True, detail=str(lst["id"]))
    result = {"ok": True, "items": len(lst["items"]), "checklist": "envoyée sous ta réponse, à cocher dans Telegram"}

    if slot is not None:
        try:
            r = cs.save_shopping_event(lst, *slot)
        except Exception as e:
            ledger.record("shopping_event", "agenda courses", False, str(e))
            return {**result, "calendar": f"échec : {type(e).__name__}: {e}"}
        store.attach_shopping_event(lst["id"], r["uid"], r["start"], r["end"])
        ledger.record("shopping_event", cs.fmt_when(r["start"], r["end"]), True)
        result["calendar"] = {"start": r["start"].astimezone(cs.TZ).isoformat(timespec="minutes"),
                              "previous_list_replaced": r["replaced"]}
    return result


# ---------------------------------------------------------------------------
# Schémas (ordre et contenu figés : ils font partie du préfixe mis en cache)
# ---------------------------------------------------------------------------

# Lieu d'entraînement (stocké dans le champ LOCATION de l'événement) : sert à l'alerte pluie.
_PLACES = [name for name, _, _ in settings.weather_locations]
_LOCATION_PROP = {
    "location": {
        "type": "string",
        "enum": _PLACES,
        "description": "Lieu d'entraînement de la séance (sert aux alertes météo). Omettre pour un jour de repos.",
    }
} if _PLACES else {}

LIST_TOOL = {
    "name": "list_calendar_events",
    "description": "Relit les événements du calendrier Radicale sur une plage (dates ISO, la date de fin seule inclut toute la journée). Utile pour vérifier l'état réel après tes modifications ou pour voir au-delà des données fournies.",
    "input_schema": {
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "ex: 2026-11-09"},
            "end_date": {"type": "string", "description": "ex: 2026-11-15 (inclus)"},
        },
        "required": ["start_date", "end_date"],
    },
}

WRITE_TOOLS = [
    {
        "name": "create_calendar_event",
        "description": "Crée un événement d'ENTRAÎNEMENT (séance, repos, course à pied) dans l'agenda d'entraînement Radicale, futur uniquement. Jamais pour des courses alimentaires ou un repas. Heures sans fuseau = fuseau local de l'athlète. La description doit être multi-lignes avec émojis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start_time": {"type": "string", "description": "ISO datetime, ex: 2026-11-10T18:30:00"},
                "end_time": {"type": "string", "description": "ISO datetime"},
                "description": {"type": "string", "description": "Détails structurés multi-lignes avec émojis."},
                **_LOCATION_PROP,
            },
            "required": ["summary", "start_time", "end_time"],
        },
    },
    {
        "name": "update_calendar_event",
        "description": "Modifie un événement futur identifié par son UID (visible dans les données de l'agenda). Heures sans fuseau = fuseau local de l'athlète. Si tu changes new_start_time sans new_end_time, la durée est conservée. Un événement « journée entière » (all_day) devient horodaté si tu lui donnes une heure : fournis alors aussi new_end_time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string"},
                "new_summary": {"type": "string"},
                "new_start_time": {"type": "string", "description": "ISO datetime"},
                "new_end_time": {"type": "string", "description": "ISO datetime"},
                "description": {"type": "string"},
                **_LOCATION_PROP,
            },
            "required": ["uid"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": "Supprime UN événement futur identifié par son UID. Pour en supprimer plusieurs, appelle l'outil une fois par UID.",
        "input_schema": {
            "type": "object",
            "properties": {"uid": {"type": "string"}},
            "required": ["uid"],
        },
    },
]

MEMORY_TOOLS = [
    {
        "name": "remember",
        "description": (
            "Mémorise durablement un fait sur l'athlète, réutilisé dans toutes les conversations et planifications futures : "
            "préférence (horaires, types de séance), habitude, contrainte (agenda, travail, famille), objectif (course, chrono), "
            "santé (blessure, douleur, sommeil, poids), nutrition (régime, allergies, aliments aimés ou évités, foyer, budget), équipement. Une phrase courte, factuelle, autonome, datée si utile. "
            "N'enregistre PAS l'éphémère (une séance ponctuelle, la météo du jour). "
            "Un seul souvenir par sujet : si un souvenir existant porte sur le même sujet (le contredit, le précise ou le "
            "complète), passe son id dans replaces_id et écris la version fusionnée, au lieu d'en ajouter un second."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": CATEGORIES},
                "text": {"type": "string", "description": "≤ 300 caractères, ex: « Préfère courir le matin avant 8h en semaine »"},
                "source": {"type": "string", "enum": ["conversation", "observation"],
                           "description": "conversation = dit par l'athlète ; observation = déduit de ses données (répété et net uniquement)"},
                "priority": {"type": "integer", "enum": [1, 2, 3], "description": (
                    "1 = règle : l'athlète l'exprime comme absolu (« toujours », « jamais », « minimum », contrainte "
                    "physique ou d'agenda) ; 2 = préférence forte (« je préfère », « idéalement », par défaut) ; "
                    "3 = indication (« plutôt », « si possible », habitude observée). Si tu hésites entre 1 et 2, "
                    "demande-le à l'athlète en une question.")},
                "replaces_id": {"type": "integer", "description": "id du souvenir rendu obsolète par celui-ci"},
            },
            "required": ["category", "text"],
        },
    },
    {
        "name": "forget",
        "description": "Supprime un souvenir devenu faux ou que l'athlète demande d'oublier.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    },
]

GOAL_TOOLS = [
    {
        "name": "set_goal",
        "description": (
            "Crée ou met à jour (goal_id) un objectif STRUCTURÉ de l'athlète : course (date, distance, chrono visé), "
            "objectif santé (poids, sommeil…) ou autre. C'est la référence pour périodiser le plan (affûtage avant la course, "
            "semaines d'assimilation). Les objectifs datés vont ici, pas dans `remember`. Pour mettre à jour, renvoie TOUS les champs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "ex: « Semi-marathon de Paris », « Revenir à 72 kg »"},
                "kind": {"type": "string", "enum": GOAL_KINDS},
                "date": {"type": "string", "description": "date ISO de la course ou échéance, ex: 2027-03-07"},
                "target": {"type": "string", "description": "ex: « 1h45 », « 72 kg », « finir sans marcher »"},
                "priority": {"type": "string", "enum": GOAL_PRIORITIES,
                             "description": "A = objectif principal de la saison, B = important, C = préparation"},
                "notes": {"type": "string", "description": "≤ 300 caractères : distance, dénivelé, contexte"},
                "goal_id": {"type": "integer", "description": "id de l'objectif à remplacer (mise à jour)"},
            },
            "required": ["title", "kind"],
        },
    },
    {
        "name": "remove_goal",
        "description": "Supprime un objectif atteint, passé ou abandonné.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    },
]

PROPOSE_TOOL = {
    "name": "propose_change",
    "description": (
        "Enregistre un changement de l'agenda d'ENTRAÎNEMENT que tu PROPOSES sans le faire (débrief, bilan, alerte météo, ou "
        "quand tu demandes confirmation). Jamais pour la liste de courses ni le stock : ceux-là, fais-les directement. L'athlète la voit dans sa propre bulle avec les boutons Oui / Modifier / Non : s'il dit Oui, ce texte te sera "
        "renvoyé pour exécution, il doit donc être autonome, précis et complet (séances, dates, heures, contenus, UID). "
        "Seul `summary` est montré à l'athlète. Une proposition par appel, et regroupe les changements liés (toute une "
        "semaine, une même idée) en une seule ; au plus 2 propositions par réponse. N'en fais pas pour un simple conseil "
        "sans changement d'agenda."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "ce que voit l'athlète : une question courte en langage courant, "
                        "à laquelle on répond oui ou non, sans UID ni horaires détaillés, ex: « On décale le fractionné de mardi à mercredi soir ? »"},
            "change": {"type": "string", "description": "le détail complet pour l'exécution, ex: « Déplacer le "
                       "fractionné du mar 10/11 18:30 (UID …) au mer 11/11 18:30, même contenu »"},
        },
        "required": ["summary", "change"],
    },
}

_KEEPS_DESC = (
    "Produits périssables seulement : combien de jours il se garde une fois acheté (poisson frais 2, viande 3, pain 2, "
    "salade 4, légumes 5-7, fruits 5-7, yaourts et fromage frais 10, œufs 21). Omettre pour ce qui se garde (épicerie "
    "sèche, conserves, surgelés, condiments) : l'athlète recevra « Encore là ? » à cette échéance."
)

_SHOPPING_CALENDAR = (
    " Elle est aussi enregistrée comme UN ÉVÉNEMENT dans l'agenda COURSES dédié (séparé de l'agenda d'entraînement) au "
    "créneau start_time, avec la checklist par rayon dans sa description, tenue à jour quand l'athlète coche ; cela "
    "remplace la précédente liste à venir du coach, et ce que l'athlète a créé lui-même n'est jamais touché. Heure : "
    "celle demandée, sinon sa préférence en mémoire, sinon un créneau raisonnable que tu annonces."
    if settings.radicale_shopping_url else " (Aucun agenda courses n'est configuré : start_time est ignoré.)"
)

SHOPPING_TOOLS = [
    {
        "name": "save_shopping_list",
        "description": (
            "Enregistre la liste de courses : l'athlète la reçoit dans Telegram sous ta réponse, avec un bouton par article "
            "à cocher au magasin." + _SHOPPING_CALENDAR + " C'est le SEUL moyen d'enregistrer une liste de courses (jamais "
            "create_calendar_event) : à appeler pour une NOUVELLE liste, une fois finalisée. Pour modifier la liste en cours "
            "(déjà acheté, à retirer, à ajouter), utilise edit_shopping_list. Si la liste existe déjà dans la conversation, réutilise-la telle "
            "quelle. Ne recopie pas la liste complète dans ta réponse : la checklist l'affiche déjà."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_covered": {"type": "integer", "description": "nombre de jours que la liste couvre"},
                "coverage": {"type": "string", "description": "repas couverts, ex: « petits-déj, déjeuners en boîte, dîners, collations ; 1 personne »"},
                "estimated_cost_min_eur": {"type": "number", "description": "coût total estimé, bas de la fourchette (€, supermarché français courant)"},
                "estimated_cost_max_eur": {"type": "number", "description": "coût total estimé, haut de la fourchette (€)"},
                "start_time": {"type": "string", "description": "ISO datetime, futur, ex: 2026-11-09T15:00:00 (fuseau local)"},
                "duration_minutes": {"type": "integer", "description": "durée de l'événement, 30 par défaut"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "ex: « Bananes »"},
                            "quantity": {"type": "string", "description": "ex: « 6 », « 500 g »"},
                            "aisle": {"type": "string", "description": "rayon avec émoji, ex: « 🥦 Fruits et légumes »"},
                            "keeps_days": {"type": "integer", "description": _KEEPS_DESC},
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["items", "days_covered", "estimated_cost_min_eur", "estimated_cost_max_eur"],
        },
    },
]

EDIT_SHOPPING_TOOL = {
    "name": "edit_shopping_list",
    "description": (
        "Modifie la liste de courses EN COURS (visible dans le contexte) sans la recréer : `check` = articles achetés "
        "(cochés, ils entrent dans le stock), `uncheck`, `remove` = articles à retirer de la liste, `add` = articles à "
        "ajouter. Noms exacts de la liste. L'événement de l'agenda courses et la checklist Telegram sont mis à jour. "
        "« J'ai déjà acheté X » → check (pas remove). À faire directement, sans proposition."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "check": {"type": "array", "items": {"type": "string"}},
            "uncheck": {"type": "array", "items": {"type": "string"}},
            "remove": {"type": "array", "items": {"type": "string"}},
            "add": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "quantity": {"type": "string"}, "aisle": {"type": "string"},
                "keeps_days": {"type": "integer", "description": _KEEPS_DESC}},
                "required": ["name"]}},
        },
    },
}

STOCK_TOOL = {
    "name": "update_stock",
    "description": (
        "Met à jour le stock en cuisine de l'athlète : `add` pour ce qu'il a acheté ou a déjà (même nom = remplacé), "
        "`remove` pour ce qui est fini, jeté ou utilisé (avec qty_left s'il en reste une partie). Utilise les noms exacts "
        "du stock du contexte. À appeler dès qu'il le dit, ou quand il confirme avoir cuisiné ou mangé ce que tu proposais."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "add": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "qty": {"type": "string", "description": "ex: « 6 », « 500 g », « 1 pot »"},
                "keeps_days": {"type": "integer", "description": _KEEPS_DESC},
                "boxes": {"type": "boolean", "description": "plat cuisiné rangé en boîtes (tups) : qty = nombre de boîtes, "
                          "ex: « 2 boîtes » ; compte dans les boîtes occupées"}},
                "required": ["name"]}},
            "remove": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "qty_left": {"type": "string", "description": "ce qu'il en reste, si entamé "
                                                         "(pour un plat en boîtes : les boîtes restantes, ex: « 1 boîte »)"}},
                "required": ["name"]}},
        },
    },
}

MEAL_TOOL = {
    "name": "propose_meal",
    "description": (
        "À appeler pour CHAQUE plat ou recette que tu proposes de manger ou de cuisiner (/eat, /recipes, ou en "
        "conversation) : l'athlète aura un bouton « mangé / cuisiné » qui retirera alors ces ingrédients du stock. "
        "`uses` = les articles du stock (ou de la liste de courses en cours) que le plat consomme, noms exacts ; "
        "qty_left = ce qu'il en restera s'il n'est pas fini. Pas les basiques du placard (sel, huile, épices) sauf "
        "s'ils sont finis. Ne mets pas le stock à jour toi-même pour ce plat."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "nom court du plat, ex: « Bowl saumon riz »"},
            "recipe": {"type": "string", "description": "/eat : la recette complète, envoyée seulement quand l'athlète "
                       "choisit ce plat (bouton « choisir ») : ingrédients avec quantités (🥫 du stock / 🛒 à acheter), puis "
                       "3 à 6 étapes courtes numérotées ; format Telegram concis. À omettre pour /recipes."},
            "makes": {"type": "string", "description": "batch cooking seulement (cuisiné pour plus tard, pas mangé tout "
                      "de suite) : nombre de boîtes, ex: « 2 boîtes » (pas plus que les boîtes libres au moment de cuisiner) ; "
                      "le plat entre alors au stock"},
            "kcal": {"type": "integer", "description": "estimation pour une portion"},
            "protein_g": {"type": "integer"},
            "carbs_g": {"type": "integer"},
            "uses": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "qty_left": {"type": "string", "description": "ce qu'il en restera, ex: « 300 g » ; omettre si fini"}},
                "required": ["name"]}},
        },
        "required": ["title", "uses"],
    },
}

LOG_MEAL_TOOL = {
    "name": "log_meal",
    "description": (
        "Note dans le journal alimentaire un repas ou une collation que l'athlète DIT avoir mangé (réponse à « qu'as-tu "
        "mangé ? » ou spontanément), un par repas. `uses` retire du stock ce qu'il a consommé (noms exacts du stock, "
        "qty_left s'il en reste) : pas besoin d'update_stock en plus. Estime kcal, protéines et glucides (portions "
        "habituelles si non précisées). Ne pas utiliser pour un plat seulement proposé (propose_meal)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "ex: « Curry de poulet + riz (1 boîte), yaourt »"},
            "time": {"type": "string", "description": "heure du repas (pas de ta réponse), ISO, ex: 2026-11-11T12:30:00"},
            "kcal": {"type": "integer"},
            "protein_g": {"type": "integer"},
            "carbs_g": {"type": "integer"},
            "uses": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "qty_left": {"type": "string"}}, "required": ["name"]}},
        },
        "required": ["description"],
    },
}

GIF_TOOL = {
    "name": "send_gif",
    "description": (
        "Envoie un GIF ou un mème drôle, comme le ferait un pote, pour OUVRIR un échange (ton premier message d'une "
        "session : il part avant ton texte) ou le CLÔTURER (l'échange se termine : merci, à plus, c'est réglé… : il part "
        "après ton texte). Jamais au milieu d'une discussion. Rarement : un seul par session au plus, et seulement quand "
        "il illustre exactement ce que tu dis (une belle séance, la pluie un jour de sortie longue…) ; jamais au hasard, "
        "jamais sur un sujet sérieux (douleur, blessure, coup dur). Ne le décris pas dans ton message."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "2 à 4 mots-clés EN ANGLAIS décrivant la réaction ou la scène, "
                      "ex: « proud high five », « running in the rain », « hungry waiting ». Jamais de nom ni d'info personnelle."},
            "moment": {"type": "string", "enum": ["ouverture", "clôture"],
                       "description": "ouverture = ton premier message de la session ; clôture = l'échange se termine"},
        },
        "required": ["query", "moment"],
    },
}

GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"


def tool_send_gif(args: dict, ledger: Ledger) -> dict:
    if not store.gif_available():
        return {"ok": False, "error": "un GIF a déjà été envoyé dans cette session : pas d'autre"}
    moment = args.get("moment")
    if moment not in ("ouverture", "clôture"):
        raise ValueError("moment : « ouverture » ou « clôture »")
    if moment == "ouverture" and not store.session_opening():
        return {"ok": False, "error": "l'échange est déjà en cours : un GIF seulement pour ouvrir (premier message) ou clôturer"}
    query = " ".join(str(args.get("query") or "").split())[:50]
    if not query:
        raise ValueError("query vide")
    res = requests.get(GIPHY_SEARCH_URL, timeout=settings.http_timeout, params={
        "api_key": settings.giphy_api_key, "q": query, "limit": 5, "rating": "pg-13", "lang": "en"})
    res.raise_for_status()
    for gif in res.json().get("data") or []:
        images = gif.get("images") or {}
        url = (images.get("downsized_medium") or {}).get("url") or (images.get("original") or {}).get("url")
        if url:
            store.mark_gif(cs.now_utc())
            ledger.record("gif", f"{moment} : {query}", True, detail=url)
            return {"ok": True, "note": "Le GIF part juste après ton message. Ne le décris pas."}
    return {"ok": False, "error": f"aucun GIF trouvé pour « {query} » : continue sans"}


_COMMON = [*MEMORY_TOOLS, *GOAL_TOOLS, PROPOSE_TOOL, *SHOPPING_TOOLS, EDIT_SHOPPING_TOOL, STOCK_TOOL, MEAL_TOOL, LOG_MEAL_TOOL,
           *([GIF_TOOL] if settings.giphy_api_key else [])]
ALL_TOOLS = [LIST_TOOL, *WRITE_TOOLS, *_COMMON]
READ_ONLY_TOOLS = [LIST_TOOL, *_COMMON]

_IMPL = {
    "list_calendar_events": tool_list,
    "create_calendar_event": tool_create,
    "update_calendar_event": tool_update,
    "delete_calendar_event": tool_delete,
    "remember": tool_remember,
    "forget": tool_forget,
    "set_goal": tool_set_goal,
    "remove_goal": tool_remove_goal,
    "propose_change": tool_propose_change,
    "save_shopping_list": tool_save_shopping_list,
    "update_stock": tool_update_stock,
    "edit_shopping_list": tool_edit_shopping_list,
    "propose_meal": tool_propose_meal,
    "log_meal": tool_log_meal,
    "send_gif": tool_send_gif,
}


def run_tool(name: str, args: dict, ledger: Ledger, allowed: set[str]) -> dict:
    """Exécute un outil ; toute exception devient {"ok": False, "error": ...} pour le modèle."""
    impl = _IMPL.get(name)
    if impl is None or name not in allowed:
        return {"ok": False, "error": f"outil indisponible : {name}"}
    try:
        return impl(args or {}, ledger)
    except Exception as e:
        logger.error(f"Outil {name} en échec : {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Compte-rendu : généré par le CODE à partir de ce qui s'est réellement passé
# ---------------------------------------------------------------------------

_VERBS = {"create": "Créé", "update": "Modifié", "delete": "Supprimé"}
_FAILED_VERBS = {"create": "ajouter", "update": "modifier", "delete": "supprimer"}
_MEMORY_ICONS = {"remember": "🧠 Noté", "forget": "🗑️ Oublié", "goal": "🎯 Objectif enregistré", "goal_remove": "🏁 Objectif retiré"}


def build_report(ledger: Ledger, truncated: bool, error: str | None = None) -> str:
    """Ce que l'athlète voit du système sous le message de Bob : rien quand tout s'est bien passé (Bob le dit avec ses
    mots), une phrase simple pour chaque échec, qui n'est jamais caché."""
    lines = []
    for a in ledger.actions:
        if a.ok:
            continue
        if a.kind in _VERBS:
            lines.append(f"⚠️ J'ai pas réussi à {_FAILED_VERBS[a.kind]} « {md_escape(a.label)} » dans ton agenda. On réessaie ?")
        elif a.kind == "shopping_event":
            lines.append("⚠️ J'ai pas réussi à mettre ta liste de courses dans l'agenda courses.")
    if truncated:
        lines.append("⚠️ J'ai dû m'arrêter en route, jette un œil à ton agenda pour vérifier.")
    if error:
        lines.append("⚠️ J'ai eu un bug en route 🥲 Réessaie ?")
    return "\n".join(lines)
