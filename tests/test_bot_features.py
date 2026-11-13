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
    b._pending, b._pending_update, b._draining, b._last_message = [], None, False, 0.0
    monkeypatch.setattr(bot_module, "MESSAGE_DEBOUNCE_S", 0)

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


def test_check_new_activity_sends_raw_recap_when_agent_fails(tmp_path, monkeypatch):
    runs = [_run(1)]
    b, store = _bot(tmp_path, monkeypatch, runs)
    store.last_activity_id = 1
    runs.insert(0, _run(2))

    async def broken_ask(*args, **kwargs):
        raise RuntimeError("API indisponible")

    monkeypatch.setattr(bot_module.agent, "ask", broken_ask)
    assert asyncio.run(b.check_new_activity()) is True
    recap, checkin = b.sent
    assert recap[0].startswith("⚡ *Nouveau run détecté !*") and "Run 2" in recap[0] and "/debrief" in recap[0]
    assert "Run 2" in checkin[0]
    assert store.last_activity_id == 2


def test_check_new_activity_retries_when_telegram_fails(tmp_path, monkeypatch):
    runs = [_run(1)]
    b, store = _bot(tmp_path, monkeypatch, runs)
    store.last_activity_id = 1
    runs.insert(0, _run(2))
    sender = b._sender

    async def down(text, **kwargs):
        raise RuntimeError("Telegram injoignable")

    b._sender = lambda update=None: down
    assert asyncio.run(b.check_new_activity()) is False
    assert store.last_activity_id == 1   # pas marqué comme vu : rien n'est perdu

    b._sender = sender
    assert asyncio.run(b.check_new_activity()) is True
    assert b.sent[0][0].startswith("⚡") and "Run 2" in b.sent[1][0] and store.last_activity_id == 2



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
    def __init__(self, data, markup=None):
        self.data, self.answers, self.edits = data, [], []
        self.message = NS(reply_markup=markup)   # clavier du message touché

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_reply_markup(self, markup):
        self.edits.append(("markup", markup))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


def _click(b, data, markup=None):
    query = FakeQuery(data, markup)
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
    (debrief, no_buttons), (text, markup) = b.sent   # le débrief, puis la proposition dans sa propre bulle
    assert debrief.startswith("⚡") and no_buttons is None and "💡" not in debrief
    assert text.strip() == "💡 Déplacer le fractionné de mardi à mercredi 18:30"
    (pid,) = {data.split(":")[2] for _, data in _buttons(markup)}
    assert [label for label, _ in _buttons(markup)] == ["✅ Oui, vas-y", "✏️ Modifier", "❌ Non"]

    q = _click(b, f"p:a:{pid}", markup)
    assert q.edits == [("💡 Déplacer le fractionné de mardi à mercredi 18:30\n✅ C'est parti.", None)]   # choix affiché, plus de boutons
    prompt, allow_writes = asked[-1]
    assert allow_writes is True and "Déplacer le fractionné de mardi à mercredi 18:30" in prompt
    n = len(asked)
    assert _click(b, f"p:a:{pid}").answers == ["Déjà traité."] and len(asked) == n   # double clic sans effet


def test_proposal_ignore_and_weather_adapt(tmp_path, monkeypatch):
    asked = []
    b, store = _bot(tmp_path, monkeypatch, asked=asked)
    pid = store.add_proposal("Décale la sortie longue")
    q = _click(b, f"p:x:{pid}", bot_module.proposal_markup(pid))
    assert store.proposal(pid)["status"] == "ignored" and not asked and "On garde" in q.edits[-1][0]
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
    assert items[0] == {"name": "Bananes", "qty": "7", "aisle": "🥦 Fruits et légumes", "keeps": None, "done": False}
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


# --- Modification de la liste en cours ------------------------------------------------

