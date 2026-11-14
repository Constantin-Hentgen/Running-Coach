"""Commandes Telegram : texte d'aide et mises en forme des réponses instantanées (sans appel au modèle)."""
import datetime
import re
from itertools import groupby

from telegram import BotCommand

from .coaching import format_goals, format_pantry
from .config import FR_DAYS, settings
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
    ("nutrition", ["nutri"], "Ce que tu as mangé : calories, macros, bilan du jour et de la semaine"),
    ("routes", ["parcours"], "2-3 boucles de course autour de chez toi ou du boulot (carte + GPX)"),
    ("newroute", [], "Une boucle que tu n'as jamais courue (carte + GPX)"),
    ("meme", [], "Un mème qui résume ta journée d'entraînement"),
    ("briefing", [], "Briefing IA complet et ajustement du programme"),
    ("help", ["start"], "Liste des commandes"),
]

BOT_COMMANDS = [BotCommand(name, desc) for name, _, desc in COMMANDS]

HELP_TEXT = """🏃 *Salut, c'est __BOT_NAME__* 🤖
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
/eat — quoi manger maintenant : 3 idées (une rapide, une tranquille, une gourmande) selon ton stock et ton entraînement, la recette quand tu choisis
/stock — ce que tu as en cuisine : les articles cochés sur ta liste y entrent, les plats que tu marques « mangé » ou « cuisiné » en sortent, et je te demande si les produits frais sont encore là quand ils arrivent à échéance
/briefing — briefing complet, ajuste le programme dans l'agenda
/meme — un mème qui résume ta journée d'entraînement 😂
/routes — 2 ou 3 boucles autour du lieu de ta séance du jour, les plus calmes et sûres (carte + fichier GPX) ; /routes travail 8 pour choisir le lieu et la distance
/newroute — une boucle que tu n'as jamais courue
/nutrition — ce que tu as mangé aujourd'hui et cette semaine : calories, protéines, glucides, lipides, et si tu manges assez pour ce que tu dépenses (table Ciqual)

/help — cette aide

Tu peux aussi m'envoyer des *vocaux* 🎙️. Quand je propose un changement, des boutons te permettent de l'appliquer.

_Automatique : débrief et ressenti après chaque run, briefing chaque matin, bilan le dimanche soir, alerte météo (pluie, chaleur, vent, verglas) sur tes séances, « encore là ? » sur les produits frais du stock, des idées de plats avant les repas, « tu as mangé quoi ? » quand je n'ai aucune trace d'un repas, un mot le soir si ta journée manque de quelque chose, et un plat nouveau à découvrir chaque semaine._""".replace("__BOT_NAME__", settings.bot_name)


def parse_days(args: list[str], default: int = AGENDA_DEFAULT_DAYS, maximum: int = AGENDA_MAX_DAYS) -> int:
    """Nombre de jours passé à une commande, borné ; valeur par défaut si absent ou invalide."""
    try:
        days = int(args[0]) if args else default
    except ValueError:
        return default
    return max(1, min(days, maximum))


NO_SUGGESTION = "PAS_DE_SUGGESTION"
DEFAULT_ROUTE_KM = 6.0
KM_IN_TITLE = re.compile(r"(\d+(?:[.,]\d+)?)\s*km", re.I)


def route_request(args: list[str], places: list[tuple[str, float, float]], today_events: list[dict]) -> tuple:
    """/routes [lieu] [km] → (nom du lieu, (lat, lon), km). Par défaut : le lieu et la distance de la séance du jour."""
    by_name = {name.casefold(): (name, lat, lon) for name, lat, lon in places}
    place, km = None, None
    for a in args:
        if a.casefold() in by_name:
            place = by_name[a.casefold()]
        else:
            try:
                km = float(a.replace(",", ".").lower().removesuffix("km"))
            except ValueError:
                pass
    run = next((e for e in today_events if "🏃" in e.get("summary", "") or KM_IN_TITLE.search(e.get("summary", ""))), None)
    if run:
        if place is None and run.get("location", "").casefold() in by_name:
            place = by_name[run["location"].casefold()]
        if km is None and (m := KM_IN_TITLE.search(run.get("summary", ""))):
            km = float(m.group(1).replace(",", "."))
    if place is None and places:
        place = places[0]
    km = min(max(km or DEFAULT_ROUTE_KM, 2.0), 30.0)
    return (place[0], (place[1], place[2]), km) if place else (None, None, km)


