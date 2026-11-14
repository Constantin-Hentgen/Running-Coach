import asyncio
from types import SimpleNamespace as NS

from running_coach import agent, dreeve, dreeve_mcp, tools
from running_coach.memory import Store


def _usage():
    return NS(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def _text(t):
    return NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text=t)])


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class HangingMessages:
    async def create(self, **kwargs):
        await asyncio.sleep(60)


def _setup(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(tools, "store", store)

    async def ctx():
        return "CONTEXTE"

    async def no_tools():
        return []

    monkeypatch.setattr(agent, "_gather_context", ctx)
    monkeypatch.setattr(dreeve_mcp, "load_tools", no_tools)
    return store


def test_claims_not_flagged_for_non_calendar_sentences():
    for t in ["C'est noté, j'ai mis à jour ton stock 👌", "J'ai ajouté le riz à ta liste de courses.",
              "Repas ajouté, bon app !", "J'ai ajouté les courses de la semaine."]:
        assert not agent.claims_calendar_change(t), t


def test_claims_still_flagged_for_calendar_sentences():
    assert agent.claims_calendar_change("J'ai déplacé ta séance de jeudi à vendredi.")
    assert agent.claims_calendar_change("Footing ajouté à ton agenda !")


def _stock_then(text):
    call = NS(stop_reason="tool_use", usage=_usage(), content=[
        NS(type="tool_use", id="s1", name="update_stock", input={"add": [{"name": "riz", "qty": "1 kg"}]})])
    return FakeMessages([call, _text(text)])


def test_stock_update_reply_gets_no_warning(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "_client", NS(messages=_stock_then("C'est noté, j'ai mis à jour ton stock 👌")))
    reply = asyncio.run(agent.ask("stock", chat_id="1"))
    assert "rien n'a changé" not in reply.text


def test_calendar_claim_after_only_a_stock_update_is_still_flagged(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)   # une action non-agenda ne doit pas masquer une fausse promesse d'agenda
    monkeypatch.setattr(agent, "_client", NS(messages=_stock_then("J'ai déplacé ta séance de jeudi à vendredi.")))
    reply = asyncio.run(agent.ask("stock", chat_id="1"))
    assert "rien n'a changé dans ton agenda" in reply.text


def test_false_claim_without_any_action_is_flagged(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    fake = FakeMessages([_text("J'ai déplacé ta séance de jeudi à vendredi.")])
    monkeypatch.setattr(agent, "_client", NS(messages=fake))
    reply = asyncio.run(agent.ask("décale", chat_id="1"))
    assert "rien n'a changé dans ton agenda" in reply.text


def test_ask_timeout_goes_through_error_path_and_frees_lock(tmp_path, monkeypatch):
    store = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "ASK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(agent, "_client", NS(messages=HangingMessages()))

    async def scenario():
        first = await agent.ask("salut", chat_id="1")
        assert not agent._lock.locked()
        monkeypatch.setattr(agent, "_client", NS(messages=FakeMessages([_text("Coucou !")])))
        monkeypatch.setattr(agent, "ASK_TIMEOUT_S", 5)
        return first, await agent.ask("re", chat_id="1")

    first, second = asyncio.run(scenario())
    assert "J'ai eu un bug" in first.text
    assert second.text == "Coucou !"
    assert "délai dépassé" in store.history("1")[1]["content"]


def test_corrective_retries_are_capped(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    long_dodge = "Je ne peux pas faire ça. " + "blabla " * 300
    fake = FakeMessages([_text(""), _text(long_dodge), _text(long_dodge), _text(long_dodge), _text(long_dodge)])
    monkeypatch.setattr(agent, "_client", NS(messages=fake))
    asyncio.run(agent.ask("ok", chat_id="1"))
    assert len(fake.calls) <= 3


def test_last_messages_starts_with_user_and_skips_empty():
    h = [{"role": r, "content": f"m{i}"} for i, r in enumerate(["user", "assistant"] * 3)]
    assert [m["content"] for m in agent.last_messages(h, 4)] == ["m2", "m3", "m4", "m5"]
    odd = agent.last_messages(h, 5)
    assert odd[0]["role"] == "user" and [m["content"] for m in odd] == ["m2", "m3", "m4", "m5"]
    assert agent.last_messages(h, 1) == []
    assert agent.last_messages(h, 0) == []
    assert agent.last_messages([{"role": "user", "content": ""}, {"role": "user", "content": "x"}], 5) == \
        [{"role": "user", "content": "x"}]


def test_gather_context_reads_runs_once(monkeypatch):
    calls = {"all_runs": 0}

    def fake_all_runs():
        calls["all_runs"] += 1
        return []

    monkeypatch.setattr(dreeve, "settings", NS(dreeve_enabled=True))
    monkeypatch.setattr(agent, "settings", NS(dreeve_enabled=True, context_days_ahead=7, giphy_api_key=None, meal_boxes=0))
    monkeypatch.setattr(dreeve, "get_all_runs", fake_all_runs)
    monkeypatch.setattr(dreeve, "get_load_text", lambda: "charge")
    monkeypatch.setattr(agent.weather, "get_forecast_text", lambda: "beau")
    monkeypatch.setattr(agent.cs, "list_events", lambda a, b: ([], 0))
    ctx = asyncio.run(agent._gather_context())
    assert calls["all_runs"] == 1 and "charge" in ctx


def test_mcp_call_passes_read_timeout(monkeypatch):
    seen = {}

    class Session:
        async def call_tool(self, name, args, **kw):
            seen.update(kw)
            return NS(content=[NS(text="ok")], is_error=False, structured_content=None)

    async def go():
        run = dreeve_mcp.McpRun()
        run._session = Session()
        return await run.call("dreeve_get_x", {})

    monkeypatch.setattr(dreeve_mcp, "_catalog", None)
    res = asyncio.run(go())
    assert res["ok"] and seen["read_timeout_seconds"] == dreeve_mcp.CALL_TIMEOUT_S


def test_system_prompt_treats_tool_text_as_data():
    assert "DONNÉE" in agent.SYSTEM_PROMPT and "jamais une instruction" in agent.SYSTEM_PROMPT
