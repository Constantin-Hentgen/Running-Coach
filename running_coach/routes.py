"""Boucles de course autour d'un lieu (maison, travail), à partir de données ouvertes.

- Itinéraires : BRouter (https://brouter.de, open source, données OpenStreetMap), profil randonnée, qui préfère les
  chemins, parcs et rues calmes aux grands axes. Chaque segment renvoie le type de voie OSM : on en déduit la part du
  parcours sur des voies sûres et calmes.
- Une boucle part du lieu, passe par deux points à distance choisie (un triangle), et revient au lieu : départ et
  arrivée au même endroit.
- Revêtement voulu (`surface`) : « route » (bitume, voies calmes, le moins de traversées de rue possible),
  « nature » (chemins, pistes, terre) ou « mixte ». Les boucles avec des allers-retours (demi-tours) sont écartées
  dès qu'il existe une alternative.
- Validées par la communauté : la part du parcours sur des sentiers balisés cartographiés dans OpenStreetMap
  (relations route=hiking locales à internationales) donne un bonus.
- « Jamais faite » : comparée aux tracés des runs Dreeve (polylignes encodées).
- Lieu en texte libre (« bois de vincennes ») : géocodé avec Nominatim (OpenStreetMap), près des lieux connus.
- Sorties : un fichier GPX et une image de carte (tuiles OpenStreetMap).
"""
import io
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import requests

from .config import settings
from .i18n import _, decimal, plural

logger = logging.getLogger(__name__)

BROUTER_URL = "https://brouter.de/brouter"
PROFILE = "hiking-mountain"
USER_AGENT = "running-coach-bot/1.0 (self-hosted personal assistant)"
ROUTE_FACTOR = 1.25          # une boucle suit les rues : ~25 % plus longue que le triangle à vol d'oiseau
DISTANCE_TOLERANCE = 0.12    # ±12 % de la distance voulue
NEVER_DONE_MIN = 0.8         # « jamais faite » : au moins 80 % du parcours hors de mes anciens tracés
ROUTES_BUDGET_S = 45         # temps total max pour calculer toutes les orientations
ROUTES_WORKERS = 4
BROUTER_TIMEOUT_S = 15
GRID_DEG = 0.0003            # ~30 m : une case de la grille des endroits déjà courus
QUIET = {"footway", "path", "pedestrian", "track", "cycleway", "living_street", "bridleway", "steps", "corridor"}
CALM = {"residential", "service", "unclassified", "road"}
PAVED = {"asphalt", "paved", "concrete", "concrete:plates", "concrete:lanes", "paving_stones", "sett", "cobblestone",
         "metal", "wood", "chipseal", "unhewn_cobblestone"}
UNPAVED_WAYS = {"path", "track", "bridleway"}   # sans tag surface : considérés comme non revêtus
CROSSING_NODES = ("highway=crossing", "highway=traffic_signals")
SURFACES = ("route", "nature", "mixte")
MAX_BACKTRACK = 0.08         # au-delà : la boucle contient un aller-retour (demi-tour)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
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
    paved: float = 1.0    # part sur bitume, pavés, béton
    crossings: int = 0    # passages piétons et feux traversés
    marked: float = 0.0   # part sur des sentiers balisés (OSM)

    @property
    def safe(self) -> float:
        return self.quiet + self.calm

    @property
    def crossings_per_km(self) -> float:
        return self.crossings / self.km if self.km else 0.0

    def score(self, km: float, surface: str = "mixte") -> float:
        """Proche de la distance voulue, sans aller-retour, et selon le revêtement voulu :
        route → bitume calme avec peu de traversées ; nature → chemins et terre ; mixte → le plus calme."""
        base = -2 * abs(self.km - km) / km - 3 * backtrack(self.points) + 0.4 * self.marked
        if surface == "route":
            return (base + self.paved + self.quiet + 0.6 * self.calm - 2 * self.busy
                    - 0.06 * min(self.crossings_per_km, 15))
        if surface == "nature":
            return base + 1.5 * (1 - self.paved) + 0.3 * self.quiet - 1.5 * self.busy - 0.02 * min(self.crossings_per_km, 15)
        return base + self.quiet + 0.6 * self.calm - 1.5 * self.busy


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
    res = requests.get(BROUTER_URL, timeout=BROUTER_TIMEOUT_S, headers={"User-Agent": USER_AGENT}, params={
        "lonlats": lonlats, "profile": PROFILE, "alternativeidx": 0, "format": "geojson"})
    res.raise_for_status()
    feature = res.json()["features"][0]
    props = feature["properties"]
    points = [(c[1], c[0], c[2] if len(c) > 2 else None) for c in feature["geometry"]["coordinates"]]
    by_class: dict[str, float] = {}
    paved = marked = 0.0
    crossings = 0
    header, *rows = props.get("messages") or [[]]
    col = lambda name: header.index(name) if name in header else None   # noqa: E731
    di, ti, ni = col("Distance"), col("WayTags"), col("NodeTags")
    for row in rows:
        way = row[ti] if ti is not None else ""
        dist = float(row[di] or 0) if di is not None else 0.0
        m = re.search(r"highway=(\S+)", way)
        kind = m.group(1) if m else "other"
        by_class[kind] = by_class.get(kind, 0) + dist
        surf = re.search(r"surface=(\S+)", way)
        if (surf.group(1) in PAVED) if surf else kind not in UNPAVED_WAYS:
            paved += dist
        if re.search(r"route_hiking_\w+=yes", way):
            marked += dist
        if ni is not None and any(t in (row[ni] or "").split() for t in CROSSING_NODES):
            crossings += 1
    total = sum(by_class.values()) or 1
    share = lambda kinds: sum(v for k, v in by_class.items() if k in kinds) / total   # noqa: E731
    return Loop(points=points, km=float(props.get("track-length") or 0) / 1000, quiet=share(QUIET), calm=share(CALM),
                busy=share(BUSY), climb=int(float(props.get("filtered ascend") or 0)), tags=by_class,
                paved=paved / total, crossings=crossings, marked=marked / total)


