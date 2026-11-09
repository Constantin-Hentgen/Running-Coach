import datetime

import pytest

from running_coach import calendar_store as cs
from running_coach import dreeve, weather
from running_coach.config import TZ, UTC, parse_locations
from running_coach.formatting import md_escape, md_unescape, split_message
from running_coach.tools import Ledger, build_report, run_tool

ALL_DAY_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//FR
BEGIN:VEVENT
UID:race@test
DTSTAMP:20261101T000000Z
DTSTART;VALUE=DATE:20261121
DTEND;VALUE=DATE:20261122
SUMMARY:Course 10 km
END:VEVENT
END:VCALENDAR
"""

TZID_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//FR
BEGIN:VEVENT
UID:run@test
DTSTAMP:20261101T000000Z
DTSTART;TZID=Europe/Paris:20261110T183000
DTEND;TZID=Europe/Paris:20261110T193000
SUMMARY:Footing
SEQUENCE:2
END:VEVENT
END:VCALENDAR
"""


def paris(*args):
    return datetime.datetime(*args, tzinfo=TZ)


# --- Dates -----------------------------------------------------------------

def test_parse_dt_naive_is_paris():
    assert cs.parse_dt("2026-11-10T18:30:00") == paris(2026, 11, 10, 18, 30).astimezone(UTC)


def test_parse_range_date_only_end_is_inclusive():
    s, e = cs.parse_range("2026-11-09", "2026-11-15")
    assert e - s == datetime.timedelta(days=7)


# --- iCalendar (régression : « Unable to guess TZID for tzinfo UTC ») ------------

def test_all_day_event_bounds():
    _, comp = cs.parse_vevent(ALL_DAY_ICS)
    assert cs.is_all_day(comp)
    assert cs.event_bounds(comp) == (paris(2026, 11, 21), paris(2026, 11, 22))
    assert cs.serialize(comp)["all_day"] is True


def test_update_all_day_event_to_timed():
    start, end = paris(2026, 11, 21, 9, 30), paris(2026, 11, 21, 11, 0)
    out = cs.apply_update(ALL_DAY_ICS, start=start, end=end)
    assert "VALUE=DATE" not in out
    _, comp = cs.parse_vevent(out)
    assert not cs.is_all_day(comp)
    assert cs.event_bounds(comp) == (start, end)
    assert comp["SUMMARY"] == "Course 10 km"


def test_update_tzid_event_keeps_other_fields_and_bumps_sequence():
    start = paris(2026, 11, 11, 7, 0)
    out = cs.apply_update(TZID_ICS, summary="Footing matinal", start=start, end=start + datetime.timedelta(hours=1),
                          description="🏃 45'\n💧 eau")
    _, comp = cs.parse_vevent(out)
    assert comp["SUMMARY"] == "Footing matinal"
    assert str(comp["DESCRIPTION"]) == "🏃 45'\n💧 eau"
    assert int(comp["SEQUENCE"]) == 3
    assert cs.event_bounds(comp)[0] == start
    assert out.count("DTSTART") == 1


def test_build_event_roundtrip():
    start, end = paris(2026, 11, 12, 18, 0), paris(2026, 11, 12, 19, 0)
    _, comp = cs.parse_vevent(cs.build_event("u@x", "Fractionné", start, end, "desc"))
    assert cs.event_bounds(comp) == (start, end)
    assert cs.serialize(comp)["uid"] == "u@x"


# --- Outils / compte-rendu --------------------------------------------------------

def test_run_tool_refuses_tool_outside_allowed_set():
    ledger = Ledger()
    res = run_tool("delete_calendar_event", {"uid": "x"}, ledger, allowed={"list_calendar_events"})
    assert res["ok"] is False and not ledger.actions


def test_build_report_escapes_and_counts():
    ledger = Ledger()
    ledger.record("create", "Sortie_longue", True)
    ledger.record("delete", "Repos", False, "boom")
    report = build_report(ledger, truncated=True)
    assert "Sortie\\_longue" in report
    assert "1 réussie(s), 1 échec(s)" in report
    assert "interrompu" in report


