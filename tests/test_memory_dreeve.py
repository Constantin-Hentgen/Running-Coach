import datetime

import pytest

from running_coach import dreeve, tools
from running_coach.memory import MAX_TEXT, Store, parse_seed
from running_coach.tools import Ledger, build_report, run_tool


SEED = [("contrainte", "Présentiel bureau lundi, mercredi et vendredi"), ("preference", "Repos de 08:00 à 23:00")]


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = Store(tmp_path / "state.json", seed=SEED)
    monkeypatch.setattr(tools, "store", s)
    return s


# --- Mémoire ----------------------------------------------------------------------

def test_parse_seed():
    assert parse_seed("contrainte: Bureau lun/mer/ven | préférence libre | objectif:Marathon 2027") == [
        ("contrainte", "Bureau lun/mer/ven"), ("autre", "préférence libre"), ("objectif", "Marathon 2027")]
    assert parse_seed(None) == [] and parse_seed("  ") == []


def test_new_store_without_seed_is_empty(tmp_path):
    assert Store(tmp_path / "s.json").memories == []


def test_new_store_is_seeded(store):
    assert len(store.memories) == 2
    assert "lundi, mercredi et vendredi" in store.render()


def test_remember_persists_across_reload(store, tmp_path):
    item = store.remember("preference", "Préfère courir le matin avant 8h")
    reloaded = Store(tmp_path / "state.json", seed=SEED)
    assert any(m["id"] == item["id"] and m["text"] == "Préfère courir le matin avant 8h" for m in reloaded.memories)
    assert len(reloaded.memories) == 3   # pas de ré-amorçage au rechargement


def test_remember_replaces_and_rejects_duplicates(store):
    old = store.remember("objectif", "Semi-marathon de Paris en mars 2027, objectif 1h45")
    new = store.remember("objectif", "Semi-marathon de Paris en mars 2027, objectif 1h40", replaces=old["id"])
    texts = [m["text"] for m in store.memories]
    assert new["text"] in texts and old["text"] not in texts
    with pytest.raises(ValueError):
        store.remember("objectif", new["text"].upper())


def test_remember_validates_input(store):
    with pytest.raises(ValueError):
        store.remember("preference", "x" * (MAX_TEXT + 1))
    with pytest.raises(ValueError):
        store.remember("preference", "   ")
    assert store.remember("inconnue", "Catégorie libre")["category"] == "autre"


def test_forget_unknown_id(store):
    with pytest.raises(LookupError):
        store.forget(999)


def test_history_and_last_activity_persist(store, tmp_path):
    store.set_history("42", [{"role": "user", "content": "hello"}])
    store.last_activity_id = "activity-7"
    reloaded = Store(tmp_path / "state.json")
    assert reloaded.history("42") == [{"role": "user", "content": "hello"}]
    assert reloaded.last_activity_id == "activity-7"


def test_corrupt_file_is_backed_up(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    s = Store(path, seed=SEED)
    assert len(s.memories) == 2
    assert (tmp_path / "state.bak").exists()


def test_memory_tools_go_through_ledger_and_report(store):
    ledger = Ledger()
    allowed = {"remember", "forget"}
    res = run_tool("remember", {"category": "santé", "text": "Gêne au tendon d'Achille gauche depuis le 05/11"}, ledger, allowed)
    assert res["ok"]
    assert run_tool("forget", {"id": res["id"]}, ledger, allowed)["ok"]
    assert run_tool("forget", {"id": 12345}, ledger, allowed)["ok"] is False
    report = build_report(ledger, truncated=False)
    assert "🧠 Noté" in report and "🗑️ Oublié" in report
    assert "Compte-rendu Radicale" not in report   # pas d'action agenda → pas de section agenda


# --- Dreeve REST ----------------------------------------------------------------

RUN = {
    "id": "activity-1", "name": "Footing", "sportType": "Run", "startDateLocal": "2026-11-05T07:10:00",
    "distanceInMeter": 10000, "movingTimeInSeconds": 3000, "elevationInMeter": 80, "averageHeartRate": 142,
}


def test_format_activity_pace_and_fields():
    line = dreeve.format_activity(RUN)
    assert "jeu 05/11" in line and "10.0 km" in line and "50min" in line and "5'00/km" in line
    assert "FC moy 142" in line and "activity-1" in line
    assert "[TrailRun]" in dreeve.format_activity({**RUN, "sportType": "TrailRun"})


def test_format_runs_weekly_volume():
    today = datetime.date(2026, 11, 8)   # dimanche
    runs = [RUN, {**RUN, "id": "activity-2", "startDateLocal": "2026-10-28T07:00:00", "distanceInMeter": 21100}]
    text = dreeve.format_runs(runs, today)
    assert "semaine du 02/11 : 10.0 km en 1 sortie(s)" in text
    assert "semaine du 26/10 : 21.1 km en 1 sortie(s)" in text
    assert "semaine du 28/09" in text   # 6 semaines affichées, même vides
    assert "Plus longue sortie" in text and "21.1 km" in text


def test_format_runs_empty():
    assert "Aucun run" in dreeve.format_runs([], datetime.date(2026, 11, 8))


def test_format_load():
    text = dreeve.format_load({"ctl": 40, "atl": 50, "tsb": {"value": -10, "status": "NEUTRAL"},
                               "acRatio": {"value": 1.25, "status": "LOW_RISK"}, "weeklyTrimp": 300,
                               "monotony": 1.2, "strain": 360, "restDaysLast7Days": 2})
    assert "TSB -10 (NEUTRAL)" in text and "ratio A/C 1.25" in text
