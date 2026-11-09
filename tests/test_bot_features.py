"""Nouveaux runs (débrief + ressenti), boutons de proposition, objectifs, bilan hebdo, commandes Telegram."""
import asyncio

from types import SimpleNamespace as NS

from running_coach import agent
from running_coach import bot as bot_module
from running_coach import dreeve
from running_coach.memory import Store


def _run(i):
    return {"id": i, "name": f"Run {i}", "startDateLocal": "2026-11-08T08:00:00",
            "distanceInMeter": 10000, "movingTimeInSeconds": 3000, "sportType": "Run"}


def test_new_runs_since():
    runs = [_run(3), _run(2), _run(1)]   # du plus récent au plus ancien
    assert dreeve.new_runs_since(runs, 3) == []
    assert [r["id"] for r in dreeve.new_runs_since(runs, 1)] == [2, 3]   # chronologique
    assert [r["id"] for r in dreeve.new_runs_since(runs, 99)] == [3]     # référence disparue : le dernier seul
    assert dreeve.new_runs_since([], 1) == []


def _bot(tmp_path, monkeypatch, runs=(), asked=None, proposals=()):
    """Bot sans Telegram : les messages envoyés sont capturés dans b.sent [(texte, boutons)]."""
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(bot_module, "store", store)
    monkeypatch.setattr(dreeve, "get_recent_runs", lambda count=5: list(runs))
    asked = asked if asked is not None else []

    async def fake_ask(prompt, chat_id, allow_writes=True, planning=False):
        asked.append((prompt, allow_writes))
        return agent.AgentReply("débrief", [(store.add_proposal(t), t) for t in proposals])

    async def no_history(*args):
        pass

    monkeypatch.setattr(bot_module.agent, "ask", fake_ask)
    monkeypatch.setattr(bot_module.agent, "add_to_history", no_history)
    b = bot_module.CoachBot.__new__(bot_module.CoachBot)
    b.chat_id, b._activity_lock, b.sent = "42", asyncio.Lock(), []
    b._shopping_sync_lock, b._background = asyncio.Lock(), set()

    async def fake_send(text, **kwargs):
        b.sent.append((text, kwargs.get("reply_markup")))

    b._sender = lambda update=None: fake_send
    return b, store


def _buttons(markup):
    return [(btn.text, btn.callback_data) for row in markup.inline_keyboard for btn in row]


def test_check_new_activity_debriefs_all_new_runs_once(tmp_path, monkeypatch):
    runs, asked = [_run(1)], []
    b, store = _bot(tmp_path, monkeypatch, runs, asked)

    assert asyncio.run(b.check_new_activity()) is False   # 1er passage : référence seulement
    assert store.last_activity_id == 1 and not asked

    runs[:0] = [_run(3), _run(2)]

    async def both():   # deux détections simultanées : un seul débrief
        return await asyncio.gather(b.check_new_activity(), b.check_new_activity())

    assert sorted(asyncio.run(both())) == [False, True]
    assert len(asked) == 1
    debrief, checkin = b.sent   # débrief, puis ressenti sur le dernier run
    assert debrief[0].startswith("⚡ *Nouveau run détecté !*") and debrief[1] is None
    assert "Run 3" in checkin[0] and ("10", "c:r:3:10") in _buttons(checkin[1])
    prompt, allow_writes = asked[0]
    assert allow_writes is False
    assert "2 nouvelles séances" in prompt and prompt.index("Run 2") < prompt.index("Run 3")
    assert "Objectifs" in prompt and "prochaines séances" in prompt
    assert store.last_activity_id == 3



# --- Commandes ---------------------------------------------------------------

import datetime  # noqa: E402

from running_coach import coaching, commands, weather_alerts  # noqa: E402
from running_coach.formatting import md_unescape  # noqa: E402


def test_parse_days():
    assert commands.parse_days([]) == 7
    assert commands.parse_days(["14"]) == 14
    assert commands.parse_days(["999"]) == 45 and commands.parse_days(["0"]) == 1
    assert commands.parse_days(["abc"]) == 7


