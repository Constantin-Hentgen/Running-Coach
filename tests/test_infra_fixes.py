"""Écritures CalDAV vérifiées, parsing BRouter, budget de temps des boucles, docs FastAPI, défauts de config."""
import datetime
import threading
import time

import pytest
from caldav.lib.error import NotFoundError

from running_coach import calendar_store as cs
from running_coach import routes
from running_coach.config import UTC

H = datetime.timedelta(hours=1)


class Obj:
    def __init__(self, cal, uid, data):
        self.cal, self.uid, self.data = cal, uid, data

    def save(self):
        self.cal.store[self.uid] = self.cal.mangle(self.data)

    def delete(self):
        if not self.cal.keep_on_delete:
            self.cal.store.pop(self.uid, None)


class FakeCal:
    """Agenda en mémoire ; `mangle` permet de simuler une écriture perdue côté serveur."""
    def __init__(self):
        self.store, self.keep_on_delete, self.mangle = {}, False, lambda d: d

    def add_event(self, ics):
        uid = next(l[4:] for l in ics.splitlines() if l.startswith("UID:"))
        self.store[uid] = self.mangle(ics)

    def get_event_by_uid(self, uid):
        if uid not in self.store:
            raise NotFoundError(uid)
        return Obj(self, uid, self.store[uid])

    def search(self, **_):
        return [Obj(self, u, d) for u, d in self.store.items()]


@pytest.fixture
def cal(monkeypatch):
    fake = FakeCal()
    monkeypatch.setattr(cs, "get_calendar", lambda: fake)
    return fake


def future(hours=24):
    return (cs.now_utc() + datetime.timedelta(hours=hours)).replace(microsecond=0)


def test_create_returns_uid_and_read_back_times(cal):
    s = future()
    res = cs.create_event("Sortie", s, s + H, None)
    assert res["uid"].endswith("@running-coach") and res["uid"] in cal.store
    assert abs((res["start"] - s).total_seconds()) < 1 and abs((res["end"] - s - H).total_seconds()) < 1


def test_create_refuses_past_and_bad_range(cal):
    past = cs.now_utc() - datetime.timedelta(days=2)
    with pytest.raises(ValueError, match="passé"):
        cs.create_event("x", past, past + H, None)
    s = future()
    with pytest.raises(ValueError, match="postérieur"):
        cs.create_event("x", s, s, None)
    assert not cal.store


def test_create_readback_mismatch_raises(cal):
    s = future()
    cal.mangle = lambda d: d.replace(f"DTSTART:{s:%Y%m%dT%H%M%S}Z", f"DTSTART:{s + 3 * H:%Y%m%dT%H%M%S}Z")   # début décalé
    with pytest.raises(RuntimeError):
        cs.create_event("x", s, s + H, None)


def test_update_keeps_duration_and_bumps_sequence(cal):
    s = future()
    uid = cs.create_event("Sortie", s, s + 2 * H, None)["uid"]
    new = s + datetime.timedelta(hours=5)
    res = cs.update_event(uid, start=new)
    assert abs((res["start"] - new).total_seconds()) < 1
    assert abs((res["end"] - res["start"] - 2 * H).total_seconds()) < 1
    assert int(cs.parse_vevent(cal.store[uid])[1]["SEQUENCE"]) == 1
    cs.update_event(uid, summary="Fractionné")
    assert int(cs.parse_vevent(cal.store[uid])[1]["SEQUENCE"]) == 2


def test_update_past_event_refused(cal):
    past = cs.now_utc() - datetime.timedelta(days=2)
    uid = "old@running-coach"
    cal.add_event(cs.build_event(uid, "Vieux", past, past + H, "x"))
    with pytest.raises(PermissionError):
        cs.update_event(uid, summary="Nouveau")
    with pytest.raises(LookupError):
        cs.update_event("inconnu@x", summary="a")


def test_update_readback_mismatch_raises(cal):
    s = future()
    uid = cs.create_event("Sortie", s, s + H, None)["uid"]
    cal.mangle = lambda d: d.replace("Fractionné", "Sortie")   # titre perdu côté serveur
    with pytest.raises(RuntimeError, match="titre"):
        cs.update_event(uid, summary="Fractionné")


def test_delete_removes_and_verifies(cal):
    s = future()
    uid = cs.create_event("Sortie", s, s + H, None)["uid"]
    res = cs.delete_event(uid)
    assert res["uid"] == uid and uid not in cal.store


def test_delete_still_present_raises(cal):
    s = future()
    uid = cs.create_event("Sortie", s, s + H, None)["uid"]
    cal.keep_on_delete = True
    with pytest.raises(RuntimeError, match="toujours présent"):
        cs.delete_event(uid)


