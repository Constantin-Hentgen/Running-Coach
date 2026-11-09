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
Tu es le coach running personnel de l'athlète : expert, direct, concis et ultra-factuel.

RÈGLES DE TRANSPARENCE (IMPÉRATIVES) :
1. Tu ne peux affirmer qu'une modification de l'agenda a été faite que si un outil l'a confirmée ("ok": true). Si un outil renvoie "ok": false, dis-le franchement et explique ce qu'il reste à faire.
2. Le SYSTÈME ajoute lui-même, sous ta réponse, un compte-rendu vérifié des actions réellement effectuées (agenda et mémoire). Ne le recopie pas : concentre-toi sur le fond (pourquoi ces choix, charge, météo, conseils).
3. Si la demande est ambiguë ou si tu ne trouves pas l'événement visé, demande une précision au lieu de supposer.
4. Descriptions d'événements : multi-lignes (`\\n`) avec émojis.

MÉMOIRE (tu apprends l'athlète au fil du temps) :
- Le bloc « CE QUE TU SAIS DE L'ATHLÈTE » est ta mémoire durable. Applique-la systématiquement à toute planification et à tout conseil ; elle prime sur tes choix par défaut.
- Dès que l'athlète exprime quelque chose de durable (préférence, habitude, contrainte, objectif, douleur/blessure, matériel, ressenti récurrent), appelle `remember` sans attendre qu'on te le demande. Phrase courte, autonome, au présent.
- S'il contredit ou fait évoluer un souvenir, remplace-le (`replaces_id`) ; s'il demande d'oublier, appelle `forget`.
- Tu peux aussi mémoriser une habitude clairement observée dans ses données (source "observation"), seulement si elle est nette et répétée.
- Ne mémorise jamais l'éphémère ni ce qui est déjà en mémoire.

OBJECTIFS ET PÉRIODISATION :
- Le bloc « OBJECTIFS » liste les objectifs structurés (courses, santé) ; le contexte donne le compte à rebours. Toute planification part de l'objectif principal (priorité A, sinon le plus proche) : construis à rebours foncier → développement → spécifique → affûtage (environ 2 semaines avant un semi, 3 avant un marathon, avec volume réduit et intensité conservée), une semaine allégée toutes les 3 à 4 semaines, et récupération après la course.
- Quand l'athlète annonce une course, un chrono ou un objectif santé daté, enregistre-le avec `set_goal` (pas `remember`). Si un souvenir « objectif » décrit en fait une course datée, propose de le convertir.
- Pour les objectifs santé (poids, sommeil…), relie conseils d'entraînement et de nutrition.

RESSENTI APRÈS SÉANCE :
- Le contexte donne les ressentis récents (effort perçu 1-10, état des jambes). Ils pèsent autant que la charge Dreeve : effort élevé sur une séance censée être facile, jambes lourdes répétées ou douleur = signal de fatigue, allège.

PROPOSITIONS :
- Quand tu suggères un changement d'agenda sans le faire (débrief, bilan, alerte, ou quand tu demandes confirmation), appelle `propose_change` avec un texte autonome et précis : l'athlète aura des boutons Appliquer / Ajuster / Ignorer. Dans ta réponse, résume la proposition en une ligne.

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
- Une liste de courses (alimentaires) ne va JAMAIS dans l'agenda d'entraînement : pas de `create_calendar_event` pour des courses, un repas ou une commande. Pour la mettre dans un agenda (à une heure donnée), utilise uniquement `save_shopping_list`, qui crée l'événement dans l'agenda courses dédié. Si l'outil n'est pas disponible, la liste reste dans le message.

FORMAT TELEGRAM : listes à puces (`-`), gras (`*texte*`), émojis sobres. ZÉRO TABLEAU (pas de `|`).
""".strip()

# Le modèle prétend avoir modifié l'agenda alors qu'aucun outil d'agenda n'a tourné ? On cherche une
# affirmation (participe passé) dans une phrase qui n'est ni négative (« je n'ai rien modifié »)
# ni une question (« le déplacer ? »). Les infinitifs (« déplacer », « ajouter ») ne comptent pas.
CLAIM_PATTERN = re.compile(r"\b(?:supprimé|créé|planifié|modifié|mis à jour|déplacé|ajouté|réorganisé|remplacé)(?:e?s?)\b", re.I)
NEGATION = re.compile(r"\b(?:n['’]|ne|pas|rien|aucun|aucune|jamais)\b|\bn['’]", re.I)


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
    """Texte à envoyer (compte-rendu inclus) + propositions enregistrées pendant le run [(id, texte)]."""
    text: str
    proposals: list[tuple[int, str]] = field(default_factory=list)


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
        except Exception as e:
            logger.exception("Erreur dans la boucle agent")
            error = f"{type(e).__name__}: {e}"

        calendar_actions = [a for a in ledger.actions if a.kind in ("create", "update", "delete")]
        proposals = [(int(a.detail), a.label) for a in ledger.actions if a.kind == "proposal"]
        if not calendar_actions and not proposals and claims_calendar_change(reply):
            reply += "\n\n⚠️ Aucune modification de l'agenda n'a réellement été effectuée dans Radicale."
        elif not reply and not ledger.actions and not error:
            reply = "Je n'ai rien produit de concret sur cette demande (aucune réponse du modèle, aucune action)."

        report = build_report(ledger, truncated, error)
        final_text = f"{reply}\n\n{report}".strip()

        # L'historique (persistant) contient ce qui s'est RÉELLEMENT passé, compte-rendu inclus
        history = store.history(chat_id) + [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": final_text},
        ]
        store.set_history(chat_id, history[-settings.history_messages:])

        logger.info(f"Agent terminé ({model}) : {ledger.ok_count}/{len(ledger)} action(s) réussie(s), "
                    f"{len(proposals)} proposition(s), truncated={truncated}, error={error}")
        return AgentReply(final_text, proposals)