def test_format_agenda_groups_by_day_and_escapes():
    events = [
        {"start": "2026-11-09T18:30+01:00", "end": "2026-11-09T19:30+01:00", "summary": "Fractionné 8*400", "location": "Maison"},
        {"start": "2026-11-09T07:00+01:00", "end": "2026-11-09T07:45+01:00", "summary": "Footing"},
        {"start": "2026-11-11T00:00+01:00", "end": "2026-11-12T00:00+01:00", "summary": "Repos", "all_day": True},
    ]
    events.sort(key=lambda e: e["start"])
    text = commands.format_agenda(events, 7)
    assert text.count("*lun 09/11*") == 1 and "*mer 11/11*" in text
    assert "- 18:30–19:30 · Fractionné 8\\*400 · 📍 Maison" in text
    assert "- journée · Repos" in text
    assert "Rien de prévu" in commands.format_agenda([], 7)


def test_format_form():
    load = {"ctl": 40, "atl": 50, "tsb": {"value": -10, "status": "NEUTRAL_RANGE"},
            "acRatio": {"value": 1.25, "status": "LOW"}, "restDaysLast7Days": 2}
    runs = [_run(1)]   # dimanche 08/11
    text = commands.format_form(load, runs, datetime.date(2026, 11, 8))
    assert "TSB) : -10 (NEUTRAL\\_RANGE)" in text
    assert "- cette semaine : 10.0 km, 1 sortie" in text and "sem. du 12/10 : 0.0 km" in text


def test_help_lists_every_command():
    for name, _, _ in commands.COMMANDS:
        assert f"/{name}" in commands.HELP_TEXT
    assert md_unescape(commands.format_weather(" (lieux : Maison)\n  lun 09/11 : ciel clair")).endswith("- lun 09/11 : ciel clair")


def test_rain_alert_lead_time():
    now = datetime.datetime(2026, 11, 8, 12, tzinfo=datetime.timezone.utc)
    assert weather_alerts.lead_time(now + datetime.timedelta(hours=31, minutes=20), now) == "dans 31 h"
    assert weather_alerts.lead_time(now + datetime.timedelta(minutes=20), now) == "imminent"
    assert weather_alerts.lead_time(now + datetime.timedelta(days=3), now) == "dans 3 j"


def test_shopping_days_and_prompt():
    assert commands.parse_days([], commands.SHOPPING_DEFAULT_DAYS, commands.SHOPPING_MAX_DAYS) == 7
    assert commands.parse_days(["30"], commands.SHOPPING_DEFAULT_DAYS, commands.SHOPPING_MAX_DAYS) == 14
    prompt = commands.shopping_prompt(3, datetime.date(2026, 11, 9))
    assert "3 jours, du lun 09/11 au mer 11/11 inclus" in prompt
    assert "objectifs sportifs et de santé" in prompt and "Ne modifie pas l'agenda" in prompt
    assert "1 jour, du lun 09/11 au lun 09/11" in commands.shopping_prompt(1, datetime.date(2026, 11, 9))



# --- Boutons, ressenti, objectifs, bilan ---------------------------------------

class FakeQuery:
    def __init__(self, data):
        self.data, self.answers, self.edits = data, [], []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_reply_markup(self, markup):
        self.edits.append(("markup", markup))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


def _click(b, data):
    query = FakeQuery(data)
    update = NS(callback_query=query, effective_chat=NS(id=42, send_action=_noop), effective_message=None, message=None)
    asyncio.run(b.on_button(update, None))
    return query


async def _noop(*args, **kwargs):
    pass


def test_proposal_buttons_apply_once(tmp_path, monkeypatch):
    asked = []
    b, store = _bot(tmp_path, monkeypatch, asked=asked, proposals=["Déplacer le fractionné de mardi à mercredi 18:30"])
    reply = asyncio.run(bot_module.agent.ask("debrief", "42", allow_writes=False))
    asyncio.run(b._deliver(reply, header="⚡ *Nouveau run détecté !*"))
    text, markup = b.sent[-1]
    assert "*💡 Proposition* : Déplacer le fractionné" in text
    (pid,) = {data.split(":")[2] for _, data in _buttons(markup)}
    assert [label for label, _ in _buttons(markup)] == ["✅ Appliquer", "✏️ Ajuster", "❌ Ignorer"]

    q = _click(b, f"p:a:{pid}")
    assert ("markup", None) in q.edits                         # boutons retirés
    prompt, allow_writes = asked[-1]
    assert allow_writes is True and "Déplacer le fractionné de mardi à mercredi 18:30" in prompt
    n = len(asked)
    assert _click(b, f"p:a:{pid}").answers == ["Déjà traité."] and len(asked) == n   # double clic sans effet


