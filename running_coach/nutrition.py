"""Nutrition calculée par le code, pas devinée par le modèle.

- Table de composition Ciqual 2020 de l'ANSES (Licence Ouverte), embarquée en CSV compact (data/ciqual_2020.csv,
  régénérable avec scripts/build_ciqual.py) : nutriments pour 100 g.
- Un repas = des aliments avec leur poids estimé ; ses nutriments sont la somme des valeurs Ciqual.
- Bilan énergétique : apports du journal vs dépense (métabolisme de base Mifflin-St Jeor × activité quotidienne,
  plus les calories des runs Dreeve).
"""
import csv
import datetime
import functools
import re
import unicodedata
from pathlib import Path

CIQUAL_CSV = Path(__file__).resolve().parent / "data" / "ciqual_2020.csv"
NUTRIENTS = ("kcal", "protein_g", "carbs_g", "fat_g", "sugars_g", "fiber_g", "salt_g")
ACTIVITY_FACTOR = 1.4          # vie quotidienne hors course (travail de bureau, marche) : dépense = métabolisme × 1,4
KCAL_PER_KG_KM = 1.0           # dépense d'un run quand Dreeve ne donne pas ses calories
PROTEIN_G_PER_KG = (1.4, 1.8)  # repères pour un coureur d'endurance
CARBS_G_PER_KG_TRAINING = (5, 7)
STOPWORDS = {"au", "aux", "a", "la", "le", "les", "de", "des", "du", "et", "en", "avec", "sans", "d", "l", "un", "une",
             "ou", "sur", "pour", "type"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower().replace("œ", "oe"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _tokens(text: str) -> list[str]:
    return [t for t in _norm(text).split() if t not in STOPWORDS and len(t) > 1]


@functools.cache
def foods() -> dict[str, dict]:
    """code Ciqual → {code, name, kcal, protein_g, …} (valeurs pour 100 g, None si inconnues)."""
    out = {}
    with CIQUAL_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["code"]] = {"code": row["code"], "name": row["name"],
                                **{k: float(row[k]) if row[k] else None for k in NUTRIENTS},
                                "_tokens": _tokens(row["name"])}
    return out


def _sing(t: str) -> str:
    return t[:-1] if len(t) > 3 and t[-1] in "sx" else t


def _score(q: str, ft: list[str]) -> float:
    """1 = même mot, 0,8 = singulier / pluriel, 0,5 = début de mot (« emmen » → « emmental »), 0 sinon."""
    best = 0.0
    for t in ft:
        if t == q:
            return 1.0
        if _sing(t) == _sing(q):
            best = max(best, 0.8)
        elif len(q) >= 4 and len(t) >= 4 and (t.startswith(q) or q.startswith(t)):
            best = max(best, 0.5)
    return best


def search(query: str, limit: int = 5) -> list[dict]:
    """Aliments Ciqual les plus proches d'une description courte (« riz blanc cuit », « oeuf dur »)."""
    q = _tokens(query)
    if not q:
        return []
    scored = []
    for food in foods().values():
        ft = food["_tokens"]
        score = sum(_score(t, ft) for t in q)
        if not score:
            continue
        first = _score(q[0], ft[:1])   # le nom commence par le mot principal
        scored.append((first > 0, round(score / len(q), 3), first, -len(ft), food))
    scored.sort(key=lambda s: s[:4], reverse=True)   # d'abord les noms qui commencent par le mot principal
    return [public(f) for *_, f in scored[:limit]]


def public(food: dict) -> dict:
    return {k: v for k, v in food.items() if not k.startswith("_")}


MAX_GRAMS = 2000   # par aliment


def compute(items: list[dict]) -> dict:
    """items : [{food, grams, code?}] → {items: [...avec l'aliment Ciqual et ses nutriments], totals, unmatched}."""
    out, unmatched = [], []
    for it in items or []:
        try:
            grams = float(it.get("grams") or 0)
        except (TypeError, ValueError):
            grams = 0
        label = " ".join(str(it.get("food") or "").split())[:80]
        food = foods().get(str(it.get("code") or "").strip()) or next(iter(search(label, 1)), None)
        if not 0 < grams <= MAX_GRAMS:   # un poids halluciné ne doit pas faire exploser la journée
            unmatched.append(f"{label or it.get('code')} (poids invalide : {grams:g} g)")
            continue
        if food is None:
            unmatched.append(label or str(it.get("code")))
            continue
        values = {k: round(food[k] * grams / 100, 1) if food.get(k) is not None else None for k in NUTRIENTS}
        out.append({"food": label or food["name"], "code": food["code"], "ciqual": food["name"], "grams": round(grams),
                    **values})
    totals = {k: round(sum(i[k] or 0 for i in out), 1) for k in NUTRIENTS} if out else {}
    if totals:
        totals["kcal"] = round(totals["kcal"])
    return {"items": out, "totals": totals, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# Repas du jour et bilan énergétique
# ---------------------------------------------------------------------------

def slot_name(slot: str) -> str:
    """Ancien « goûter » stocké dans le journal = collation."""
    return "collation" if slot == "goûter" else slot


def meal_of(t: datetime.time) -> str:
    """Le repas qui se mange à cette heure."""
    h = t.hour + t.minute / 60
    if 4 <= h < 10.5:
        return "petit-déjeuner"
    if 10.5 <= h < 15.5:
        return "déjeuner"
    if 15.5 <= h < 17.5:
        return "collation"
    return "dîner"


def bmr(profile: dict, today: datetime.date) -> float | None:
    """Métabolisme de base (Mifflin-St Jeor), None si le profil est incomplet."""
    try:
        w, h = float(profile["weight_kg"]), float(profile["height_cm"])
        age = today.year - int(profile["birth_year"])
        sex = str(profile["sex"]).lower()[:1]
    except (KeyError, TypeError, ValueError):
        return None
    return 10 * w + 6.25 * h - 5 * age + (5 if sex in ("h", "m") else -161)


def run_kcal(run: dict, weight_kg: float | None) -> float:
    if run.get("calories"):
        return float(run["calories"])
    km = (run.get("distanceInMeter") or 0) / 1000
    return km * (weight_kg or 70) * KCAL_PER_KG_KM


def day_totals(meals: list[dict], day: datetime.date) -> dict:
    of_day = [m for m in meals if m["date"] == day.isoformat()]
    totals = {k: round(sum(m.get(k) or 0 for m in of_day), 1) for k in NUTRIENTS}
    totals["meals"] = len([m for m in of_day if not m["text"].startswith("rien (")])
    totals["slots"] = sorted({slot_name(m.get("slot") or meal_of(datetime.time.fromisoformat(m["time"]))) for m in of_day
                              if not m["text"].startswith("rien (")})
    return totals


def day_needs(profile: dict, runs: list[dict], day: datetime.date) -> tuple[float | None, float]:
    """(dépense estimée du jour, dont course)."""
    base = bmr(profile, day)
    running = sum(run_kcal(r, profile.get("weight_kg")) for r in runs if (r.get("startDateLocal") or "")[:10] == day.isoformat())
    return (base * ACTIVITY_FACTOR + running if base else None), running


def _fmt(t: dict) -> str:
    parts = [f"{t['kcal']:.0f} kcal", f"P {t['protein_g']:.0f} g", f"G {t['carbs_g']:.0f} g", f"L {t['fat_g']:.0f} g",
             f"fibres {t['fiber_g']:.0f} g"]
    return ", ".join(parts)


def profile_line(profile: dict, today: datetime.date) -> str:
    missing = [label for key, label in (("weight_kg", "poids"), ("height_cm", "taille"), ("birth_year", "année de naissance"),
                                        ("sex", "sexe")) if not profile.get(key)]
    if missing:
        return (f"profil incomplet ({', '.join(missing)} manquant) : demande-les une fois, simplement, au bon moment, "
                "puis set_profile ; sans ça, pas de bilan énergétique")
    base = bmr(profile, today)
    return (f"{profile['weight_kg']} kg, {profile['height_cm']} cm, {today.year - int(profile['birth_year'])} ans → "
            f"métabolisme de base ~{base:.0f} kcal, besoins d'un jour sans course ~{base * ACTIVITY_FACTOR:.0f} kcal")


def energy_context(meals: list[dict], runs: list[dict], profile: dict, today: datetime.date) -> str:
    """Bilan nutritionnel pour le coach : aujourd'hui, hier, moyenne 7 jours, repères (calculé par le code)."""
    lines = [f"- Profil : {profile_line(profile, today)}"]
    w = profile.get("weight_kg")
    for label, day in (("aujourd'hui (jusqu'ici)", today), ("hier", today - datetime.timedelta(days=1))):
        t = day_totals(meals, day)
        needs, running = day_needs(profile, runs, day)
        line = f"- {label} : {_fmt(t)} en {t['meals']} repas ({', '.join(t['slots']) or 'aucun'})"
        if needs:
            gap = t["kcal"] - needs
            line += (f" ; besoins ~{needs:.0f} kcal (dont course {running:.0f})"
                     + (f" → il reste ~{-gap:.0f} kcal à manger" if day == today and gap < 0 else
                        f" → {'déficit' if gap < 0 else 'surplus'} de ~{abs(gap):.0f} kcal"))
        lines.append(line)
    week = [today - datetime.timedelta(days=i) for i in range(1, 8)]
    logged = [(d, day_totals(meals, d)) for d in week]
    logged = [(d, t) for d, t in logged if t["meals"] >= 2]   # journal trop incomplet : on ne juge pas la journée
    if logged:
        avg = {k: sum(t[k] for _, t in logged) / len(logged) for k in NUTRIENTS}
        line = f"- 7 derniers jours ({len(logged)} jours bien notés) : moyenne {avg['kcal']:.0f} kcal/j"
        needs = [day_needs(profile, runs, d)[0] for d, _ in logged]
        if all(needs):
            balance = avg["kcal"] - sum(needs) / len(needs)
            line += f", besoins ~{sum(needs) / len(needs):.0f} → {'déficit' if balance < 0 else 'surplus'} moyen ~{abs(balance):.0f} kcal/j"
        if w:
            line += f", protéines {avg['protein_g'] / float(w):.1f} g/kg, glucides {avg['carbs_g'] / float(w):.1f} g/kg"
        lines.append(line)
    if w:
        lo, hi = PROTEIN_G_PER_KG
        clo, chi = CARBS_G_PER_KG_TRAINING
        lines.append(f"- Repères : protéines {lo * float(w):.0f}-{hi * float(w):.0f} g/j, glucides les jours de course "
                     f"{clo * float(w):.0f}-{chi * float(w):.0f} g/j")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Contrôle quotidien : ce qui cloche (détecté par le code, formulé ensuite par le coach)
# ---------------------------------------------------------------------------

MAIN_MEALS = ("petit-déjeuner", "déjeuner", "dîner")
LOW_INTAKE = 0.75        # apports < 75 % des besoins : trop peu
HIGH_INTAKE = 1.4        # > 140 % : inhabituellement haut
LIGHT_MAIN_MEAL_KCAL = 250
LOW_PROTEIN_G_PER_KG = 1.2
LOW_CARBS_G_PER_KG_RUN_DAY = 4


def anomalies(meals: list[dict], runs: list[dict], profile: dict, day: datetime.date) -> list[str]:
    """Ce qui cloche dans la journée : repas manquants ou sautés, apports trop bas ou trop hauts, repas très léger,
    protéines ou glucides bas un jour de course. Rien si la journée n'est pas notée du tout (pas d'info, pas de jugement)."""
    of_day = [m for m in meals if m["date"] == day.isoformat()]
    if not of_day:
        return []
    out = []
    slot = lambda m: slot_name(m.get("slot") or meal_of(datetime.time.fromisoformat(m["time"])))
    for name in MAIN_MEALS:
        mine = [m for m in of_day if slot(m) == name]
        if not mine:
            out.append(f"aucune trace du {name}")
        elif all(m["text"].startswith("rien (") for m in mine):
            out.append(f"{name} sauté")
        elif (kcal := sum(m.get("kcal") or 0 for m in mine)) and kcal < LIGHT_MAIN_MEAL_KCAL:
            out.append(f"{name} très léger (~{kcal:.0f} kcal)")
    t = day_totals(meals, day)
    needs, running = day_needs(profile, runs, day)
    if needs and t["meals"] >= 2:
        ratio = t["kcal"] / needs
        if ratio < LOW_INTAKE:
            out.append(f"apports bas : ~{t['kcal']:.0f} kcal pour ~{needs:.0f} de besoins (déficit ~{needs - t['kcal']:.0f})")
        elif ratio > HIGH_INTAKE:
            out.append(f"apports inhabituellement hauts : ~{t['kcal']:.0f} kcal pour ~{needs:.0f} de besoins")
    w = profile.get("weight_kg")
    if w and t["meals"] >= 2:
        w = float(w)
        if t["protein_g"] / w < LOW_PROTEIN_G_PER_KG:
            out.append(f"protéines basses : {t['protein_g']:.0f} g ({t['protein_g'] / w:.1f} g/kg)")
        if running and t["carbs_g"] / w < LOW_CARBS_G_PER_KG_RUN_DAY:
            out.append(f"jour de course avec peu de glucides : {t['carbs_g']:.0f} g ({t['carbs_g'] / w:.1f} g/kg)")
    return out
