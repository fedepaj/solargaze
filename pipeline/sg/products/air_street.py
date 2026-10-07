"""The 50 m street-scale correction to the CAMS tables (air_lur.py)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

from ..errors import Invalid, Upstream
from ..product import Context, Product


class AirStreet(Product):
    name, version, subdir = "air_street", 1, "air_street"
    card = {
        "theme": "air", "variant": "street", "order": 10, "kind": "street-air", "base": "air",
        "label": "Street", "title": "Air quality street by street",
        "resolution_m": 50,
        "source": "CAMS (Copernicus) corrected by a land-use regression fitted to monitoring stations "
                  "(EEA in Europe); OpenStreetMap, ESA WorldCover, Copernicus DEM",
        "licence": "Copernicus licence; EEA re-use policy; ODbL; CC-BY 4.0",
        "note": "The CAMS climatology at 50 m: NO₂ and PM10 corrected by a land-use regression fitted to the "
                "monitoring stations (roads, buildings, green, terrain), ozone from NO₂ by titration, PM2.5 left "
                "as CAMS — a statistical model of where the stations are, not a measurement where you are.",
    }
    depends = ("wind", "air")
    min_buildings = 2000

    def _models_path(self, ctx: Context) -> Path:
        return ctx.cache_dir / "stations" / "lur_models.json"

    def prepare(self, tiles: list[str], ctx: Context) -> None:
        p = self._models_path(ctx)
        if not p.exists():
            raise Upstream(f"no fitted model at {p}: run air_lur.py features and fit first")
        ctx.shared["air_street"] = json.loads(p.read_text())

    def inputs(self, tile: str, ctx: Context) -> dict:
        return {"models": hashlib.md5(self._models_path(ctx).read_bytes()).hexdigest()[:12]}

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import air_lur
        from tiles import Tile
        return air_lur.build_street(Tile.parse(tile), stage, ctx.shared["air_street"])

    def validate(self, tile: str, stage: Path, info: dict) -> None:
        enc = info["encoding"]
        for var, file in info["files"].items():
            b = np.asarray(Image.open(stage / Path(file).name))
            if b.shape != (info["rows"], info["cols"]):
                raise Invalid(f"{file} is {b.shape}")
            if b.min() == b.max():
                raise Invalid(f"{var}: one value everywhere ({b.min()})")
            lo, hi = info["models"][var]["ln_ratio_range"]
            lr = enc["byte1_ln_ratio"] + (b.astype(float) - 1) * enc["step_ln_ratio"]
            if lr.min() < lo - 0.02 or lr.max() > hi + 0.02:
                raise Invalid(f"{var}: ln ratio {lr.min():.2f}…{lr.max():.2f} outside the model's {lo:.2f}…{hi:.2f}")
