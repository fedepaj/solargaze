"""The building mask the browser solves the street-level wind on (wind/run.py)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from ..errors import Invalid, NoData
from ..product import Context, Product


class Wind(Product):
    name, version, subdir = "wind", 1, "wind"
    refresh_days = 365          # OSM keeps growing; a year-old mask is worth redoing

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import run as wind_run
        from tiles import Tile
        info = wind_run.build(Tile.parse(tile), stage)
        if not info.get("osm_buildings"):
            raise NoData("no OSM building in the tile")
        return info

    def validate(self, tile: str, stage: Path, info: dict) -> None:
        h = np.asarray(Image.open(stage / "heights.png"))
        if h.shape != (info["rows"], info["cols"]):
            raise Invalid(f"heights.png is {h.shape}, meta says {info['rows']}×{info['cols']}")
        if not (h > 0).any():
            raise Invalid(f"{info['osm_buildings']} buildings but an empty mask")
