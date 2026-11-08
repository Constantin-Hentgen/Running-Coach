"""Commandes Telegram : texte d'aide et mises en forme des réponses instantanées (sans appel au modèle)."""
import datetime
from itertools import groupby

from telegram import BotCommand

from .config import FR_DAYS
from .dreeve import format_activity, weekly_volume
from .formatting import md_escape

AGENDA_DEFAULT_DAYS = 7
AGENDA_MAX_DAYS = 45

# (commande principale, alias, description du menu « / » de Telegram)
COMMANDS = [
    ("plan", [], "Séances prévues (7 jours, ou /plan 14)"),
    ("load", [], "Charge d'entraînement et volume récent"),
    ("runs", [], "Tes derniers runs"),
    ("weather", [], "Prévisions par lieu d'entraînement"),
    ("memory", [], "Ce que le coach sait de toi"),
    ("debrief", [], "Débrief IA de ton dernier run"),
    ("briefing", [], "Briefing IA complet et ajustement du programme"),
    ("help", ["start"], "Liste des commandes"),
]

BOT_COMMANDS = [BotCommand(name, desc) for name, _, desc in COMMANDS]

HELP_TEXT = """🏃 *Ton coach running*
Écris-moi simplement : « décale ma sortie de samedi », « je suis crevé, allège demain », « planifie ma semaine »…

*Infos instantanées* (sans IA, gratuites)
/plan — séances prévues sur 7 jours (/plan 14 pour 14 jours)
/load — charge d'entraînement (TSB, ratio A/C) et volume
/runs — tes 5 derniers runs
/weather — prévisions par lieu
/memory — ce que je sais de toi

*Avec le coach* (IA)
/debrief — débrief de ton dernier run
/briefing — briefing complet, ajuste le programme dans l'agenda

/help — cette aide

_Automatique : débrief après chaque run, briefing chaque matin, alerte si pluie sur une séance._"""


def parse_days(args: list[str]) -> int:
    """Nombre de jours demandé à /plan, borné ; valeur par défaut si absent ou invalide."""
    try:
        days = int(args[0]) if args else AGENDA_DEFAULT_DAYS
    except ValueError:
        return AGENDA_DEFAULT_DAYS
    return max(1, min(days, AGENDA_MAX_DAYS))


def _day_label(d: datetime.date) -> str:
    return f"{FR_DAYS[d.weekday()]} {d:%d/%m}"


def format_agenda(events: list[dict], days: int) -> str:
    header = f"📅 *Programme, {days} prochain{'s' if days > 1 else ''} jour{'s' if days > 1 else ''}*"
    if not events:
        return f"{header}\nRien de prévu. Demande-moi de planifier, ou lance /briefing."

    def day_of(ev):
        return datetime.datetime.fromisoformat(ev["start"]).date()

    lines = [header]
    for day, group in groupby(events, key=day_of):
        lines.append(f"\n*{_day_label(day)}*")
        for ev in group:
            if ev.get("all_day"):
                when = "journée"
            else:
                s, e = datetime.datetime.fromisoformat(ev["start"]), datetime.datetime.fromisoformat(ev["end"])
                when = f"{s:%H:%M}–{e:%H:%M}"
            place = f" · 📍 {md_escape(ev['location'])}" if ev.get("location") else ""
            lines.append(f"- {when} · {md_escape(ev.get('summary') or 'Séance')}{place}")
    return "\n".join(lines)


def format_form(load: dict, runs: list[dict], today: datetime.date) -> str:
    tsb, ac = load.get("tsb") or {}, load.get("acRatio") or {}
    lines = [
        "🔋 *Forme*",
        f"- Forme (CTL) : {load.get('ctl')}",
        f"- Fatigue (ATL) : {load.get('atl')}",
        f"- Fraîcheur (TSB) : {tsb.get('value')} ({md_escape(str(tsb.get('status')))})",
        f"- Ratio aigu/chronique : {ac.get('value')} ({md_escape(str(ac.get('status')))})",
        f"- Jours de repos sur 7 j : {load.get('restDaysLast7Days')}",
        "",
        "📊 *Volume par semaine*",
    ]
    for monday, km, n in reversed(weekly_volume(runs, today, 4)):
        label = "cette semaine" if monday == today - datetime.timedelta(days=today.weekday()) else f"sem. du {monday:%d/%m}"
        lines.append(f"- {label} : {km:.1f} km, {n} sortie{'s' if n > 1 else ''}")
    return "\n".join(lines)


def format_last_runs(runs: list[dict]) -> str:
    if not runs:
        return "Aucun run trouvé dans Dreeve."
    return "🏃 *Derniers runs*\n" + "\n".join(md_escape(format_activity(r)) for r in runs)


def format_weather(forecast_text: str) -> str:
    """Remet en forme le texte de weather.get_forecast_text pour Telegram."""
    lines = ["🌤️ *Météo*"]
    for line in forecast_text.splitlines():
        if line.startswith("    "):
            lines.append(f"   • {md_escape(line.strip())}")
        elif line.startswith("  "):
            lines.append(f"- {md_escape(line.strip())}")
        elif line.strip():
            lines.append(md_escape(line.strip()))
    return "\n".join(lines)
