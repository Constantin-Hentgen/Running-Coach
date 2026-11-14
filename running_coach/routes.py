"""Boucles de course autour d'un lieu (maison, travail), à partir de données ouvertes.

- Itinéraires : BRouter (https://brouter.de, open source, données OpenStreetMap), profil randonnée, qui préfère les
  chemins, parcs et rues calmes aux grands axes. Chaque segment renvoie le type de voie OSM : on en déduit la part du
  parcours sur des voies sûres et calmes.
- Une boucle part du lieu, passe par deux points à distance choisie (un triangle), et revient au lieu : départ et
  arrivée au même endroit.
- « Jamais faite » : comparée aux tracés des runs Dreeve (polylignes encodées).
- Sorties : un fichier GPX et une image de carte (tuiles OpenStreetMap).
"""
import io
import logging
import math
import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import requests

from .config import settings

logger = logging.getLogger(__name__)

BROUTER_URL = "https://brouter.de/brouter"
PROFILE = "hiking-mountain"
USER_AGENT = "running-coach-bot/1.0 (self-hosted personal assistant)"
ROUTE_FACTOR = 1.25          # une boucle suit les rues : ~25 % plus longue que le triangle à vol d'oiseau
DISTANCE_TOLERANCE = 0.12    # ±12 % de la distance voulue
NEVER_DONE_MIN = 0.8         # « jamais faite » : au moins 80 % du parcours hors de mes anciens tracés
GRID_DEG = 0.0003            # ~30 m : une case de la grille des endroits déjà courus
QUIET = {"footway", "path", "pedestrian", "track", "cycleway", "living_street", "bridleway", "steps", "corridor"}
CALM = {"residential", "service", "unclassified", "road"}
BUSY = {"primary", "primary_link", "secondary", "secondary_link", "trunk", "trunk_link", "tertiary", "tertiary_link"}


@dataclass
class Loop:
    points: list[tuple[float, float, float | None]]   # (lat, lon, altitude)
    km: float
    quiet: float          # part sur chemins, parcs, pistes cyclables et voies piétonnes
    calm: float           # part sur petites rues
    busy: float           # part sur grands axes
    climb: int = 0
    novelty: float | None = None   # part du parcours jamais courue (si comparée)
    bearing: float = 0
    tags: dict = field(default_factory=dict)

    @property
    def safe(self) -> float:
        return self.quiet + self.calm

    def score(self, km: float) -> float:
        """Plus c'est calme et proche de la distance voulue, mieux c'est ; les allers-retours sont pénalisés."""
        return self.quiet + 0.6 * self.calm - 1.5 * self.busy - 2 * abs(self.km - km) / km - 0.8 * backtrack(self.points)


# ---------------------------------------------------------------------------
# Géométrie
# ---------------------------------------------------------------------------

def offset(lat: float, lon: float, km: float, bearing_deg: float) -> tuple[float, float]:
    b = math.radians(bearing_deg)
    dlat = km / 111.2 * math.cos(b)
    dlon = km / (111.2 * math.cos(math.radians(lat))) * math.sin(b)
    return lat + dlat, lon + dlon


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return round(lat / GRID_DEG), round(lon / GRID_DEG)


