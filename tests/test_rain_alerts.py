import datetime

import pytest

from running_coach import rain_alerts as ra
from running_coach.config import TZ, settings


def at(day, hour, minute=0):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def hourly(day, rain_hours, prob=80, mm=1.2):
    """Prévision horaire sur 24 h : pluie aux heures listées, sec ailleurs."""
    return [(at(day, h), prob if h in rain_hours else 5, mm if h in rain_hours else 0.0) for h in range(24)]


def event(uid, start, end, summary="Footing", **extra):
    return {"uid": uid, "summary": summary, "start": start.isoformat(), "end": end.isoformat(), "description": "", **extra}


@pytest.fixture(autouse=True)
def rain_settings():
    saved = {k: getattr(settings, k) for k in ("rain_hours_ahead", "rain_probability", "rain_min_mm", "rain_skip_keywords")}
    for k, v in {"rain_hours_ahead": 24, "rain_probability": 60, "rain_min_mm": 0.3,
                 "rain_skip_keywords": ["repos", "rest", "off"]}.items():
        object.__setattr__(settings, k, v)
    yield
    for k, v in saved.items():
        object.__setattr__(settings, k, v)


NOW = at(10, 8)
FORECASTS = {"Maison": hourly(10, {18, 19}), "Travail": hourly(10, set())}


def test_window_risk_uses_overlapping_hours_only():
    risk = ra.window_risk(FORECASTS["Maison"], at(10, 18, 30), at(10, 19, 30))
    assert risk.probability == 80 and risk.mm == 2.4   # heures 18h et 19h
    assert ra.window_risk(FORECASTS["Maison"], at(10, 12), at(10, 13)).is_rainy() is False
    assert ra.window_risk([], at(10, 12), at(10, 13)) is None


def test_alert_for_rainy_session_with_other_place_comparison():
    alerts = ra.find_alerts([event("a", at(10, 18, 30), at(10, 19, 30), location="Maison")], FORECASTS, NOW, lambda k: False)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.place == "Maison" and a.others["Travail"].mm == 0.0
    text = ra.format_alert(a)
    assert "Pluie prévue" in text and "mar 10/11 18:30–19:30" in text and "Travail" in text and "bonne alternative" in text


def test_place_resolution():
    places = ["Maison", "Travail"]
    assert ra.resolve_place({"location": "travail"}, places) == "Travail"
    assert ra.resolve_place({"summary": "Fractionné", "description": "📍 Départ du Travail"}, places) == "Travail"
    assert ra.resolve_place({"summary": "Footing"}, places) == "Maison"   # défaut : premier lieu


def test_dry_place_means_no_alert():
    ev = event("b", at(10, 18), at(10, 19), location="Travail")
    assert ra.find_alerts([ev], FORECASTS, NOW, lambda k: False) == []


@pytest.mark.parametrize("ev", [
    event("rest", at(10, 8), at(10, 23), summary="Repos"),                 # mot-clé + trop long
    event("rest2", at(10, 18), at(10, 19), summary="Jour OFF"),           # mot-clé
    event("allday", at(10, 0), at(11, 0), summary="Trail", all_day=True), # journée entière
    event("past", at(10, 6), at(10, 7)),                                  # déjà passé
    event("far", at(12, 18), at(12, 19)),                                 # au-delà de l'horizon
])
def test_non_training_or_out_of_window_events_are_ignored(ev):
    assert ra.find_alerts([ev], {"Maison": hourly(10, set(range(24))) + hourly(12, set(range(24)))},
                          NOW, lambda k: False) == []


def test_keyword_is_matched_as_whole_word():
    ev = event("c", at(10, 18), at(10, 19), summary="Footing depuis l'office")
    assert len(ra.find_alerts([ev], FORECASTS, NOW, lambda k: False)) == 1


def test_already_alerted_session_is_not_repeated_but_moved_session_is():
    ev = event("d", at(10, 18), at(10, 19))
    first = ra.find_alerts([ev], FORECASTS, NOW, lambda k: False)
    sent = {first[0].key}
    assert ra.find_alerts([ev], FORECASTS, NOW, sent.__contains__) == []
    moved = event("d", at(10, 19), at(10, 20))
    assert len(ra.find_alerts([moved], FORECASTS, NOW, sent.__contains__)) == 1


def test_thresholds():
    light = {"Maison": hourly(10, {18}, prob=90, mm=0.1)}   # probable mais négligeable
    unlikely = {"Maison": hourly(10, {18}, prob=30, mm=2.0)}
    ev = [event("e", at(10, 18), at(10, 19))]
    assert ra.find_alerts(ev, light, NOW, lambda k: False) == []
    assert ra.find_alerts(ev, unlikely, NOW, lambda k: False) == []


def test_store_dedup_persists_and_purges(tmp_path):
    from running_coach.memory import Store
    s = Store(tmp_path / "s.json")
    past_end = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    future_end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)
    s.mark_alerted("old", past_end)
    s.mark_alerted("new", future_end)
    reloaded = Store(tmp_path / "s.json")
    assert reloaded.was_alerted("new") and not reloaded.was_alerted("old")


def test_other_place_also_rainy_is_not_suggested():
    both = {"Maison": hourly(10, {18}), "Travail": hourly(10, {18}, prob=70, mm=0.8)}
    alerts = ra.find_alerts([event("f", at(10, 18), at(10, 19))], both, NOW, lambda k: False)
    assert "bonne alternative" not in ra.format_alert(alerts[0])
