"""Road traffic noise, Lden at 10 m (noise_osm.py): a screened line-source
model on the extract's roads and buildings, calibrated on END noise maps."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from ..errors import Invalid
from ..product import Context, Product


class Noise(Product):
    name, version, subdir = "noise", 1, "noise"
    depends = ("wind",)          # the tile's buildings are indexed and counted
    min_buildings = 2000
    card = {
        "theme": "noise", "variant": "roads", "order": 10, "kind": "raster-static",
        "label": "Roads", "title": "Road traffic noise, Lden",
        "unit": "dB", "scale_c": [40, 80], "resolution_m": 10,
        "source": "OpenStreetMap roads and buildings; calibrated on EEA END strategic noise maps (Berlin, checked on Hamburg)",
        "licence": "ODbL",
        "note": "Road traffic noise as a day–evening–night level (Lden), modelled from the class of every road and "
                "the buildings that screen it, calibrated on the official END noise maps of Berlin and checked on "
                "Hamburg's: within one 5 dB band of the official map on nine cells in ten. No traffic counts, "
                "speeds, barriers, railways or aircraft; where a city has an official noise map, that is the better source.",
    }

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import noise_osm
        from tiles import Tile
        return noise_osm.build(Tile.parse(tile), stage)

    def validate(self, tile: str, stage: Path, info: dict) -> None:
        img = np.asarray(Image.open(stage / "lden.png"))
        if img.shape != (info["rows"], info["cols"]):
            raise Invalid(f"lden.png is {img.shape}, meta says {info['rows']}×{info['cols']}")
        for k in info.get("overviews", []):
            if not (stage / f"lden.o{k}.png").exists():
                raise Invalid(f"overview o{k} missing")
        values = img[img > 0]
        if not values.size:
            raise Invalid("every cell is a building")
        db = 25 + (values.astype(np.float32) - 1) * 0.5
        if not (20 < float(np.median(db)) < 90):
            raise Invalid(f"implausible median level {np.median(db):.1f} dB")
