import datetime

import pytest

from running_coach import weather_alerts as wa
from running_coach.config import TZ, settings
from running_coach.memory import Store
from running_coach.weather_alerts import Hour


def at(day, hour, minute=0):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def hourly(day, rain_hours=(), prob=80, mm=1.2, temp=12.0, feels=None, gust=20.0, code=3, **by_hour):
    """Prévision horaire sur 24 h : pluie aux heures listées, sec ailleurs ; `by_hour` surcharge une heure."""
    hours = []
    for h in range(24):
        rainy = h in rain_hours
        hour = Hour(at(day, h), prob if rainy else 5, mm if rainy else 0.0, temp, feels if feels is not None else temp, gust, code)
        for field, value in by_hour.get(f"h{h}", {}).items():
            setattr(hour, field, value)
        hours.append(hour)
    return hours


def event(uid, start, end, summary="Footing", **extra):
    return {"uid": uid, "summary": summary, "start": start.isoformat(), "end": end.isoformat(), "description": "", **extra}


@pytest.fixture(autouse=True)
def alert_settings():
    values = {"rain_hours_ahead": 48, "rain_probability": 60, "rain_min_mm": 0.3, "rain_skip_keywords": ["repos", "rest", "off"],
              "heat_alert_c": 28.0, "gust_alert_kmh": 50.0, "freeze_alert_c": 1.0}
    saved = {k: getattr(settings, k) for k in values}
    for k, v in values.items():
        object.__setattr__(settings, k, v)
    yield
    for k, v in saved.items():
        object.__setattr__(settings, k, v)


NOW = at(10, 8)
FORECASTS = {"Maison": hourly(10, {18, 19}), "Travail": hourly(10)}
NOTHING_SENT = lambda key: None  # noqa: E731


def notices(events, forecasts=FORECASTS, state_of=NOTHING_SENT, now=NOW):
    return wa.find_notices(events, forecasts, now, state_of)


def test_rain_alert_with_other_place_comparison():
    found = notices([event("a", at(10, 18, 30), at(10, 19, 30), location="Maison")])
    assert len(found) == 1
    n = found[0]
    assert n.kind == "alert" and set(n.hazards) == {"pluie"} and n.others["Travail"] == {}
    text = wa.format_notice(n, NOW)
    assert "Pluie prévue" in text and "mar 10/11 18:30–19:30 (dans 10 h)" in text
    assert "Travail : ✅ RAS, bonne alternative" in text


def test_heat_wind_and_ice_hazards():
    hot = {"Maison": hourly(10, feels=31.0)}
    windy = {"Maison": hourly(10, gust=65.0)}
    icy_wet = {"Maison": hourly(10, temp=-1.0, h7={"mm": 0.5})}           # sol mouillé dans les 12 h avant
    icy_dry = {"Maison": hourly(10, temp=-1.0)}                            # froid mais sec : pas de verglas
    snowy = {"Maison": hourly(10, temp=0.5, code=73)}
    ev = [event("h", at(10, 18), at(10, 19))]
    assert set(notices(ev, hot)[0].hazards) == {"chaleur"}
    assert set(notices(ev, windy)[0].hazards) == {"vent"}
    assert set(notices(ev, icy_wet)[0].hazards) == {"verglas"}
    assert notices(ev, icy_dry) == []
    assert set(notices(ev, snowy)[0].hazards) == {"verglas"}
    text = wa.format_notice(notices(ev, windy)[0], NOW)
    assert "Météo à surveiller" in text and "rafales 65 km/h" in text


def test_dry_place_means_no_alert():
    assert notices([event("b", at(10, 18), at(10, 19), location="Travail")]) == []


@pytest.mark.parametrize("ev", [
    event("rest", at(10, 8), at(10, 23), summary="Repos"),                 # mot-clé + trop long
    event("rest2", at(10, 18), at(10, 19), summary="Jour OFF"),           # mot-clé
    event("allday", at(10, 0), at(11, 0), summary="Trail", all_day=True), # journée entière
    event("past", at(10, 6), at(10, 7)),                                  # déjà passé
    event("far", at(13, 18), at(13, 19)),                                 # au-delà de l'horizon (48 h)
])
def test_non_training_or_out_of_window_events_are_ignored(ev):
    rainy = {"Maison": hourly(10, set(range(24))) + hourly(13, set(range(24)))}
    assert notices([ev], rainy) == []


