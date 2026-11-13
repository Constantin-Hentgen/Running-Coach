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
# Priorité d'un souvenir : 1 = règle (toujours appliquée, sauf demande explicite contraire), 2 = préférence forte
# (appliquée sauf bonne raison, à expliquer), 3 = indication (à prendre en compte quand c'est possible).
PRIORITIES = {1: "RÈGLES (toujours appliquées, sauf demande explicite contraire de l'athlète)",
              2: "PRÉFÉRENCES FORTES (appliquées sauf bonne raison, à expliquer)",
              3: "INDICATIONS (à prendre en compte quand c'est possible)"}
DEFAULT_PRIORITY = 2
MAX_ITEMS = 80
MAX_TEXT = 300
GOAL_KINDS = ["course", "santé", "autre"]
GOAL_PRIORITIES = ["A", "B", "C"]
MAX_GOALS = 15
MAX_CHECKINS = 60
PROPOSAL_TTL_DAYS = 14
MAX_SHOPPING_ITEMS = 60   # boutons Telegram : 100 max par message, rayons compris
MAX_PANTRY_ITEMS = 120
MAX_KEEPS_DAYS = 60       # au-delà : produit qui se garde, pas de date
PANTRY_ASK_GRACE_DAYS = 3 # « Encore là ? » sans réponse depuis ce délai : l'article sort du stock
MEAL_LOG_DAYS = 30        # journal alimentaire conservé
LEGS = {"fraiches": "fraîches", "normales": "normales", "lourdes": "lourdes", "douleur": "douleur"}



def keeps_days(value) -> int | None:
    """Conservation d'un produit en jours : entier > 0 et raisonnable, sinon None (se garde : pas de date)."""
    try:
        days = int(float(value))
    except (TypeError, ValueError):
        return None
    return days if 0 < days <= MAX_KEEPS_DAYS else None


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


def priority_of(value) -> int:
    try:
        return int(value) if int(value) in PRIORITIES else DEFAULT_PRIORITY
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY


