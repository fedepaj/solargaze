"""
Building footprints for a tile, as a height raster.

The wind solver only needs to know where the air cannot go at street level,
so a building is a polygon with one number attached: how tall it is. OSM
has both, unevenly. The height rule, in order of trust:

1. ``height=*`` — a measured or surveyed value, in metres unless it says
   feet. Rare in Italy outside landmarks.
2. ``building:levels=*`` × 3.2 m — a storey of Italian housing stock is
   3.0–3.3 m floor to floor; 3.2 leans tall so that a ground floor with
   shops still counts as one level.
3. Otherwise 10 m — three storeys, the median Roman building. It errs low
   for the palazzi of the centre and high for the sheds of the periphery,
   which is the right kind of wrong: the solver only asks whether the
   building is taller than the 5 m slice it works at, and a 10 m default
   answers that correctly for almost everything that is not a shed.

Footprints come from the Overpass API in one query for the whole tile
bbox, cached raw under ``pipeline/cache/`` so a re-run costs nothing. If
the server refuses the size, the bbox is split in four and each quarter is
asked for on its own, still sequentially and still cached — that is what
Overpass asks of large area queries.

The raster is a regular lat/lon grid of ``n × n`` cells over the tile, row
0 at the north edge to match the other products. A cell counts as building
when more than half its area is covered — footprints are burned at twice
the resolution and pooled — so that a 7 m wide vicolo stays open and a
bin shed does not become an obstacle. The value is the tallest building
touching the cell, in metres, float32; ``heights.png`` holds the same
rounded up to whole metres in one 8-bit channel.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path

import numpy as np
import rasterio.features
import rasterio.transform
import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tiles import Tile  # noqa: E402

log = logging.getLogger("wind.buildings")

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
LEVEL_HEIGHT_M = 3.2
DEFAULT_HEIGHT_M = 10.0
HEIGHT_RULE = (
    "height tag (metres, or feet if so marked); else building:levels x 3.2 m; "
    "else 10 m. building:part is ignored; the tallest footprint wins where they overlap."
)
METRES_PER_LSB = 1.0  # heights.png: byte = ceil(height), so 255 m saturates

QUERY = """[out:json][timeout:900][maxsize:1073741824];
(
  way["building"]({s},{w},{n},{e});
  relation["building"]["type"="multipolygon"]({s},{w},{n},{e});
);
out geom qt;
"""


# ---------------------------------------------------------------- fetching

def _cache_path(bbox: tuple[float, float, float, float]) -> Path:
    w, s, e, n = bbox
    return CACHE_DIR / f"overpass_buildings_{s:.4f}_{w:.4f}_{n:.4f}_{e:.4f}.json"


def _query_once(url: str, bbox, dest: Path) -> None:
    """One request, streamed to disk: the answer for a city can be hundreds
    of megabytes and holding it twice would not fit next to the solver."""
    w, s, e, n = bbox
    q = QUERY.format(s=s, w=w, n=n, e=e)
    tmp = dest.with_suffix(".part")
    with requests.post(url, data={"data": q}, stream=True, timeout=(30, 960),
                       headers={"User-Agent": "solargaze-wind-pipeline/1 (offline tile builder)"}) as r:
        if r.status_code in (429, 504, 502, 503):
            raise RuntimeError(f"overpass busy: {r.status_code}")
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    # A "remark" means the query died server-side (out of memory / timeout)
    # even though the status was 200; the JSON is then truncated or empty.
    with open(tmp, "rb") as f:
        head = f.read(4096).decode("utf-8", "replace")
    if '"remark"' in head or '"elements"' not in head:
        tail = tmp.read_text(errors="replace")[-600:]
        raise RuntimeError(f"overpass returned no elements: {tail}")
    tmp.rename(dest)


def fetch_bbox(bbox, depth: int = 0) -> list[dict]:
    """Elements for a bbox, from cache or the network. Retries with growing
    pauses and a different mirror each time; after that, splits in four."""
    dest = _cache_path(bbox)
    if dest.exists():
        log.info("cache hit %s", dest.name)
        return json.loads(dest.read_bytes())["elements"]
    delays = [10, 30, 90, 180]
    for attempt, delay in enumerate(delays + [None]):
        url = OVERPASS_URLS[attempt % len(OVERPASS_URLS)]
        try:
            log.info("overpass %s bbox=%s", url, bbox)
            t0 = time.time()
            _query_once(url, bbox, dest)
            log.info("got %.1f MB in %.0f s", dest.stat().st_size / 1e6, time.time() - t0)
            return json.loads(dest.read_bytes())["elements"]
        except (requests.RequestException, RuntimeError) as exc:
            log.warning("attempt %d failed: %s", attempt + 1, exc)
            if delay is None:
                break
            time.sleep(delay)
    if depth >= 2:
        raise RuntimeError(f"overpass gave up on {bbox}")
    w, s, e, n = bbox
    cx, cy = (w + e) / 2, (s + n) / 2
    elements: list[dict] = []
    for sub in ((w, s, cx, cy), (cx, s, e, cy), (w, cy, cx, n), (cx, cy, e, n)):
        elements += fetch_bbox(sub, depth + 1)
        time.sleep(2)
    return elements


def fetch_buildings(tile: Tile) -> list[dict]:
    # Geofabrik extracts indexed by osm_extract.py answer first when their
    # boundaries cover the tile: local, and not at the mercy of a busy Overpass.
    import osm_extract
    local = osm_extract.read_tile(tile)
    if local is not None:
        log.info("%d buildings for %s from the local extracts", len(local), tile.id)
        return local
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    elements = fetch_bbox(tile.bounds)
    # Quarters overlap nothing, but a building straddling a cut is returned
    # by both sides; the solver must not count it twice.
    seen: set[tuple[str, int]] = set()
    unique = []
    for el in elements:
        key = (el["type"], el["id"])
        if key not in seen:
            seen.add(key)
            unique.append(el)
    return unique


# ---------------------------------------------------------------- heights

_NUM = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(m|metres|meters|ft|feet|')?\s*$", re.I)


def _parse_length(s: str) -> float | None:
    m = _NUM.match(s)
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "m").lower()
    return v * 0.3048 if unit in ("ft", "feet", "'") else v


def building_height(tags: dict) -> tuple[float, str]:
    """(metres, which rule fired)"""
    if "height" in tags:
        h = _parse_length(tags["height"])
        if h is not None and 0 < h < 500:
            return h, "height"
    if "building:levels" in tags:
        try:
            levels = float(tags["building:levels"].replace(",", "."))
        except ValueError:
            levels = None
        if levels is not None and 0 < levels < 120:
            return levels * LEVEL_HEIGHT_M, "levels"
    return DEFAULT_HEIGHT_M, "default"


# ---------------------------------------------------------------- geometry

def _ring(coords: list[dict]) -> list[tuple[float, float]]:
    return [(p["lon"], p["lat"]) for p in coords]


def _assemble_rings(ways: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Chain open way pieces end to end into closed rings. Multipolygon
    outers are often drawn as several ways sharing endpoints."""
    pieces = [list(w) for w in ways if len(w) >= 2]
    rings = []
    while pieces:
        ring = pieces.pop()
        while ring[0] != ring[-1]:
            for k, p in enumerate(pieces):
                if p[0] == ring[-1]:
                    ring += p[1:]
                elif p[-1] == ring[-1]:
                    ring += p[-2::-1]
                elif p[-1] == ring[0]:
                    ring = p[:-1] + ring
                elif p[0] == ring[0]:
                    ring = p[::-1][:-1] + ring
                else:
                    continue
                pieces.pop(k)
                break
            else:
                break  # dangling piece: leave it unclosed and drop it below
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def footprints(elements: list[dict]) -> list[tuple[dict, float, str]]:
    """(GeoJSON polygon, height m, height source) per building."""
    out = []
    for el in elements:
        tags = el.get("tags") or {}
        h, src = building_height(tags)
        if el["type"] == "polygon":
            # From an extract: rings already closed and assembled by osmium.
            out.append(({"type": "Polygon", "coordinates": el["coordinates"]}, h, src))
        elif el["type"] == "way":
            geom = el.get("geometry")
            if not geom or len(geom) < 4:
                continue
            ring = _ring(geom)
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            out.append(({"type": "Polygon", "coordinates": [ring]}, h, src))
        elif el["type"] == "relation":
            outers, inners = [], []
            for m in el.get("members", []):
                if m.get("type") != "way" or "geometry" not in m:
                    continue
                (outers if m.get("role", "outer") != "inner" else inners).append(_ring(m["geometry"]))
            outer_rings = _assemble_rings(outers)
            inner_rings = _assemble_rings(inners)
            if not outer_rings:
                continue
            # Holes are attached to every outer: rasterio ignores a hole
            # outside its shell, so the over-assignment is harmless.
            for ring in outer_rings:
                out.append(({"type": "Polygon", "coordinates": [ring, *inner_rings]}, h, src))
    return out


