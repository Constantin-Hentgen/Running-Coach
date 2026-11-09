"""Outils exposés au modèle + registre (ledger) des actions réellement effectuées."""
import logging
from dataclasses import dataclass, field

from . import calendar_store as cs
from .config import settings
from .formatting import md_escape
from .memory import CATEGORIES, store

logger = logging.getLogger(__name__)


@dataclass
class Action:
    kind: str       # "create" | "update" | "delete" | "remember" | "forget"
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
        "description": "Crée un événement (séance ou repos) dans Radicale, futur uniquement. Heures sans fuseau = fuseau local de l'athlète. La description doit être multi-lignes avec émojis.",
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

ALL_TOOLS = [LIST_TOOL, *WRITE_TOOLS, *MEMORY_TOOLS]
READ_ONLY_TOOLS = [LIST_TOOL, *MEMORY_TOOLS]

_IMPL = {
    "list_calendar_events": tool_list,
    "create_calendar_event": tool_create,
    "update_calendar_event": tool_update,
    "delete_calendar_event": tool_delete,
    "remember": tool_remember,
    "forget": tool_forget,
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
_MEMORY_ICONS = {"remember": "🧠 Noté", "forget": "🗑️ Oublié"}


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
    if truncated:
        lines.append("⚠️ Traitement interrompu (limite atteinte) : l'opération est peut-être incomplète, vérifie l'agenda.")
    if error:
        lines.append(f"⚠️ Erreur technique en cours de route : {md_escape(error)}")
    return "\n".join(lines)