def test_session_two_days_ahead_is_watched():
    found = notices([event("j2", at(11, 18), at(11, 19))], {"Maison": hourly(11, {18})})
    assert len(found) == 1 and wa.lead_time(found[0].start, NOW) == "dans 34 h"


def test_keyword_is_matched_as_whole_word():
    assert len(notices([event("c", at(10, 18), at(10, 19), summary="Footing depuis l'office")])) == 1


def test_hazard_signalled_once_new_hazard_alerts_again_and_moved_session_is_reevaluated():
    ev = event("d", at(10, 18), at(10, 19))
    first = notices([ev])[0]
    sent = {first.key: {"hazards": first.signalled, "cleared": False}}
    assert notices([ev], state_of=sent.get) == []                         # même risque : silence
    windy_rain = {"Maison": hourly(10, {18, 19}, gust=70.0)}
    again = notices([ev], windy_rain, sent.get)
    assert len(again) == 1 and again[0].new == {"vent"} and set(again[0].signalled) == {"pluie", "vent"}
    moved = event("d", at(10, 19), at(10, 20))
    assert len(notices([moved], state_of=sent.get)) == 1


def test_all_clear_when_forecast_improves_then_rain_coming_back_alerts_again():
    ev = event("g", at(10, 18), at(10, 19))
    key = notices([ev])[0].key
    sent = {key: {"hazards": ["pluie"], "cleared": False}}
    dry = {"Maison": hourly(10)}
    clear = notices([ev], dry, sent.get)
    assert len(clear) == 1 and clear[0].kind == "clear" and clear[0].cleared == ["pluie"]
    assert "Bonne nouvelle" in wa.format_notice(clear[0], NOW)
    sent[key] = {"hazards": clear[0].signalled, "cleared": True}
    assert notices([ev], dry, sent.get) == []                             # une seule « bonne nouvelle »
    assert notices([ev], state_of=sent.get)[0].kind == "alert"             # la pluie revient : nouvelle alerte
    assert notices([ev], dry, {key: {"hazards": ["pluie"], "cleared": False}}.get, now=at(10, 17, 30)) == []  # trop tard


def test_thresholds():
    light = {"Maison": hourly(10, {18}, prob=90, mm=0.1)}   # probable mais négligeable
    unlikely = {"Maison": hourly(10, {18}, prob=30, mm=2.0)}
    ev = [event("e", at(10, 18), at(10, 19))]
    assert notices(ev, light) == [] and notices(ev, unlikely) == []


def test_other_place_also_rainy_is_not_suggested():
    both = {"Maison": hourly(10, {18}), "Travail": hourly(10, {18}, prob=70, mm=0.8)}
    text = wa.format_notice(notices([event("f", at(10, 18), at(10, 19))], both)[0])
    assert "bonne alternative" not in text and "Travail : ☔ pluie" in text


def test_adapt_prompt_names_session_uid_and_alternatives():
    n = notices([event("uid-42", at(10, 18), at(10, 19))])[0]
    prompt = wa.adapt_prompt(n)
    assert "UID uid-42" in prompt and "Travail : RAS" in prompt and "pluie" in prompt


def test_place_resolution():
    places = ["Maison", "Travail"]
    assert wa.resolve_place({"location": "travail"}, places) == "Travail"
    assert wa.resolve_place({"summary": "Fractionné au Travail"}, places) == "Travail"
    assert wa.resolve_place({"summary": "Footing"}, places) == "Maison"


def test_alert_state_persists_purges_and_migrates_old_format(tmp_path):
    import json
    future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)).isoformat()
    (tmp_path / "s.json").write_text(json.dumps({"rain_alerts": {"legacy": future}}))
    s = Store(tmp_path / "s.json")
    assert s.alert_state("legacy") == {"until": future, "hazards": ["pluie"], "cleared": False}
    past_end = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    future_end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)
    s.save_alert_state("old", past_end, ["pluie"])
    s.save_alert_state("new", future_end, ["vent", "pluie"])
    reloaded = Store(tmp_path / "s.json")
    assert reloaded.alert_state("new")["hazards"] == ["pluie", "vent"] and reloaded.alert_state("old") is None