# ---------------------------------------------------------------- raster

def rasterize_heights(polys, tile: Tile, n: int, oversample: int = 2) -> np.ndarray:
    """float32 (n, n) height raster, row 0 north; 0 where there is no building."""
    w, s, e, nn = tile.bounds
    m = n * oversample
    transform = rasterio.transform.from_bounds(w, s, e, nn, m, m)
    # Ascending height so that where footprints overlap the REPLACE merge
    # leaves the tallest — building:part stacks and courtyards mapped twice.
    ordered = sorted(polys, key=lambda p: p[1])
    fine = rasterio.features.rasterize(
        ((g, h) for g, h, _ in ordered), out_shape=(m, m), transform=transform,
        fill=0.0, all_touched=False, dtype="float32",
    )
    blocks = fine.reshape(n, oversample, n, oversample)
    covered = (blocks > 0).mean(axis=(1, 3))
    tallest = blocks.max(axis=(1, 3))
    heights = np.where(covered >= 0.5, tallest, 0.0).astype(np.float32)
    return heights


def save_heights_png(heights: np.ndarray, path: Path) -> None:
    # Rounded up, so that "byte > slice" picks exactly the cells the solver
    # treated as walls: a 5.3 m shed is an obstacle at 5 m and must not
    # read back as 5.
    byte = np.clip(np.ceil(heights / METRES_PER_LSB), 0, 255).astype(np.uint8)
    Image.fromarray(byte, "L").save(path, optimize=True)