def test_proposal_ignore_and_weather_adapt(tmp_path, monkeypatch):
    asked = []
    b, store = _bot(tmp_path, monkeypatch, asked=asked)
    pid = store.add_proposal("Décale la sortie longue")
    _click(b, f"p:x:{pid}")
    assert store.proposal(pid)["status"] == "ignored" and not asked and "On garde" in b.sent[-1][0]
    wid = store.add_proposal("Alerte météo sur ma séance … Adapte-la au mieux", kind="weather")
    _click(b, f"p:a:{wid}")
    assert asked[-1][0] == "Alerte météo sur ma séance … Adapte-la au mieux"   # le texte EST la demande


def test_checkin_flow_rpe_then_legs_and_pain_follow_up(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch)
    asyncio.run(b._ask_checkin(_run(7)))
    _, markup = b.sent[-1]
    assert len(_buttons(markup)) == 10
    q = _click(b, "c:r:7:8")
    assert "8/10" in q.edits[-1][0] and ("🔴 Douleur", "c:l:7:douleur") in _buttons(q.edits[-1][1])
    _click(b, "c:l:7:douleur")
    item = store.state["checkins"]["7"]
    assert item == {"date": "2026-11-08", "minutes": 50, "name": "Run 7", "rpe": 8, "legs": "douleur"}
    assert "Où as-tu mal" in b.sent[-1][0]
    n = len(b.sent)
    asyncio.run(b._ask_checkin(_run(7), only_if_missing=True))   # déjà noté : on ne redemande pas
    assert len(b.sent) == n
    ctx = coaching.checkins_context(store.recent_checkins(14, datetime.date(2026, 11, 9)))
    assert "effort 8/10 (très difficile), jambes douleur" in ctx and "effort × minutes) sur la période : 400" in ctx


def test_goals_store_render_and_countdown(tmp_path):
    store = Store(tmp_path / "s.json")
    semi = store.set_goal("Semi de Paris", kind="course", date="2027-03-07", target="1h45", priority="A")
    store.set_goal("Revenir à 72 kg", kind="santé", target="72 kg")
    store.set_goal("Semi de Paris", kind="course", date="2027-03-07", target="1h42", priority="A", goal_id=semi["id"])
    assert len(store.goals) == 2 and store.goals[0]["target"] == "1h42"
    today = datetime.date(2026, 11, 9)
    ctx = coaching.goals_context(store.goals, today)
    assert ctx.splitlines()[0].startswith("- #1 [course, priorité A] Semi de Paris — le 2027-03-07 — cible : 1h42")
    assert "dans 118 j (~16 sem.)" in ctx and "sans date" in ctx
    reply = commands.format_goals_reply(store.goals, today)
    assert "🏁 Semi de Paris \\[A] : 2027-03-07, dans 118 j" in reply
    store.remove_goal(semi["id"])
    assert [g["title"] for g in store.goals] == ["Revenir à 72 kg"]
    assert "Aucun objectif" in commands.format_goals_reply([], today)


def test_weekly_adherence():
    monday = datetime.date(2026, 11, 2)
    planned = [
        {"summary": "Footing", "start": "2026-11-03T18:00+01:00"},
        {"summary": "Fractionné", "start": "2026-11-05T18:00+01:00"},
        {"summary": "Sortie longue", "start": "2026-11-08T09:00+01:00"},
    ]
    runs = [dict(_run(1), startDateLocal="2026-11-03T18:05:00"), dict(_run(2), startDateLocal="2026-11-08T09:10:00"),
            dict(_run(3), startDateLocal="2026-11-07T10:00:00")]
    stats = coaching.adherence(planned, runs, monday)
    assert stats["rate"] == 67 and [e["summary"] for e in stats["missed"]] == ["Fractionné"]
    assert [r["id"] for r in stats["extra"]] == [3] and round(stats["km"], 1) == 30.0
    text = coaching.format_adherence(stats)
    assert "Séances prévues réalisées : 2/3 (67 %)" in text and "non faite : Fractionné (jeu 05/11)" in text