def test_edit_shopping_list_checks_removes_adds_and_syncs_event(tmp_path, monkeypatch):
    from running_coach import tools
    store = Store(tmp_path / "s.json")
    monkeypatch.setattr(tools, "store", store)
    synced = []
    monkeypatch.setattr(tools.cs, "set_shopping_description", lambda uid, text: synced.append((uid, text)))
    lst = store.set_shopping_list([{"name": "Bananes", "qty": "7"}, {"name": "Riz"}, {"name": "Œufs", "qty": "6"},
                                   {"name": "Thon en boîte"}], days=7)
    store.attach_shopping_event(lst["id"], "uid-courses")
    store.toggle_shopping_item(lst["id"], 2)          # Œufs déjà cochés (index 2)

    ledger = tools.Ledger()
    out = tools.tool_edit_shopping_list({"check": ["bananes", "Thon en boîte"], "remove": ["Riz"],
                                         "add": [{"name": "Pain", "quantity": "1"}], "uncheck": ["Pâtes"]}, ledger)
    new = store.shopping_list
    assert [(i["name"], i["done"]) for i in new["items"]] == [("Bananes", True), ("Œufs", True), ("Thon en boîte", True), ("Pain", False)]
    assert out["removed"] == ["Riz"] and out["added"] == ["Pain"] and out["not_found"] == ["Pâtes"]
    assert new["id"] != lst["id"] and new["event_uid"] == "uid-courses" and new["days"] == 7   # même événement
    # Stock : les articles cochés y sont, une seule fois, rattachés à leur nouvelle position
    assert sorted((p["name"], p["source"]) for p in store.pantry) == [
        ("Bananes", f"liste {new['id']}:0"), ("Thon en boîte", f"liste {new['id']}:2"), ("Œufs", f"liste {new['id']}:1")]
    store.toggle_shopping_item(new["id"], 1)          # décocher Œufs dans la nouvelle checklist : sort du stock
    assert "Œufs" not in [p["name"] for p in store.pantry]
    assert store.toggle_shopping_item(lst["id"], 0) is None   # anciens boutons inactifs
    # Agenda courses mis à jour, checklist renvoyée, compte-rendu
    assert synced[-1][0] == "uid-courses" and "✅ Bananes — 7" in synced[-1][1] and "Riz" not in synced[-1][1]
    assert [a.detail for a in ledger.actions if a.kind == "shopping"] == [str(new["id"])]
    assert "✏️ Liste de courses modifiée : ✅ Bananes, ✅ Thon en boîte, −Riz, +Pain" in tools.build_report(ledger, False)


def test_shopping_list_is_in_the_coach_context(tmp_path):
    store = Store(tmp_path / "s.json")
    assert coaching.shopping_context(None) == "aucune"
    lst = store.set_shopping_list([{"name": "Bananes", "qty": "7", "aisle": "🥦 Fruits"}, {"name": "Riz"}], days=7)
    start = datetime.datetime(2026, 11, 10, 18, 0, tzinfo=datetime.timezone.utc)
    store.attach_shopping_event(lst["id"], "u", start, start + datetime.timedelta(minutes=30))
    store.toggle_shopping_item(lst["id"], 0)
    ctx = coaching.shopping_context(store.shopping_list)
    assert ctx.startswith("(2 articles, 1 achetés, couvre 7 jours, dans l'agenda courses le mar 10/11 19:00)")
    assert "- ✅ Bananes (7) [🥦 Fruits]\n- ☐ Riz" in ctx


def test_edit_without_list_fails_cleanly(tmp_path):
    import pytest
    with pytest.raises(LookupError):
        Store(tmp_path / "s.json").edit_shopping_list(check=["Riz"])


# --- Plats mangés / cuisinés et péremption du stock ------------------------------------

def test_propose_meal_tool_records_a_meal_with_its_ingredients(tmp_path, monkeypatch):
    import pytest
    from running_coach import tools
    store = Store(tmp_path / "s.json")
    monkeypatch.setattr(tools, "store", store)
    ledger = tools.Ledger()
    out = tools.tool_propose_meal({"title": "Bowl saumon riz", "uses": [
        {"name": "Saumon"}, {"name": "Riz", "qty_left": "700 g"}, {"name": " "}]}, ledger)
    meal = store.proposal(out["meal_id"])
    assert meal["kind"] == "meal" and meal["text"] == "Bowl saumon riz"
    assert meal["uses"] == [{"name": "Saumon"}, {"name": "Riz", "qty_left": "700 g"}]
    assert [(a.kind, a.label, a.detail) for a in ledger.actions] == [("meal", "Bowl saumon riz", str(out["meal_id"]))]
    with pytest.raises(ValueError):
        tools.tool_propose_meal({"title": "Rien", "uses": []}, ledger)
    assert "propose_meal" in {t["name"] for t in tools.READ_ONLY_TOOLS}   # /eat et /recipes sont en lecture seule


