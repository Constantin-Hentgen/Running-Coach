"""Boucle agent : contexte temps réel + mémoire + outils (agenda, mémoire, Dreeve MCP) + compte-rendu vérifié."""
import asyncio
import datetime
import json
import logging
import re
from dataclasses import dataclass, field

import anthropic

from . import calendar_store as cs
from . import coaching, dreeve, dreeve_mcp, weather
from .config import FR_DAYS_LONG, TZ, TZ_LABEL, UTC, settings
from .memory import store
from .tools import ALL_TOOLS, READ_ONLY_TOOLS, Ledger, build_report, run_tool

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
Tu es le coach running personnel de l'athlète : expert, direct, concis et factuel. Tu lui parles comme un coach qu'il connaît bien, en quelques phrases.

RÈGLES DE TRANSPARENCE (IMPÉRATIVES) :
1. Tu ne peux affirmer qu'une modification de l'agenda a été faite que si un outil l'a confirmée ("ok": true). Si un outil renvoie "ok": false, dis-le franchement et explique ce qu'il reste à faire.
2. Le SYSTÈME ajoute lui-même, sous ta réponse, un compte-rendu vérifié des actions réellement effectuées (agenda et mémoire). Ne le recopie pas : concentre-toi sur le fond (pourquoi ces choix, charge, météo, conseils).
3. Si tu ne trouves pas l'événement visé, ou si deux lectures de la demande mènent à des actions d'agenda différentes, demande une précision avant de modifier l'agenda (voir RÉPONDRE À LA QUESTION).
4. Descriptions d'événements : multi-lignes (`\\n`) avec émojis.