ITEMS = [{"name": "Bananes", "quantity": "7", "aisle": "🥦 Fruits et légumes"},
         {"name": "Riz", "quantity": "500 g", "aisle": "🍚 Féculents"},
         {"name": "Pommes", "aisle": "🥦 Fruits et légumes"},
         {"name": "  "}]   # ignoré


def test_shopping_tool_checklist_and_calendar_event(tmp_path, monkeypatch):
    import dataclasses

    from running_coach import tools
    store = Store(tmp_path / "s.json")
    monkeypatch.setattr(tools, "store", store)
    calls = []

    def fake_save(lst, start, end):
        calls.append((lst["items"], start, end))
        return {"uid": "u@running-coach-shopping", "summary": "🛒", "start": start, "end": end, "replaced": 1}

    monkeypatch.setattr(tools.cs, "save_shopping_event", fake_save)

    # Sans agenda courses : checklist seule, start_time ignoré
    monkeypatch.setattr(tools, "settings", dataclasses.replace(tools.settings, radicale_shopping_url=None))
    ledger = tools.Ledger()
    out = tools.tool_save_shopping_list({"items": ITEMS, "start_time": "2026-11-09T15:00:00", "days_covered": 7,
                                         "coverage": "déjeuners et dîners, 1 personne",
                                         "estimated_cost_min_eur": 70, "estimated_cost_max_eur": 55}, ledger)
    assert out["items"] == 3 and "calendar" not in out and not calls
    lst = store.shopping_list
    assert (lst["days"], lst["coverage"], lst["cost_min"], lst["cost_max"]) == (7, "déjeuners et dîners, 1 personne", 55, 70)
    assert lst["recipes"] is None
    assert [i["name"] for i in store.shopping_list["items"]] == ["Bananes", "Riz", "Pommes"]
    assert tools.build_report(ledger, False) == ""   # la checklist suit la réponse, rien à signaler

    # Avec agenda courses : événement au créneau voulu, rattaché à la liste
    monkeypatch.setattr(tools, "settings", dataclasses.replace(tools.settings, radicale_shopping_url="https://dav/shop/"))
    ledger = tools.Ledger()
    out = tools.tool_save_shopping_list({"items": ITEMS, "start_time": "2026-11-09T15:00:00"}, ledger)
    (items, start, end), = calls
    local = start.astimezone(tools.cs.TZ)
    assert (local.hour, local.minute, (end - start).seconds) == (15, 0, 1800)   # 30 min par défaut
    assert items[0] == {"name": "Bananes", "qty": "7", "aisle": "🥦 Fruits et légumes", "done": False}
    assert store.shopping_list["event_uid"] == "u@running-coach-shopping" and out["calendar"]["previous_list_replaced"] == 1
    assert "📅 Liste de courses ajoutée à l'agenda courses : lun 09/11 15:00–15:30" in tools.build_report(ledger, False)
    assert [a.detail for a in ledger.actions if a.kind == "shopping"] == [str(store.shopping_list["id"])]

    prompt = commands.shopping_prompt(7, datetime.date(2026, 11, 9), save_to_calendar=True)
    assert "save_shopping_list" in prompt and "start_time" in prompt
    assert "start_time" not in commands.shopping_prompt(7, datetime.date(2026, 11, 9))


def test_shopping_tool_is_always_offered_and_calendar_tool_refuses_groceries():
    from running_coach import tools
    assert "save_shopping_list" in {t["name"] for t in tools.ALL_TOOLS} | {t["name"] for t in tools.READ_ONLY_TOOLS}
    create = next(t for t in tools.ALL_TOOLS if t["name"] == "create_calendar_event")
    assert "Jamais pour des courses alimentaires" in create["description"]


