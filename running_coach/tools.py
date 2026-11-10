"""Outils exposés au modèle + registre (ledger) des actions réellement effectuées."""
import datetime
import logging
from dataclasses import dataclass, field

from . import calendar_store as cs
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
                          replaces=int(replaces) if replaces not in (None, "") else None)
    ledger.record("remember", f"[{item['category']}] {item['text']}", True)
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


def tool_propose_change(args: dict, ledger: Ledger) -> dict:
    text = " ".join((args.get("change") or "").split())
    if not text:
        raise ValueError("proposition vide")
    pid = store.add_proposal(text[:1000])
    ledger.record("proposal", text, True, detail=str(pid))
    return {"ok": True, "proposal_id": pid, "note": "Des boutons Appliquer / Ajuster / Ignorer seront affichés sous ta réponse."}


def tool_update_stock(args: dict, ledger: Ledger) -> dict:
    r = store.update_pantry(add=args.get("add"), remove=args.get("remove"))
    changes = [f"+{x}" for x in r["added"]] + [f"−{x}" for x in r["removed"]] + [f"{x}" for x in r["updated"]]
    if changes:
        ledger.record("stock", ", ".join(changes), True)
    return {"ok": True, **r}


def tool_propose_meal(args: dict, ledger: Ledger) -> dict:
    title = " ".join((args.get("title") or "").split())[:80]
    if not title:
        raise ValueError("titre vide")
    uses = []
    for u in args.get("uses") or []:
        name = " ".join(str(u.get("name") or "").split())
        if name:
            left = " ".join(str(u.get("qty_left") or "").split())[:40]
            uses.append({"name": name, **({"qty_left": left} if left else {})})
    if not uses:
        raise ValueError("aucun ingrédient du stock : pas de bouton pour ce plat")
    pid = store.add_proposal(title, kind="meal", uses=uses)
    ledger.record("meal", title, True, detail=str(pid))
    return {"ok": True, "meal_id": pid,
            "note": "Un bouton « mangé / cuisiné » est affiché sous ta réponse ; ne mets pas le stock à jour toi-même."}


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
            "Si cela contredit ou précise un souvenir existant, passe son id dans replaces_id pour le remplacer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": CATEGORIES},
                "text": {"type": "string", "description": "≤ 300 caractères, ex: « Préfère courir le matin avant 8h en semaine »"},
                "source": {"type": "string", "enum": ["conversation", "observation"],
                           "description": "conversation = dit par l'athlète ; observation = déduit de ses données (répété et net uniquement)"},
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
        "quand tu demandes confirmation). Jamais pour la liste de courses ni le stock : ceux-là, fais-les directement. L'athlète verra des boutons Appliquer / Ajuster / Ignorer : s'il clique Appliquer, ce texte te sera "
        "renvoyé pour exécution, il doit donc être autonome et précis (séance, date, heure, contenu, UID si connu). "
        "Une proposition par appel ; n'en fais pas pour un simple conseil sans changement d'agenda."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"change": {"type": "string", "description": "ex: « Déplacer le fractionné du mar 10/11 18:30 (UID …) au mer 11/11 18:30, même contenu »"}},
        "required": ["change"],
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
                "keeps_days": {"type": "integer", "description": _KEEPS_DESC}},
                "required": ["name"]}},
            "remove": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "qty_left": {"type": "string", "description": "ce qu'il en reste, si entamé"}},
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
            "uses": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "qty_left": {"type": "string", "description": "ce qu'il en restera, ex: « 300 g » ; omettre si fini"}},
                "required": ["name"]}},
        },
        "required": ["title", "uses"],
    },
}

_COMMON = [*MEMORY_TOOLS, *GOAL_TOOLS, PROPOSE_TOOL, *SHOPPING_TOOLS, EDIT_SHOPPING_TOOL, STOCK_TOOL, MEAL_TOOL]
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
_MEMORY_ICONS = {"remember": "🧠 Noté", "forget": "🗑️ Oublié", "goal": "🎯 Objectif enregistré", "goal_remove": "🏁 Objectif retiré"}


def build_report(ledger: Ledger, truncated: bool, error: str | None = None) -> str:
    lines = []
    calendar = [a for a in ledger.actions if a.kind in _VERBS]
    memory = [a for a in ledger.actions if a.kind in _MEMORY_ICONS]
    if calendar:
        ok = [a for a in calendar if a.ok]
        ko = [a for a in calendar if not a.ok]
        lines.append("*📋 Compte-rendu Radicale (relu sur le serveur)*")
        lines += [f"- ✅ {_VERBS[a.kind]} : {md_escape(a.label)}" for a in ok]
        lines += [f"- ❌ ÉCHEC ({_VERBS[a.kind].lower()}) : {md_escape(a.label)} — {md_escape(a.detail)}" for a in ko]
        lines.append(f"_{len(ok)} réussie(s), {len(ko)} échec(s)_")
    if memory:
        lines += [f"{_MEMORY_ICONS[a.kind]} : {md_escape(a.label)}" for a in memory]
    for a in ledger.actions:
        if a.kind == "stock":
            lines.append(f"🥫 Stock : {md_escape(a.label)}")
        if a.kind == "shopping_edit":
            lines.append(f"✏️ Liste de courses modifiée : {md_escape(a.label)}")
        if a.kind == "shopping_event":
            lines.append(f"📅 Liste de courses ajoutée à l'agenda courses : {md_escape(a.label)}" if a.ok
                         else f"❌ Liste de courses non ajoutée à l'agenda courses : {md_escape(a.detail)}")
    if truncated:
        lines.append("⚠️ Traitement interrompu (limite atteinte) : l'opération est peut-être incomplète, vérifie l'agenda.")
    if error:
        lines.append(f"⚠️ Erreur technique en cours de route : {md_escape(error)}")
    return "\n".join(lines)
