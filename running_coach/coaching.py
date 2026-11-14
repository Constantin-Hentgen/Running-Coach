"""Logique de coaching calculée par le code (pas par le modèle) : compte à rebours des objectifs,
résumé des ressentis après séance, bilan prévu / réalisé de la semaine."""
import datetime
import re

from .config import FR_DAYS, TZ
from .dreeve import distance_km, format_activity
from .memory import LEGS, format_goal, sorted_goals

RPE_LABELS = {1: "très facile", 2: "facile", 3: "facile", 4: "modéré", 5: "modéré", 6: "soutenu",
              7: "difficile", 8: "très difficile", 9: "très très difficile", 10: "maximal"}


def _day(d: datetime.date) -> str:
    return f"{FR_DAYS[d.weekday()]} {d:%d/%m}"


# ---------------------------------------------------------------------------
# Objectifs
# ---------------------------------------------------------------------------

def countdown(goal: dict, today: datetime.date) -> str:
    if not goal.get("date"):
        return "sans date"
    days = (datetime.date.fromisoformat(goal["date"]) - today).days
    if days < 0:
        return f"passé depuis {-days} j"
    if days == 0:
        return "aujourd'hui"
    weeks = days // 7
    return f"dans {days} j" + (f" (~{weeks} sem.)" if weeks >= 2 else "")


def goals_context(goals: list[dict], today: datetime.date) -> str:
    if not goals:
        return "aucun objectif structuré (demande-le à l'athlète s'il parle d'une course ou d'un objectif)"
    return "\n".join(f"{format_goal(g)} → {countdown(g, today)}" for g in sorted_goals(goals))


