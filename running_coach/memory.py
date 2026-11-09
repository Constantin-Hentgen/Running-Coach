"""Mémoire persistante du coach : ce qu'il apprend sur toi au fil des conversations.

Stockée en JSON dans DATA_DIR (volume Docker) avec le reste de l'état : historique de conversation,
objectifs structurés, propositions en attente (boutons), ressentis après séance, alertes météo
déjà envoyées et id de la dernière activité vue, pour survivre aux redémarrages du conteneur.
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

CATEGORIES = ["preference", "habitude", "contrainte", "objectif", "santé", "nutrition", "équipement", "autre"]
MAX_ITEMS = 80
MAX_TEXT = 300
GOAL_KINDS = ["course", "santé", "autre"]
GOAL_PRIORITIES = ["A", "B", "C"]
MAX_GOALS = 15
MAX_CHECKINS = 60
PROPOSAL_TTL_DAYS = 14
MAX_SHOPPING_ITEMS = 60   # boutons Telegram : 100 max par message, rayons compris
LEGS = {"fraiches": "fraîches", "normales": "normales", "lourdes": "lourdes", "douleur": "douleur"}



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
        state.setdefault("goals", [])
        state.setdefault("next_goal_id", 1)
        state.setdefault("proposals", {})     # id → {text, kind, created, status}
        state.setdefault("next_proposal_id", 1)
        state.setdefault("checkins", {})      # id d'activité → {date, minutes, name, rpe, legs}
        state.setdefault("shopping", None)    # liste de courses en cours : {id, items, event_uid, created}
        state.setdefault("next_shopping_id", 1)
        # clé séance → {until, hazards, cleared} ; migre l'ancien format « rain_alerts » (clé → fin du créneau)
        alerts = state.setdefault("weather_alerts", {})
        for key, until in state.pop("rain_alerts", {}).items():
            alerts.setdefault(key, {"until": until, "hazards": ["pluie"], "cleared": False})
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

    # --- alertes météo ---------------------------------------------------------

    def alert_state(self, key: str) -> dict | None:
        """Ce qui a déjà été signalé pour une séance : {until, hazards, cleared}, ou None."""
        return self.state["weather_alerts"].get(key)

    def save_alert_state(self, key: str, until: datetime.datetime, hazards: list[str], cleared: bool = False) -> None:
        with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc)
            alerts = {k: v for k, v in self.state["weather_alerts"].items()
                      if datetime.datetime.fromisoformat(v["until"]) > now}   # purge des séances passées
            alerts[key] = {"until": until.astimezone(datetime.timezone.utc).isoformat(),
                           "hazards": sorted(hazards), "cleared": cleared}
            self.state["weather_alerts"] = alerts
            self._save()

    # --- objectifs structurés -------------------------------------------------------

    @property
    def goals(self) -> list[dict]:
        return list(self.state["goals"])

    def set_goal(self, title: str, kind: str = "course", date: str | None = None, target: str | None = None,
                 priority: str | None = None, notes: str | None = None, goal_id: int | None = None) -> dict:
        """Crée un objectif, ou le remplace entièrement si `goal_id` est donné."""
        title = " ".join((title or "").split())
        if not title:
            raise ValueError("titre vide")
        if date:
            datetime.date.fromisoformat(date)   # ValueError si invalide
        item = {
            "title": title[:120],
            "kind": kind if kind in GOAL_KINDS else "autre",
            "date": date or None,
            "target": (target or "").strip()[:120] or None,
            "priority": priority if priority in GOAL_PRIORITIES else None,
            "notes": " ".join((notes or "").split())[:MAX_TEXT] or None,
        }
        with self._lock:
            if goal_id is not None:
                for i, g in enumerate(self.state["goals"]):
                    if g["id"] == int(goal_id):
                        item["id"] = g["id"]
                        self.state["goals"][i] = item
                        break
                else:
                    raise LookupError(f"aucun objectif #{goal_id}")
            else:
                if len(self.state["goals"]) >= MAX_GOALS:
                    raise ValueError(f"trop d'objectifs ({MAX_GOALS}) : supprime ceux qui sont atteints ou abandonnés")
                item["id"] = self.state["next_goal_id"]
                self.state["next_goal_id"] += 1
                self.state["goals"].append(item)
            self._save()
        return item

    def remove_goal(self, goal_id: int) -> dict:
        with self._lock:
            for i, g in enumerate(self.state["goals"]):
                if g["id"] == int(goal_id):
                    item = self.state["goals"].pop(i)
                    self._save()
                    return item
        raise LookupError(f"aucun objectif #{goal_id}")

    def render_goals(self) -> str:
        """Bloc stable (sans compte à rebours, qui change chaque jour) pour le prompt système."""
        if not self.state["goals"]:
            return "(aucun objectif structuré)"
        return "\n".join(format_goal(g) for g in sorted_goals(self.state["goals"]))

    # --- propositions (boutons Appliquer / Ajuster / Ignorer) -------------------------

    def add_proposal(self, text: str, kind: str = "plan") -> int:
        with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc)
            cutoff = now - datetime.timedelta(days=PROPOSAL_TTL_DAYS)
            self.state["proposals"] = {k: v for k, v in self.state["proposals"].items()
                                       if datetime.datetime.fromisoformat(v["created"]) > cutoff}
            pid = self.state["next_proposal_id"]
            self.state["next_proposal_id"] += 1
            self.state["proposals"][str(pid)] = {"text": text, "kind": kind, "created": now.isoformat(), "status": "open"}
            self._save()
        return pid

    def proposal(self, pid: int | str) -> dict | None:
        return self.state["proposals"].get(str(pid))

    def close_proposal(self, pid: int | str, status: str) -> dict | None:
        """Passe une proposition ouverte à `status` ; None si inconnue ou déjà traitée (double clic)."""
        with self._lock:
            item = self.state["proposals"].get(str(pid))
            if not item or item["status"] != "open":
                return None
            item["status"] = status
            self._save()
            return dict(item)

    # --- liste de courses à cocher -----------------------------------------------------

    @property
    def shopping_list(self) -> dict | None:
        lst = self.state["shopping"]
        return None if lst is None else {**lst, "items": [dict(i) for i in lst["items"]]}

    def set_shopping_list(self, items: list[dict]) -> dict:
        """Nouvelle liste (remplace la précédente). Articles : {name, qty, aisle}, tous décochés."""
        with self._lock:
            lst = {
                "id": self.state["next_shopping_id"],
                "items": [{"name": i["name"], "qty": i.get("qty"), "aisle": i.get("aisle"), "done": False}
                          for i in items[:MAX_SHOPPING_ITEMS]],
                "event_uid": None,
                "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            self.state["next_shopping_id"] += 1
            self.state["shopping"] = lst
            self._save()
        return self.shopping_list

    def _current_shopping(self, list_id) -> dict | None:
        lst = self.state["shopping"]
        return lst if lst is not None and lst["id"] == int(list_id) else None

    def attach_shopping_event(self, list_id, uid: str) -> None:
        with self._lock:
            if (lst := self._current_shopping(list_id)) is not None:
                lst["event_uid"] = uid
                self._save()

    def toggle_shopping_item(self, list_id, index: int) -> dict | None:
        """Coche / décoche un article ; None si la liste a été remplacée depuis."""
        with self._lock:
            lst = self._current_shopping(list_id)
            if lst is None or not 0 <= index < len(lst["items"]):
                return None
            lst["items"][index]["done"] = not lst["items"][index]["done"]
            self._save()
        return self.shopping_list

    def reset_shopping(self, list_id) -> dict | None:
        with self._lock:
            lst = self._current_shopping(list_id)
            if lst is None:
                return None
            for item in lst["items"]:
                item["done"] = False
            self._save()
        return self.shopping_list

    # --- ressenti après séance --------------------------------------------------------

    def set_checkin(self, activity_id, **fields) -> dict:
        with self._lock:
            checkins = self.state["checkins"]
            item = checkins.setdefault(str(activity_id), {})
            item.update({k: v for k, v in fields.items() if v is not None})
            if len(checkins) > MAX_CHECKINS:   # garde les plus récents
                for key in sorted(checkins, key=lambda k: checkins[k].get("date", ""))[:len(checkins) - MAX_CHECKINS]:
                    checkins.pop(key)
            self._save()
            return dict(item)

    def recent_checkins(self, days: int, today: datetime.date) -> list[dict]:
        since = (today - datetime.timedelta(days=days)).isoformat()
        items = [dict(v, activity_id=k) for k, v in self.state["checkins"].items() if v.get("date", "") >= since]
        return sorted(items, key=lambda c: c["date"])

    @property
    def last_activity_id(self):
        return self.state["last_activity_id"]

    @last_activity_id.setter
    def last_activity_id(self, value) -> None:
        with self._lock:
            self.state["last_activity_id"] = value
            self._save()


def sorted_goals(goals: list[dict]) -> list[dict]:
    """Par date (les objectifs sans date à la fin), puis par id."""
    return sorted(goals, key=lambda g: (g.get("date") or "9999", g["id"]))


def format_goal(g: dict) -> str:
    parts = [f"#{g['id']} [{g['kind']}{', priorité ' + g['priority'] if g.get('priority') else ''}] {g['title']}"]
    if g.get("date"):
        parts.append(f"le {g['date']}")
    if g.get("target"):
        parts.append(f"cible : {g['target']}")
    if g.get("notes"):
        parts.append(f"({g['notes']})")
    return "- " + " — ".join(parts)


store = Store(Path(settings.data_dir) / "coach_state.json", seed=parse_seed(settings.seed_memory))
