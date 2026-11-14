"""Boucles de course : choix du lieu et de la distance, polylignes Dreeve, « jamais faite », sélection, GPX."""
from running_coach import commands, routes

PLACES = [("maison", 48.80, 2.30), ("travail", 48.85, 2.35)]


def test_route_request_uses_todays_session_then_arguments():
    run = {"summary": "🏃 Footing facile 6,5 km", "location": "Travail"}
    assert commands.route_request([], PLACES, [run]) == ("travail", (48.85, 2.35), 6.5)
    assert commands.route_request(["maison", "10"], PLACES, [run]) == ("maison", (48.80, 2.30), 10.0)
    assert commands.route_request(["8km"], PLACES, []) == ("maison", (48.80, 2.30), 8.0)   # pas de séance : 1er lieu
    assert commands.route_request(["90"], PLACES, [])[2] == 30.0                         # bornée


def test_decode_polyline_and_novelty():
    assert routes.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    past = [{"encodedPolyline": "_p~iF~ps|U_ulLnnqC"}]
    done = routes.run_cells(past)
    assert routes.novelty([(38.5, -120.2, None)], done) == 0.0
    assert routes.novelty([(10.0, 10.0, None)], done) == 1.0


def _fake_loop(km, quiet, busy, points):
    return routes.Loop(points=points, km=km, quiet=quiet, calm=1 - quiet - busy, busy=busy)


def test_find_loops_prefers_quiet_distinct_loops_and_never_done(monkeypatch):
    def fake_loop_at(start, km, bearing):
        quiet = 0.9 if bearing in (0, 180) else 0.3
        pts = [(start[0] + 0.001 * i * (1 + bearing / 90), start[1], None) for i in range(50)]
        lp = _fake_loop(km, quiet, 0.05, pts)
        lp.bearing = bearing
        return lp

    monkeypatch.setattr(routes, "loop_at", fake_loop_at)
    loops = routes.find_loops((48.8, 2.3), 6, 3)
    assert [lp.bearing for lp in loops][:2] == [0, 180]                     # les plus calmes d'abord
    assert all(min(abs(a.bearing - b.bearing), 360 - abs(a.bearing - b.bearing)) >= 90
               for i, a in enumerate(loops) for b in loops[i + 1:])        # orientations différentes
    already = [{"encodedPolyline": ""}]
    new = routes.find_loops((48.8, 2.3), 6, 1, already, never_done=True)
    assert len(new) == 1 and new[0].novelty == 1.0


def test_gpx_and_description():
    lp = _fake_loop(6.24, 0.8, 0.0, [(48.8, 2.3, 35.0), (48.81, 2.31, None)])
    lp.novelty = 0.92
    gpx = routes.to_gpx(lp, "Boucle 1 · maison & co").decode()
    assert gpx.startswith('<?xml version="1.0"') and 'lat="48.800000" lon="2.300000"><ele>35</ele>' in gpx
    assert "maison &amp; co" in gpx and "OpenStreetMap" in gpx
    assert routes.describe(lp) == "6,2 km · 100 % chemins et rues calmes · 92 % jamais couru"


def test_backtrack_detects_out_and_back():
    loop = [(48.8 + 0.001 * i, 2.3, None) for i in range(40)] + [(48.8 + 0.001 * i, 2.31, None) for i in range(40)]
    out_and_back = [(48.8 + 0.001 * i, 2.3, None) for i in range(40)] + [(48.8 + 0.001 * i, 2.3, None) for i in range(39, -1, -1)]
    assert routes.backtrack(loop) < 0.05 < routes.backtrack(out_and_back)


# --- /newroute : options, lieu, revêtement, traversées (#40) ------------------

def test_parse_route_args_distance_surface_and_free_text_place():
    ask = commands.parse_route_args(["10", "route", "à", "bois", "de", "vincennes"], PLACES)
    assert (ask.km, ask.surface, ask.place, ask.query) == (10.0, "route", None, "bois de vincennes")
    ask = commands.parse_route_args(["trail", "travail", "8km"], PLACES)
    assert (ask.km, ask.surface, ask.place, ask.query) == (8.0, "nature", ("travail", 48.85, 2.35), "")
    ask = commands.parse_route_args([], PLACES)
    assert (ask.km, ask.surface, ask.place, ask.query) == (None, None, None, "")


