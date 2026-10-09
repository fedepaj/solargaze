"""Light pollution: how dark the night sky is overhead, year by year since
2012, from VIIRS night lights and a glow kernel fitted on the World Atlas
(light_viirs.py)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from ..errors import Invalid
from ..product import Context, Product
from ..state import DONE, Record


class _OpsStore:
    """The reduced yearly radiance, shared between runners on the ops bucket."""

    def __init__(self, remote):
        self.remote = remote

    def get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError
        try:
            return self.remote._call(lambda: self.remote.s3.get_object(Bucket=self.remote.ops, Key=key)["Body"].read())
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def put(self, key: str, raw: bytes) -> None:
        self.remote._call(lambda: self.remote.s3.put_object(Bucket=self.remote.ops, Key=key, Body=raw))


class Light(Product):
    name, version, subdir = "light", 3, "light"   # 3: from 1992, DMSP before VIIRS
    card = {
        "theme": "light", "variant": "sky", "order": 10, "kind": "sky-brightness",
        "label": "Night sky", "title": "Night-sky brightness",
        "unit": "mag/arcsec²", "scale_c": [16.5, 22.0], "resolution_m": 460,
        "source": "NASA Black Marble VNP46A4 (VIIRS night lights, yearly) from 2012; DMSP-OLS harmonised by Li et al. "
                  "2020 for 1992–2011; glow kernel fitted on Falchi et al. 2016",
        "licence": "public domain (NASA); CC BY 4.0 (harmonised DMSP)",
        "coarse_before": 2012,
        "note": "How bright the sky overhead is on a clear, moonless night, as a Sky Quality Meter reads it: 22 is a "
                "pristine sky, 17 a city centre, year by year since 1992. Each year's VIIRS night lights spread by a "
                "kernel of distance fitted on the World Atlas of Artificial Night Sky Brightness, within a factor of 1.5 of it on 94 cells in "
                "100 where it was never fitted. VIIRS is blind to blue light, so white LEDs are undercounted; "
                "altitude and terrain are not modelled. Before 2012 the older DMSP satellites stand in: coarser, "
                "saturated in city centres, within a factor of 1.5 of the VIIRS sky on about 8 cells in 10.",
    }

    def unavailable(self) -> str | None:
        import os
        if (os.environ.get("EARTHDATA_TOKEN") or os.environ.get("EARTHDATA_CONFIGURED") == "true"
                or (os.environ.get("EARTHDATA_USERNAME") and os.environ.get("EARTHDATA_PASSWORD"))):
            return None
        return "no Earthdata credentials (EARTHDATA_TOKEN, or EARTHDATA_USERNAME and EARTHDATA_PASSWORD)"

    def reason(self, tile: str, rec: Record, *, git: str = "") -> str | None:
        """Besides the usual: a done tile is built again when a new year's
        composite comes out (once a year, in spring)."""
        why = super().reason(tile, rec, git=git)
        if why or rec.status != DONE:
            return why
        latest = self._latest()
        if latest and rec.inputs.get("last_year", 0) < latest:
            return f"new year {latest}"
        return None

    _latest_year = None

    def _latest(self) -> int | None:
        if self._latest_year is None:
            import light_viirs
            try:
                Light._latest_year = light_viirs.latest_year()
            except Exception:  # noqa: BLE001 — CMR out of reach: nothing is due on its account
                Light._latest_year = 0
        return self._latest_year or None

    def prepare(self, tiles: list[str], ctx: Context) -> None:
        """The years, and every 10° cell the tiles' glow windows reach, for
        each of them: from the ops bucket when a runner has been there
        before (about 1 MB a cell and year), else from Earthdata."""
        import light_viirs as lv
        from tiles import Tile
        from ..remote import Remote
        remote = Remote.maybe(ctx.data_dir, ctx.cache_dir / "state")
        lv._store = _OpsStore(remote) if remote else None
        years = lv.years_to(lv.latest_year())
        cells = set()
        for t in tiles:
            cells.update(lv.cells_for(lv.window(Tile.parse(t).bounds)))
        for y in years:
            if y <= lv.VIIRS_FROM:      # 2012 in both: the DMSP years are chained to VIIRS there
                lv.dmsp_file(y, self._cache(ctx), lv._store)
            if y < lv.VIIRS_FROM:
                continue
            for h, v in sorted(cells):
                lv.r30(y, h, v, self._cache(ctx), lv._store)
        ctx.shared["light_last_year"] = years[-1]

    def _cache(self, ctx: Context) -> Path:
        return ctx.cache_dir / "light"

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import light_viirs
        from tiles import Tile
        return light_viirs.build(Tile.parse(tile), stage, self._cache(ctx), last_year=ctx.shared.get("light_last_year"))

    def inputs(self, tile: str, ctx: Context) -> dict:
        return {"last_year": ctx.shared.get("light_last_year")}

    def validate(self, tile: str, stage: Path, info: dict) -> None:
        img = np.asarray(Image.open(stage / "sky.png"))
        want = (info["rows"] * len(info["years"]), info["cols"])
        if img.shape != want:
            raise Invalid(f"sky.png is {img.shape}, meta says {len(info['years'])} years of {info['rows']}×{info['cols']}")
        if (img == 0).any():
            raise Invalid("sky.png has cells with no value")
        if not (14.0 < info["mag_min"] <= info["mag_max"] <= 22.0 + 1e-6):
            raise Invalid(f"implausible sky brightness {info['mag_min']}–{info['mag_max']} mag/arcsec²")
