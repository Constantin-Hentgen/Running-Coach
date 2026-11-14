"""Outils MCP de Dreeve (lecture seule), exposés au modèle à côté des outils d'agenda.

Dreeve sert MCP en « streamable HTTP » sur `<instance>/mcp`, avec une clé dédiée
`drvm_…` (DREEVE_MCP_KEY, distincte de la clé REST `drv_…`). La même clé doit
aussi être définie dans le `.env` de Dreeve, sinon il refuse toute requête.

- Le catalogue d'outils est chargé une fois (puis figé et trié) : il fait partie
  du préfixe mis en cache côté Anthropic, il ne doit pas varier d'un appel à l'autre.
- Une session MCP est ouverte paresseusement pendant un run d'agent (seulement si
  le modèle appelle un outil Dreeve) et fermée à la fin du run, dans la même tâche.
"""
import asyncio
import contextlib
import json
import logging

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from .config import settings

logger = logging.getLogger(__name__)

# Tous les outils Dreeve sont en lecture seule (doc Dreeve) : on les expose tous, pour que le
# coach profite des métriques calculées par Dreeve (records, prédictions…) au lieu de les estimer.
# Seuls ceux-ci sont retirés : leurs données sont déjà dans le contexte pré-chargé.
EXCLUDED = {"get_training_load", "get_most_recent_activity"}
MAX_RESULT_CHARS = 20_000
CONNECT_TIMEOUT_S = 15   # ouverture de session MCP
CALL_TIMEOUT_S = 30      # un appel d'outil qui reste muet ne doit pas figer le run
PREFIX = "dreeve_"   # évite toute collision avec les outils locaux, et dit au modèle d'où vient la donnée

_catalog: list[dict] | None = None


def _root_cause(e: BaseException) -> str:
    """Le SDK MCP enveloppe tout dans des ExceptionGroup : on remonte à l'erreur utile (401, DNS, refus…)."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"


def enabled() -> bool:
    return bool(settings.dreeve_mcp_key and settings.dreeve_mcp_url)


@contextlib.asynccontextmanager
async def _connect():
    headers = {"Authorization": f"Bearer {settings.dreeve_mcp_key}"}
    async with create_mcp_http_client(headers=headers) as http:
        async with streamable_http_client(settings.dreeve_mcp_url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _to_anthropic(tool) -> dict:
    schema = dict(tool.input_schema or {"type": "object", "properties": {}})
    schema.setdefault("type", "object")
    return {
        "name": PREFIX + tool.name,
        "description": f"[Dreeve, lecture seule] {tool.description or ''}".strip(),
        "input_schema": schema,
    }


async def load_tools() -> list[dict]:
    """Catalogue figé (chargé au premier appel réussi). Liste vide si MCP indisponible."""
    global _catalog
    if _catalog is not None or not enabled():
        return _catalog or []
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT_S + CALL_TIMEOUT_S):
            async with _connect() as session:
                listed = await session.list_tools()
    except Exception as e:
        logger.error(f"[DREEVE MCP] Connexion impossible ({settings.dreeve_mcp_url}) : {_root_cause(e)} — vérifie que "
                     "DREEVE_MCP_KEY (drvm_…, pas la clé REST drv_…) est définie ici ET dans le .env de Dreeve.")
        return []
    tools = sorted((t for t in listed.tools if t.name not in EXCLUDED), key=lambda t: t.name)
    _catalog = [_to_anthropic(t) for t in tools]
    logger.info(f"[DREEVE MCP] {len(_catalog)} outil(s) : {', '.join(t.name for t in tools)}")
    return _catalog


def owns(tool_name: str) -> bool:
    return tool_name.startswith(PREFIX)


class McpRun:
    """Session MCP paresseuse, limitée à un run d'agent (à utiliser via `async with`)."""

    def __init__(self):
        self._stack = contextlib.AsyncExitStack()
        self._session: ClientSession | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        with contextlib.suppress(Exception):
            await self._stack.aclose()

    async def call(self, tool_name: str, args: dict) -> dict:
        name = tool_name.removeprefix(PREFIX)
        if name in EXCLUDED or (_catalog is not None and PREFIX + name not in {t["name"] for t in _catalog}):
            return {"ok": False, "error": f"outil Dreeve non autorisé : {name}"}
        try:
            if self._session is None:
                self._session = await asyncio.wait_for(self._stack.enter_async_context(_connect()), CONNECT_TIMEOUT_S)
            result = await asyncio.wait_for(
                self._session.call_tool(name, args or {}, read_timeout_seconds=CALL_TIMEOUT_S), CALL_TIMEOUT_S + 5)
        except Exception as e:
            cause = _root_cause(e)
            logger.error(f"[DREEVE MCP] {name} en échec : {cause}")
            self._session = None
            with contextlib.suppress(Exception):
                await self._stack.aclose()
            self._stack = contextlib.AsyncExitStack()
            return {"ok": False, "error": f"Dreeve MCP indisponible : {cause}"}

        text = "\n".join(getattr(c, "text", "") for c in (getattr(result, "content", None) or []))
        if not text and getattr(result, "structured_content", None) is not None:
            text = json.dumps(result.structured_content, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n…(tronqué)"
        return {"ok": not getattr(result, "is_error", False), "data": text}