def test_build_report_empty():
    assert build_report(Ledger(), truncated=False) == ""


# --- Telegram -------------------------------------------------------------------

def test_split_message_respects_limit_even_for_long_lines():
    text = "a" * 25 + "\n" + "b" * 5
    chunks = split_message(text, limit=10)
    assert all(len(c) <= 11 for c in chunks)   # +1 pour le \n final
    assert "".join(c.replace("\n", "") for c in chunks) == "a" * 25 + "b" * 5


def test_md_escape_roundtrip():
    s = "*gras* _it_ [lien] `code`"
    assert md_unescape(md_escape(s)) == s


# --- Dreeve / météo -------------------------------------------------------------

@pytest.mark.parametrize("act, km", [
    ({"distanceInMeter": 10500}, 10.5),
    ({"distance": 5000}, 5.0),
    ({"distanceInMeter": None, "distance": 3000}, 3.0),
    ({}, 0.0),
    ({"distance": "n/a"}, 0.0),
])
def test_distance_km(act, km):
    assert dreeve.distance_km(act) == km


DAILY = {"time": ["2026-11-09"], "weathercode": [61], "temperature_2m_min": [4.2], "temperature_2m_max": [11.0],
         "precipitation_sum": [3.1], "precipitation_probability_max": [80], "windspeed_10m_max": [22.0]}


def test_format_forecast_single_location():
    text = weather.format_forecast([DAILY], ["Maison"])
    assert "lun 09/11 : 🌧️ pluie, 4.2–11.0°C, pluie 3.1 mm (80 %)" in text
    assert "Maison" not in text   # un seul lieu : pas besoin de le nommer à chaque ligne
    assert weather.format_forecast([{}], ["Maison"]) == "indisponible"


def test_format_forecast_two_locations_grouped_by_day():
    work = {**DAILY, "weathercode": [0], "precipitation_sum": [0.0], "precipitation_probability_max": [5]}
    text = weather.format_forecast([DAILY, work], ["Maison", "Travail"])
    lines = text.strip("\n").split("\n")
    assert lines[0].strip() == "lun 09/11 :"
    assert lines[1].strip().startswith("Maison : 🌧️ pluie")
    assert lines[2].strip().startswith("Travail : ☀️ dégagé")


def test_parse_locations():
    assert parse_locations("Maison: 48.85, 2.35 | Travail: 48.89,2.24") == [
        ("Maison", 48.85, 2.35), ("Travail", 48.89, 2.24)]
    assert parse_locations("48.85,2.35") == [("Lieu 1", 48.85, 2.35)]
    assert parse_locations(None) == [] and parse_locations(" | ") == []
    with pytest.raises(ValueError):
        parse_locations("Maison: abc")
    with pytest.raises(ValueError):
        parse_locations("Maison: 148.85, 2.35")


def test_fetch_daily_handles_single_and_multi_responses(monkeypatch):
    from running_coach import config

    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    calls = []

    def fake_get(url, params, timeout):
        calls.append(params)
        n = len(params["latitude"].split(","))
        return Resp({"daily": DAILY} if n == 1 else [{"daily": DAILY}] * n)

    monkeypatch.setattr(weather.requests, "get", fake_get)
    two = [("Maison", 48.85, 2.35), ("Travail", 48.89, 2.24)]
    original = config.settings.weather_locations
    object.__setattr__(config.settings, "weather_locations", two)
    try:
        assert len(weather.fetch_daily()) == 2
        assert calls[-1]["latitude"] == "48.85,48.89" and calls[-1]["longitude"] == "2.35,2.24"
        object.__setattr__(config.settings, "weather_locations", two[:1])
        assert len(weather.fetch_daily()) == 1
    finally:
        object.__setattr__(config.settings, "weather_locations", original)