def test_shopping_checklist_toggle_sync_and_replacement(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch)
    lst = store.set_shopping_list([{"name": "Bananes", "qty": "7", "aisle": "🥦 Fruits"}, {"name": "Riz", "aisle": "🍚 Féculents"}])
    store.attach_shopping_event(lst["id"], "uid-courses")
    store.state["shopping"].update(days=7, cost_min=55, cost_max=70, coverage="déjeuners et dîners")
    synced = []
    monkeypatch.setattr(bot_module.cs, "set_shopping_description", lambda uid, text: synced.append((uid, text)))

    # La checklist part avec la réponse de l'agent
    asyncio.run(b._deliver(agent.AgentReply("Voici ta liste.", shopping_list=lst["id"])))
    text, markup = b.sent[-1]
    assert "0/2 ✅" in text and "📆 7 jours · 55–70 € estimés" in text and "🍽️ déjeuners et dîners" in text
    assert _buttons(markup) == [("— 🥦 Fruits —", "s:n"), ("☐ Bananes — 7", f"s:t:{lst['id']}:0"),
                                ("— 🍚 Féculents —", "s:n"), ("☐ Riz", f"s:t:{lst['id']}:1"),
                                ("🧹 Tout décocher", f"s:r:{lst['id']}")]

    async def tap(data):
        q = FakeQuery(data)
        update = NS(callback_query=q, effective_chat=NS(id=42), effective_message=None, message=None)
        await b.on_button(update, None)
        await asyncio.gather(*b._background)   # attend la recopie dans l'agenda
        return q

    q = asyncio.run(tap(f"s:t:{lst['id']}:0"))
    new_text, new_markup = q.edits[-1]
    assert "1/2 ✅" in new_text and ("✅ Bananes — 7", f"s:t:{lst['id']}:0") in _buttons(new_markup)
    assert synced[-1][0] == "uid-courses" and synced[-1][1].startswith("Couvre : 7 jours, déjeuners et dîners\nCoût estimé : 55–70 €")
    assert synced[-1][1].endswith("🥦 Fruits\n✅ Bananes — 7\n\n🍚 Féculents\n☐ Riz")

    q = asyncio.run(tap(f"s:t:{lst['id']}:1"))
    assert "tout est dans le panier" in q.edits[-1][0]
    asyncio.run(tap(f"s:r:{lst['id']}"))
    assert not any(i["done"] for i in store.shopping_list["items"]) and "☐ Bananes" in synced[-1][1]

    # Une nouvelle liste remplace l'ancienne : les anciens boutons ne font plus rien
    store.set_shopping_list([{"name": "Œufs"}])
    q = asyncio.run(tap(f"s:t:{lst['id']}:0"))
    assert "remplacée" in q.answers[-1] and not q.edits