def build(tile: Tile, n: int, out_dir: Path) -> tuple[np.ndarray, dict]:
    """The raster plus the facts about it that meta.json wants."""
    t0 = time.time()
    elements = fetch_buildings(tile)
    polys = footprints(elements)
    sources = {"height": 0, "levels": 0, "default": 0}
    for _, _, src in polys:
        sources[src] += 1
    log.info("%d buildings (%s) in %.0f s", len(polys), sources, time.time() - t0)
    heights = rasterize_heights(polys, tile, n)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_heights_png(heights, out_dir / "heights.png")
    # The raw Overpass answer is only worth keeping while this tile is being
    # built: at up to 70 MB a tile, a region's worth would fill the disk.
    _cache_path(tile.bounds).unlink(missing_ok=True)
    info = {
        "osm_buildings": len(polys),
        "height_source_counts": sources,
        "height_rule": HEIGHT_RULE,
        "heights_png": {"file": "heights.png", "metres_per_lsb": METRES_PER_LSB, "rounding": "up",
                        "cell_rule": "building where >50% of the cell is covered; value = tallest footprint"},
    }
    return heights, info


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    tile = Tile.parse(sys.argv[1])
    els = fetch_buildings(tile)
    polys = footprints(els)
    print(len(els), "elements", len(polys), "footprints")
