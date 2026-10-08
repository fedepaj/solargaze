"""Surface heat: Landsat 8/9 surface temperature by month (heat_landsat.py)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from ..errors import Invalid
from ..product import Context, Product


class Heat(Product):
    name, version, subdir = "heat", 3, "heat"   # 3: one-channel 90 m, packed months, overviews
    card = {
        "theme": "heat", "variant": "morning", "order": 10, "kind": "raster-months",
        "label": "Morning", "title": "Surface heat on clear mornings",
        "when": "mornings", "daypart": "day", "span_c": 6, "resolution_m": 90,
        "source": "Landsat 8/9 Collection 2 Level-2 surface temperature (USGS), via Microsoft Planetary Computer",
        "licence": "public domain",
        "note": "A per-pixel median of clear mid-morning Landsat passes (about 10:30 local) since 2020, by "
                "month: what the roofs and streets typically read, not the air. From afar it is shown coarser.",
    }
    depends = ("wind",)
    min_buildings = 2000
    plausible_c = (-20, 75)      # a month's median surface temperature, °C, on a clear morning

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
        lo, hi = self.plausible_c
        if not medians or not all(lo < v < hi for v in medians):
            raise Invalid(f"implausible monthly medians {medians}")
        if not np.asarray(months).any():
            raise Invalid("every pixel is no-data")
