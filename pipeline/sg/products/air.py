"""CAMS air-quality climatology by month × hour (air_cams_bulk.py), folded for
the whole set of tiles once and cut per tile."""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..errors import Invalid
from ..product import Context, Product

YEARS = list(range(2020, 2025))


class Air(Product):
    name, version, subdir = "air", 1, "air"
    depends = ("wind",)
    min_buildings = 2000

    def prepare(self, tiles: list[str], ctx: Context) -> None:
        import air_cams_bulk as acb
        from tiles import Tile
        ts = [Tile.parse(t) for t in tiles]
        w = min(t.bounds[0] for t in ts) - 0.2
        s = min(t.bounds[1] for t in ts) - 0.2
        e = max(t.bounds[2] for t in ts) + 0.2
        n = max(t.bounds[3] for t in ts) + 0.2
        wanted, result = acb.fold_region_cached((w, s, e, n), YEARS, ts)
        kinds = "validated" if YEARS[-1] <= acb.VALIDATED_UNTIL else f"validated to {acb.VALIDATED_UNTIL}, interim after"
        note = f"Copernicus Atmosphere Data Store, cams-europe-air-quality-reanalyses ({kinds})"
        ctx.shared["air"] = (wanted, result, note)

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import air_cams_bulk as acb
        from tiles import Tile
        wanted, result, note = ctx.shared["air"]
        return acb.build_tile(Tile.parse(tile), stage, YEARS, wanted, result, note)

    def validate(self, tile: str, stage: Path, info: dict) -> None:
        data = json.loads((stage / "climatology.json").read_text())
        if len(data["nodes"]) != info["nodes"] or not data["nodes"]:
            raise Invalid("node count disagrees with the meta")
        for node in data["nodes"]:
            for var in info["vars"]:
                cells = [x for row in node[var]["by_month_hour"] for x in row]
                ok = [x for x in cells if x is not None and math.isfinite(x)]
                if len(ok) < 0.9 * len(cells):
                    raise Invalid(f"{var} at {node['requested']}: {len(ok)} of {len(cells)} cells")
                if not 0 <= node[var]["annual_mean"] < 400:
                    raise Invalid(f"{var} annual mean {node[var]['annual_mean']} at {node['requested']}")
