"""Where and when the ground was built, by five-year epoch since 1975, from
the Global Human Settlement Layer (built_ghsl.py). Drawn over today's 3D
city as what was not there yet in the year on the slider."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from ..errors import Invalid
from ..product import Context, Product


class Built(Product):
    name, version, subdir = "built", 1, "built"
    card = {
        "theme": "growth", "variant": "built", "order": 10, "kind": "built-epochs",
        "label": "Built", "title": "Built since the year on the slider",
        "unit": "share of the ground", "scale_c": [0, 0.6], "resolution_m": 90,
        "source": "GHS-BUILT-S R2023A, European Commission Joint Research Centre",
        "licence": "CC BY 4.0 (© European Union)",
        "note": "What was built after the year on the slider, over the city of today: the share of each 90 m cell "
                "that buildings cover, epoch by epoch from 1975 to 2020, from the JRC's Global Human Settlement Layer "
                "(Landsat and Sentinel-2). Comparable between epochs, but a single cell is an estimate, and the 1970s "
                "and 1980s the least certain. Nothing after 2020.",
    }

    def prepare(self, tiles: list[str], ctx: Context) -> None:
        """Every epoch's 10° tiles the region's tiles fall in (about 40 MB each)."""
        import built_ghsl as bg
        from tiles import Tile
        cells = set()
        for t in tiles:
            cells.update(bg.cells_for(Tile.parse(t).bounds))
        for ep in bg.EPOCHS:
            for r, c in sorted(cells):
                bg.tif(ep, r, c, self._cache(ctx))

    def _cache(self, ctx: Context) -> Path:
        return ctx.cache_dir / "ghsl"

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import built_ghsl
        from tiles import Tile
        return built_ghsl.build(Tile.parse(tile), stage, self._cache(ctx))

    def validate(self, tile: str, stage: Path, info: dict) -> None:
        img = np.asarray(Image.open(stage / "built.png"))
        want = (info["rows"] * len(info["years"]), info["cols"])
        if img.shape != want:
            raise Invalid(f"built.png is {img.shape}, meta says {len(info['years'])} epochs of {info['rows']}×{info['cols']}")
        import built_ghsl
        f = built_ghsl.decode(img, len(info["years"]))
        if f.min() < -1e-6 or f.max() > 1 + 1e-6:
            raise Invalid(f"built.png decodes to shares {f.min():.2f}–{f.max():.2f}, outside 0–1")
        for k in info.get("overviews", []):
            if not (stage / f"built.o{k}.png").exists():
                raise Invalid(f"overview o{k} missing")