def test_fill_from_session_uses_next_session_place_or_address():
    import datetime
    from running_coach.config import TZ
    now = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=TZ)
    done = {"summary": "🏃 Footing 5 km", "location": "maison", "start": "2026-11-10T07:00:00+01:00", "end": "2026-11-10T08:00:00+01:00"}
    later = {"summary": "🏃 Sortie longue 14 km", "location": "Parc de Sceaux", "start": "2026-11-11T09:00:00+01:00",
             "end": "2026-11-11T10:30:00+01:00"}
    ask = commands.fill_from_session(commands.parse_route_args([], PLACES), PLACES, [done, later], now)
    assert (ask.km, ask.query, ask.place) == (14.0, "Parc de Sceaux", None)     # séance passée ignorée ; adresse à géocoder
    forced = commands.fill_from_session(commands.parse_route_args(["travail", "6"], PLACES), PLACES, [later], now)
    assert (forced.km, forced.place[0], forced.query) == (6.0, "travail", "")    # ce qui est donné l'emporte


def _brouter_response(rows):
    header = ["Longitude", "Latitude", "Elevation", "Distance", "CostPerKm", "ElevCost", "TurnCost", "NodeCost",
              "InitialCost", "WayTags", "NodeTags", "Time", "Energy"]
    msgs = [header] + [["0", "0", "0", str(d), "0", "0", "0", "0", "0", way, node, "0", "0"] for d, way, node in rows]
    return {"features": [{"geometry": {"coordinates": [[2.3, 48.8, 35], [2.31, 48.81, 36]]},
                          "properties": {"track-length": "1000", "filtered ascend": "4", "messages": msgs}}]}


def test_route_reads_surface_crossings_and_waymarked_share(monkeypatch):
    rows = [(400, "highway=footway surface=asphalt", "highway=crossing crossing=traffic_signals"),
            (300, "highway=path route_hiking_lwn=yes", ""),                     # sans surface : chemin = nature
            (200, "highway=residential surface=gravel", "highway=traffic_signals"),
            (100, "highway=residential", "kerb=lowered")]
    monkeypatch.setattr(routes.requests, "get", lambda *a, **k: NS(raise_for_status=lambda: None,
                                                                   json=lambda: _brouter_response(rows)))
    lp = routes.route([(48.8, 2.3), (48.81, 2.31)])
    assert lp.km == 1.0 and lp.crossings == 2
    assert round(lp.paved, 2) == 0.5 and round(lp.marked, 2) == 0.3


def test_score_follows_wanted_surface_and_describe():
    pts = [(48.8 + 0.001 * i, 2.3, None) for i in range(40)] + [(48.8 + 0.001 * i, 2.31, None) for i in range(40)]
    road = routes.Loop(points=pts, km=6, quiet=0.3, calm=0.6, busy=0.0, paved=1.0, crossings=6)
    trail = routes.Loop(points=pts, km=6, quiet=0.8, calm=0.1, busy=0.0, paved=0.1, crossings=1, marked=0.5)
    assert road.score(6, "route") > trail.score(6, "route")
    assert trail.score(6, "nature") > road.score(6, "nature")
    busy_crossings = routes.Loop(points=pts, km=6, quiet=0.3, calm=0.6, busy=0.0, paved=1.0, crossings=60)
    assert road.score(6, "route") > busy_crossings.score(6, "route")              # moins de traversées
    assert routes.describe(road, "route") == "6,0 km · 100 % bitume · 90 % voies calmes · 6 traversées"
    assert routes.describe(trail, "nature") == "6,0 km · 90 % nature · 50 % sur sentiers balisés"


def test_find_loops_drops_out_and_back_when_a_real_loop_exists(monkeypatch):
    def fake_loop_at(start, km, bearing):
        out = [(48.8 + 0.001 * i, 2.3, None) for i in range(40)]
        pts = out + out[::-1] if bearing == 0 else out + [(48.8 + 0.001 * i, 2.31, None) for i in range(40)]
        lp = routes.Loop(points=pts, km=km, quiet=1.0 if bearing == 0 else 0.4, calm=0.0, busy=0.0)
        lp.bearing = bearing
        return lp

    monkeypatch.setattr(routes, "loop_at", fake_loop_at)
    loops = routes.find_loops((48.8, 2.3), 6, 3)
    assert loops and all(lp.bearing != 0 for lp in loops)   # l'aller-retour, pourtant le plus calme, est écarté


from types import SimpleNamespace as NS  # noqa: E402
