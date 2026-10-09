"""Light pollution: how dark the night sky is overhead, from the year's VIIRS
night lights and a glow kernel fitted on the World Atlas (light_viirs.py)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from ..errors import Invalid
from ..product import Context, Product


class Light(Product):
    name, version, subdir = "light", 1, "light"
    # A new year's composite comes out in spring; a tile asks again three
    # times a year and picks it up within months.
    refresh_days = 120
    card = {
        "theme": "light", "variant": "sky", "order": 10, "kind": "sky-brightness",
        "label": "Night sky", "title": "Night-sky brightness",
        "unit": "mag/arcsec²", "scale_c": [16.5, 22.0], "resolution_m": 460,
        "source": "NASA Black Marble VNP46A4 (VIIRS night lights, yearly); glow kernel fitted on Falchi et al. 2016",
        "licence": "public domain (NASA)",
        "note": "How bright the sky overhead is on a clear, moonless night, as a Sky Quality Meter reads it: 22 is a "
                "pristine sky, 17 a city centre. The year's VIIRS night lights spread by a kernel of distance fitted "
                "on the World Atlas of Artificial Night Sky Brightness, within a factor of 1.5 of it on 94 cells in "
                "100 where it was never fitted. VIIRS is blind to blue light, so white LEDs are undercounted; "
                "altitude and terrain are not modelled.",
    }

    def unavailable(self) -> str | None:
        import os
        if (os.environ.get("EARTHDATA_TOKEN") or os.environ.get("EARTHDATA_CONFIGURED") == "true"
                or (os.environ.get("EARTHDATA_USERNAME") and os.environ.get("EARTHDATA_PASSWORD"))):
            return None
        return "no Earthdata credentials (EARTHDATA_TOKEN, or EARTHDATA_USERNAME and EARTHDATA_PASSWORD)"

    def prepare(self, tiles: list[str], ctx: Context) -> None:
        """The year, and every 10° composite the tiles' glow windows reach,
        downloaded once for the region (a few hundred MB)."""
        import math
        import light_viirs as lv
        from tiles import Tile
        year = lv.latest_year()
        reach = lv.KERNEL["reach_km"] / 111.32
        cells = set()
        for t in tiles:
            w, s, e, n = Tile.parse(t).bounds
            mx = reach / math.cos(math.radians((s + n) / 2))
            cells.update(lv.cells_for((w - mx, max(s - reach, -89.9), e + mx, min(n + reach, 89.9))))
        for h, v in sorted(cells):
            lv._file(year, h, v, self._cache(ctx))
        ctx.shared["light_year"] = year

    def _cache(self, ctx: Context) -> Path:
        return ctx.cache_dir / "light"

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import light_viirs
        from tiles import Tile
        return light_viirs.build(Tile.parse(tile), stage, self._cache(ctx), year=ctx.shared.get("light_year"))

    def inputs(self, tile: str, ctx: Context) -> dict:
        return {"year": ctx.shared.get("light_year")}

    def validate(self, tile: str, stage: Path, info: dict) -> None:
        img = np.asarray(Image.open(stage / "sky.png"))
        if img.shape != (info["rows"], info["cols"]):
            raise Invalid(f"sky.png is {img.shape}, meta says {info['rows']}×{info['cols']}")
        if (img == 0).any():
            raise Invalid("sky.png has cells with no value")
        if not (14.0 < info["mag_min"] <= info["mag_max"] <= 22.0 + 1e-6):
            raise Invalid(f"implausible sky brightness {info['mag_min']}–{info['mag_max']} mag/arcsec²")
