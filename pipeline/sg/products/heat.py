"""Surface heat: Landsat 8/9 surface temperature by month (heat_landsat.py)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from ..errors import Invalid
from ..product import Context, Product


class Heat(Product):
    name, version, subdir = "heat", 3, "heat"   # 3: one-channel 90 m, packed months, overviews
    depends = ("wind",)
    min_buildings = 2000

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import heat_landsat
        from tiles import Tile
        return heat_landsat.build(Tile.parse(tile), stage)

    def validate(self, tile: str, stage: Path, info: dict) -> None:
        if not info.get("months"):
            raise Invalid("no month in the product")
        months = Image.open(stage / "months.png")
        if months.size != (info["cols"], 12 * info["rows"]):
            raise Invalid(f"months.png is {months.size}, meta says {info['cols']}×{12 * info['rows']}")
        for k in info.get("overviews", []):
            if not (stage / f"months.o{k}.png").exists():
                raise Invalid(f"overview o{k} missing")
        medians = [m["tile_median_c"] for m in info["months"].values() if m.get("tile_median_c") is not None]
        if not medians or not all(-20 < v < 75 for v in medians):
            raise Invalid(f"implausible monthly medians {medians}")
        if not np.asarray(months).any():
            raise Invalid("every pixel is no-data")