def format_goals(goals: list[dict], today: datetime.date) -> str:
    """Réponse Telegram de /goals (Markdown à échapper par l'appelant)."""
    if not goals:
        return ""
    icons = {"course": "🏁", "santé": "💚", "autre": "🎯"}
    lines = []
    for g in sorted_goals(goals):
        head = f"{icons.get(g['kind'], '🎯')} {g['title']}"
        if g.get("priority"):
            head += f" [{g['priority']}]"
        details = [x for x in (g.get("date") and f"{g['date']}, {countdown(g, today)}", g.get("target") and f"cible {g['target']}") if x]
        lines.append(head + (f" : {', '.join(details)}" if details else ""))
        if g.get("notes"):
            lines.append(f"   {g['notes']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ressenti après séance
# ---------------------------------------------------------------------------

def checkins_context(checkins: list[dict]) -> str:
    """Ressentis récents + charge perçue (RPE × minutes, « sRPE »)."""
    rated = [c for c in checkins if c.get("rpe")]
    if not checkins:
        return "aucun ressenti enregistré récemment"
    lines = []
    for c in checkins:
        parts = [c.get("name") or "séance"]
        if c.get("rpe"):
            parts.append(f"effort {c['rpe']}/10 ({RPE_LABELS.get(c['rpe'], '?')})")
        if c.get("legs"):
            parts.append(f"jambes {LEGS.get(c['legs'], c['legs'])}")
        lines.append(f"- {c['date']} : {', '.join(parts)}")
    if rated:
        srpe = sum(c["rpe"] * (c.get("minutes") or 0) for c in rated)
        lines.append(f"Charge perçue (somme effort × minutes) sur la période : {srpe:.0f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Liste de courses en cours
# ---------------------------------------------------------------------------

def shopping_context(lst: dict | None) -> str:
    if lst is None:
        return "aucune"
    done = sum(i["done"] for i in lst["items"])
    meta = [f"{len(lst['items'])} articles, {done} achetés"]
    if lst.get("days"):
        meta.append(f"couvre {lst['days']} jours")
    if lst.get("event_start"):
        start = datetime.datetime.fromisoformat(lst["event_start"]).astimezone(TZ)
        meta.append(f"dans l'agenda courses le {_day(start.date())} {start:%H:%M}")
    lines = [f"({', '.join(meta)}) — ✅ = acheté :"]
    lines += [f"- {'✅' if i['done'] else '☐'} {i['name']}" + (f" ({i['qty']})" if i.get("qty") else "")
              + (f" [{i['aisle']}]" if i.get("aisle") else "") for i in lst["items"]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stock en cuisine
# ---------------------------------------------------------------------------

def pantry_context(pantry: list[dict], today: datetime.date) -> str:
    if not pantry:
        return "vide ou inconnu (demande à l'athlète ce qu'il a s'il faut cuisiner)"
    lines = []
    for p in sorted(pantry, key=lambda p: p.get("added") or ""):
        age = ""
        if p.get("added"):
            days = (today - datetime.date.fromisoformat(p["added"])).days
            age = " — acheté aujourd'hui" if days == 0 else f" — acheté il y a {days} j"
        lines.append(f"- {p['name']}" + (f" ({p['qty']})" if p.get("qty") else "") + age + _due_label(p, today))
    return "\n".join(lines)


def box_count(qty) -> int:
    """« 2 boîtes » → 2 ; sans nombre lisible → 1."""
    m = re.match(r"\s*(\d+)", str(qty or ""))
    return int(m.group(1)) if m else 1


def boxes_in_use(pantry: list[dict]) -> list[tuple[str, int]]:
    """Plats cuisinés en boîtes encore au stock : [(nom, nombre de boîtes)]."""
    return [(p["name"], box_count(p.get("qty"))) for p in pantry if p.get("boxes")]


def boxes_context(pantry: list[dict], total: int) -> str:
    used = boxes_in_use(pantry)
    n = sum(k for _, k in used)
    detail = f" ({', '.join(f'{name} ×{k}' for name, k in used)})" if used else ""
    return f"{n}/{total} occupées{detail} → {max(0, total - n)} libre(s)"


def _due_label(p: dict, today: datetime.date) -> str:
    if not p.get("due"):
        return ""
    due = datetime.date.fromisoformat(p["due"])
    if p.get("asked"):
        return " — à vérifier (« encore là ? » envoyé)"
    if due < today:
        return " — date dépassée"
    return " — à consommer aujourd'hui" if due == today else f" — se garde jusqu'au {_day(due)}"


def format_pantry(pantry: list[dict], today: datetime.date) -> str:
    """Réponse de /stock (texte brut, à échapper par l'appelant)."""
    return pantry_context(pantry, today).replace("— acheté", "·")


# ---------------------------------------------------------------------------
# Journal alimentaire et questions « qu'as-tu mangé ? »
# ---------------------------------------------------------------------------

def meal_slot(t: datetime.time) -> str:
    """Repas correspondant à une heure de question (posée environ une heure après le repas)."""
    hour = (t.hour - 1) % 24
    if 4 <= hour < 11:
        return "petit-déjeuner"
    if 11 <= hour < 16:
        return "déjeuner"
    if 16 <= hour < 18:
        return "collation"
    return "dîner"


def meal_window(times: list[tuple[int, int]], index: int) -> tuple[datetime.time, datetime.time, str]:
    """Plage couverte par la question n° index : depuis la question précédente (04:00 pour la première), 5 h au plus."""
    end = datetime.time(*times[index])
    previous = times[index - 1] if index > 0 else (4, 0)
    # au plus 5 h avant : la collation de 16 h n'est pas une trace du dîner
    earliest = max(0, end.hour * 60 + end.minute - 5 * 60)
    start_min = max(previous[0] * 60 + previous[1], earliest)
    return datetime.time(start_min // 60, start_min % 60), end, meal_slot(end)


def has_meal_between(meals: list[dict], day: datetime.date, start: datetime.time, end: datetime.time) -> bool:
    lo, hi, d = f"{start:%H:%M}", f"{end:%H:%M}", day.isoformat()
    return any(m["date"] == d and lo < m["time"] <= hi for m in meals)


def _macros(kcal, protein, carbs, fat=None) -> str:
    parts = [f"~{kcal} kcal" if kcal is not None else "", f"P {protein} g" if protein is not None else "",
             f"G {carbs} g" if carbs is not None else "", f"L {fat} g" if fat is not None else ""]
    return ", ".join(p for p in parts if p)


def meals_context(meals: list[dict], today: datetime.date) -> str:
    """Repas notés (aujourd'hui et hier) avec le total estimé du jour."""
    if not meals:
        return "aucun repas noté aujourd'hui ni hier"
    lines = []
    for day in (today - datetime.timedelta(days=1), today):
        of_day = [m for m in meals if m["date"] == day.isoformat()]
        label = "aujourd'hui" if day == today else "hier"
        if not of_day:
            lines.append(f"{label} ({_day(day)}) : rien de noté")
            continue
        lines.append(f"{label} ({_day(day)}) :")
        for m in of_day:
            macros = _macros(m.get("kcal"), m.get("protein_g"), m.get("carbs_g"), m.get("fat_g"))
            lines.append(f"- {m['time']} {m['text']}" + (f" ({macros})" if macros else ""))
        totals = [sum(m.get(k) or 0 for m in of_day) if any(m.get(k) is not None for m in of_day) else None
                  for k in ("kcal", "protein_g", "carbs_g")]
        if any(t is not None for t in totals):
            lines.append(f"  total estimé : {_macros(*totals)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bilan de la semaine : prévu (agenda) vs réalisé (Dreeve)
# ---------------------------------------------------------------------------

def week_bounds(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    monday = today - datetime.timedelta(days=today.weekday())
    return monday, monday + datetime.timedelta(days=6)


def adherence(planned: list[dict], runs: list[dict], monday: datetime.date) -> dict:
    """Une séance prévue compte comme faite si un run existe le même jour (un run ne valide qu'une séance)."""
    runs_by_day: dict[datetime.date, list[dict]] = {}
    for r in runs:
        try:
            d = datetime.date.fromisoformat((r.get("startDateLocal") or "")[:10])
        except ValueError:
            continue
        runs_by_day.setdefault(d, []).append(r)

    done, missed = [], []
    used: dict[datetime.date, int] = {}
    for ev in planned:
        d = datetime.datetime.fromisoformat(ev["start"]).date()
        if used.get(d, 0) < len(runs_by_day.get(d, [])):
            used[d] = used.get(d, 0) + 1
            done.append(ev)
        else:
            missed.append(ev)
    extra = [r for d, rs in runs_by_day.items() for r in rs[used.get(d, 0):]]
    return {
        "monday": monday,
        "planned": len(planned),
        "done": done,
        "missed": missed,
        "extra": extra,
        "km": sum(distance_km(r) for r in runs),
        "runs": len(runs),
        "rate": round(100 * len(done) / len(planned)) if planned else None,
    }


def format_adherence(a: dict) -> str:
    lines = [f"Semaine du {a['monday']:%d/%m} : {a['runs']} run(s), {a['km']:.1f} km."]
    if a["planned"]:
        lines.append(f"Séances prévues réalisées : {len(a['done'])}/{a['planned']} ({a['rate']} %).")
    else:
        lines.append("Aucune séance n'était planifiée dans l'agenda.")
    for ev in a["missed"]:
        d = datetime.datetime.fromisoformat(ev["start"]).date()
        lines.append(f"- non faite : {ev.get('summary') or 'séance'} ({_day(d)})")
    for r in a["extra"]:
        lines.append(f"- hors plan : {format_activity(r)[2:]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Variété des repas et plat découverte de la semaine
# ---------------------------------------------------------------------------

VARIETY_DAYS = 14


def dish_key(text: str) -> str:
    """« 🍝 Pâtes au thon, maïs (1 tup, ce midi) » → « pâtes au thon » : le plat, sans émoji ni détails."""
    text = re.sub(r"\([^)]*\)", "", text or "")
    text = re.split(r"[,;+]| et ", text)[0]
    return " ".join(re.sub(r"[^\w' -]", " ", text.lower()).split())


def meal_variety(meals: list[dict], today: datetime.date, days: int = VARIETY_DAYS) -> str:
    since = (today - datetime.timedelta(days=days)).isoformat()
    eaten = [m for m in meals if m["date"] > since and not m["text"].startswith("rien (")]
    if len(eaten) < 3:
        return "pas assez de repas notés pour juger"
    counts: dict[str, int] = {}
    for m in eaten:
        key = dish_key(m["text"])
        if key:
            counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
    repeated = ", ".join(f"{k} ×{n}" for k, n in top if n > 1) or "aucun plat répété"
    return (f"{days} derniers jours : {len(eaten)} repas notés, {len(counts)} plats différents ; "
            f"les plus fréquents : {repeated}")


def discovery_context(discoveries: list[dict], meals: list[dict], today: datetime.date) -> str:
    if not discoveries:
        return "aucune pour l'instant"
    last = discoveries[-1]
    key = dish_key(last["title"])
    tried = bool(key) and any(m["date"] >= last["date"] and key in dish_key(m["text"]) for m in meals)
    status = "goûté 👍" if tried else "pas encore goûté : il peut être l'une des options de /eat"
    older = ", ".join(d["title"] for d in discoveries[-6:-1])
    return (f"cette semaine : {last['title']} (proposé le {last['date']}, {status})"
            + (f" ; déjà proposés avant (ne pas reproposer) : {older}" if older else ""))