def test_location_roundtrip():
    start, end = paris(2026, 11, 12, 18, 0), paris(2026, 11, 12, 19, 0)
    ics = cs.build_event("u@x", "Fractionné", start, end, "desc", location="Travail")
    _, comp = cs.parse_vevent(ics)
    assert cs.serialize(comp)["location"] == "Travail"
    _, comp = cs.parse_vevent(cs.apply_update(ics, location="Maison"))
    assert cs.serialize(comp)["location"] == "Maison"
    _, comp = cs.parse_vevent(cs.build_event("v@x", "Repos", start, end, "desc"))
    assert "location" not in cs.serialize(comp)



# --- Agenda courses ---------------------------------------------------------------

class FakeObj:
    def __init__(self, cal, data):
        self.cal, self.data = cal, data

    def delete(self):
        self.cal.objs.remove(self)


class FakeShoppingCalendar:
    def __init__(self):
        self.objs = []

    def add(self, ics):
        self.objs.append(FakeObj(self, ics))

    def add_event(self, ics):
        self.add(ics)

    def events(self):
        return [o for o in self.objs if "BEGIN:VEVENT" in o.data]

    def todos(self, include_completed=False):
        return [o for o in self.objs if "BEGIN:VTODO" in o.data]

    def get_event_by_uid(self, uid):
        return next(o for o in self.objs if f"UID:{uid}" in o.data)


def test_shopping_event_replaces_only_coachs_upcoming_list(monkeypatch):
    fake = FakeShoppingCalendar()
    monkeypatch.setattr(cs, "get_shopping_calendar", lambda: fake)
    now = cs.now_utc()
    later, earlier = now + datetime.timedelta(days=1), now - datetime.timedelta(days=3)
    hour = datetime.timedelta(hours=1)
    fake.add(cs.build_event("mine@phone", "Marché", later, later + hour, "à moi"))                       # à l'athlète
    fake.add(cs.build_event("old@running-coach-shopping", "🛒 Courses", earlier, earlier + hour, "x"))  # passé : gardé
    fake.add(cs.build_event("next@running-coach-shopping", "🛒 Courses", later, later + hour, "x"))     # à venir : remplacé
    fake.add("BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VTODO\nUID:t@running-coach-shopping\nSUMMARY:Bananes\nEND:VTODO\nEND:VCALENDAR\n")

    start = now + datetime.timedelta(hours=3)
    items = [{"name": "Bananes", "qty": "7", "aisle": "🥦 Fruits et légumes"}, {"name": "Riz", "qty": "500 g", "aisle": "🍚 Féculents"},
             {"name": "Pommes", "aisle": "🥦 Fruits et légumes", "done": True}]
    lst = {"items": items, "days": 7, "coverage": "déjeuners et dîners", "cost_min": 54.6, "cost_max": 70}
    r = cs.save_shopping_event(lst, start, start + datetime.timedelta(minutes=30))
    assert r["replaced"] == 2 and r["summary"] == "🛒 Courses (3 articles, 7 jours · 55–70 € estimés)"
    uids = {next(line for line in o.data.splitlines() if line.startswith("UID:")) for o in fake.objs}
    assert uids == {"UID:mine@phone", "UID:old@running-coach-shopping", f"UID:{r['uid']}"}
    _, comp = cs.parse_vevent(fake.get_event_by_uid(r["uid"]).data)
    assert str(comp["DESCRIPTION"]) == ("Couvre : 7 jours, déjeuners et dîners\nCoût estimé : 55–70 €\nRecettes : /recipes dans Telegram"
                                        "\n\n🥦 Fruits et légumes\n☐ Bananes — 7\n✅ Pommes\n\n🍚 Féculents\n☐ Riz — 500 g")
    with pytest.raises(ValueError):
        cs.save_shopping_event(lst, earlier, earlier + hour)   # passé refusé


def test_shopping_summary_and_cost_formats():
    assert cs.shopping_summary_line({"days": 7, "cost_min": 55, "cost_max": 70}) == "7 jours · 55–70 € estimés"
    assert cs.shopping_summary_line({"days": 1, "cost_min": 12.4, "cost_max": None}) == "1 jour · ~12 € estimés"
    assert cs.shopping_summary_line({"days": None, "cost_min": None, "cost_max": None}) == ""