def test_meal_button_removes_ingredients_once(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch)
    store.update_pantry(add=[{"name": "Saumon", "qty": "2 pavés"}, {"name": "Riz", "qty": "1 kg"}, {"name": "Pain"}])
    mid = store.add_proposal("Bowl saumon riz", kind="meal",
                             uses=[{"name": "saumon"}, {"name": "Riz", "qty_left": "700 g"}, {"name": "Avocat"}])
    other = store.add_proposal("Tartines", kind="meal", uses=[{"name": "Pain"}])
    asyncio.run(b._deliver(agent.AgentReply("Voici le plat", meals=[(mid, "Bowl saumon riz"), (other, "Tartines")])))
    text, markup = b.sent[-1]
    assert "Touche un plat" in text
    assert _buttons(markup) == [("✅ Bowl saumon riz", f"m:{mid}"), ("✅ Tartines", f"m:{other}")]

    q = _click(b, f"m:{mid}", markup)
    assert {p["name"]: p["qty"] for p in store.pantry} == {"Riz": "700 g", "Pain": None}
    assert _buttons(q.edits[-1][1]) == [("✅ Tartines", f"m:{other}")]        # seul le bouton touché disparaît
    assert "−Saumon, Riz (700 g)" in b.sent[-1][0] and "Avocat" in b.sent[-1][0]
    assert _click(b, f"m:{mid}").answers == ["Déjà noté."]                    # double clic sans effet
    assert {p["name"] for p in store.pantry} == {"Riz", "Pain"}
    assert _click(b, f"m:{other}", without := bot_module.without_button(markup, f"m:{mid}")).edits[-1] == ("markup", None)
    assert without is not None and [p["name"] for p in store.pantry] == ["Riz"]


def test_eat_choice_sends_the_recipe_updates_stock_and_drops_other_dishes(tmp_path, monkeypatch):
    from running_coach import tools
    b, store = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(tools, "store", store)
    store.update_pantry(add=[{"name": "Pâtes", "qty": "500 g"}, {"name": "Thon"}])
    ledger = tools.Ledger()
    pasta = tools.tool_propose_meal({"title": "Pâtes au thon", "kcal": 700, "recipe": "1. Cuire les pâtes\n2. Ajouter le thon",
                                     "uses": [{"name": "Pâtes", "qty_left": "350 g"}, {"name": "Thon"}]}, ledger)["meal_id"]
    omelette = tools.tool_propose_meal({"title": "Omelette", "recipe": "🛒 Œufs\n1. Battre", "uses": []}, ledger)["meal_id"]
    asyncio.run(b._deliver(agent.AgentReply("Deux idées", meals=[(pasta, "Pâtes au thon"), (omelette, "Omelette")])))
    text, markup = b.sent[-1]
    assert "Choisis ton plat" in text and "Cuire" not in text                 # recette envoyée au choix seulement
    assert _buttons(markup) == [("🍽️ Pâtes au thon", f"m:{pasta}"), ("🍽️ Omelette", f"m:{omelette}")]

    q = _click(b, f"m:{pasta}", markup)
    assert q.edits[-1] == ("markup", None)                                    # les autres plats tombent
    assert "Pâtes au thon" in b.sent[-1][0] and "1. Cuire les pâtes" in b.sent[-1][0] and "−Thon" in b.sent[-1][0]
    assert {p["name"]: p["qty"] for p in store.pantry} == {"Pâtes": "350 g"}
    assert [m["kcal"] for m in store.meals_since(datetime.date(2020, 1, 1))] == [700]
    assert _click(b, f"m:{omelette}").answers == ["Déjà noté."]


def test_recipes_keep_their_cooked_buttons_when_redisplayed(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch)
    lst = store.set_shopping_list([{"name": "Poulet", "keeps": 3}])
    mid = store.add_proposal("Curry de poulet", kind="meal", uses=[{"name": "Poulet"}])

    async def fake_ask(prompt, chat_id, allow_writes=True, planning=False):
        assert "propose_meal" in prompt
        return agent.AgentReply("🗓️ Recettes", meals=[(mid, "Curry de poulet")])

    monkeypatch.setattr(bot_module.agent, "ask", fake_ask)
    replies = []

    async def reply_text(text, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    update = NS(message=NS(reply_text=reply_text), effective_message=NS(reply_text=reply_text),
                effective_chat=NS(id=42, send_action=_noop))
    asyncio.run(b.on_recipes(update, NS(args=[])))
    assert store.shopping_list["recipe_meals"] == [mid]
    asyncio.run(b.on_recipes(update, NS(args=[])))                          # réaffichage sans IA, boutons compris
    assert replies[-1][0].startswith("🗓️ Recettes") and _buttons(replies[-1][1]) == [("✅ Curry de poulet", f"m:{mid}")]
    store.close_proposal(mid, "applied")
    asyncio.run(b.on_recipes(update, NS(args=[])))
    assert replies[-1][0].strip() == "🗓️ Recettes" and replies[-1][1] is None   # déjà cuisiné : plus de bouton
    assert lst["id"] == store.shopping_list["id"]


def test_fresh_items_get_a_keep_by_date_from_list_and_conversation(tmp_path, monkeypatch):
    from running_coach import memory
    store = Store(tmp_path / "s.json")
    lst = store.set_shopping_list([{"name": "Cabillaud", "keeps": 2}, {"name": "Riz", "keeps": None},
                                   {"name": "Sel", "keeps": "999"}])
    for i in range(3):
        store.toggle_shopping_item(lst["id"], i)
    store.update_pantry(add=[{"name": "Salade", "keeps_days": 4}])
    today = datetime.datetime.now(memory.TZ).date()
    due = {p["name"]: p["due"] for p in store.pantry}
    assert due == {"Cabillaud": (today + datetime.timedelta(days=2)).isoformat(), "Riz": None, "Sel": None,
                   "Salade": (today + datetime.timedelta(days=4)).isoformat()}
    assert len({p["id"] for p in store.pantry}) == 4
    # Modifier la liste ne remet pas les dates à zéro
    store.state["pantry"][0]["added"] = "2026-01-01"
    store.edit_shopping_list(add=[{"name": "Pain", "keeps_days": 2}])
    assert next(p for p in store.pantry if p["name"] == "Cabillaud")["added"] == "2026-01-01"
    ctx = coaching.pantry_context(store.pantry, today)
    assert "Salade — acheté aujourd'hui — se garde jusqu'au " in ctx and "- Riz — acheté aujourd'hui\n" in ctx + "\n"


def test_old_pantry_items_get_ids_on_load(tmp_path):
    import json
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"pantry": [{"name": "Riz", "qty": None, "added": "2026-11-01", "source": None}]}))
    store = Store(path)
    assert store.pantry[0]["id"] == 1 and store.state["next_pantry_id"] == 2