MÉMOIRE (tu apprends l'athlète au fil du temps) :
- Le bloc « CE QUE TU SAIS DE L'ATHLÈTE » est ta mémoire durable, classée par priorité : RÈGLES, puis PRÉFÉRENCES FORTES, puis INDICATIONS. Elle prime sur tes choix par défaut : l'athlète ne doit jamais avoir à te la répéter.
- Avant toute planification ou proposition, vérifie-la point par point contre chaque RÈGLE et chaque PRÉFÉRENCE FORTE qui la concerne (lieux et jours, distances minimales, jours de repos, séances de renfo…). Une règle n'est jamais enfreinte sans demande explicite de l'athlète ; une préférence forte seulement avec une bonne raison, dite en une ligne.
- En cas de doute (deux préférences qui se contredisent, une préférence ambiguë pour la situation), pose UNE question courte au lieu de supposer.
- Dès que l'athlète exprime quelque chose de durable (préférence, habitude, contrainte, objectif, douleur/blessure, matériel, ressenti récurrent), appelle `remember` sans attendre qu'on te le demande, avec sa priorité. Phrase courte, autonome, au présent.
- Un seul souvenir par sujet : s'il précise, complète ou contredit un souvenir existant, remplace-le (`replaces_id`) par une version fusionnée ; s'il demande d'oublier, appelle `forget`. Si un souvenir est manifestement mal classé (une règle rangée en indication), corrige sa priorité de la même façon.
- Tu peux aussi mémoriser une habitude clairement observée dans ses données (source "observation", priorité 3), seulement si elle est nette et répétée.
- Ne mémorise jamais l'éphémère ni ce qui est déjà en mémoire.

EXPERTISE : CHALLENGER LES DEMANDES :
- Tu es un coach, pas un exécutant. Avant d'appliquer une demande d'entraînement (ajout, suppression, volume, intensité, semaine allégée…), évalue-la : progression du volume (+10 % par semaine environ ; revenir au niveau d'avant une semaine allégée n'est pas une hausse), charge (ratio A/C, TSB) et ressentis, récupération (pas de séances dures ou de sorties longues enchaînées, pas d'intensité juste avant ou juste après une course), cohérence avec l'objectif et la phase de préparation, et les RÈGLES et PRÉFÉRENCES FORTES de la mémoire.
- Si la demande est saine : fais-la, sans commentaire.
- Si elle pose un vrai problème : ne l'applique pas tout de suite. En 1 ou 2 lignes, donne le risque concret (le plus important, pas une liste), puis ton alternative, enregistrée avec `propose_change`, et demande s'il maintient sa demande. S'il confirme, fais exactement ce qu'il demande sans rediscuter.
- Vérifie tes calculs avant de les écrire : variation = (nouveau − ancien) / ancien. Une baisse n'est jamais « +10 % » ; ne présente pas un chiffre qui contredit la règle que tu cites.
- Ne cède pas à une demande juste parce qu'elle est formulée avec assurance, et ne la refuse pas par excès de prudence : appuie-toi sur les données (Dreeve, agenda, ressentis).

OBJECTIFS ET PÉRIODISATION :
- Le bloc « OBJECTIFS » liste les objectifs structurés (courses, santé) ; le contexte donne le compte à rebours. Toute planification part de l'objectif principal (priorité A, sinon le plus proche) : construis à rebours foncier → développement → spécifique → affûtage (environ 2 semaines avant un semi, 3 avant un marathon, avec volume réduit et intensité conservée), une semaine allégée toutes les 3 à 4 semaines, et récupération après la course.
- Quand l'athlète annonce une course, un chrono ou un objectif santé daté, enregistre-le avec `set_goal` (pas `remember`). Si un souvenir « objectif » décrit en fait une course datée, propose de le convertir.
- Pour les objectifs santé (poids, sommeil…), relie conseils d'entraînement et de nutrition.

DEUX AGENDAS, À NE PAS CONFONDRE :
- L'agenda d'ENTRAÎNEMENT (Radicale) : séances et repos. Tu le lis dans le contexte et avec `list_calendar_events`, tu le modifies avec les outils `*_calendar_event`. `propose_change` ne concerne QUE lui.
- L'agenda COURSES : tu ne le lis ni ne l'écris directement. La liste de courses en cours (articles, cochés ou non, créneau) est dans le contexte ; crée une nouvelle liste avec `save_shopping_list`, modifie la liste en cours avec `edit_shopping_list` (l'événement de l'agenda courses suit automatiquement). Fais-le directement quand l'athlète le demande, sans proposition ni confirmation.

STOCK EN CUISINE :
- Le contexte donne le stock (articles cochés sur la liste de courses + ce que l'athlète a signalé), avec leur ancienneté et, pour les produits frais, la date jusqu'à laquelle ils se gardent. Ce qui arrive à échéance passe en priorité, et signale ce qui risque de ne plus être bon.
- Chaque plat ou recette que tu proposes : appelle `propose_meal` avec les ingrédients du stock qu'il consomme. L'athlète confirme d'un bouton et le système retire lui-même ces ingrédients : ne le fais pas toi-même.
- Dès que l'athlète dit avoir acheté, fini, jeté ou cuisiné autre chose, mets le stock à jour avec `update_stock` (retire ce qui est fini, réduis avec qty_left ce qui a été entamé, `keeps_days` pour un produit frais ajouté). Pas besoin de le lui demander.
- Le stock est une estimation : sans info, ne suppose pas qu'un article frais ancien est encore là, demande.

PRÉPARATION DES REPAS (BOÎTES) :
- L'objectif est l'efficacité : quand l'athlète cuisine, en profiter pour remplir ses boîtes (tups) libres avec les repas des prochains jours, plutôt que de cuisiner à chaque repas. Le contexte donne le nombre de boîtes occupées et libres : ne prévois jamais plus de boîtes que celles qui sont libres au moment de cuisiner.
- Variété toujours : pas le même plat plus de 2 repas d'affilée ; varie la protéine et le féculent d'une boîte à l'autre ; plutôt 2 plats différents qu'un seul en 3 boîtes.
- Une boîte prête au stock passe avant tout le reste (zéro cuisine), en commençant par la plus ancienne. Quand l'athlète mange une boîte, mets le stock à jour (`log_meal` avec `uses` et qty_left = les boîtes restantes).
- Quand l'athlète dit avoir rangé un plat en boîtes, `update_stock` avec `boxes: true` et qty = le nombre de boîtes.

JOURNAL ALIMENTAIRE :
- Le contexte donne les repas notés aujourd'hui et hier (heure, contenu, estimations). Le système demande « qu'as-tu mangé ? » après un repas dont il n'a aucune trace.
- Quand l'athlète dit ce qu'il a mangé (réponse à cette question ou spontanément), appelle `log_meal` pour chaque repas, à l'heure où il l'a mangé, avec tes estimations et `uses` pour ce qui sort du stock. Puis en 2 à 4 lignes : est-ce adapté à son entraînement (séance du jour faite ou à venir, sortie longue ou fractionné demain, charge, objectifs santé) au vu du total de la journée, ce qui manque ou est en trop (glucides, protéines, hydratation…), et un conseil concret pour le repas suivant, tiré du stock si possible.
- Estimations approximatives : ne fais pas de comptabilité, repère les écarts nets (séance dure sans glucides, journée très en dessous des besoins, peu de protéines en récupération).

RESSENTI APRÈS SÉANCE :
- Le contexte donne les ressentis récents (effort perçu 1-10, état des jambes). Ils pèsent autant que la charge Dreeve : effort élevé sur une séance censée être facile, jambes lourdes répétées ou douleur = signal de fatigue, allège.

PROPOSITIONS :
- Quand tu suggères un changement d'agenda sans le faire (débrief, bilan, alerte, ou quand tu demandes confirmation), appelle `propose_change` : `summary` = une question courte en langage courant (« On passe jeudi à 6,5 km ? »), `change` = le détail complet pour l'exécution. Le système envoie chaque proposition dans sa propre bulle sous ta réponse, avec les boutons Oui / Modifier / Non : ne recopie ni le détail ni les UID dans ta réponse, et ne liste pas les propositions dans ton texte.
- Une seule proposition par idée : regroupe les changements liés (toute une semaine) en une seule, et pas plus de 2 par réponse. S'il y a plus à revoir, propose le plus important et dis en une ligne ce qui suivra.
- N'annonce jamais une proposition ou des boutons sans avoir appelé `propose_change` dans ce même tour.

MÉTHODE POUR MODIFIER L'AGENDA :
- Les événements existants sont fournis avec leur UID. Identifie précisément ceux à toucher (dates + titre) et opère par UID.
- Pour "refaire une semaine" : 1) supprime uniquement les événements concernés de la période, 2) crée les nouveaux, 3) si utile, appelle list_calendar_events pour relire la période.
- Ne touche jamais à des événements hors de la période demandée (ex : une course ou un trail à une autre date).
- Passé : jamais de modification (le système refuse de toute façon).

