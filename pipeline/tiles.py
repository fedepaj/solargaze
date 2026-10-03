"""
The tiling scheme every offline product follows.

Italy is cut into quarter-degree squares — about 28 km north–south and 19–23 km
east–west at these latitudes — keyed by their south-west corner. A tile id
looks like ``N41.75E12.25``: that one holds Rome. Everything precomputed for a
tile lives under ``data/tiles/<id>/`` next to a ``meta.json`` that says what is
there, so the app can look up the tile under the pin with one integer division
and one fetch.

Quarter degrees, and not something rounder in kilometres, because every source
we read is in geographic coordinates already: Landsat scenes reprojected to
EPSG:4326, Open-Meteo grids, Overpass bounding boxes. A tile is then a plain
rectangle for all of them, and a raster of it is ``cols × rows`` pixels at a
fixed degree step with no projection maths at runtime.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

STEP = 0.25
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "tiles"


@dataclass(frozen=True)
class Tile:
    lat0: float
    lon0: float

    @property
    def id(self) -> str:
        return f"N{self.lat0:.2f}E{self.lon0:.2f}"

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """west, south, east, north"""
        return (self.lon0, self.lat0, self.lon0 + STEP, self.lat0 + STEP)

    @property
    def path(self) -> Path:
        return DATA_DIR / self.id

    @property
    def centre(self) -> tuple[float, float]:
        return (self.lat0 + STEP / 2, self.lon0 + STEP / 2)

    def shape(self, degrees_per_pixel: float) -> tuple[int, int]:
        """rows, cols of a raster covering the tile at that step"""
        n = round(STEP / degrees_per_pixel)
        return (n, n)

    @staticmethod
    def containing(lat: float, lon: float) -> "Tile":
        return Tile(math.floor(lat / STEP) * STEP, math.floor(lon / STEP) * STEP)

    @staticmethod
    def parse(tile_id: str) -> "Tile":
        lat, lon = tile_id[1:].split("E")
        return Tile(float(lat), float(lon))


def read_meta(tile: Tile) -> dict:
    p = tile.path / "meta.json"
    if not p.exists():
        return {"id": tile.id, "bounds": tile.bounds, "products": {}}
    return json.loads(p.read_text())


def _atomic(path: Path, text: str) -> None:
    """Readers see the old file or the new one, never half of either."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def write_meta(tile: Tile | str, product: str, info: dict, refresh: bool = True) -> None:
    """Record a product under the tile, and (unless told not to — the runner
    refreshes once per run, not once per tile) refresh the top-level index."""
    tile = Tile.parse(tile) if isinstance(tile, str) else tile
    tile.path.mkdir(parents=True, exist_ok=True)
    meta = read_meta(tile)
    meta["id"] = tile.id
    meta["bounds"] = list(tile.bounds)
    meta.setdefault("products", {})[product] = info
    _atomic(tile.path / "meta.json", json.dumps(meta, indent=2) + "\n")
    if refresh:
        refresh_index()


def refresh_index() -> None:
    """data/tiles/index.json: every tile with anything in it, for the app."""
    tiles = []
    for p in sorted(DATA_DIR.glob("N*E*/meta.json")):
        m = json.loads(p.read_text())
        tiles.append({"id": m["id"], "bounds": m["bounds"], "products": sorted(m.get("products", {}))})
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic(DATA_DIR / "index.json", json.dumps({"step": STEP, "tiles": tiles}, indent=2) + "\n")


if __name__ == "__main__":
    import sys
    lat, lon = map(float, sys.argv[1:3])
    t = Tile.containing(lat, lon)
    print(t.id, t.bounds)