def _n(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def format_nutrition(meals: list[dict], runs: list[dict], profile: dict, today: datetime.date) -> str:
    """/nutrition : aujourd'hui, repas par repas, et les 7 derniers jours (calculé depuis Ciqual, sans IA)."""
    from . import nutrition
    t = nutrition.day_totals(meals, today)
    lines = [f"🍽️ *Aujourd'hui* : {_n(t['kcal'])} kcal · 🥩 {t['protein_g']:.0f} g protéines · 🍚 {t['carbs_g']:.0f} g "
             f"glucides · 🧈 {t['fat_g']:.0f} g lipides · 🥦 {t['fiber_g']:.0f} g fibres"]
    needs, running = nutrition.day_needs(profile, runs, today)
    if needs:
        left = needs - t["kcal"]
        lines.append(f"Besoins du jour ~{_n(needs)} kcal" + (f" (dont 🏃 {_n(running)})" if running else "")
                     + (f" → encore ~{_n(left)} kcal à manger 💪" if left > 0 else " → c'est couvert 👌"))
    else:
        lines.append("_Dis-moi ton poids, ta taille, ton année de naissance et ton sexe pour que je calcule tes besoins._")
    for m in [m for m in meals if m["date"] == today.isoformat()]:
        kcal = f" · {_n(m['kcal'])} kcal" if m.get("kcal") is not None else ""
        lines.append(f"- {m['time']} {md_escape(m['text'])}{kcal}")
    days = [today - datetime.timedelta(days=i) for i in range(1, 8)]
    logged = [(d, nutrition.day_totals(meals, d)) for d in days]
    logged = [(d, x) for d, x in logged if x["meals"] >= 2]
    if logged:
        avg = {k: sum(x[k] for _, x in logged) / len(logged) for k in ("kcal", "protein_g", "carbs_g", "fat_g")}
        line = f"\n📅 *7 derniers jours* ({len(logged)} j bien notés) : {_n(avg['kcal'])} kcal/j en moyenne"
        week_needs = [nutrition.day_needs(profile, runs, d)[0] for d, _ in logged]
        if all(week_needs):
            gap = avg["kcal"] - sum(week_needs) / len(week_needs)
            line += f", {'déficit' if gap < 0 else 'surplus'} de ~{_n(abs(gap))} kcal/j"
        if profile.get("weight_kg"):
            w = float(profile["weight_kg"])
            lo, hi = nutrition.PROTEIN_G_PER_KG
            line += f" · protéines {avg['protein_g'] / w:.1f} g/kg (repère {lo:g}–{hi:g})"
        lines.append(line)
    if not any(m["date"] == today.isoformat() for m in meals) and not logged:
        lines.append("\nRien de noté pour l'instant : dis-moi ce que tu manges, je m'occupe du reste 😉")
    lines.append("\n_Valeurs : table Ciqual (ANSES), estimations à partir des portions._")
    return "\n".join(lines)


def shops_open(now: datetime.datetime) -> bool:
    """Encore le temps d'aller acheter quelque chose (SHOP_LAST_CALL, « off » = toujours ; la nuit, jamais)."""
    if not settings.shop_last_call:
        return True
    return datetime.time(7, 0) <= now.time() < datetime.time(*settings.shop_last_call)


def meal_now(now: datetime.datetime) -> str:
    """Le repas qu'on va manger maintenant (ou juste après) : c'est le seul dont on parle."""
    h = now.hour + now.minute / 60
    if 4 <= h < 10.5:
        return "petit-déjeuner"
    if 10.5 <= h < 15:
        return "déjeuner"
    if 15 <= h < 17.5:
        return "goûter"
    return "dîner"


MEAL_WHEN = {"petit-déjeuner": "ce matin", "déjeuner": "ce midi", "goûter": "le goûter", "dîner": "ce soir"}


def eat_prompt(now: datetime.datetime, spontaneous: bool = False) -> str:
    meal = meal_now(now)
    target = (f"Le repas concerné est le {meal} ({MEAL_WHEN[meal]}), celui que je vais manger maintenant : ne propose "
              f"que pour ce repas, jamais pour un repas suivant.\n")
    if spontaneous:   # envoyé par le système juste avant un repas, sans demande de l'athlète
        intro = (
            f"[Suggestion automatique avant le repas] Il est {FR_DAYS[now.weekday()]} {now:%d/%m %H:%M}, je vais bientôt "
            "manger. Si d'après ma mémoire ou l'agenda je ne mange pas chez moi à ce repas (jour de bureau le midi, "
            f"restaurant, déplacement…), réponds uniquement « {NO_SUGGESTION} » et rien d'autre. Sinon, propose-moi "
            "directement quoi manger, sans me poser de question d'abord, en commençant par une courte phrase naturelle "
            f"(« Pour {MEAL_WHEN[meal]}, je te propose : »).\n"
        ) + target
    else:
        intro = f"Que dois-je manger maintenant ? Il est {FR_DAYS[now.weekday()]} {now:%d/%m %H:%M}.\n" + target
    return (
        intro +
        "Déduis-le de mon stock (contexte), de l'heure (petit-déjeuner, déjeuner, collation, dîner), de mon entraînement "
        "(séance qui vient de se terminer ou qui arrive aujourd'hui ou demain matin, charge, ressentis), de mes objectifs et "
        "de ma mémoire (goûts, allergies, rassasiement).\n"
        + ("" if spontaneous else
           "Si une information change vraiment la réponse et que tu ne peux pas la déduire (combien de temps j'ai, si je "
           "mange seul, si un produit frais ancien est encore là…), pose-moi d'abord une ou deux questions courtes et "
           "arrête-toi là. Sinon, ") +
        "propose-moi exactement 3 plats, un par envie, et laisse-moi choisir (ne dis jamais lequel est le meilleur, "
        "sauf si je te le demande) :\n"
        "- express : prêt en 15 min max, zéro prise de tête (une boîte prête du stock passe en premier ici) ;\n"
        "- chill : simple et réconfortant, 20 à 40 min tranquille ;\n"
        "- gourmand : le plat plaisir, un peu plus d'effort.\n"
        "Donne-les dans cet ordre, sans écrire le nom de l'envie ni d'émoji d'envie devant.\n"
        "Si le plat découverte de la semaine (contexte) n'est pas encore goûté, il peut être l'une des 3 options. "
        "Varie par rapport à ce qui revient souvent (contexte : variété des repas). "
        "Pense efficacité : un plat qui se cuisine en plus grande quantité pour remplir mes boîtes libres (contexte) est "
        "un bon choix, en restant varié avec ce qui est déjà en boîtes. "
        + ("Je peux encore passer acheter un ou deux trucs si ça vaut le coup. " if shops_open(now) else
           "Les magasins sont fermés : uniquement des plats faisables avec ce qu'il y a en stock, rien à acheter. ") +
        "Mets un maximum d'émojis pour que je me projette : chaque plat commence par un émoji qui le symbolise, et ses "
        "ingrédients principaux ont le leur. Format : une courte phrase d'intro, puis une ligne par plat, rien d'autre :\n"
        "🌮 *Le plat* · 🥚🥔🧀 · ⏱️ temps (· 🛒 ce qu'il faut acheter, seulement s'il manque quelque chose)\n"
        "Pour chaque plat, cherche d'abord une recette de base avec search_recipe (le nom du plat seulement) : si elle "
        "colle, simplifie-la et adapte-la à mon stock, et finis la recette par « 📖 » + son lien ; garde-la bien simple. "
        "Pas de recette dans ton message : pour chaque plat, appelle propose_meal avec `mood`, `recipe` (ingrédients avec "
        "quantités pour le repas + les boîtes à remplir, 3 à 6 étapes courtes, et combien de boîtes mettre de côté), les "
        "ingrédients du stock qu'il consomme (`uses`) et ses estimations (kcal, protéines, glucides). J'aurai un bouton "
        "par plat : quand je choisis, je reçois sa recette, ses ingrédients sortent du stock et le repas est noté. "
        "Tiens compte de ce que j'ai déjà mangé aujourd'hui (repas notés). Ne modifie pas l'agenda."
    )


MEME_PROMPT = (
    "[/meme] Résume ma journée d'entraînement en un GIF ou un mème drôle. Regarde ce qui s'est passé aujourd'hui : "
    "runs du jour (Dreeve), agenda, ressentis, repas, météo. Trouve l'angle drôle et bienveillant (sortie sous la "
    "pluie, jambes en bois, repos assumé, gros repas, séance sautée…), appelle send_gif avec 2 à 4 mots-clés en "
    "anglais qui collent pile à ma journée, et écris UNE seule ligne drôle pour l'accompagner, comme un pote. "
    "Ne modifie rien."
)

NUTRITION_CHECK_PROMPT = (
    "[Contrôle nutrition du soir] Le système a relevé ceci dans ma journée : {issues}.\n"
    "Écris-moi comme un pote qui me soutient : 1 ou 2 messages très courts, bienveillants, sans culpabiliser ni faire "
    "la leçon. Le point le plus important seulement, et une idée concrète et simple pour faire mieux demain (petit-déj, "
    "collation…), si possible avec ce que j'ai en stock. Garde en tête le long terme : c'est la tendance qui compte. "
    "Si un repas n'a « aucune trace », demande-moi simplement ce que j'ai mangé plutôt que de supposer que je l'ai "
    "sauté. Ne modifie rien."
)

NEW_DISH_PROMPT = (
    "[Plat découverte de la semaine] Regarde la variété de mes repas (contexte) et propose-moi UN plat nouveau à "
    "découvrir cette semaine : que je n'ai jamais mangé (journal), jamais proposé en découverte avant (contexte), "
    "simple (40 min max), de saison et qui colle à mes objectifs. Choisis-le pour casser ce qui revient le plus "
    "souvent. Cherche une recette de base avec search_recipe, puis appelle propose_meal avec discovery: true, `mood`, "
    "`recipe` (finie par « 📖 » + le lien si tu t'en sers), `uses` et tes estimations. "
    "Message : 1 ou 2 lignes, comme un pote qui te fait découvrir un truc, avec l'émoji du plat. Ne modifie pas "
    "l'agenda."
)


def recipes_prompt(lst: dict) -> str:
    """Recettes tirées de la liste de courses en cours."""
    items = "\n".join(f"- {i['name']}" + (f" ({i['qty']})" if i.get("qty") else "") for i in lst["items"])
    covers = ", ".join(x for x in (lst.get("days") and f"{lst['days']} jours", lst.get("coverage")) if x) or "la période prévue"
    return (
        f"Propose les recettes de ma liste de courses actuelle (elle couvre : {covers}) :\n{items}\n\n"
        "Contraintes : faciles (30 min actives max), efficaces (cuisiner en batch pour remplir mes boîtes libres, voir "
        "contexte : jamais plus de boîtes que celles qui sont libres à chaque session, donc des sessions courtes et "
        "régulières plutôt qu'une grosse), variées (pas le même plat plus de 2 repas d'affilée, protéines et féculents "
        "différents d'une boîte à l'autre), adaptées aux boîtes "
        "(se conservent 3-4 jours au frigo et se réchauffent bien, ou se congèlent), avec surtout les articles de la liste "
        "(+ basiques du placard : sel, poivre, épices, huile). Relie-les à mes séances et objectifs : plus de glucides la "
        "veille ou après les séances dures ou longues, protéines pour la récupération, et respecte ma mémoire (goûts, "
        "allergies, rassasiement).\n"
        "Format Telegram concis :\n"
        "- 🗓️ *Plan de batch cooking* : quoi cuisiner quand, combien de boîtes à chaque session (dans la limite de mes "
        "boîtes) et quelle boîte pour quel repas.\n"
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
        "se conservent 3-4 jours au frigo, se réchauffent bien, ou se congèlent), variées, dimensionnées pour mes "
        "boîtes (contexte : nombre total et libres), plus petits-déjeuners et collations "
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