def test_daily_pantry_check_asks_then_expires(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch)
    store.update_pantry(add=[{"name": "Saumon", "qty": "300 g", "keeps_days": 2}, {"name": "Brocoli", "keeps_days": 2},
                             {"name": "Riz"}, {"name": "Yaourts", "keeps_days": 10}])
    ids = {p["name"]: p["id"] for p in store.pantry}
    for p in store.state["pantry"]:   # achetés il y a 3 jours
        p["added"] = "2026-11-07"
        p["due"] = p["due"] and ("2026-11-09" if p["keeps"] == 2 else "2026-11-17")

    class Day(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2026, 11, 10, 18, 0, tzinfo=tz)

    monkeypatch.setattr(bot_module.datetime, "datetime", Day)
    asyncio.run(b.check_pantry())
    text, markup = b.sent[-1]
    assert "Encore là ?" in text and "- Saumon (300 g) · acheté il y a 3 j" in text and "Riz" not in text
    assert _buttons(markup) == [("✅ Saumon", f"k:y:{ids['Saumon']}"), ("🗑️ Fini", f"k:n:{ids['Saumon']}"),
                                ("✅ Brocoli", f"k:y:{ids['Brocoli']}"), ("🗑️ Fini", f"k:n:{ids['Brocoli']}")]
    assert "à vérifier" in coaching.pantry_context(store.pantry, datetime.date(2026, 11, 10))
    n = len(b.sent)
    asyncio.run(b.check_pantry())                                            # déjà demandé : pas de relance
    assert len(b.sent) == n

    q = _click(b, f"k:y:{ids['Saumon']}", markup)                            # encore là : date repoussée
    saumon = next(p for p in store.pantry if p["name"] == "Saumon")
    assert saumon["due"] == "2026-11-11" and saumon["asked"] is None and "encore là" in q.answers[-1]
    left = q.edits[-1][1]
    assert _buttons(left) == [("✅ Brocoli", f"k:y:{ids['Brocoli']}"), ("🗑️ Fini", f"k:n:{ids['Brocoli']}")]
    q = _click(b, f"k:n:{ids['Brocoli']}", left)                             # fini : retiré, message clos
    assert "Brocoli" not in {p["name"] for p in store.pantry} and q.edits[-1] == ("🥫 Stock vérifié ✅", None)
    assert _click(b, f"k:n:{ids['Brocoli']}").answers == ["Déjà retiré du stock."]

    # Sans réponse 3 jours après la question : retiré automatiquement
    store.mark_pantry_asked([ids["Saumon"]], datetime.date(2026, 11, 10))
    Day.now = classmethod(lambda cls, tz=None: datetime.datetime(2026, 11, 13, 18, 0, tzinfo=tz))
    asyncio.run(b.check_pantry())
    assert "Retirés du stock (pas de réponse) : Saumon" in b.sent[-1][0]
    assert {p["name"] for p in store.pantry} == {"Riz", "Yaourts"}


def test_pantry_check_time_setting(monkeypatch):
    from running_coach import config
    assert config._briefing_time("18:30", "PANTRY_CHECK_TIME") == (18, 30)
    assert config._briefing_time("off", "PANTRY_CHECK_TIME") is None
    import pytest
    with pytest.raises(ValueError, match="PANTRY_CHECK_TIME"):
        config._briefing_time("soir", "PANTRY_CHECK_TIME")