def loop_at(start: tuple[float, float], km: float, bearing: float) -> Loop:
    """Triangle départ → A → B → départ, orienté vers `bearing`, ajusté une fois pour coller à la distance."""
    radius = km / (3 * ROUTE_FACTOR)
    best = None
    for __ in range(2):
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
               never_done: bool = False, surface: str = "mixte") -> list[Loop]:
    """Les meilleures boucles autour de `start` : orientations différentes, les mieux notées pour `surface` d'abord,
    sans aller-retour si possible. never_done : seulement des boucles dont l'essentiel n'a jamais été couru."""
    done = run_cells(runs or []) if (runs or never_done) else set()
    bearings = [i * 360 / 8 for i in range(8)] if never_done else [i * 360 / 6 for i in range(6)]
    loops = []
    pool = ThreadPoolExecutor(max_workers=ROUTES_WORKERS)
    futures = [pool.submit(loop_at, start, km, b) for b in bearings]
    __, late = wait(futures, timeout=ROUTES_BUDGET_S)
    pool.shutdown(wait=False, cancel_futures=True)   # les orientations en retard sont abandonnées
    for bearing, fut in zip(bearings, futures):   # ordre des orientations conservé : résultat déterministe
        if fut in late:
            logger.warning(f"Boucle vers {bearing:.0f}° ignorée : budget de {ROUTES_BUDGET_S} s dépassé")
            continue
        try:
            loop = fut.result()
        except Exception as e:
            logger.warning(f"Boucle vers {bearing:.0f}° impossible : {e}")
            continue
        if loop.km <= 0:
            continue
        loop.novelty = novelty(loop.points, done) if (done or never_done) else None   # rien couru : tout est nouveau
        loops.append(loop)
    loops = [lp for lp in loops if backtrack(lp.points) <= MAX_BACKTRACK] or loops   # pas de demi-tour si possible
    if never_done:
        loops = [lp for lp in loops if (lp.novelty or 0) >= NEVER_DONE_MIN] or \
                sorted(loops, key=lambda lp: lp.novelty or 0, reverse=True)[:1]
    loops.sort(key=lambda lp: lp.score(km, surface) + (0.5 * lp.novelty if never_done and lp.novelty else 0), reverse=True)
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
    coords = [(lon, lat) for lat, lon, __ in loop.points]
    m.add_line(Line(coords, "white", 9))
    m.add_line(Line(coords, "#FF5A1F", 5))
    m.add_marker(CircleMarker((start[1], start[0]), "white", 18))
    m.add_marker(CircleMarker((start[1], start[0]), "#14968A", 12))
    out = io.BytesIO()
    m.render().convert("RGB").save(out, format="JPEG", quality=85)
    return out.getvalue()


def geocode(query: str, near: tuple[float, float] | None = None) -> tuple[str, float, float] | None:
    """Lieu en texte libre → (nom court, lat, lon) via Nominatim, en privilégiant les environs de `near`."""
    params = {"q": query, "format": "jsonv2", "limit": 1, "accept-language": "fr"}
    if near:
        lat, lon = near
        params["viewbox"] = f"{lon - 0.3},{lat + 0.3},{lon + 0.3},{lat - 0.3}"   # préférence, pas une limite
    res = requests.get(NOMINATIM_URL, params=params, timeout=settings.http_timeout, headers={"User-Agent": USER_AGENT})
    res.raise_for_status()
    hits = res.json()
    if not hits:
        return None
    hit = hits[0]
    name = hit.get("name") or (hit.get("display_name") or query).split(",")[0]
    return name, float(hit["lat"]), float(hit["lon"])


def describe(loop: Loop, surface: str = "mixte") -> str:
    pct = lambda x: f"{x * 100:.0f}"   # noqa: E731
    parts = [decimal(f"{loop.km:.1f}") + " km"]
    if surface == "route":
        parts += [_("{pct} % bitume").format(pct=pct(loop.paved)), _("{pct} % voies calmes").format(pct=pct(loop.safe)),
                  (_("{n} traversées") if plural(loop.crossings) else _("{n} traversée")).format(n=loop.crossings)]
    elif surface == "nature":
        parts.append(_("{pct} % nature").format(pct=pct(1 - loop.paved)))
    else:
        parts.append(_("{pct} % chemins et rues calmes").format(pct=pct(loop.safe)))
    if loop.marked >= 0.15:
        parts.append(_("{pct} % sur sentiers balisés").format(pct=pct(loop.marked)))
    if loop.climb >= 20:
        parts.append(f"D+ {loop.climb} m")
    if loop.novelty is not None:
        parts.append(_("{pct} % jamais couru").format(pct=pct(loop.novelty)))
    return " · ".join(parts)
