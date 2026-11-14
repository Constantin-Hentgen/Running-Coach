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

    assert out == "C'est noté, on garde ça." and "🧠" not in out   # pas de journal de bord sous le message
    assert any("fractionné le mardi soir" in m["text"] for m in store.memories)
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
    assert {"propose_change", "set_goal"} <= names and "create_calendar_event" not in names
    assert not {"forget", "remove_goal"} & names   # lecture seule : rien d'effaçable


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


def test_echoed_report_is_stripped_and_history_keeps_a_system_note(tmp_path, monkeypatch):
    echoed = ("Semaine corrigée.\n\n📋 Compte-rendu Radicale (relu sur le serveur)\n- ✅ Supprimé : Run (mer 25/11)\n"
              "6 réussie(s), 0 échec(s)\n\n🧠 Noté : [autre] footings ≥ 4,5 km\nBonne séance !")
    assert agent.strip_echoed_report(echoed) == "Semaine corrigée.\n\nBonne séance !"
    ledger = tools.Ledger()
    ledger.record("delete", "Run (mer 25/11)", True)
    ledger.record("update", "Sortie longue", False, detail="introuvable")
    note = agent.history_note(ledger, False, None)
    assert note.startswith("[Note système, ne pas recopier :") and "📋" not in note
    assert "delete ok « Run (mer 25/11) »" in note and "update ÉCHEC « Sortie longue » (introuvable)" in note
    assert agent.history_note(tools.Ledger(), False, None) == ""
    assert "Tutoie" in agent.SYSTEM_PROMPT and "Jamais d'UID" in agent.SYSTEM_PROMPT


def test_system_prompt_asks_to_challenge_unsound_requests():
    p = agent.SYSTEM_PROMPT
    assert "CHALLENGER LES DEMANDES" in p and "sans rediscuter" in p and "(nouveau − ancien) / ancien" in p
    assert p.index("CHALLENGER") < p.index("OBJECTIFS ET PÉRIODISATION")


def test_empty_reply_is_retried_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(tools, "store", store)

    async def fake_context():
        return "CONTEXTE"

    async def no_tools():
        return []

    monkeypatch.setattr(agent, "_gather_context", fake_context)
    monkeypatch.setattr(dreeve_mcp, "load_tools", no_tools)
    empty = NS(stop_reason="end_turn", usage=_usage(), content=[])
    final = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text="C'est noté.")])
    fake = FakeMessages([empty, final])
    monkeypatch.setattr(agent, "_client", NS(messages=fake))
    reply = asyncio.run(agent.ask("ok", chat_id="1"))
    assert reply.text == "C'est noté." and len(fake.calls) == 2


def test_dodge_detection():
    assert agent.dodges_the_question("Je ne suis pas sûr de ce que tu vises : la progression ou la course ?")
    assert agent.dodges_the_question("Je ne sais pas exactement de quoi tu parles.")
    assert agent.dodges_the_question("Tu veux dire lequel ?\n- a) garder\n- b) remonter")
    assert agent.dodges_the_question("Avant tout :\n- a) Garder le plan\n- b) Remonter le volume\n- c) Autre semaine ?")
    assert not agent.dodges_the_question("Non, pas une bonne idée : ton ratio A/C est à 3,37.")
    assert not agent.dodges_the_question("Option A ou B pour le repos ?\n- A) pas d'événement\n- B) repos explicites")
    long_ok = "Oui, garde-la. " + "Détail. " * 60 + "Je ne suis pas sûr de la météo dimanche."
    assert not agent.dodges_the_question(long_ok)
    assert "RÉPONDRE À LA QUESTION" in agent.SYSTEM_PROMPT


def test_dodging_reply_is_retried_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(tools, "store", store)

    async def fake_context():
        return "CONTEXTE"

    async def no_tools():
        return []

    monkeypatch.setattr(agent, "_gather_context", fake_context)
    monkeypatch.setattr(dreeve_mcp, "load_tools", no_tools)
    dodge = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text="Je ne suis pas sûr de ce que tu vises.")])
    final = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text="Non : +58 % c'est trop.")])
    fake = FakeMessages([dodge, final])
    monkeypatch.setattr(agent, "_client", NS(messages=fake))
    reply = asyncio.run(agent.ask("c'est une bonne idée ?", chat_id="1"))
    assert reply.text == "Non : +58 % c'est trop." and len(fake.calls) == 2
    assert {"role": "user", "content": agent.DODGE_NUDGE} in fake.calls[1]["messages"]


