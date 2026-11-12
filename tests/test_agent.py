import asyncio
from types import SimpleNamespace as NS

from running_coach import agent, dreeve_mcp, tools
from running_coach.memory import Store

SEED = [("contrainte", "Présentiel bureau lundi, mercredi et vendredi")]


def _usage():
    return NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeMcpRun:
    calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def call(self, name, args):
        FakeMcpRun.calls.append((name, args))
        return {"ok": True, "data": "splits…"}


def test_ask_learns_preference_and_routes_mcp(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json", seed=SEED)
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(tools, "store", store)

    async def fake_context():
        return "CONTEXTE"

    async def fake_tools():
        return [{"name": "dreeve_get_activity_splits", "description": "", "input_schema": {"type": "object"}}]

    monkeypatch.setattr(agent, "_gather_context", fake_context)
    monkeypatch.setattr(dreeve_mcp, "load_tools", fake_tools)
    monkeypatch.setattr(dreeve_mcp, "McpRun", FakeMcpRun)

    tool_round = NS(stop_reason="tool_use", usage=_usage(), content=[
        NS(type="tool_use", id="t1", name="remember",
           input={"category": "preference", "text": "Préfère le fractionné le mardi soir"}),
        NS(type="tool_use", id="t2", name="dreeve_get_activity_splits", input={"activityId": "activity-1"}),
    ])
    final = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text="C'est noté, on garde ça.")])
    fake = FakeMessages([tool_round, final])
    monkeypatch.setattr(agent, "_client", NS(messages=fake))

    out = asyncio.run(agent.ask("Je préfère faire mon fractionné le mardi soir", chat_id="42")).text

    assert "🧠 Noté" in out and "fractionné le mardi soir" in out
    assert "Aucune modification" not in out
    assert FakeMcpRun.calls == [("dreeve_get_activity_splits", {"activityId": "activity-1"})]
    assert any("mardi soir" in m["text"] for m in store.memories)
    # le prompt système contient la mémoire, et le cache est demandé
    first = fake.calls[0]
    assert "lundi, mercredi et vendredi" in first["system"][1]["text"]
    assert first["model"] == agent.settings.model   # conversation : modèle par défaut
    assert first["cache_control"] == {"type": "ephemeral"}
    # résultats d'outils renvoyés dans un seul message utilisateur
    tool_results = fake.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in tool_results] == ["t1", "t2"]
    # historique persistant
    assert [m["role"] for m in store.history("42")] == ["user", "assistant"]


def test_read_only_mode_hides_calendar_writes(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(agent, "store", store)

    async def fake_context():
        return "CONTEXTE"

    async def no_tools():
        return []

    monkeypatch.setattr(agent, "_gather_context", fake_context)
    monkeypatch.setattr(dreeve_mcp, "load_tools", no_tools)
    final = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text="Bon run !")])
    fake = FakeMessages([final])
    monkeypatch.setattr(agent, "_client", NS(messages=fake))

    asyncio.run(agent.ask("debrief", chat_id="1", allow_writes=False))
    names = {t["name"] for t in fake.calls[0]["tools"]}
    assert "create_calendar_event" not in names and "remember" in names


def test_planning_model_and_proposal_without_false_claim_warning(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(tools, "store", store)

    async def fake_context():
        return "CONTEXTE"

    async def no_tools():
        return []

    monkeypatch.setattr(agent, "_gather_context", fake_context)
    monkeypatch.setattr(dreeve_mcp, "load_tools", no_tools)
    propose = NS(stop_reason="tool_use", usage=_usage(), content=[
        NS(type="tool_use", id="p1", name="propose_change", input={"summary": "Seuil décalé à vendredi",
                                                                   "change": "Déplacer le seuil de jeudi à vendredi 18:00"}),
    ])
    final = NS(stop_reason="end_turn", usage=_usage(),
               content=[NS(type="text", text="Je te propose de déplacer le seuil à vendredi.")])
    fake = FakeMessages([propose, final])
    monkeypatch.setattr(agent, "_client", NS(messages=fake))

    reply = asyncio.run(agent.ask("bilan", chat_id="1", allow_writes=False, planning=True))
    assert fake.calls[0]["model"] == agent.settings.planning_model
    assert len(reply.proposals) == 1 and reply.proposals[0][1] == "Seuil décalé à vendredi"
    assert store.proposal(reply.proposals[0][0])["status"] == "open"
    assert store.proposal(reply.proposals[0][0])["text"] == "Déplacer le seuil de jeudi à vendredi 18:00"
    assert "Aucune modification" not in reply.text   # « déplacer » proposé, pas prétendu fait
    names = {t["name"] for t in fake.calls[0]["tools"]}
    assert {"propose_change", "set_goal", "remove_goal"} <= names and "create_calendar_event" not in names


def test_claim_detection_ignores_negations_questions_and_infinitives():
    assert agent.claims_calendar_change("J'ai déplacé ta sortie longue à dimanche.")
    assert agent.claims_calendar_change("Séance créée pour mardi 18:30 !")
    assert not agent.claims_calendar_change("L'événement existe déjà. Je n'ai rien modifié.")
    assert not agent.claims_calendar_change("Tu veux que je la déplace ?\n- Le déplacer ou changer sa durée ;")
    assert not agent.claims_calendar_change("Aucune séance n'a été supprimée.")


def test_announced_proposal_without_tool_call_is_retried(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(tools, "store", store)

    async def fake_context():
        return "CONTEXTE"

    async def no_tools():
        return []

    monkeypatch.setattr(agent, "_gather_context", fake_context)
    monkeypatch.setattr(dreeve_mcp, "load_tools", no_tools)
    forgot = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text="Voici 3 changements, boutons ci-dessous.")])
    propose = NS(stop_reason="tool_use", usage=_usage(), content=[
        NS(type="tool_use", id="p1", name="propose_change", input={"summary": "Semaine allégée à 15 km", "change": "détail"})])
    final = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text="Voici la proposition.")])
    fake = FakeMessages([forgot, propose, final])
    monkeypatch.setattr(agent, "_client", NS(messages=fake))

    reply = asyncio.run(agent.ask("tu proposes quoi ?", chat_id="1"))
    assert [label for _, label in reply.proposals] == ["Semaine allégée à 15 km"]
    assert reply.text.startswith("Voici la proposition.")
    assert {"role": "user", "content": agent.MISSING_PROPOSAL_NUDGE} in fake.calls[1]["messages"]   # relance explicite


def test_proposal_announcement_detection():
    assert agent.announces_missing_proposal("Tu as les boutons Appliquer / Ignorer sous ce message.", [])
    assert agent.announces_missing_proposal("Ma proposition est ci-dessous.", [])
    assert not agent.announces_missing_proposal("Je peux l'appliquer si tu veux.", [])
    assert not agent.announces_missing_proposal("Clique sur Appliquer.", [(1, "x")])


def test_long_proposal_is_kept_whole_with_a_short_summary(tmp_path, monkeypatch):
    import pytest
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(tools, "store", store)
    detail = "A) " + "modifier la séance (UID x) ; " * 60 + "C) fin"
    out = tools.tool_propose_change({"summary": "3 semaines remises à +10 %", "change": detail}, tools.Ledger())
    assert store.proposal(out["proposal_id"])["text"].endswith("C) fin")                # plus de coupure à 1000
    with pytest.raises(ValueError, match="summary"):
        tools.tool_propose_change({"change": "x"}, tools.Ledger())
