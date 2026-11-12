"""Commandes Telegram : texte d'aide et mises en forme des réponses instantanées (sans appel au modèle)."""
import datetime
from itertools import groupby

from telegram import BotCommand

from .coaching import format_goals, format_pantry
from .config import FR_DAYS
from .dreeve import format_activity, weekly_volume
from .formatting import md_escape

AGENDA_DEFAULT_DAYS = 7
AGENDA_MAX_DAYS = 45
SHOPPING_DEFAULT_DAYS = 7
SHOPPING_MAX_DAYS = 14   # au-delà, les produits frais ne tiennent pas

# (commande principale, alias, description du menu « / » de Telegram)
COMMANDS = [
    ("plan", [], "Séances prévues (7 jours, ou /plan 14)"),
    ("load", [], "Charge d'entraînement et volume récent"),
    ("runs", [], "Tes derniers runs"),
    ("weather", [], "Prévisions par lieu d'entraînement"),
    ("goals", [], "Tes objectifs et le compte à rebours"),
    ("memory", [], "Ce que le coach sait de toi"),
    ("debrief", [], "Débrief IA de ton dernier run"),
    ("review", [], "Bilan IA de la semaine (prévu vs fait)"),
    ("shopping", [], "Liste de courses selon tes objectifs (7 j, ou /shopping 3)"),
    ("list", [], "Ta liste de courses en cours, à cocher"),
    ("recipes", [], "Recettes en boîtes pour ta liste de courses"),
    ("eat", [], "Quoi manger maintenant (stock, heure, entraînement)"),
    ("stock", [], "Ce que tu as en cuisine"),
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
/goals — tes objectifs et le compte à rebours
/memory — ce que je sais de toi

*Avec le coach* (IA)
/debrief — débrief de ton dernier run
/review — bilan de la semaine : prévu vs fait, forme, semaine prochaine
/shopping — liste de courses pour 7 jours selon tes objectifs sportifs et santé (/shopping 3 pour 3 jours), à cocher au magasin
/list — réaffiche ta liste de courses en cours, à cocher
/recipes — recettes faciles à préparer en boîtes avec ta liste (/recipes new pour en avoir d'autres)
/eat — quoi manger maintenant, selon ton stock, l'heure et ton entraînement (temps, étapes, ce qu'il faut acheter)
/stock — ce que tu as en cuisine : les articles cochés sur ta liste y entrent, les plats que tu marques « mangé » ou « cuisiné » en sortent, et je te demande si les produits frais sont encore là quand ils arrivent à échéance
/briefing — briefing complet, ajuste le programme dans l'agenda

/help — cette aide

Tu peux aussi m'envoyer des *vocaux* 🎙️. Quand je propose un changement, des boutons te permettent de l'appliquer.

_Automatique : débrief et ressenti après chaque run, briefing chaque matin, bilan le dimanche soir, alerte météo (pluie, chaleur, vent, verglas) sur tes séances, « encore là ? » sur les produits frais du stock, « qu'as-tu mangé ? » quand je n'ai aucune trace d'un repas._"""


def parse_days(args: list[str], default: int = AGENDA_DEFAULT_DAYS, maximum: int = AGENDA_MAX_DAYS) -> int:
    """Nombre de jours passé à une commande, borné ; valeur par défaut si absent ou invalide."""
    try:
        days = int(args[0]) if args else default
    except ValueError:
        return default
    return max(1, min(days, maximum))


def eat_prompt(now: datetime.datetime) -> str:
    return (
        f"Que dois-je manger maintenant ? Il est {FR_DAYS[now.weekday()]} {now:%d/%m %H:%M}.\n"
        "Déduis-le de mon stock (contexte), de l'heure (petit-déjeuner, déjeuner, collation, dîner), de mon entraînement "
        "(séance qui vient de se terminer ou qui arrive aujourd'hui ou demain matin, charge, ressentis), de mes objectifs et "
        "de ma mémoire (goûts, allergies, rassasiement).\n"
        "Si une information change vraiment la réponse et que tu ne peux pas la déduire (combien de temps j'ai, si je "
        "mange seul, si un produit frais ancien est encore là…), pose-moi d'abord une ou deux questions courtes et "
        "arrête-toi là. Sinon, propose-moi 2 ou 3 plats variés au choix (dont un express si possible), "
        "format Telegram très concis, une ligne chacun :\n"
        "🍽️ *Le plat* — pourquoi maintenant (lien avec la séance ou l'objectif) · ⏱️ temps · 🛒 ce qu'il faut acheter, "
        "ou « tout en stock ».\n"
        "Pas de recette dans ton message : pour chaque plat, appelle propose_meal avec `recipe` (ingrédients avec "
        "quantités, 3 à 6 étapes courtes, et combien de boîtes en faire pour plus tard si ça s'y prête), les "
        "ingrédients du stock qu'il consomme (`uses`) et ses estimations (kcal, protéines, glucides). J'aurai un bouton "
        "par plat : quand je choisis, je reçois sa recette, ses ingrédients sortent du stock et le repas est noté. "
        "Tiens compte de ce que j'ai déjà mangé aujourd'hui (repas notés). Ne modifie pas l'agenda."
    )


def recipes_prompt(lst: dict) -> str:
    """Recettes tirées de la liste de courses en cours."""
    items = "\n".join(f"- {i['name']}" + (f" ({i['qty']})" if i.get("qty") else "") for i in lst["items"])
    covers = ", ".join(x for x in (lst.get("days") and f"{lst['days']} jours", lst.get("coverage")) if x) or "la période prévue"
    return (
        f"Propose les recettes de ma liste de courses actuelle (elle couvre : {covers}) :\n{items}\n\n"
        "Contraintes : faciles (30 min actives max), efficaces (2 sessions de cuisine max, en batch), adaptées aux boîtes "
        "(se conservent 3-4 jours au frigo et se réchauffent bien, ou se congèlent), avec surtout les articles de la liste "
        "(+ basiques du placard : sel, poivre, épices, huile). Relie-les à mes séances et objectifs : plus de glucides la "
        "veille ou après les séances dures ou longues, protéines pour la récupération, et respecte ma mémoire (goûts, "
        "allergies, rassasiement).\n"
        "Format Telegram concis :\n"
        "- 🗓️ *Plan de batch cooking* : quoi cuisiner quand (ex. dimanche et mercredi) et combien de boîtes.\n"
        "- Pour chaque recette (4 à 6) : *nom* — nombre de boîtes, temps, conservation (frigo / congélo), ingrédients de la "
        "liste avec quantités, 3 à 5 étapes courtes, et pour quel moment (déjeuner, dîner, après telle séance).\n"
        "- 🥣 Petits-déjeuners et collations rapides : 2-3 lignes.\n"
        "- Une ligne pour un article de la liste non utilisé ou un ingrédient qui manquerait.\n"
        "Pour chaque recette, appelle propose_meal (titre = nom de la recette, uses = articles de la liste ou du stock "
        "qu'elle consomme, makes = nombre de boîtes ou portions, estimations par portion) : j'aurai un bouton « cuisiné » "
        "qui retirera les ingrédients du stock et y mettra les boîtes.\n"
        "Ne modifie pas l'agenda et n'appelle pas save_shopping_list."
    )


def format_stock_reply(pantry: list[dict], today: datetime.date) -> str:
    if not pantry:
        return ("🥫 *Stock*\nRien d'enregistré. Coche les articles de ta liste de courses quand tu les achètes, ou "
                "dis-moi ce que tu as (« j'ai du riz, des œufs et du thon »).")
    return ("🥫 *Stock* (" + str(len(pantry)) + " articles)\n" + md_escape(format_pantry(pantry, today))
            + "\n\n_Pour corriger : « j'ai fini le riz », « il reste 2 œufs », « j'ai acheté du pain »._")


def format_goals_reply(goals: list[dict], today: datetime.date) -> str:
    if not goals:
        return ("🎯 *Objectifs*\nAucun objectif enregistré. Dis-moi simplement ta prochaine course (date, distance, chrono "
                "visé) ou un objectif santé, je l'enregistre et je construis le plan à rebours.")
    return ("🎯 *Objectifs*\n" + md_escape(format_goals(goals, today))
            + "\n\n_Pour modifier : « décale mon semi au 14 mars », « vise plutôt 1h50 », « j'abandonne le trail »._")


def shopping_prompt(days: int, today: datetime.date, save_to_calendar: bool = False) -> str:
    end = today + datetime.timedelta(days=days - 1)
    return (
        f"Prépare ma liste de courses pour {days} jour{'s' if days > 1 else ''}, du {_day_label(today)} au {_day_label(end)} inclus.\n"
        "Base-toi sur : mes objectifs sportifs et de santé, mes préférences, régimes, allergies et contraintes alimentaires "
        "(mémoire) ; les séances prévues dans l'agenda sur cette période (jours durs ou longs : plus de glucides et de "
        "récupération ; jours de repos : plus léger) ; ma charge d'entraînement actuelle.\n"
        "Tiens compte de mon stock (contexte) : n'achète pas ce que j'ai déjà en quantité suffisante, et utilise en "
        "priorité ce qui doit être consommé vite.\n"
        "Pense la liste autour de 4 à 6 recettes faciles et efficaces à préparer à l'avance en boîtes (batch cooking : "
        "se conservent 3-4 jours au frigo, se réchauffent bien, ou se congèlent), plus petits-déjeuners et collations "
        "simples ; je demanderai les recettes avec /recipes.\n"
        "Format Telegram, concis :\n"
        "- 1 ou 2 lignes sur la logique de la période (séances clés, jours de repos) et les recettes prévues (juste leurs noms).\n"
        "- Les articles groupés par rayon avec un émoji (fruits et légumes, protéines, féculents, produits frais, épicerie, "
        "surgelés…), avec des quantités totales réalistes pour la période (dans save_shopping_list).\n"
        "- Au plus 3 repères pratiques liés aux séances (ex. collation avant le fractionné de mardi).\n"
        "S'il te manque une information essentielle (nombre de personnes, régime, allergies), fais une hypothèse raisonnable, "
        "dis-la en une ligne et demande-la-moi. Ne modifie pas l'agenda."
        + " Une fois la liste finalisée, enregistre-la avec save_shopping_list (articles, quantités, rayons, nombre de jours "
          f"couverts ({days}), repas couverts, et coût total estimé en fourchette d'euros, prix d'un supermarché français "
          "courant) : je la reçois en checklist à cocher avec ces informations en tête, donc ne la recopie pas en entier "
          "dans ta réponse."
        + (" Donne aussi start_time : le créneau de courses ou de commande que je préfère (mémoire) ; à défaut, choisis "
           "un créneau raisonnable et dis-le." if save_to_calendar else "")
    )


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