INTERACTION_GAP_MIN = 90   # silence au-delà duquel une nouvelle session d'échange commence

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
        state.setdefault("interaction", {"start": None, "last": None, "gif_at": None})   # session en cours, dernier GIF
        state.setdefault("goals", [])
        state.setdefault("next_goal_id", 1)
        state.setdefault("proposals", {})     # id → {text, kind, created, status}
        state.setdefault("next_proposal_id", 1)
        state.setdefault("checkins", {})      # id d'activité → {date, minutes, name, rpe, legs}
        state.setdefault("shopping", None)    # liste de courses en cours : {id, items, event_uid, created}
        state.setdefault("next_shopping_id", 1)
        # stock en cuisine : {id, name, qty, added, source, keeps (jours ou None), due (date à vérifier), asked (date)}
        state.setdefault("pantry", [])
        state.setdefault("next_pantry_id", 1)
        # journal alimentaire : {id, date, time (HH:MM), text, kcal, protein_g, carbs_g, source}
        state.setdefault("meals", [])
        state.setdefault("discoveries", [])   # plat nouveau de la semaine : [{title, date}] (jamais reproposé)
        state.setdefault("next_meal_id", 1)
        state.setdefault("meal_checks", {})   # « date créneau » → asked | skipped | answered (question « qu'as-tu mangé ? »)
        for p in state["pantry"]:
            if "id" not in p:
                p["id"] = state["next_pantry_id"]
                state["next_pantry_id"] += 1
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
    def _append(state: dict, category: str, text: str, source: str, priority: int = DEFAULT_PRIORITY) -> dict:
        item = {
            "id": state["next_id"],
            "category": category,
            "priority": priority,
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

    def remember(self, category: str, text: str, source: str = "conversation", replaces: int | None = None,
                 priority: int | None = None) -> dict:
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
            item = self._append(self.state, category, text, source, priority_of(priority))
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
        """Bloc texte stable pour le prompt système : par priorité (règles d'abord), puis catégorie, puis id."""
        if not self.state["memories"]:
            return "(rien pour l'instant)"
        order = {c: i for i, c in enumerate(CATEGORIES)}
        blocks = []
        for prio, title in PRIORITIES.items():
            items = sorted((m for m in self.state["memories"] if priority_of(m.get("priority")) == prio),
                           key=lambda m: (order.get(m["category"], 99), m["id"]))
            if items:
                blocks.append(f"{title} :\n" + "\n".join(
                    f"- #{m['id']} [{m['category']}] {m['text']} (depuis {m['date']})" for m in items))
        return "\n\n".join(blocks)

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

    def add_proposal(self, text: str, kind: str = "plan", **data) -> int:
        """kind : plan | weather | meal (data : uses = ingrédients du stock à retirer quand le plat est mangé)."""
        with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc)
            cutoff = now - datetime.timedelta(days=PROPOSAL_TTL_DAYS)
            self.state["proposals"] = {k: v for k, v in self.state["proposals"].items()
                                       if datetime.datetime.fromisoformat(v["created"]) > cutoff}
            pid = self.state["next_proposal_id"]
            self.state["next_proposal_id"] += 1
            self.state["proposals"][str(pid)] = {"text": text, "kind": kind, "created": now.isoformat(), "status": "open", **data}
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

    def set_shopping_list(self, items: list[dict], days: int | None = None, coverage: str | None = None,
                          cost_min: float | None = None, cost_max: float | None = None) -> dict:
        """Nouvelle liste (remplace la précédente). Articles : {name, qty, aisle}, tous décochés.
        days / coverage : ce que la liste couvre ; cost_min / cost_max : coût estimé en euros."""
        if cost_min is not None and cost_max is not None and cost_max < cost_min:
            cost_min, cost_max = cost_max, cost_min
        with self._lock:
            lst = {
                "id": self.state["next_shopping_id"],
                "items": [{"name": i["name"], "qty": i.get("qty"), "aisle": i.get("aisle"), "keeps": keeps_days(i.get("keeps")),
                           "done": False} for i in items[:MAX_SHOPPING_ITEMS]],
                "days": days,
                "coverage": coverage,
                "cost_min": cost_min,
                "cost_max": cost_max,
                "recipes": None,
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

    def attach_shopping_event(self, list_id, uid: str, start: datetime.datetime | None = None,
                              end: datetime.datetime | None = None) -> None:
        """Rattache l'événement de l'agenda courses (et son créneau, réutilisé si la liste est modifiée)."""
        with self._lock:
            if (lst := self._current_shopping(list_id)) is not None:
                lst["event_uid"] = uid
                lst["event_start"] = start.isoformat() if start else None
                lst["event_end"] = end.isoformat() if end else None
                self._save()

    def set_recipes(self, list_id, text: str, meals: list[int] | None = None) -> None:
        """Recettes générées pour cette liste (réaffichées sans IA par /recipes), avec les id de leurs boutons « cuisiné »."""
        with self._lock:
            if (lst := self._current_shopping(list_id)) is not None:
                lst["recipes"] = text
                lst["recipe_meals"] = list(meals or [])
                self._save()

    def toggle_shopping_item(self, list_id, index: int) -> dict | None:
        """Coche / décoche un article ; None si la liste a été remplacée depuis.
        Coché = acheté : l'article entre dans le stock ; décoché : il en sort."""
        with self._lock:
            lst = self._current_shopping(list_id)
            if lst is None or not 0 <= index < len(lst["items"]):
                return None
            item = lst["items"][index]
            item["done"] = not item["done"]
            source = f"liste {lst['id']}:{index}"
            if item["done"]:
                self._pantry_put(item["name"], item.get("qty"), source, item.get("keeps"))
            else:
                self.state["pantry"] = [p for p in self.state["pantry"] if p.get("source") != source]
            self._save()
        return self.shopping_list

    def reset_shopping(self, list_id) -> dict | None:
        with self._lock:
            lst = self._current_shopping(list_id)
            if lst is None:
                return None
            for item in lst["items"]:
                item["done"] = False
            prefix = f"liste {lst['id']}:"
            self.state["pantry"] = [p for p in self.state["pantry"] if not (p.get("source") or "").startswith(prefix)]
            self._save()
        return self.shopping_list

    def edit_shopping_list(self, check: list[str] | None = None, uncheck: list[str] | None = None,
                           remove: list[str] | None = None, add: list[dict] | None = None) -> tuple[dict, dict]:
        """Modifie la liste en cours sans la recréer : coche (= acheté, entre dans le stock), décoche, retire, ajoute.
        La liste change d'id (les anciens boutons Telegram, indexés par position, ne doivent plus agir) ;
        événement d'agenda, couverture, coût et recettes sont conservés. Retourne (liste, résumé)."""
        out = {"checked": [], "unchecked": [], "removed": [], "added": [], "not_found": []}
        with self._lock:
            old = self.state["shopping"]
            if old is None:
                raise LookupError("aucune liste de courses en cours : crée-la avec save_shopping_list")
            items = [dict(i, _source=f"liste {old['id']}:{n}") for n, i in enumerate(old["items"])]

            def find(name):
                name = " ".join(str(name).split()).casefold()
                return next((i for i in items if i["name"].casefold() == name), None)

            for name in remove or []:
                if (item := find(name)) is None:
                    out["not_found"].append(name)
                else:
                    items.remove(item)
                    out["removed"].append(item["name"])
            for a in add or []:
                name = " ".join(str(a.get("name") or "").split())
                if name and find(name) is None:
                    items.append({"name": name, "qty": (a.get("qty") or a.get("quantity") or None),
                                  "aisle": a.get("aisle") or None, "keeps": keeps_days(a.get("keeps_days") or a.get("keeps")),
                                  "done": False, "_source": None})
                    out["added"].append(name)
            for names, done, key in ((check, True, "checked"), (uncheck, False, "unchecked")):
                for name in names or []:
                    if (item := find(name)) is None:
                        out["not_found"].append(name)
                    else:
                        item["done"] = done
                        out[key].append(item["name"])

            new_id = self.state["next_shopping_id"]
            self.state["next_shopping_id"] += 1
            # Stock : les articles cochés y sont, sous la référence de leur nouvelle position (date d'achat conservée) ;
            # les décochés et retirés en sortent.
            by_source = {p.get("source"): p for p in self.state["pantry"] if p.get("source")}
            kept = set()
            for n, item in enumerate(items):
                if not item["done"]:
                    continue
                if (entry := by_source.get(item["_source"])) is not None:
                    entry["source"] = f"liste {new_id}:{n}"
                    kept.add(id(entry))
                else:
                    kept.add(id(self._pantry_put(item["name"], item.get("qty"), f"liste {new_id}:{n}", item.get("keeps"))))
            old_sources = {f"liste {old['id']}:{n}" for n in range(len(old["items"]))}
            self.state["pantry"] = [p for p in self.state["pantry"] if id(p) in kept or p.get("source") not in old_sources]
            self.state["shopping"] = {**old, "id": new_id,
                                      "items": [{k: v for k, v in i.items() if k != "_source"} for i in items]}
            self._save()
        return self.shopping_list, out

    # --- stock en cuisine ---------------------------------------------------------------

    @property
    def pantry(self) -> list[dict]:
        return [dict(p) for p in self.state["pantry"]]

    def _pantry_put(self, name: str, qty: str | None, source: str | None = None, keeps: int | None = None) -> dict:
        """Ajoute (ou remplace, même nom) un article du stock. keeps : conservation en jours (périssable), sinon None.
        Appelant : verrou tenu."""
        name = " ".join(name.split())[:80]
        self.state["pantry"] = [p for p in self.state["pantry"] if p["name"].casefold() != name.casefold()]
        if len(self.state["pantry"]) >= MAX_PANTRY_ITEMS:
            raise ValueError(f"stock plein ({MAX_PANTRY_ITEMS} articles) : retire ce qui est fini d'abord")
        today = datetime.datetime.now(TZ).date()
        keeps = keeps_days(keeps)
        item = {"id": self.state["next_pantry_id"], "name": name, "qty": (qty or "").strip()[:40] or None,
                "added": today.isoformat(), "source": source, "keeps": keeps,
                "due": (today + datetime.timedelta(days=keeps)).isoformat() if keeps else None, "asked": None}
        self.state["next_pantry_id"] += 1
        self.state["pantry"].append(item)
        return item

    def update_pantry(self, add: list[dict] | None = None, remove: list[dict] | None = None) -> dict:
        """add : [{name, qty, keeps_days?, boxes?}] (même nom = remplacé) ; remove : [{name, qty_left?}] (qty_left = il en reste).
        Retourne {added, removed, updated, not_found}."""
        out = {"added": [], "removed": [], "updated": [], "not_found": []}
        with self._lock:
            for r in remove or []:
                name = " ".join(str(r.get("name") or "").split())
                match = next((p for p in self.state["pantry"] if p["name"].casefold() == name.casefold()), None)
                if match is None:
                    out["not_found"].append(name)
                elif r.get("qty_left"):
                    match["qty"] = str(r["qty_left"]).strip()[:40]
                    out["updated"].append(f"{match['name']} ({match['qty']})")
                else:
                    self.state["pantry"].remove(match)
                    out["removed"].append(match["name"])
            for a in add or []:
                name = " ".join(str(a.get("name") or "").split())
                if name:
                    item = self._pantry_put(name, a.get("qty"), "conversation", a.get("keeps_days"))
                    if a.get("boxes"):   # plat cuisiné en boîtes : compte dans les tups occupées
                        item["boxes"] = True
                    out["added"].append(item["name"] + (f" ({item['qty']})" if item["qty"] else ""))
            self._save()
        return out

    # Péremption : un article périssable arrivé à sa date (`due`) est soumis à l'athlète (« Encore là ? ») ;
    # « encore là » repousse la date, « fini » le retire, sans réponse il sort du stock après quelques jours.

    def pantry_to_check(self, today: datetime.date) -> list[dict]:
        """Articles arrivés à leur date et pas encore soumis."""
        return [dict(p) for p in self.state["pantry"]
                if p.get("due") and p["due"] <= today.isoformat() and not p.get("asked")]

    def mark_pantry_asked(self, ids: list[int], today: datetime.date) -> None:
        with self._lock:
            for p in self.state["pantry"]:
                if p.get("id") in ids:
                    p["asked"] = today.isoformat()
            self._save()

    def expire_pantry(self, today: datetime.date) -> list[str]:
        """Retire les articles soumis sans réponse depuis PANTRY_ASK_GRACE_DAYS jours ; retourne leurs noms."""
        limit = (today - datetime.timedelta(days=PANTRY_ASK_GRACE_DAYS)).isoformat()
        with self._lock:
            gone = [p for p in self.state["pantry"] if p.get("asked") and p["asked"] <= limit]
            if gone:
                self.state["pantry"] = [p for p in self.state["pantry"] if p not in gone]
                self._save()
        return [p["name"] for p in gone]

    def answer_pantry_check(self, pantry_id: int, still_there: bool, today: datetime.date) -> dict | None:
        """Encore là : nouvelle date dans la moitié de la conservation (au moins 1 j) ; fini : retiré.
        None si l'article n'est plus dans le stock."""
        with self._lock:
            item = next((p for p in self.state["pantry"] if p.get("id") == int(pantry_id)), None)
            if item is None:
                return None
            if still_there:
                item["due"] = (today + datetime.timedelta(days=max(1, (item.get("keeps") or 2) // 2))).isoformat()
                item["asked"] = None
            else:
                self.state["pantry"].remove(item)
            self._save()
            return dict(item)


    # --- journal alimentaire ---------------------------------------------------------

    def log_meal(self, text: str, at: datetime.datetime, source: str = "conversation",
                 kcal=None, protein_g=None, carbs_g=None) -> dict:
        """Repas mangé (texte libre + estimations facultatives). Les entrées de plus de MEAL_LOG_DAYS jours sont purgées."""
        text = " ".join((text or "").split())[:200]
        if not text:
            raise ValueError("repas vide")

        def number(v):
            try:
                return max(0, round(float(v))) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None
        at = at.astimezone(TZ)
        with self._lock:
            cutoff = (at.date() - datetime.timedelta(days=MEAL_LOG_DAYS)).isoformat()
            self.state["meals"] = [m for m in self.state["meals"] if m["date"] >= cutoff]
            item = {"id": self.state["next_meal_id"], "date": at.date().isoformat(), "time": f"{at:%H:%M}",
                    "text": text, "kcal": number(kcal), "protein_g": number(protein_g), "carbs_g": number(carbs_g),
                    "source": source}
            self.state["next_meal_id"] += 1
            self.state["meals"].append(item)
            self.state["meals"].sort(key=lambda m: (m["date"], m["time"]))
            self._save()
        return dict(item)

    @property
    def discoveries(self) -> list[dict]:
        return [dict(d) for d in self.state["discoveries"]]

    def add_discovery(self, title: str, day: datetime.date) -> None:
        with self._lock:
            self.state["discoveries"].append({"title": title, "date": day.isoformat()})
            self.state["discoveries"] = self.state["discoveries"][-104:]   # deux ans de découvertes
            self._save()

    def meals_since(self, day: datetime.date) -> list[dict]:
        return [dict(m) for m in self.state["meals"] if m["date"] >= day.isoformat()]

    def meal_check(self, key: str) -> str | None:
        return self.state["meal_checks"].get(key)

    def set_meal_check(self, key: str, status: str) -> None:
        with self._lock:
            checks = self.state["meal_checks"]
            checks[key] = status
            for old in sorted(checks)[:-60]:   # garde les plus récents (clés « AAAA-MM-JJ créneau »)
                checks.pop(old)
            self._save()

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

    # --- session d'échange (un GIF au plus par session) ------------------------------------

    def touch_interaction(self, now: datetime.datetime) -> None:
        """Un échange avec le coach : une nouvelle session commence après INTERACTION_GAP_MIN minutes de silence
        (le premier message d'une session l'« ouvre »)."""
        with self._lock:
            s = self.state["interaction"]
            if not s["last"] or now - datetime.datetime.fromisoformat(s["last"]) > datetime.timedelta(minutes=INTERACTION_GAP_MIN):
                s["start"] = now.isoformat()
            s["last"] = now.isoformat()
            self._save()

    def session_opening(self) -> bool:
        """Le message en cours est le premier de la session."""
        s = self.state["interaction"]
        return bool(s["start"]) and s["start"] == s["last"]

    def gif_available(self) -> bool:
        s = self.state["interaction"]
        return not s["gif_at"] or not s["start"] or s["gif_at"] < s["start"]

    def mark_gif(self, now: datetime.datetime) -> None:
        with self._lock:
            self.state["interaction"]["gif_at"] = now.isoformat()
            self._save()

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