def backtrack(points: list) -> float:
    """Part du parcours qui repasse au même endroit (aller-retour) : 0 = vraie boucle."""
    seen, again, cells = {}, 0, [_cell(p[0], p[1]) for p in points]
    for i, c in enumerate(cells):
        if c in seen and i - seen[c] > 10:   # revenu sur une case quittée depuis longtemps
            again += 1
        seen.setdefault(c, i)
    return again / max(1, len(cells))


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Polyligne encodée (format Google, utilisé par Strava / Dreeve) → [(lat, lon)]."""
    points, index, lat, lon = [], 0, 0, 0
    while index < len(encoded or ""):
        for coord in range(2):
            shift, result = 0, 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if coord == 0:
                lat += delta
            else:
                lon += delta
        points.append((lat / 1e5, lon / 1e5))
    return points


def run_cells(runs: list[dict]) -> set[tuple[int, int]]:
    """Les cases (~30 m) où je suis déjà passé en courant, avec leurs voisines (tracés GPS imprécis)."""
    cells = set()
    for r in runs:
        pts = decode_polyline(r.get("encodedPolyline") or "")
        for (a, b), (c, d) in zip(pts, pts[1:] or pts):
            steps = max(1, int(haversine_km((a, b), (c, d)) / 0.02))   # un point tous les ~20 m
            for k in range(steps + 1):
                lat, lon = a + (c - a) * k / steps, b + (d - b) * k / steps
                x, y = _cell(lat, lon)
                cells.update((x + i, y + j) for i in (-1, 0, 1) for j in (-1, 0, 1))
    return cells


def novelty(points: list, done: set[tuple[int, int]]) -> float:
    if not points:
        return 0.0
    return sum(_cell(p[0], p[1]) not in done for p in points) / len(points)


# ---------------------------------------------------------------------------
# BRouter
# ---------------------------------------------------------------------------

def route(waypoints: list[tuple[float, float]]) -> Loop:
    lonlats = "|".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)
    res = requests.get(BROUTER_URL, timeout=max(settings.http_timeout, 20), headers={"User-Agent": USER_AGENT}, params={
        "lonlats": lonlats, "profile": PROFILE, "alternativeidx": 0, "format": "geojson"})
    res.raise_for_status()
    feature = res.json()["features"][0]
    props = feature["properties"]
    points = [(c[1], c[0], c[2] if len(c) > 2 else None) for c in feature["geometry"]["coordinates"]]
    by_class: dict[str, float] = {}
    header, *rows = props.get("messages") or [[]]
    di, ti = (header.index("Distance"), header.index("WayTags")) if header else (None, None)
    for row in rows:
        m = re.search(r"highway=(\S+)", row[ti] if ti is not None else "")
        kind = m.group(1) if m else "other"
        by_class[kind] = by_class.get(kind, 0) + float(row[di] or 0)
    total = sum(by_class.values()) or 1
    share = lambda kinds: sum(v for k, v in by_class.items() if k in kinds) / total
    return Loop(points=points, km=float(props.get("track-length") or 0) / 1000, quiet=share(QUIET), calm=share(CALM),
                busy=share(BUSY), climb=int(float(props.get("filtered ascend") or 0)), tags=by_class)


def loop_at(start: tuple[float, float], km: float, bearing: float) -> Loop:
    """Triangle départ → A → B → départ, orienté vers `bearing`, ajusté une fois pour coller à la distance."""
    radius = km / (3 * ROUTE_FACTOR)
    best = None
    for _ in range(2):
        a = offset(*start, radius, bearing - 30)
        b = offset(*start, radius, bearing + 30)
        loop = route([start, a, b, start])
        loop.bearing = bearing
        best = loop if best is None or abs(loop.km - km) < abs(best.km - km) else best
        if loop.km and abs(loop.km - km) / km <= DISTANCE_TOLERANCE:
            break
        radius *= km / loop.km if loop.km else 1
    return best


def find_loops(start: tuple[float, float], km: float, count: int = 3, runs: list[dict] | None = None,
               never_done: bool = False) -> list[Loop]:
    """Les meilleures boucles autour de `start` : orientations différentes, les plus calmes et sûres d'abord.
    never_done : seulement des boucles dont l'essentiel du parcours n'a jamais été couru."""
    done = run_cells(runs or []) if (runs or never_done) else set()
    bearings = [i * 360 / 8 for i in range(8)] if never_done else [i * 360 / 6 for i in range(6)]
    loops = []
    for bearing in bearings:
        try:
            loop = loop_at(start, km, bearing)
        except Exception as e:
            logger.warning(f"Boucle vers {bearing:.0f}° impossible : {e}")
            continue
        if loop.km <= 0:
            continue
        loop.novelty = novelty(loop.points, done) if (done or never_done) else None   # rien couru : tout est nouveau
        loops.append(loop)
    if never_done:
        loops = [lp for lp in loops if (lp.novelty or 0) >= NEVER_DONE_MIN] or \
                sorted(loops, key=lambda lp: lp.novelty or 0, reverse=True)[:1]
    loops.sort(key=lambda lp: lp.score(km) + (0.5 * lp.novelty if never_done and lp.novelty else 0), reverse=True)
    picked = []
    for lp in loops:   # des boucles vraiment différentes : orientations éloignées
        if all(min(abs(lp.bearing - p.bearing), 360 - abs(lp.bearing - p.bearing)) >= 90 for p in picked):
            picked.append(lp)
        if len(picked) == count:
            break
    return picked


# ---------------------------------------------------------------------------
# GPX et carte
# ---------------------------------------------------------------------------

def to_gpx(loop: Loop, name: str) -> bytes:
    pts = "\n".join(
        f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}">' + (f"<ele>{ele:.0f}</ele>" if ele is not None else "") + "</trkpt>"
        for lat, lon, ele in loop.points)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<gpx version="1.1" creator="running-coach" xmlns="http://www.topografix.com/GPX/1/1">\n'
            f'  <metadata><name>{escape(name)}</name><copyright author="OpenStreetMap contributors">'
            f'<license>https://opendatacommons.org/licenses/odbl/</license></copyright></metadata>\n'
            f'  <trk><name>{escape(name)}</name><trkseg>\n{pts}\n  </trkseg></trk>\n</gpx>\n').encode("utf-8")


def to_map_png(loop: Loop, start: tuple[float, float]) -> bytes:
    from staticmap import CircleMarker, Line, StaticMap
    m = StaticMap(800, 800, padding_x=40, padding_y=40, url_template="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                  headers={"User-Agent": USER_AGENT})
    coords = [(lon, lat) for lat, lon, _ in loop.points]
    m.add_line(Line(coords, "white", 9))
    m.add_line(Line(coords, "#FF5A1F", 5))
    m.add_marker(CircleMarker((start[1], start[0]), "white", 18))
    m.add_marker(CircleMarker((start[1], start[0]), "#14968A", 12))
    out = io.BytesIO()
    m.render().convert("RGB").save(out, format="JPEG", quality=85)
    return out.getvalue()


def describe(loop: Loop) -> str:
    parts = [f"{loop.km:.1f} km".replace(".", ","), f"{loop.safe * 100:.0f} % chemins et rues calmes"]
    if loop.climb >= 20:
        parts.append(f"D+ {loop.climb} m")
    if loop.novelty is not None:
        parts.append(f"{loop.novelty * 100:.0f} % jamais couru")
    return " · ".join(parts)
