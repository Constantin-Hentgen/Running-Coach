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
    assert "semaine du 24/08" in text   # 12 semaines affichées, même vides
    assert "plus longue sortie : mer 28/10" in text and "21.1 km" in text
    assert "2 runs depuis le 2026-10-28, 31 km au total" in text
    assert "- 2026-10 : 21.1 km en 1 sortie(s)" in text and "- 2026-11 : 10.0 km en 1 sortie(s)" in text
    assert "meilleure allure sur 21.1 km ou plus : 2'22/km" in text
    listed = text.split("Tous les runs")[1]
    assert listed.index("activity-1)") < listed.index("activity-2)")   # chaque run, le plus récent d'abord


def test_format_runs_keeps_older_runs_monthly_only(monkeypatch):
    monkeypatch.setattr(dreeve, "DETAILED_RUNS", 1)
    runs = [RUN, {**RUN, "id": "activity-2", "startDateLocal": "2026-07-05T07:00:00", "description": "premier semi"}]
    text = dreeve.format_runs(runs, datetime.date(2026, 11, 8))
    assert "activity-2)" not in text and "le 1 plus anciens" not in text and "les 1 plus anciens" in text
    assert "- 2026-07 : 10.0 km" in text
    assert "« premier semi »" in dreeve.format_activity(runs[1])


def test_all_runs_paginated_and_cached(monkeypatch):
    pages = {1: [{"id": 3}, {"id": 2}], 2: [{"id": 1}]}
    calls = []

    def fake_get(path, timeout=None, **params):
        page = params.get("pagination[page]")
        calls.append(page)
        if page is None:   # get_recent_runs(1)
            return {"activities": [{"id": 3}]}
        return {"activities": pages[page], "pagination": {"totalPages": 2}}

    monkeypatch.setattr(dreeve, "_get_json", fake_get)
    monkeypatch.setattr(dreeve, "_history_cache", (0.0, None, []))
    assert [r["id"] for r in dreeve.get_all_runs()] == [3, 2, 1]
    assert [r["id"] for r in dreeve.get_all_runs()] == [3, 2, 1] and calls == [None, 1, 2, None]   # cache


def test_format_runs_empty():
    assert "aucun run" in dreeve.format_runs([], datetime.date(2026, 11, 8))


def test_format_load():
    text = dreeve.format_load({"ctl": 40, "atl": 50, "tsb": {"value": -10, "status": "NEUTRAL"},
                               "acRatio": {"value": 1.25, "status": "LOW_RISK"}, "weeklyTrimp": 300,
                               "monotony": 1.2, "strain": 360, "restDaysLast7Days": 2})
    assert "TSB -10 (NEUTRAL)" in text and "ratio A/C 1.25" in text


def test_memories_are_rendered_by_priority_and_old_ones_default_to_strong(tmp_path):
    store = Store(tmp_path / "s.json")
    store.remember("preference", "Court plutôt le midi", priority=3)
    store.remember("contrainte", "Footings de 4,5 km minimum", priority=1)
    old = store.remember("habitude", "Télétravail lundi et jeudi")
    del store.state["memories"][-1]["priority"]                       # souvenir d'avant les priorités
    store.remember("preference", "Pas deux sorties consécutives", priority="9")   # invalide → défaut
    text = store.render()
    rules, strong, hints = text.split("\n\n")
    assert rules.startswith("RÈGLES") and "#2 [contrainte] Footings de 4,5 km minimum" in rules
    assert strong.startswith("PRÉFÉRENCES FORTES") and f"#{old['id']} [habitude]" in strong and "consécutives" in strong
    assert hints.startswith("INDICATIONS") and "le midi" in hints


def test_remember_tool_takes_a_priority_and_merges_by_replacing(tmp_path, monkeypatch):
    from running_coach import tools
    store = Store(tmp_path / "s.json")
    monkeypatch.setattr(tools, "store", store)
    ledger = tools.Ledger()
    first = tools.tool_remember({"category": "preference", "text": "Footings de 4,5 km"}, ledger)
    tools.tool_remember({"category": "contrainte", "text": "Footings de 4,5 km minimum, 5 à 6,5 km ou sortie longue",
                         "priority": 1, "replaces_id": first["id"]}, ledger)
    (m,) = store.state["memories"]
    assert m["priority"] == 1 and m["text"].startswith("Footings de 4,5 km minimum")
    assert ledger.actions[-1].label.endswith("(règle)")
