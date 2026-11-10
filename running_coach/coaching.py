"""Logique de coaching calculée par le code (pas par le modèle) : compte à rebours des objectifs,
résumé des ressentis après séance, bilan prévu / réalisé de la semaine."""
import datetime

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