def test_delete_past_event_refused(cal):
    past = cs.now_utc() - datetime.timedelta(days=2)
    cal.add_event(cs.build_event("old@running-coach", "Vieux", past, past + H, "x"))
    with pytest.raises(PermissionError):
        cs.delete_event("old@running-coach")
    assert "old@running-coach" in cal.store


def test_calendar_client_is_per_thread(monkeypatch):
    made = []

    class Client:
        def __init__(self, **kw):
            made.append(self)

        def calendar(self, url):
            return object()

    monkeypatch.setattr(cs.caldav, "DAVClient", Client)
    monkeypatch.setattr(cs, "_local", threading.local())
    a = cs.get_calendar()
    assert cs.get_calendar() is a
    seen = []
    t = threading.Thread(target=lambda: seen.append(cs.get_calendar()))
    t.start()
    t.join()
    assert seen[0] is not a and len(made) == 2


# --- BRouter ---------------------------------------------------------------

HEADER = ["Longitude", "Latitude", "Elevation", "Distance", "CostPerKm", "ElevCost", "TurnCost", "NodeCost",
          "InitialCost", "WayTags", "NodeTags", "Time", "Energy"]


def _row(dist, tags):
    return ["2.3", "48.8", "35", str(dist), "0", "0", "0", "0", "0", tags, "", "1", "1"]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_route_parses_brouter_geojson(monkeypatch):
    payload = {"features": [{
        "properties": {"track-length": "5000", "filtered ascend": "42", "messages": [
            HEADER, _row(2000, "highway=footway surface=asphalt"), _row(1000, "highway=residential"),
            _row(500, "highway=primary"), _row(500, "highway=path"), _row(1000, "surface=gravel")]},
        "geometry": {"coordinates": [[2.3, 48.8, 35], [2.31, 48.81, 40], [2.32, 48.82]]}}]}
    calls = {}

    def fake_get(url, **kw):
        calls.update(kw)
        return FakeResponse(payload)

    monkeypatch.setattr(routes.requests, "get", fake_get)
    loop = routes.route([(48.8, 2.3), (48.81, 2.31)])
    assert loop.km == pytest.approx(5.0) and loop.climb == 42
    assert loop.quiet == pytest.approx(2500 / 5000)    # footway + path
    assert loop.calm == pytest.approx(1000 / 5000)
    assert loop.busy == pytest.approx(500 / 5000)
    assert loop.points[0] == (48.8, 2.3, 35) and loop.points[2][2] is None
    assert calls["timeout"] == routes.BROUTER_TIMEOUT_S <= 15


# --- find_loops : budget de temps -------------------------------------------

def _fake_loop(bearing):
    pts = [(48.8 + i * 0.001, 2.3 + bearing * 1e-5 * i, None) for i in range(20)]
    return routes.Loop(points=pts, km=10.0, quiet=0.5, calm=0.3, busy=0.0, bearing=bearing)


def test_find_loops_skips_bearings_over_budget(monkeypatch):
    def fake(start, km, bearing):
        if bearing in (60, 180):
            time.sleep(1.5)
        return _fake_loop(bearing)

    monkeypatch.setattr(routes, "loop_at", fake)
    monkeypatch.setattr(routes, "ROUTES_BUDGET_S", 0.3)
    t0 = time.monotonic()
    loops = routes.find_loops((48.8, 2.3), 10, count=6)
    assert time.monotonic() - t0 < 1.2
    assert loops and all(lp.bearing not in (60, 180) for lp in loops)


def test_find_loops_deterministic_when_all_fast(monkeypatch):
    monkeypatch.setattr(routes, "loop_at", lambda s, km, b: _fake_loop(b))
    a = [lp.bearing for lp in routes.find_loops((48.8, 2.3), 10, count=3)]
    b = [lp.bearing for lp in routes.find_loops((48.8, 2.3), 10, count=3)]
    assert a == b and len(a) == 3


# --- App et config -----------------------------------------------------------

def test_app_docs_disabled():
    from running_coach.app import app
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None


def test_nutrition_check_time_default(monkeypatch):
    from running_coach import config
    monkeypatch.delenv("NUTRITION_CHECK_TIME", raising=False)
    src = open(config.__file__, encoding="utf-8").read()
    assert '"NUTRITION_CHECK_TIME", "22:00"' in src
    assert config._briefing_time(config._str("NUTRITION_CHECK_TIME", "22:00"), "NUTRITION_CHECK_TIME") == (22, 0)