# --- Journal alimentaire et « qu'as-tu mangé ? » -------------------------------------

def test_meal_windows_and_slots():
    times = [(10, 30), (14, 30), (21, 30)]
    assert coaching.meal_window(times, 0) == (datetime.time(5, 30), datetime.time(10, 30), "petit-déjeuner")
    assert coaching.meal_window(times, 1) == (datetime.time(10, 30), datetime.time(14, 30), "déjeuner")
    assert coaching.meal_window(times, 2) == (datetime.time(16, 30), datetime.time(21, 30), "dîner")   # 5 h max
    meals = [{"date": "2026-11-11", "time": "16:00"}]
    day = datetime.date(2026, 11, 11)
    assert not coaching.has_meal_between(meals, day, datetime.time(16, 30), datetime.time(21, 30))   # collation
    assert coaching.has_meal_between(meals, day, datetime.time(10, 30), datetime.time(16, 0))
    from running_coach import config
    assert config._meal_times("21:30, 10:30") == [(10, 30), (21, 30)] and config._meal_times("off") == []


def test_log_meal_tool_logs_updates_stock_and_reports(tmp_path, monkeypatch):
    import pytest
    from running_coach import tools
    store = Store(tmp_path / "s.json")
    monkeypatch.setattr(tools, "store", store)
    store.update_pantry(add=[{"name": "Curry de poulet", "qty": "4 boîtes"}, {"name": "Pommes", "qty": "6"}])
    ledger = tools.Ledger()
    out = tools.tool_log_meal({"description": "Curry de poulet (1 boîte) + pomme", "time": "2026-11-10T12:45:00",
                               "kcal": 720, "protein_g": "42", "carbs_g": 85.4,
                               "uses": [{"name": "Curry de poulet", "qty_left": "3 boîtes"}, {"name": "Pommes", "qty_left": "5"}]}, ledger)
    assert out["logged"]["time"] == "12:45" and out["logged"]["protein_g"] == 42 and out["logged"]["carbs_g"] == 85
    assert {p["name"]: p["qty"] for p in store.pantry} == {"Curry de poulet": "3 boîtes", "Pommes": "5"}
    report = tools.build_report(ledger, False)
    assert "🍽️ Repas noté : 12:45 Curry de poulet" in report and "🥫 Stock : Curry de poulet (3 boîtes), Pommes (5)" in report
    with pytest.raises(ValueError):
        tools.tool_log_meal({"description": "Dîner", "time": "2099-01-01T20:00:00"}, ledger)
    store.log_meal("Porridge banane", datetime.datetime(2026, 11, 11, 7, 30, tzinfo=tools.cs.TZ), kcal=450, protein_g=15)
    ctx = coaching.meals_context(store.meals_since(datetime.date(2026, 11, 10)), datetime.date(2026, 11, 11))
    assert "hier (mar 10/11) :\n- 12:45 Curry de poulet (1 boîte) + pomme (~720 kcal, P 42 g, G 85 g)" in ctx
    assert "aujourd'hui (mer 11/11) :\n- 07:30 Porridge banane (~450 kcal, P 15 g)\n  total estimé : ~450 kcal, P 15 g" in ctx
    assert {"log_meal", "propose_meal"} <= {t["name"] for t in tools.READ_ONLY_TOOLS}


def test_eaten_button_logs_the_meal_and_cooked_button_stocks_boxes(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch)
    store.update_pantry(add=[{"name": "Saumon"}, {"name": "Poulet"}, {"name": "Riz", "qty": "1 kg"}])
    eaten = store.add_proposal("Bowl saumon", kind="meal", uses=[{"name": "Saumon"}], makes=None,
                               macros={"kcal": 650, "protein_g": 40, "carbs_g": None})
    cooked = store.add_proposal("Curry de poulet", kind="meal", uses=[{"name": "Poulet"}, {"name": "Riz", "qty_left": "600 g"}],
                                makes="4 boîtes", macros={})
    _click(b, f"m:{eaten}")
    (meal,) = store.meals_since(datetime.date(2020, 1, 1))
    assert meal["text"] == "Bowl saumon" and meal["kcal"] == 650 and meal["source"] == "bouton"
    _click(b, f"m:{cooked}")
    assert "cuisiné" in b.sent[-1][0] and "+Curry de poulet (4 boîtes)" in b.sent[-1][0]
    assert {p["name"]: p["qty"] for p in store.pantry} == {"Riz": "600 g", "Curry de poulet": "4 boîtes"}
    assert len(store.meals_since(datetime.date(2020, 1, 1))) == 1                # cuisiné ≠ mangé


def _at(monkeypatch, hour, minute, day=11):
    class Now(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2026, 11, day, hour, minute, tzinfo=tz)
    monkeypatch.setattr(bot_module.datetime, "datetime", Now)


