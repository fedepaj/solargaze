"""The building mask the browser solves the street-level wind on (wind/run.py)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..errors import Invalid, NoData
from ..product import Context, Product


FEW = 20    # up to this many buildings, an empty mask is the buildings being small, not a bug


class Wind(Product):
    name, version, subdir = "wind", 1, "wind"
    card = {
        "kind": "building-mask", "title": "Buildings for the street-level wind", "resolution_m": 10,
        "source": "OpenStreetMap buildings via Geofabrik extracts", "licence": "ODbL",
        "note": "Where a tile has been built, the wind is threaded between the OpenStreetMap buildings by a "
                "potential-flow model solved in the browser: channelling, shelter and corner gusts, but no "
                "wakes — a picture, not a measurement.",
    }
    refresh_days = 365          # OSM keeps growing; a year-old mask is worth redoing

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import run as wind_run
        from tiles import Tile
        info = wind_run.build(Tile.parse(tile), stage)
        if not info.get("osm_buildings"):
            raise NoData("no OSM building in the tile")
        if info["osm_buildings"] <= FEW and not self._drawn(stage, info):
            # A cell is a building only where more than half of it is covered: a few sheds fill none.
            raise NoData(f"{info['osm_buildings']} OSM buildings, none covering half a cell")
        return info

    @staticmethod
    def _drawn(stage: Path, info: dict) -> bool:
        import stored
        return bool((stored.read(stage / info["heights_png"]["file"]) > 0).any())

    def validate(self, tile: str, stage: Path, info: dict) -> None:
        import stored
        h = stored.read(stage / info["heights_png"]["file"])
        if h.shape != (info["rows"], info["cols"]):
            raise Invalid(f"the heights are {h.shape}, meta says {info['rows']}×{info['cols']}")
        if not (h > 0).any():
            raise Invalid(f"{info['osm_buildings']} buildings but an empty mask")