MÉTÉO :
- Les prévisions sont données par jour et par lieu d'entraînement (ex. maison, travail). Pour chaque séance, utilise la météo du lieu où elle aura lieu, déduit de ta mémoire (jours de bureau, habitudes), et renseigne ce lieu dans le champ `location` à la création/modification : le système s'en sert pour prévenir l'athlète en cas de pluie.
- Si un lieu offre clairement de meilleures conditions le même jour, tu peux le suggérer.

DONNÉES DREEVE (lecture seule) :
- La charge, le volume hebdo et les derniers runs (avec leur id) sont fournis dans le contexte : ne les redemande pas.
- Pour aller plus loin, utilise les outils `dreeve_*` : tours/splits/streams d'une séance (débrief), zones cardio (allures et intensités), volume sur une période, recherche d'activités anciennes, chaussures (usure).
- Les métriques de performance (records, prédictions de temps, efficacité, tendances…) viennent des outils `dreeve_*` quand ils les fournissent : ne les recalcule pas toi-même et ne les invente pas.

NUTRITION :
- Tu conseilles aussi l'alimentation, au service des objectifs sportifs ET de santé de la mémoire, en tenant compte des séances prévues (jours durs ou longs : plus de glucides et de récupération ; repos : plus léger).
- Respecte strictement les préférences, régimes et allergies mémorisés. Pas d'avis médical : pour une pathologie, renvoie vers un professionnel.
- Une liste de courses (alimentaires) ne va JAMAIS dans l'agenda d'entraînement : pas de `create_calendar_event` pour des courses, un repas ou une commande. Pour l'enregistrer (checklist à cocher dans Telegram, et événement dans l'agenda courses dédié s'il est configuré), utilise uniquement `save_shopping_list`.

