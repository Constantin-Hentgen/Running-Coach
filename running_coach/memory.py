"""Mémoire persistante du coach : ce qu'il apprend sur toi au fil des conversations.

Stockée en JSON dans DATA_DIR (volume Docker), avec l'historique de conversation et
l'id de la dernière activité vue, pour survivre aux redémarrages du conteneur.
Écriture atomique (fichier temporaire + rename).
"""
import datetime
import json
import logging
import os
import threading
from pathlib import Path

from .config import TZ, settings

logger = logging.getLogger(__name__)

CATEGORIES = ["preference", "habitude", "contrainte", "objectif", "santé", "équipement", "autre"]
MAX_ITEMS = 80
MAX_TEXT = 300



def parse_seed(raw: str | None) -> list[tuple[str, str]]:
    """COACH_SEED_MEMORY : « catégorie: texte | catégorie: texte » (catégorie facultative → « autre »)."""
    items = []
    for part in (raw or "").split("|"):
        part = part.strip()
        if not part:
            continue
        category, sep, text = part.partition(":")
        if sep and category.strip().lower() in CATEGORIES:
            items.append((category.strip().lower(), text.strip()))
        else:
            items.append(("autre", part))
    return items


class Store:
    def __init__(self, path: Path, seed: list[tuple[str, str]] | None = None):
        self.path = path
        self._seed = seed or []
        self._lock = threading.Lock()
        self.state = self._load()

    # --- persistance ----------------------------------------------------------

    def _load(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        except Exception as e:
            logger.error(f"Mémoire illisible ({self.path}) : {e} — repart de zéro (fichier conservé en .bak)")
            self.path.replace(self.path.with_suffix(".bak"))
            state = {}
        state.setdefault("memories", [])
        state.setdefault("next_id", 1)
        state.setdefault("history", {})
        state.setdefault("last_activity_id", None)
        state.setdefault("rain_alerts", {})   # clé séance → fin du créneau (ISO), pour ne prévenir qu'une fois
        if not state["memories"] and state["next_id"] == 1:
            # Connaissances de départ (COACH_SEED_MEMORY), uniquement à la création : ensuite tout se gère en conversation.
            for cat, text in self._seed:
                self._append(state, cat, text, "initial")
        return state

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            logger.error(f"Impossible d'écrire la mémoire ({self.path}) : {e}")

    @staticmethod
    def _append(state: dict, category: str, text: str, source: str) -> dict:
        item = {
            "id": state["next_id"],
            "category": category,
            "text": text,
            "source": source,
            "date": datetime.datetime.now(TZ).date().isoformat(),
        }
        state["next_id"] += 1
        state["memories"].append(item)
        return item

    # --- mémoire -------------------------------------------------------------

    @property
    def memories(self) -> list[dict]:
        return list(self.state["memories"])

    def remember(self, category: str, text: str, source: str = "conversation", replaces: int | None = None) -> dict:
        text = " ".join((text or "").split())
        if not text:
            raise ValueError("texte vide")
        if len(text) > MAX_TEXT:
            raise ValueError(f"texte trop long ({len(text)} > {MAX_TEXT} caractères) : résume-le")
        if category not in CATEGORIES:
            category = "autre"
        with self._lock:
            if replaces is not None:
                self._remove(replaces)
            if any(m["text"].casefold() == text.casefold() for m in self.state["memories"]):
                raise ValueError("déjà mémorisé")
            if len(self.state["memories"]) >= MAX_ITEMS:
                raise ValueError(f"mémoire pleine ({MAX_ITEMS}) : oublie ou fusionne des éléments obsolètes d'abord")
            item = self._append(self.state, category, text, source)
            self._save()
        return item

    def _remove(self, memory_id: int) -> dict:
        for i, m in enumerate(self.state["memories"]):
            if m["id"] == memory_id:
                return self.state["memories"].pop(i)
        raise LookupError(f"aucun souvenir #{memory_id}")

    def forget(self, memory_id: int) -> dict:
        with self._lock:
            item = self._remove(int(memory_id))
            self._save()
        return item

    def render(self) -> str:
        """Bloc texte stable (trié par catégorie puis id) pour le prompt système."""
        if not self.state["memories"]:
            return "(rien pour l'instant)"
        order = {c: i for i, c in enumerate(CATEGORIES)}
        items = sorted(self.state["memories"], key=lambda m: (order.get(m["category"], 99), m["id"]))
        return "\n".join(f"- #{m['id']} [{m['category']}] {m['text']} (depuis {m['date']})" for m in items)

    # --- historique & état ------------------------------------------------------

    def history(self, chat_id: str) -> list[dict]:
        return list(self.state["history"].get(chat_id, []))

    def set_history(self, chat_id: str, messages: list[dict]) -> None:
        with self._lock:
            self.state["history"][chat_id] = messages
            self._save()

    def was_alerted(self, key: str) -> bool:
        return key in self.state["rain_alerts"]

    def mark_alerted(self, key: str, until: datetime.datetime) -> None:
        with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc)
            alerts = {k: v for k, v in self.state["rain_alerts"].items()
                      if datetime.datetime.fromisoformat(v) > now}   # purge des séances passées
            alerts[key] = until.astimezone(datetime.timezone.utc).isoformat()
            self.state["rain_alerts"] = alerts
            self._save()

    @property
    def last_activity_id(self):
        return self.state["last_activity_id"]

    @last_activity_id.setter
    def last_activity_id(self, value) -> None:
        with self._lock:
            self.state["last_activity_id"] = value
            self._save()


store = Store(Path(settings.data_dir) / "coach_state.json", seed=parse_seed(settings.seed_memory))
