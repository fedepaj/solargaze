"""
Buildings and roads from Geofabrik extracts instead of Overpass.

Overpass is a shared, rate-limited service that answers 504 for hours when
it is busy, and a region is hundreds of queries. A Geofabrik extract is one
file — ``centro-latest.osm.pbf`` holds Tuscany, Umbria, Marche and Lazio in
under half a gigabyte — read once, locally, with no limits: every building
in it is sorted into the quarter-degree tiles it touches and appended to one
JSON-lines file per tile under ``pipeline/cache/extract_tiles/``.
``buildings.py`` looks there first and falls back to Overpass for the rest.

    pipeline/.venv/bin/python pipeline/wind/osm_extract.py centro sud nord-ovest

Each run rebuilds the index from the extracts it is given, so name all of
them at once. They are downloaded by hand into ``pipeline/cache/geofabrik/``
from https://download.geofabrik.de/europe/italy/ (ODbL, © OpenStreetMap
contributors); the ``.poly`` boundary of each is fetched next to it.

An extract is cut along its region's boundary, so a tile across the edge of
what was indexed — Lazio into Abruzzo, Liguria into France — holds only part
of its buildings. The boundaries are what decides: a tile counts as covered
when every bit of land in it lies inside some indexed extract, and only a
covered tile is answered from here. Buildings in two extracts near a shared
border are written twice and read once.

Roads come out of the same pass, for the street-scale air layer: every way
tagged with a carriageway ``highway`` value, as a line with one of three
classes (major: motorway, trunk, primary; secondary: secondary, tertiary;
local: residential, unclassified, living street), under ``roads/<id>.jsonl``.
Service roads, tracks and footways are left out: they carry little traffic
and would swamp the local class.

Memory stays flat whatever the extract: buildings are buffered per tile and
appended to disk as the buffers fill. osmium assembles multipolygon
relations into proper areas, so courtyards come out as holes without the
ring-stitching Overpass answers needed.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import osmium
import requests
import shapely
from osmium import geom
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tiles import STEP, Tile  # noqa: E402

log = logging.getLogger("wind.extract")

CACHE = Path(__file__).resolve().parent.parent / "cache"
GEOFABRIK = CACHE / "geofabrik"
OUT = CACHE / "extract_tiles"
MANIFEST = OUT / "extracts.json"
KEEP_TAGS = ("building", "height", "building:height", "building:levels", "levels", "roof:levels", "building:part")
POLY_URLS = ("https://download.geofabrik.de/europe/italy/{}.poly", "https://download.geofabrik.de/europe/{}.poly")
ROAD_CLASSES = {
    **dict.fromkeys(("motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"), "major"),
    **dict.fromkeys(("secondary", "secondary_link", "tertiary", "tertiary_link"), "secondary"),
    **dict.fromkeys(("residential", "unclassified", "living_street"), "local"),
}
# Land left outside every extract is looked for on this grid (~500 m), which
# is finer than the 1 km land mask that answers for each point.
PROBE_STEP = 0.005


class TileSink:
    """Per-tile buffers, appended to ``<id>.jsonl`` as they fill."""

    def __init__(self, out: Path, flush_at: int = 200_000):
        self.out = out
        self.flush_at = flush_at
        self.buf: dict[str, list[str]] = {}
        self.pending = 0

    def add(self, tid: str, line: str) -> None:
        self.buf.setdefault(tid, []).append(line)
        self.pending += 1
        if self.pending >= self.flush_at:
            self.flush()

    def flush(self) -> None:
        for tid, lines in self.buf.items():
            with open(self.out / f"{tid}.jsonl", "a") as f:
                f.writelines(lines)
        self.buf.clear()
        self.pending = 0


def tiles_of(xs, ys):
    """Every tile the bbox of these coordinates touches."""
    for lat in range(math.floor(min(ys) / STEP), math.floor(max(ys) / STEP) + 1):
        for lon in range(math.floor(min(xs) / STEP), math.floor(max(xs) / STEP) + 1):
            yield Tile(lat * STEP, lon * STEP).id


class Buildings(osmium.SimpleHandler):
    """Every closed building outline and every carriageway, into the tiles
    their bboxes touch."""

    def __init__(self, sink: TileSink, roads: TileSink):
        super().__init__()
        self.factory = geom.GeoJSONFactory()
        self.sink = sink
        self.roads = roads
        self.count = 0
        self.road_count = 0

    def way(self, w):
        cls = ROAD_CLASSES.get(w.tags.get("highway"))
        if not cls:
            return
        try:
            coords = [(round(n.lon, 6), round(n.lat, 6)) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        if len(coords) < 2:
            return
        line = json.dumps({"id": f"w{w.id}", "c": cls, "coordinates": coords}, separators=(",", ":")) + "\n"
        for tid in tiles_of([x for x, _ in coords], [y for _, y in coords]):
            self.roads.add(tid, line)
        self.road_count += 1

    def area(self, a):
        kind = a.tags.get("building")
        if not kind or kind == "no":
            return
        try:
            gj = json.loads(self.factory.create_multipolygon(a))
        except Exception:  # an outline osmium could not close
            return
        tags = {k: a.tags[k] for k in KEEP_TAGS if k in a.tags}
        polys = gj["coordinates"] if gj["type"] == "MultiPolygon" else [gj["coordinates"]]
        osm_id = f"{'w' if a.from_way() else 'r'}{a.orig_id()}"
        for i, rings in enumerate(polys):
            xs = [x for x, _ in rings[0]]
            ys = [y for _, y in rings[0]]
            line = json.dumps({"type": "polygon", "id": f"{osm_id}/{i}", "tags": tags, "coordinates": rings},
                              separators=(",", ":")) + "\n"
            # A building straddling a tile edge belongs to both tiles.
            for tid in tiles_of(xs, ys):
                self.sink.add(tid, line)
        self.count += 1


# ---------------------------------------------------------------- boundaries

def extract_name(arg: str) -> str:
    """'centro', 'centro-latest.osm.pbf' or a path → 'centro'."""
    return Path(arg).name.removesuffix(".osm.pbf").removesuffix("-latest")


def poly_path(name: str) -> Path:
    p = GEOFABRIK / f"{name}.poly"
    if p.exists():
        return p
    for url in POLY_URLS:
        r = requests.get(url.format(name), timeout=60)
        if r.ok and r.text.rstrip().endswith("END"):
            p.write_text(r.text)
            return p
    raise FileNotFoundError(f"no boundary for {name}; put {p.name} in {GEOFABRIK}")


def read_poly(path: Path):
    """Osmosis .poly: a name, then rings of 'lon lat' each closed by END,
    a ring whose header starts with '!' being a hole; a final END."""
    lines = iter(path.read_text().splitlines())
    next(lines)
    outers, holes = [], []
    for header in lines:
        header = header.strip()
        if not header:
            continue
        if header == "END":
            break
        ring = []
        for line in lines:
            line = line.strip()
            if line == "END":
                break
            if line:
                x, y = map(float, line.split()[:2])
                ring.append((x, y))
        (holes if header.startswith("!") else outers).append(Polygon(ring))
    shape = unary_union(outers)
    return shape.difference(unary_union(holes)) if holes else shape


@functools.cache
def coverage():
    """Union of the boundaries of every indexed extract, or None."""
    if not MANIFEST.exists():
        return None
    extracts = json.loads(MANIFEST.read_text())["extracts"]
    return unary_union([read_poly(GEOFABRIK / e["poly"]) for e in extracts]) if extracts else None


def covered(tile: Tile) -> bool:
    """Is all the land of the tile inside some indexed extract?"""
    return covered_box(tile.bounds)


def covered_box(bounds) -> bool:
    """Is all the land of the (west, south, east, north) box inside some
    indexed extract? A window around a point near a border can be, where
    the whole tile is not."""
    cov = coverage()
    if cov is None:
        return False
    rest = box(*bounds).difference(cov)
    if rest.is_empty:
        return True
    from global_land_mask import globe
    w, s, e, n = rest.bounds
    lon, lat = np.meshgrid(np.arange(w, e + PROBE_STEP, PROBE_STEP), np.arange(s, n + PROBE_STEP, PROBE_STEP))
    inside = shapely.contains_xy(rest, lon, lat)
    lat, lon = np.clip(lat[inside], -90, 90), lon[inside]
    return not (lat.size and globe.is_land(lat, lon).any())


# ---------------------------------------------------------------- index and read

def index_extracts(args: list[str]) -> None:
    names = [extract_name(a) for a in args]
    pbfs = []
    for a, name in zip(args, names):
        p = Path(a) if Path(a).exists() else GEOFABRIK / f"{name}-latest.osm.pbf"
        if not p.exists():
            raise FileNotFoundError(p)
        pbfs.append(p)
    polys = [poly_path(name) for name in names]
    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "roads").mkdir(parents=True)
    sink, roads = TileSink(OUT), TileSink(OUT / "roads")
    for pbf in pbfs:
        t0 = time.time()
        log.info("reading %s", pbf.name)
        h = Buildings(sink, roads)
        h.apply_file(str(pbf), locations=True)
        sink.flush()
        roads.flush()
        log.info("%s: %d buildings, %d road ways in %.0f s", pbf.name, h.count, h.road_count, time.time() - t0)
    MANIFEST.write_text(json.dumps({
        "extracts": [{"name": n, "pbf": p.name, "poly": q.name,
                      "pbf_modified": time.strftime("%Y-%m-%d", time.gmtime(p.stat().st_mtime))}
                     for n, p, q in zip(names, pbfs, polys)],
    }, indent=2) + "\n")
    coverage.cache_clear()
    log.info("%d tiles indexed", len(list(OUT.glob("*.jsonl"))))


def _read_jsonl(p: Path) -> list[dict]:
    """Elements of one tile file, each once (an element in two extracts
    near their shared border is written twice)."""
    if not p.exists():
        return []  # covered and empty: sea, or fields with nothing mapped
    seen: set[str] = set()
    out = []
    with open(p) as f:
        for line in f:
            el = json.loads(line)
            if el["id"] not in seen:
                seen.add(el["id"])
                out.append(el)
    return out


def read_tile(tile: Tile, whole: bool = True) -> list[dict] | None:
    """The tile's buildings from the extracts, or None when they do not
    cover it (and Overpass has to be asked). With whole=False the caller
    has checked the part it needs with covered_box, and gets what there is."""
    if whole and not covered(tile):
        return None
    return _read_jsonl(OUT / f"{tile.id}.jsonl")


def read_roads(tile: Tile, whole: bool = True) -> list[dict] | None:
    """The tile's carriageways, as read_tile."""
    # An index built before roads were extracted has no roads/ at all: that
    # is "not known", not "no roads".
    if not (OUT / "roads").is_dir() or (whole and not covered(tile)):
        return None
    return _read_jsonl(OUT / "roads" / f"{tile.id}.jsonl")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    index_extracts(sys.argv[1:])