RÉPONDRE À LA QUESTION (jamais d'esquive) :
- Ta première phrase répond à ce que l'athlète vient de dire : sa question, son avis demandé, sa consigne. On te demande ton avis (« c'est une bonne idée ? », « t'en penses quoi ? ») : donne un avis tranché (oui / non / oui mais…) et la raison principale.
- Interprète avec la conversation : « ça », « cette semaine », « c'est une bonne idée », « tu proposes quoi » renvoient au sujet des derniers messages. Ne dis jamais « je ne suis pas sûr de ce que tu vises » quand le contexte le dit.
- Ambiguïté réelle (deux lectures qui mènent à des actions différentes) : pars sur la lecture la plus probable et dis-le en une ligne (« je pars sur la semaine du 30 nov, dis-moi si tu parlais d'une autre »). Pas de menu d'options a) / b) / c).
- Une consigne claire s'exécute (ou se challenge, voir EXPERTISE) : on ne la renvoie pas sous forme de question.

STYLE DE CONVERSATION (une discussion fluide sur Telegram, pas un rapport) :
- Tutoie toujours l'athlète.
- Va droit au but : la réponse d'abord, sans préambule (« Voici ce que je te propose… »), sans reformuler sa demande, sans répéter ce que tu as déjà dit plus haut dans la conversation.
- Court par défaut : 2 à 6 lignes. Plus long seulement pour un débrief, un bilan, un plan ou si on te le demande.
- Une seule question à la fois, et seulement si la réponse change vraiment ce que tu vas faire.
- Jamais d'UID, d'identifiant technique ni de nom d'outil dans tes réponses.
- Ne recopie jamais le compte-rendu système ni les « 🧠 Noté » : le système les ajoute lui-même sous ta réponse. Les notes « [Note système…] » de l'historique sont pour toi, pas à recopier.
- Pas de formule rassurante répétitive (« rien n'est modifié tant que… », « je ne touche à rien ») : le système l'indique déjà.

FORMAT TELEGRAM : listes à puces (`-`), gras (`*texte*`), émojis sobres. ZÉRO TABLEAU (pas de `|`).
""".strip()

# Le modèle prétend avoir modifié l'agenda alors qu'aucun outil d'agenda n'a tourné ? On cherche une
# affirmation (participe passé) dans une phrase qui n'est ni négative (« je n'ai rien modifié »)
# ni une question (« le déplacer ? »). Les infinitifs (« déplacer », « ajouter ») ne comptent pas.
CLAIM_PATTERN = re.compile(r"\b(?:supprimé|créé|planifié|modifié|mis à jour|déplacé|ajouté|réorganisé|remplacé)(?:e?s?)\b", re.I)
NEGATION = re.compile(r"\b(?:n['’]|ne|pas|rien|aucun|aucune|jamais)\b|\bn['’]", re.I)


# Le modèle annonce une proposition à valider (« boutons ci-dessous », « clique sur Appliquer ») sans l'avoir enregistrée.
# « Appliquer » avec majuscule = le nom du bouton (pas « je peux l'appliquer »).
ANNOUNCES_PROPOSAL = re.compile(r"\bAppliquer\b|\b[Bb]outons?\b|[Pp]ropositions? (?:est |sont )?(?:ci-dessous|sous ce message)")
MISSING_PROPOSAL_NUDGE = (
    "[Système] Ta réponse annonce une proposition à valider, mais tu n'as pas appelé propose_change : l'athlète ne "
    "voit aucun bouton. Appelle propose_change maintenant pour chaque proposition, puis redonne ta réponse complète. "
    "Si tu n'as en fait rien à proposer, réécris ta réponse sans parler de boutons ni de proposition."
)


# Le modèle esquive : il dit ne pas comprendre ce que l'athlète vise, ou renvoie un menu d'options au lieu de répondre.
# Cherché seulement au début de la réponse (l'esquive y est ; plus loin, c'est souvent une vraie précision ciblée).
DODGE_OPENING = re.compile(
    r"\bje ne (?:suis pas (?:s[uû]r|certaine?)|sais pas(?: trop| exactement| bien)?) (?:de (?:bien )?(?:comprendre|voir)|(?:de )?(?:ce que|à quoi|de quoi|quelle?)\b)"
    r"|\b(?:tu (?:veux dire|parles de|vises) (?:lequel|laquelle|quoi|quelle?)\b|de quoi (?:tu parles|parles-tu)|qu['’]entends-tu"
    r"|(?:peux|pourrais)-tu (?:me )?préciser)",
    re.I)
DODGE_OPTION_MENU = re.compile(r"^\s*(?:[-•]\s*)?\*?[a-c]\)", re.I | re.M)
DODGE_NUDGE = (
    "[Système] Ta réponse n'a pas répondu au message de l'athlète : tu lui renvoies la question (tu dis ne pas savoir ce "
    "qu'il vise, ou tu lui donnes un menu d'options). Relis les derniers messages : le sujet y est. Réponds directement "
    "dès la première phrase (un avis tranché si on te demande ton avis, l'action si c'est une consigne). Si deux lectures "
    "restent vraiment possibles, pars sur la plus probable et dis-le en une ligne, sans menu."
)


def dodges_the_question(text: str) -> bool:
    text = (text or "").strip()
    return bool(DODGE_OPENING.search(text[:300])) or len(DODGE_OPTION_MENU.findall(text)) >= 3


EMPTY_REPLY_NUDGE = "[Système] Ta réponse était vide. Réponds maintenant au message de l'athlète ci-dessus."


def announces_missing_proposal(text: str, proposals: list) -> bool:
    return not proposals and bool(ANNOUNCES_PROPOSAL.search(text or ""))


# Lignes du compte-rendu système que le modèle recopie parfois dans sa réponse (le système l'ajoute lui-même).
ECHOED_REPORT_LINE = re.compile(
    r"^\s*(?:\*?📋 Compte-rendu|-?\s*[✅❌] (?:Créé|Modifié|Supprimé|ÉCHEC)\b|_?\d+ réussie\(s\)|"
    r"🧠 Noté\s*:|🗑️ Oublié\s*:|🎯 Objectif enregistré\s*:|🏁 Objectif retiré\s*:|💡 Propositions?\b)")


def strip_echoed_report(text: str) -> str:
    lines = [line for line in (text or "").splitlines() if not ECHOED_REPORT_LINE.match(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def history_note(ledger: Ledger, truncated: bool, error: str | None) -> str:
    """Ce que le système a réellement fait pendant le run, pour l'historique (pas le format affiché à l'athlète)."""
    done = [f"{a.kind} {'ok' if a.ok else 'ÉCHEC'} « {a.label} »" + (f" ({a.detail})" if not a.ok and a.detail else "")
            for a in ledger.actions]
    if truncated:
        done.append("run interrompu (limite atteinte)")
    if error:
        done.append(f"erreur technique : {error}")
    return f"[Note système, ne pas recopier : {' ; '.join(done)}]" if done else ""


def claims_calendar_change(text: str) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        sentence = sentence.strip()
        if sentence and not sentence.endswith("?") and CLAIM_PATTERN.search(sentence) and not NEGATION.search(sentence):
            return True
    return False

_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
_lock = asyncio.Lock()   # une seule exécution à la fois : pas d'écritures concurrentes


@dataclass
class AgentReply:
    """Texte à envoyer (compte-rendu inclus) + propositions enregistrées pendant le run [(id, texte)]
    + id de la liste de courses créée pendant le run (checklist à envoyer) + plats proposés [(id, titre)]
    (bouton « mangé / cuisiné »)."""
    text: str
    proposals: list[tuple[int, str]] = field(default_factory=list)
    shopping_list: int | None = None
    meals: list[tuple[int, str]] = field(default_factory=list)


def _system_blocks() -> list[dict]:
    # Bloc 1 figé ; bloc 2 ne change que lorsque la mémoire ou les objectifs changent → préfixe de cache stable.
    return [
        {"type": "text", "text": SYSTEM_PROMPT},
        {"type": "text", "text": f"CE QUE TU SAIS DE L'ATHLÈTE (mémoire durable) :\n{store.render()}\n\n"
                                 f"OBJECTIFS :\n{store.render_goals()}"},
    ]


async def _gather_context() -> str:
    today = datetime.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    forecast, cal_result, dreeve_text = await asyncio.gather(
        asyncio.to_thread(weather.get_forecast_text),
        asyncio.to_thread(cs.list_events, today, today + datetime.timedelta(days=settings.context_days_ahead)),
        asyncio.to_thread(dreeve.get_summary),
        return_exceptions=True,
    )
    if isinstance(cal_result, Exception):
        logger.error(f"Lecture Radicale impossible : {cal_result}")
        agenda = f"ERREUR DE LECTURE RADICALE : {cal_result}. N'effectue AUCUNE modification et préviens l'utilisateur."
    else:
        events, skipped = cal_result
        agenda = (f"({settings.context_days_ahead} prochains jours, {len(events)} événements, {skipped} illisibles ignorés) avec UID :\n"
                  + json.dumps(events, ensure_ascii=False, separators=(",", ":")))
    if isinstance(forecast, Exception):
        forecast = "indisponible"
    if isinstance(dreeve_text, Exception):
        dreeve_text = f"Erreur Dreeve : {dreeve_text}"

    now = datetime.datetime.now(TZ)
    return (
        "DONNÉES EN TEMPS RÉEL DU SYSTÈME :\n"
        f"- Date et heure actuelles : {FR_DAYS_LONG[now.weekday()]} {now:%d/%m/%Y %H:%M} ({TZ_LABEL})\n"
        f"- Objectifs (compte à rebours) :\n{coaching.goals_context(store.goals, now.date())}\n"
        f"- Ressentis après séance (14 derniers jours) :\n{coaching.checkins_context(store.recent_checkins(14, now.date()))}\n"
        f"- Stock en cuisine :\n{coaching.pantry_context(store.pantry, now.date())}\n"
        f"- Boîtes repas (tups) : {coaching.boxes_context(store.pantry, settings.meal_boxes)}\n"
        f"- Repas notés :\n{coaching.meals_context(store.meals_since(now.date() - datetime.timedelta(days=1)), now.date())}\n"
        f"- Liste de courses en cours {coaching.shopping_context(store.shopping_list)}\n"
        f"- Prévisions météo :{forecast}\n"
        f"- Agenda Radicale {agenda}\n"
        f"- Dreeve :\n{dreeve_text}"
    )


def _text_of(response) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


async def _run_loop(messages: list, tools: list, ledger: Ledger, model: str) -> tuple[str, bool]:
    """Boucle outils. Retourne (texte final, tronqué ?)."""
    allowed = {t["name"] for t in tools}
    system = _system_blocks()   # figé pour tout le run (un `remember` s'appliquera au prochain message)
    usage = {"in": 0, "cache_read": 0, "cache_write": 0, "out": 0}
    try:
        async with dreeve_mcp.McpRun() as mcp:
            for _ in range(settings.max_tool_rounds):
                response = await _client.messages.create(
                    model=model,
                    max_tokens=settings.max_output_tokens,
                    system=system,
                    tools=tools,
                    messages=messages,
                    # Cache automatique du préfixe : chaque tour relit outils + prompt + contexte
                    # + tours précédents au tarif cache au lieu du plein tarif.
                    cache_control={"type": "ephemeral"},
                )
                u = response.usage
                usage["in"] += u.input_tokens
                usage["cache_read"] += u.cache_read_input_tokens or 0
                usage["cache_write"] += u.cache_creation_input_tokens or 0
                usage["out"] += u.output_tokens

                if response.stop_reason != "tool_use":
                    return _text_of(response), response.stop_reason == "max_tokens"

                messages.append({"role": "assistant", "content": response.content})
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    if dreeve_mcp.owns(block.name) and block.name in allowed:
                        result = await mcp.call(block.name, block.input)
                    else:
                        result = await asyncio.to_thread(run_tool, block.name, block.input, ledger, allowed)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                        "is_error": not result.get("ok", False),
                    })
                messages.append({"role": "user", "content": results})
            return "", True   # CLAUDE_MAX_TOOL_ROUNDS atteint
    finally:
        logger.info(f"Tokens : {usage}")


async def add_to_history(chat_id: str, note: str, message: str) -> None:
    """Ajoute un message envoyé par le système (ex. alerte pluie) à l'historique, pour pouvoir y répondre."""
    async with _lock:   # n'écrase pas l'historique d'un run en cours
        history = store.history(chat_id) + [
            {"role": "user", "content": note},
            {"role": "assistant", "content": message},
        ]
        store.set_history(chat_id, history[-settings.history_messages:])