def test_meal_question_only_without_any_trace_and_once(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch)
    import dataclasses
    monkeypatch.setattr(bot_module, "settings",
                        dataclasses.replace(bot_module.settings, meal_check_times=[(10, 30), (14, 30), (21, 30)]))
    tz = bot_module.TZ
    store.log_meal("Porridge", datetime.datetime(2026, 11, 11, 7, 45, tzinfo=tz))
    _at(monkeypatch, 10, 30)
    asyncio.run(b.check_meal(0))                                             # petit-déj noté : pas de question
    assert not b.sent

    _at(monkeypatch, 14, 30)
    asyncio.run(b.check_meal(1))
    text, markup = b.sent[-1]
    assert "Ce midi, tu as mangé quoi ?" in text and "texte ou en vocal" in text   # rien à deviner
    assert _buttons(markup) == [("🙅 Sauté", "q:s:2026-11-11:1")]
    asyncio.run(b.check_meal(1))                                             # déjà posée : pas deux fois
    assert len(b.sent) == 1

    _click(b, "q:s:2026-11-11:1")                                            # sauté : noté à la fin de la plage
    skipped = store.meals_since(datetime.date(2026, 11, 11))[-1]
    assert (skipped["time"], skipped["text"], skipped["kcal"]) == ("14:30", "rien (déjeuner sauté)", 0)
    assert "déjeuner sauté" in b.sent[-1][0]
    _at(monkeypatch, 21, 30)                                                 # le « sauté » n'efface pas le dîner
    asyncio.run(b.check_meal(2))
    assert "Ce soir" in b.sent[-1][0]
    _click(b, "q:x:2026-11-11:2")                                            # « Passer » des anciens messages
    assert store.meal_check("2026-11-11 dîner") == "skipped" and "on passe" in b.sent[-1][0]


# --- Boîtes (tups) -------------------------------------------------------------------

def test_boxes_are_counted_from_cooked_dishes_and_shrink_when_eaten(tmp_path, monkeypatch):
    from running_coach import tools
    b, store = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(tools, "store", store)
    store.update_pantry(add=[{"name": "Poulet"}, {"name": "Riz", "qty": "1 kg"}])
    assert coaching.boxes_context(store.pantry, 3) == "0/3 occupées → 3 libre(s)"
    cooked = store.add_proposal("Curry de poulet", kind="meal", uses=[{"name": "Poulet"}], makes="2 boîtes", macros={})
    _click(b, f"m:{cooked}")
    assert "📦 Boîtes : 2/3 occupées (Curry de poulet ×2) → 1 libre(s)" in b.sent[-1][0]
    tools.tool_log_meal({"description": "Curry (1 boîte)", "uses": [{"name": "Curry de poulet", "qty_left": "1 boîte"}]},
                        tools.Ledger())
    assert coaching.boxes_context(store.pantry, 3) == "1/3 occupées (Curry de poulet ×1) → 2 libre(s)"
    tools.tool_update_stock({"add": [{"name": "Chili", "qty": "2 boîtes", "boxes": True}]}, tools.Ledger())
    assert coaching.boxes_in_use(store.pantry) == [("Curry de poulet", 1), ("Chili", 2)]
    assert coaching.box_count(None) == 1 and coaching.box_count("3 tups") == 3


def test_a_dish_cannot_need_more_boxes_than_owned(tmp_path, monkeypatch):
    import pytest
    from running_coach import tools
    monkeypatch.setattr(tools, "store", Store(tmp_path / "s.json"))
    with pytest.raises(ValueError, match="3 boîtes"):
        tools.tool_propose_meal({"title": "Lasagnes", "makes": "6 boîtes", "uses": [{"name": "Pâtes"}]}, tools.Ledger())
    assert tools.tool_propose_meal({"title": "Lasagnes", "makes": "3 boîtes", "uses": [{"name": "Pâtes"}]},
                                   tools.Ledger())["ok"]


def test_meal_prep_prompts_aim_at_filling_free_boxes_with_variety():
    for prompt in (commands.eat_prompt(datetime.datetime(2026, 11, 9, 12, 30)),
                   commands.recipes_prompt({"items": [{"name": "Riz"}]}),
                   commands.shopping_prompt(7, datetime.date(2026, 11, 9))):
        assert "boîtes" in prompt and "vari" in prompt
    assert "PRÉPARATION DES REPAS" in agent.SYSTEM_PROMPT


