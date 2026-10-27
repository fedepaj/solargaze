"""The 50 m street-scale correction to the CAMS tables (air_lur.py)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from ..errors import Invalid, NoData, Upstream
from ..product import Context, Product


class AirStreet(Product):
    name, version, subdir = "air_street", 1, "air_street"
    card = {
        "theme": "air", "variant": "street", "order": 10, "kind": "street-air", "base": "air",
        "label": "Street", "title": "Air quality street by street",
        "resolution_m": 50,
        "source": "CAMS (Copernicus) corrected by a land-use regression fitted to each country's monitoring stations "
                  "(EEA in Europe, EPA in the United States, OpenAQ elsewhere); OpenStreetMap, ESA WorldCover, Copernicus DEM",
        "licence": "Copernicus licence; EEA re-use policy; US public domain; ODbL; CC-BY 4.0",
        "note": "The CAMS climatology at 50 m: NO₂ and PM10 corrected by a land-use regression fitted to the "
                "monitoring stations (roads, buildings, green, terrain), ozone from NO₂ by titration, PM2.5 left "
                "as CAMS — a statistical model of where the stations are, not a measurement where you are.",
    }
    depends = ("wind", "air")
    min_buildings = 2000

    def prepare(self, tiles: list[str], ctx: Context) -> None:
        """The model of the country the run's region is in (sg/stations.py,
        fitted on the runners), the pollutants that beat CAMS in its
        validation; on a machine without the bucket, the model file of
        before (air_lur.py fit)."""
        from .. import stations
        from ..remote import Remote
        country = ctx.shared.get("country")
        remote = Remote.maybe(ctx.data_dir, ctx.cache_dir / "state") if country else None
        m = stations.model(remote, country) if remote else None
        if m is None:
            p = ctx.cache_dir / "stations" / "lur_models.json"
            if not p.exists():
                raise Upstream(f"no air model for {country or 'this region'} yet")
            ctx.shared["air_street"] = (json.loads(p.read_text()), None, hashlib.md5(p.read_bytes()).hexdigest()[:12])
            return
        models = {v: mv for v, mv in m["models"].items() if mv.get("beats_cams")}
        note = (f"monitoring stations of {country} (" + ", ".join(
            f"{v}: {mv['stations']}{' pooled with ' + mv['pooled_from'] if mv.get('pooled_from') else ''}"
            for v, mv in models.items()) + "), annual means 2020–2024")
        ctx.shared["air_street"] = (models, note, m["fitted"])

    def inputs(self, tile: str, ctx: Context) -> dict:
        return {"model": ctx.shared["air_street"][2]} if "air_street" in ctx.shared else {}

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import air_lur
        from tiles import Tile
        models, note, _ = ctx.shared["air_street"]
        if not models:
            raise NoData("no pollutant whose street model beats CAMS here: CAMS alone")
        return air_lur.build_street(Tile.parse(tile), stage, models, note)

    def validate(self, tile: str, stage: Path, info: dict) -> None:
        import stored
        enc = info["encoding"]
        for var, file in info["files"].items():
            b = stored.read(stage / Path(file).name)
            if b.shape != (info["rows"], info["cols"]):
                raise Invalid(f"{file} is {b.shape}")
            if b.min() == b.max():
                raise Invalid(f"{var}: one value everywhere ({b.min()})")
            lo, hi = info["models"][var]["ln_ratio_range"]
            lr = enc["byte1_ln_ratio"] + (b.astype(float) - 1) * enc["step_ln_ratio"]
            if lr.min() < lo - 0.02 or lr.max() > hi + 0.02:
                raise Invalid(f"{var}: ln ratio {lr.min():.2f}…{lr.max():.2f} outside the model's {lo:.2f}…{hi:.2f}")