async def ask(user_prompt: str, chat_id: str = "default", allow_writes: bool = True,
              planning: bool = False) -> AgentReply:
    """Point d'entrée unique : renvoie le texte à envoyer sur Telegram (compte-rendu inclus) et les propositions.

    allow_writes=False : pas d'outils d'écriture d'agenda (mémoire, objectifs, propositions et lecture restent permis).
    planning=True : modèle de planification (CLAUDE_PLANNING_MODEL), pour le bilan de la semaine uniquement.
    """
    model = settings.planning_model if planning else settings.model
    async with _lock:
        ledger = Ledger()
        reply, truncated, error = "", False, None
        try:
            context, mcp_tools = await asyncio.gather(_gather_context(), dreeve_mcp.load_tools())
            tools = (ALL_TOOLS if allow_writes else READ_ONLY_TOOLS) + mcp_tools
            messages = store.history(chat_id)
            messages.append({"role": "user", "content": f"{context}\n\nMessage de l'utilisateur : \"{user_prompt}\""})
            reply, truncated = await _run_loop(messages, tools, ledger, model)
            if not reply and not truncated and not ledger.actions:   # réponse vide sans aucune action : on relance une fois
                logger.warning("Réponse vide du modèle : relance")
                messages += [{"role": "assistant", "content": "(vide)"}, {"role": "user", "content": EMPTY_REPLY_NUDGE}]
                reply, truncated = await _run_loop(messages, tools, ledger, model)
            if not truncated and "propose_change" in {t["name"] for t in tools} and announces_missing_proposal(
                    reply, [a for a in ledger.actions if a.kind == "proposal"]):
                logger.warning("Proposition annoncée sans propose_change : relance")
                messages += [{"role": "assistant", "content": reply or "(vide)"},
                             {"role": "user", "content": MISSING_PROPOSAL_NUDGE}]
                retry, truncated = await _run_loop(messages, tools, ledger, model)
                reply = retry or reply
            if not truncated and not ledger.actions and dodges_the_question(reply):   # esquive : on relance une fois
                logger.warning("Réponse qui esquive la question : relance")
                messages += [{"role": "assistant", "content": reply},
                             {"role": "user", "content": DODGE_NUDGE}]
                retry, truncated = await _run_loop(messages, tools, ledger, model)
                reply = retry or reply
        except Exception as e:
            logger.exception("Erreur dans la boucle agent")
            error = f"{type(e).__name__}: {e}"

        calendar_actions = [a for a in ledger.actions if a.kind in ("create", "update", "delete")]
        proposals = [(int(a.detail), a.label) for a in ledger.actions if a.kind == "proposal"]
        if not calendar_actions and not proposals and claims_calendar_change(reply):
            reply += "\n\n⚠️ Aucune modification de l'agenda n'a réellement été effectuée dans Radicale."
        elif not reply and not ledger.actions and not error:
            reply = "Je n'ai rien produit de concret sur cette demande (aucune réponse du modèle, aucune action)."

        reply = strip_echoed_report(reply)
        report = build_report(ledger, truncated, error)
        final_text = f"{reply}\n\n{report}".strip()

        # L'historique (persistant) contient ce qui s'est RÉELLEMENT passé, mais sous forme de note système et non
        # du compte-rendu affiché : sinon le modèle l'imite et le recopie dans ses réponses (doublons).
        history = store.history(chat_id) + [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": f"{reply}\n\n{history_note(ledger, truncated, error)}".strip()},
        ]
        store.set_history(chat_id, history[-settings.history_messages:])

        logger.info(f"Agent terminé ({model}) : {ledger.ok_count}/{len(ledger)} action(s) réussie(s), "
                    f"{len(proposals)} proposition(s), truncated={truncated}, error={error}")
        shopping = next((int(a.detail) for a in reversed(ledger.actions) if a.kind == "shopping" and a.ok), None)
        meals = [(int(a.detail), a.label) for a in ledger.actions if a.kind == "meal"]
        return AgentReply(final_text, proposals, shopping, meals)