def test_skip_marker_returns_nothing_and_keeps_history_clean(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(tools, "store", store)

    async def fake_context():
        return "CONTEXTE"

    async def no_tools():
        return []

    monkeypatch.setattr(agent, "_gather_context", fake_context)
    monkeypatch.setattr(dreeve_mcp, "load_tools", no_tools)
    skip = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text="« PAS_DE_SUGGESTION »")])
    monkeypatch.setattr(agent, "_client", NS(messages=FakeMessages([skip])))
    reply = asyncio.run(agent.ask("repas ?", chat_id="1", allow_writes=False, skip_marker="PAS_DE_SUGGESTION"))
    assert reply.text == "" and not reply.meals and store.history("1") == []


def test_too_long_reply_is_rewritten_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(tools, "store", store)

    async def fake_context():
        return "CONTEXTE"

    async def no_tools():
        return []

    monkeypatch.setattr(agent, "_gather_context", fake_context)
    monkeypatch.setattr(dreeve_mcp, "load_tools", no_tools)
    wall = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text="Bla bla. " * 120)])
    short = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text="Bonne séance.\n\nOn garde jeudi ?")])
    fake = FakeMessages([wall, short])
    monkeypatch.setattr(agent, "_client", NS(messages=fake))
    reply = asyncio.run(agent.ask("ça va ?", chat_id="1"))
    assert reply.text == "Bonne séance.\n\nOn garde jeudi ?" and len(fake.calls) == 2
    assert {"role": "user", "content": agent.TOO_LONG_NUDGE} in fake.calls[1]["messages"]
    assert not agent.too_long("Court.\n\n" + "- séance\n" * 20)
    assert agent.too_long("x" * 450)


def test_tool_calls_written_as_text_are_retried_and_never_shown(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(agent, "store", store)
    monkeypatch.setattr(tools, "store", store)

    async def fake_context():
        return "CONTEXTE"

    async def no_tools():
        return []

    monkeypatch.setattr(agent, "_gather_context", fake_context)
    monkeypatch.setattr(dreeve_mcp, "load_tools", no_tools)
    fake_text = "🍽️ Tortilla · ⏱️ 25 min\n\n[propose_meal: Tortilla pommes de terre]"
    first = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text=fake_text)])
    call = NS(stop_reason="tool_use", usage=_usage(), content=[NS(type="tool_use", id="m1", name="propose_meal", input={
        "title": "Tortilla pommes de terre", "recipe": "1. Cuire", "uses": [{"name": "Œufs"}]})])
    final = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text="🍽️ Tortilla · ⏱️ 25 min\n[propose_meal]")])
    fake = FakeMessages([first, call, final])
    monkeypatch.setattr(agent, "_client", NS(messages=fake))
    reply = asyncio.run(agent.ask("/eat", chat_id="1", allow_writes=False))
    assert [t for _, t in reply.meals] == ["Tortilla pommes de terre"]               # vraiment appelé
    nudge = agent.PSEUDO_TOOL_NUDGE.format(names="propose_meal")
    assert {"role": "user", "content": nudge} in fake.calls[1]["messages"]           # relance explicite
    assert reply.text == "🍽️ Tortilla · ⏱️ 25 min"                                   # jamais affiché
    assert agent.pseudo_tool_calls("[send_gif: high five]\n[Note système, x]\n[propose_meal]") == ["send_gif", "propose_meal"]


def test_bot_name_comes_from_settings():
    from running_coach import commands
    from running_coach.config import settings
    assert f"Tu es {settings.bot_name}," in agent.SYSTEM_PROMPT and "__BOT_NAME__" not in agent.SYSTEM_PROMPT
    assert f"c'est {settings.bot_name}" in commands.HELP_TEXT and "__BOT_NAME__" not in commands.HELP_TEXT