def test_messages_sent_in_a_row_get_one_reply(tmp_path, monkeypatch):
    asked = []
    b, store = _bot(tmp_path, monkeypatch, asked=asked)
    monkeypatch.setattr(bot_module, "MESSAGE_DEBOUNCE_S", 0.05)

    async def reply_text(text, **kwargs):
        pass

    def update(text):
        return NS(message=NS(text=text, reply_text=reply_text), effective_message=NS(reply_text=reply_text),
                  effective_chat=NS(id=42, send_action=_noop))

    async def burst():
        first = asyncio.create_task(b.on_message(update("fais toutes les modifs pour les +10 %"), None))
        await asyncio.sleep(0.01)
        await b.on_message(update("et une semaine à 15 km max toutes les 3 semaines"), None)   # rend la main tout de suite
        await first

    asyncio.run(burst())
    (prompt, _), = asked
    assert "à traiter ensemble" in prompt and "1. fais toutes les modifs" in prompt and "2. et une semaine" in prompt
    assert bot_module.merge_messages(["seul"]) == "seul"



def test_each_proposal_has_its_own_bubble_and_answering_one_keeps_the_other(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch, proposals=["On passe jeudi à 6,5 km ?", "On retire le dimanche ?"])
    reply = asyncio.run(bot_module.agent.ask("x", "42"))
    asyncio.run(b._deliver(reply))
    assert [t.strip() for t, _ in b.sent] == ["débrief", "💡 On passe jeudi à 6,5 km ?", "💡 On retire le dimanche ?"]
    (p1, m1), (p2, m2) = [(int(_buttons(m)[0][1].split(":")[2]), m) for _, m in b.sent[1:]]
    _click(b, f"p:x:{p1}", m1)
    assert store.proposal(p1)["status"] == "ignored" and store.proposal(p2)["status"] == "open"
    assert _click(b, f"p:x:{p2}", m2).answers == [None]                       # la seconde répond toujours


def test_old_message_with_two_proposals_only_loses_the_answered_row(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch)
    p1, p2 = store.add_proposal("a", summary="A ?"), store.add_proposal("b", summary="B ?")
    old = bot_module.InlineKeyboardMarkup([*bot_module.proposal_markup(p1).inline_keyboard,
                                           *bot_module.proposal_markup(p2).inline_keyboard])
    q = _click(b, f"p:x:{p1}", old)
    (kind, markup), = q.edits
    assert kind == "markup" and {d.split(":")[2] for _, d in _buttons(markup)} == {str(p2)}


def test_debrief_that_hangs_falls_back_to_raw_stats(tmp_path, monkeypatch):
    runs = [_run(1)]
    b, store = _bot(tmp_path, monkeypatch, runs)
    store.last_activity_id = 0

    async def hang(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(bot_module.agent, "ask", hang)
    monkeypatch.setattr(bot_module, "DEBRIEF_TIMEOUT_S", 0.01)
    assert asyncio.run(b.check_new_activity()) is True
    assert "Run 1" in b.sent[0][0] and "indisponible" in b.sent[0][0]
    assert store.last_activity_id == 1 and bot_module.ACTIVITY_STATUS["result"] == "débrief envoyé : 1"



def test_proactive_messages_reach_send_message_with_bound_chat_id(tmp_path, monkeypatch):
    """Régression : le texte passé en positionnel tombait sur chat_id (« multiple values for argument 'chat_id' »)."""
    sent = []

    async def send_message(chat_id, text, parse_mode=None, reply_markup=None):   # même ordre que telegram.Bot
        sent.append((chat_id, text))

    b = bot_module.CoachBot.__new__(bot_module.CoachBot)
    b.chat_id, b.app = "42", NS(bot=NS(send_message=send_message))
    asyncio.run(b._push("⚡ *Nouveau run détecté !*"))
    assert sent == [("42", "⚡ *Nouveau run détecté !*\n")] or sent == [("42", "⚡ *Nouveau run détecté !*")]



def test_meal_question_guesses_boxes_and_suggested_dishes_one_tap(tmp_path, monkeypatch):
    import dataclasses
    b, store = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot_module, "settings",
                        dataclasses.replace(bot_module.settings, meal_check_times=[(10, 30), (14, 30), (21, 30)]))
    store.update_pantry(add=[{"name": "Chili", "qty": "2 boîtes", "boxes": True}, {"name": "Thon", "qty": "2 boîtes"}])
    suggested = store.add_proposal("Pâtes au thon", kind="meal", uses=[{"name": "Thon", "qty_left": "1 boîte"}],
                                   macros={"kcal": 650})
    _at(monkeypatch, 14, 30)
    asyncio.run(b.check_meal(1))
    text, markup = b.sent[-1]
    assert text.startswith("🍽️ *Ce midi, tu as mangé quoi ?*") and "Touche le plat" in text
    labels = [label for label, _ in _buttons(markup)]
    assert labels == ["🍱 Chili", "🍽️ Pâtes au thon", "🙅 Sauté"]   # boîte d'abord, puis le plat proposé

    chili = next(d for label, d in _buttons(markup) if "Chili" in label)
    q = _click(b, chili, markup)
    assert q.edits == [("markup", None)]                                       # la question est close
    meal = store.meals_since(datetime.date(2026, 11, 11))[-1]
    assert (meal["text"], meal["time"]) == ("Chili", "14:30")                  # à l'heure du repas, pas du clic
    assert b.sent[-1][0].strip() == "👍 Chili, noté. Il reste : Chili (1 boîte)."
    assert store.meal_check("2026-11-11 déjeuner") == "answered"
    assert store.proposal(suggested)["status"] == "open"                       # pas mangé : reste candidat
    assert _click(b, chili, markup).answers == ["Déjà noté."]

    _at(monkeypatch, 21, 30)                                                   # dîner : le plat proposé, une boîte de moins
    asyncio.run(b.check_meal(2))
    _, markup = b.sent[-1]
    pasta = next(d for label, d in _buttons(markup) if "Pâtes" in label)
    _click(b, pasta, markup)
    assert store.proposal(suggested)["status"] == "applied"                    # l'original ne sera plus proposé
    assert {p["name"]: p["qty"] for p in store.pantry}["Thon"] == "1 boîte"
    assert store.meals_since(datetime.date(2026, 11, 11))[-1]["kcal"] == 650