def test_list_command_shows_current_checklist(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch)
    replies = []

    async def reply_text(text, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    update = NS(message=NS(reply_text=reply_text), effective_chat=NS(id=42))
    asyncio.run(b.on_list(update, None))
    assert "Pas de liste" in replies[-1][0]
    store.set_shopping_list([{"name": "Œufs", "qty": "6"}])
    asyncio.run(b.on_list(update, None))
    assert "0/1 ✅" in replies[-1][0] and ("☐ Œufs — 6", "s:t:1:0") in _buttons(replies[-1][1])


def test_only_the_weekly_review_uses_the_planning_model(tmp_path, monkeypatch):
    """Sonnet coûte plus cher : seul le bilan de la semaine l'utilise ; tout le reste (briefing, courses…) reste sur Haiku."""
    b, store = _bot(tmp_path, monkeypatch, runs=[_run(1)])
    calls = []

    async def fake_ask(prompt, chat_id, allow_writes=True, planning=False):
        calls.append((prompt[:40], planning))
        return agent.AgentReply("ok")

    monkeypatch.setattr(bot_module.agent, "ask", fake_ask)
    monkeypatch.setattr(b, "_review_prompt", lambda: "Fais le bilan de ma semaine.")
    import dataclasses
    monkeypatch.setattr(bot_module, "settings", dataclasses.replace(bot_module.settings, dreeve_url="http://d", dreeve_api_key="k"))

    async def reply_text(text, **kwargs):
        pass

    def update(text="x"):
        return NS(message=NS(text=text, reply_text=reply_text), effective_message=NS(reply_text=reply_text),
                  effective_chat=NS(id=42, send_action=_noop))

    def run(handler, args=()):
        asyncio.run(handler(update(), NS(args=list(args))))
        return calls.pop()[1]

    assert run(b.on_shopping) is False
    assert run(b.on_shopping, ["3"]) is False
    assert run(b.on_message) is False          # « mets la liste de courses à 15h »
    assert run(b.on_debrief) is False
    assert run(b.on_briefing) is False
    assert run(b.on_review) is True
    asyncio.run(b.morning_briefing())
    assert calls.pop()[1] is False
    asyncio.run(b.weekly_review())
    assert calls.pop()[1] is True



def test_recipes_generated_once_then_cached_and_regenerated_on_demand(tmp_path, monkeypatch):
    asked = []
    b, store = _bot(tmp_path, monkeypatch, asked=asked)
    replies = []

    async def reply_text(text, **kwargs):
        replies.append(text)

    def update():
        return NS(message=NS(reply_text=reply_text), effective_message=NS(reply_text=reply_text),
                  effective_chat=NS(id=42, send_action=_noop))

    asyncio.run(b.on_recipes(update(), NS(args=[])))
    assert "Pas de liste" in replies[-1] and not asked

    store.set_shopping_list([{"name": "Poulet", "qty": "800 g"}, {"name": "Riz"}], days=7, coverage="déjeuners")
    asyncio.run(b.on_recipes(update(), NS(args=[])))
    prompt, allow_writes = asked[-1]
    assert allow_writes is False and "- Poulet (800 g)\n- Riz" in prompt and "7 jours, déjeuners" in prompt
    assert "boîtes" in prompt and "save_shopping_list" in prompt
    assert store.shopping_list["recipes"] == "débrief"   # réponse (factice) de l'agent mise en cache

    asyncio.run(b.on_recipes(update(), NS(args=[])))      # 2e fois : réaffichée sans IA
    assert len(asked) == 1 and replies[-1].strip() == "débrief"
    asyncio.run(b.on_recipes(update(), NS(args=["new"])))  # /recipes new : régénérées
    assert len(asked) == 2


def test_list_edit_updates_same_calendar_slot_and_time_is_required(tmp_path, monkeypatch):
    import dataclasses

    import pytest

    from running_coach import tools
    store = Store(tmp_path / "s.json")
    monkeypatch.setattr(tools, "store", store)
    monkeypatch.setattr(tools, "settings", dataclasses.replace(tools.settings, radicale_shopping_url="https://dav/shop/"))
    slots = []

    def fake_save(lst, start, end):
        slots.append((start, end, [i["name"] for i in lst["items"]]))
        return {"uid": f"u{len(slots)}@running-coach-shopping", "summary": "🛒", "start": start, "end": end, "replaced": len(slots) - 1}

    monkeypatch.setattr(tools.cs, "save_shopping_event", fake_save)
    base = {"days_covered": 7, "estimated_cost_min_eur": 50, "estimated_cost_max_eur": 60}

    with pytest.raises(ValueError, match="start_time requis"):   # agenda configuré, aucun créneau connu
        tools.tool_save_shopping_list({**base, "items": [{"name": "Riz"}]}, tools.Ledger())
    assert store.shopping_list is None

    start = (datetime.datetime.now(tools.cs.TZ) + datetime.timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    tools.tool_save_shopping_list({**base, "items": [{"name": "Riz"}, {"name": "Cabillaud"}],
                                   "start_time": start.isoformat()}, tools.Ledger())
    # « enlève le cabillaud » : liste complète corrigée, sans heure → même créneau, événement remplacé
    out = tools.tool_save_shopping_list({**base, "items": [{"name": "Riz"}]}, tools.Ledger())
    (s1, e1, first), (s2, e2, second) = slots
    assert (s2, e2) == (s1, e1) and first == ["Riz", "Cabillaud"] and second == ["Riz"]
    assert out["calendar"]["previous_list_replaced"] == 1 and store.shopping_list["event_uid"] == "u2@running-coach-shopping"


def test_recipes_are_written_into_the_shopping_event(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch)
    lst = store.set_shopping_list([{"name": "Poulet", "qty": "800 g", "aisle": "🥩 Protéines"}], days=7)
    store.attach_shopping_event(lst["id"], "uid-courses")
    synced = []
    monkeypatch.setattr(bot_module.cs, "set_shopping_description", lambda uid, text: synced.append(text))

    async def fake_ask(prompt, chat_id, allow_writes=True, planning=False):
        return agent.AgentReply("🗓️ *Plan de batch cooking* : dimanche, 4 boîtes\n*Poulet riz curry* — 4 boîtes")

    monkeypatch.setattr(bot_module.agent, "ask", fake_ask)

    async def reply_text(text, **kwargs):
        pass

    update = NS(message=NS(reply_text=reply_text), effective_message=NS(reply_text=reply_text),
                effective_chat=NS(id=42, send_action=_noop))
    asyncio.run(b.on_recipes(update, NS(args=[])))
    (description,) = synced
    assert "☐ Poulet — 800 g" in description and "Recettes : /recipes" not in description
    assert description.endswith("🍳 RECETTES\n\n🗓️ Plan de batch cooking : dimanche, 4 boîtes\nPoulet riz curry — 4 boîtes")


# --- Stock en cuisine et /eat ----------------------------------------------------

def test_ticked_items_enter_the_stock_and_unticked_leave(tmp_path):
    store = Store(tmp_path / "s.json")
    lst = store.set_shopping_list([{"name": "Bananes", "qty": "7"}, {"name": "Riz", "qty": "500 g"}])
    store.toggle_shopping_item(lst["id"], 0)
    store.toggle_shopping_item(lst["id"], 1)
    assert [(p["name"], p["qty"]) for p in store.pantry] == [("Bananes", "7"), ("Riz", "500 g")]
    store.toggle_shopping_item(lst["id"], 1)          # décoché : sort du stock
    assert [p["name"] for p in store.pantry] == ["Bananes"]
    store.update_pantry(add=[{"name": "Œufs", "qty": "6"}])
    store.reset_shopping(lst["id"])                   # « Tout décocher » : retire ce que la liste avait ajouté
    assert [p["name"] for p in Store(tmp_path / "s.json").pantry] == ["Œufs"]   # persisté


def test_update_stock_tool_add_remove_partial_and_report(tmp_path, monkeypatch):
    from running_coach import tools
    store = Store(tmp_path / "s.json")
    monkeypatch.setattr(tools, "store", store)
    store.update_pantry(add=[{"name": "Riz", "qty": "1 kg"}, {"name": "Œufs", "qty": "6"}, {"name": "Cabillaud"}])
    ledger = tools.Ledger()
    out = tools.tool_update_stock({"add": [{"name": "Pain"}], "remove": [
        {"name": "cabillaud"}, {"name": "Œufs", "qty_left": "2"}, {"name": "Pâtes"}]}, ledger)
    assert out["removed"] == ["Cabillaud"] and out["updated"] == ["Œufs (2)"] and out["not_found"] == ["Pâtes"]
    assert {p["name"]: p["qty"] for p in store.pantry} == {"Riz": "1 kg", "Œufs": "2", "Pain": None}
    assert "🥫 Stock : +Pain, −Cabillaud, Œufs (2)" in tools.build_report(ledger, False)


def test_pantry_context_shows_age_and_stock_reply(tmp_path):
    today = datetime.date(2026, 11, 9)
    pantry = [{"name": "Cabillaud", "qty": "600 g", "added": "2026-11-06"}, {"name": "Pain", "qty": None, "added": "2026-11-09"}]
    ctx = coaching.pantry_context(pantry, today)
    assert ctx == "- Cabillaud (600 g) — acheté il y a 3 j\n- Pain — acheté aujourd'hui"
    assert "vide" in coaching.pantry_context([], today)
    reply = commands.format_stock_reply(pantry, today)
    assert "(2 articles)" in reply and "- Cabillaud (600 g) · il y a 3 j" in reply
    assert "Rien d'enregistré" in commands.format_stock_reply([], today)


def test_eat_command_is_read_only_on_haiku_with_time_and_questions(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch)
    calls = []

    async def fake_ask(prompt, chat_id, allow_writes=True, planning=False):
        calls.append((prompt, allow_writes, planning))
        return agent.AgentReply("ok")

    monkeypatch.setattr(bot_module.agent, "ask", fake_ask)

    async def reply_text(text, **kwargs):
        pass

    update = NS(message=NS(reply_text=reply_text), effective_message=NS(reply_text=reply_text),
                effective_chat=NS(id=42, send_action=_noop))
    asyncio.run(b.on_eat(update, NS(args=[])))
    (prompt, allow_writes, planning), = calls
    assert allow_writes is False and planning is False
    assert "Que dois-je manger maintenant ? Il est " in prompt and "stock" in prompt and "questions" in prompt
    prompt = commands.eat_prompt(datetime.datetime(2026, 11, 9, 12, 30))
    assert "lun 09/11 12:30" in prompt and "acheter" in prompt and "update" not in prompt.lower().replace("mettre à jour", "")
