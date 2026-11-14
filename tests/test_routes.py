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
