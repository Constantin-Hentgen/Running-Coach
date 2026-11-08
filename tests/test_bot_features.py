"""Détection des nouveaux runs, débrief, commandes Telegram, délai des alertes pluie."""
import asyncio

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


def _bot(tmp_path, monkeypatch, runs, asked):
    store = Store(tmp_path / "state.json")
    monkeypatch.setattr(bot_module, "store", store)
    monkeypatch.setattr(dreeve, "get_recent_runs", lambda: list(runs))

    async def fake_ask(prompt, chat_id, allow_writes=True):
        asked.append((prompt, allow_writes))
        return "débrief"

    monkeypatch.setattr(bot_module.agent, "ask", fake_ask)
    b = bot_module.CoachBot.__new__(bot_module.CoachBot)
    b.chat_id, b._activity_lock, b.pushed = "42", asyncio.Lock(), []

    async def push(text):
        b.pushed.append(text)

    b._push = push
    return b, store


def test_check_new_activity_debriefs_all_new_runs_once(tmp_path, monkeypatch):
    runs, asked = [_run(1)], []
    b, store = _bot(tmp_path, monkeypatch, runs, asked)

    assert asyncio.run(b.check_new_activity()) is False   # 1er passage : référence seulement
    assert store.last_activity_id == 1 and not asked

    runs[:0] = [_run(3), _run(2)]

    async def both():   # deux détections simultanées : un seul débrief
        return await asyncio.gather(b.check_new_activity(), b.check_new_activity())

    assert sorted(asyncio.run(both())) == [False, True]
    assert len(asked) == 1 and len(b.pushed) == 1
    prompt, allow_writes = asked[0]
    assert allow_writes is False
    assert "2 nouvelles séances" in prompt and prompt.index("Run 2") < prompt.index("Run 3")
    assert "Objectifs" in prompt and "prochaines séances" in prompt
    assert store.last_activity_id == 3



# --- Commandes ---------------------------------------------------------------

import datetime  # noqa: E402

from running_coach import commands, rain_alerts  # noqa: E402
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
    assert rain_alerts.lead_time(now + datetime.timedelta(hours=31, minutes=20), now) == "dans 31 h"
    assert rain_alerts.lead_time(now + datetime.timedelta(minutes=20), now) == "imminent"
    assert rain_alerts.lead_time(now + datetime.timedelta(days=3), now) == "dans 3 j"