def test_last_box_is_said_and_skipping_closes_the_guesses(tmp_path, monkeypatch):
    import dataclasses
    b, store = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot_module, "settings",
                        dataclasses.replace(bot_module.settings, meal_check_times=[(14, 30), (21, 30)]))
    store.update_pantry(add=[{"name": "Curry", "qty": "1 boîte", "boxes": True}])
    _at(monkeypatch, 14, 30)
    asyncio.run(b.check_meal(0))
    _, markup = b.sent[-1]
    _click(b, _buttons(markup)[0][1], markup)
    assert "dernière boîte" in b.sent[-1][0] and not store.pantry

    store.update_pantry(add=[{"name": "Dahl", "qty": "2 boîtes", "boxes": True}])
    _at(monkeypatch, 21, 30)
    asyncio.run(b.check_meal(1))
    _, markup = b.sent[-1]
    dahl = _buttons(markup)[0][1]
    _click(b, "q:s:2026-11-11:1", markup)
    assert _click(b, dahl, markup).answers == ["Déjà noté."]                  # sauté : le plat deviné ne compte plus


def test_meal_suggestion_before_eating_and_when_it_stays_quiet(tmp_path, monkeypatch):
    b, store = _bot(tmp_path, monkeypatch)
    calls = []

    async def fake_ask(prompt, chat_id, allow_writes=True, planning=False, skip_marker=None):
        calls.append((prompt, allow_writes, skip_marker))
        mid = store.add_proposal("Omelette", kind="meal", uses=[], recipe="1. Casser les œufs")
        return agent.AgentReply("Pour ce midi, je te propose :", meals=[(mid, "Omelette")])

    monkeypatch.setattr(bot_module.agent, "ask", fake_ask)
    _at(monkeypatch, 11, 30)
    asyncio.run(b.suggest_meal())
    prompt, allow_writes, marker = calls[-1]
    assert "[Suggestion automatique avant le repas]" in prompt and "bureau" in prompt and "question" in prompt
    assert allow_writes is False and marker == bot_module.commands.NO_SUGGESTION
    text, markup = b.sent[-1]
    assert text.startswith("Pour ce midi") and [l for l, _ in _buttons(markup)] == ["🍽️ Omelette"]

    asyncio.run(b.suggest_meal())                       # dishes just offered (as after /eat): no second message
    assert len(calls) == 1

    async def office(*args, **kwargs):                  # the coach knows lunch is at the office: nothing sent
        return agent.AgentReply("")

    monkeypatch.setattr(bot_module.agent, "ask", office)
    for item in store.state["proposals"].values():
        item["status"] = "ignored"
    n = len(b.sent)
    asyncio.run(b.suggest_meal())
    assert len(b.sent) == n

    store.log_meal("Sandwich", datetime.datetime(2026, 11, 11, 11, 0, tzinfo=bot_module.TZ))
    monkeypatch.setattr(bot_module.agent, "ask", fake_ask)
    asyncio.run(b.suggest_meal())                       # already eaten: no suggestion
    assert len(calls) == 1


def test_eat_suggest_times_setting():
    from running_coach import config
    assert config._meal_times("07:00,11:30,18:30", "EAT_SUGGEST_TIMES") == [(7, 0), (11, 30), (18, 30)]
    assert config._meal_times("off", "EAT_SUGGEST_TIMES") == []
