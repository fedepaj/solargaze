"""CAMS air-quality climatology by month × hour (air_cams_bulk.py), folded for
the whole set of tiles once and cut per tile: the European reanalysis in its
domain, the global one (EAC4) elsewhere. Built by its own runner, with the
Data Store's key (CDSAPI_URL, CDSAPI_KEY), not in the regions' build jobs."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from ..errors import Invalid
from ..product import Context, Product

YEARS = list(range(2020, 2025))


class Air(Product):
    name, version, subdir = "air", 1, "air"
    card = {
        "kind": "climatology", "title": "CAMS air quality by month and hour", "resolution_m": 10000,
        "source": "Copernicus Atmosphere Monitoring Service (CAMS): the European reanalysis 2020–2024 in Europe, "
                  "the global reanalysis (EAC4, ~80 km) elsewhere",
        "licence": "Copernicus licence",
        "note": "The five-year habit of the air for each month and local hour, on CAMS's ~10 km grid in Europe and "
                "its ~80 km global grid elsewhere; the street layer is built on it.",
    }
    depends = ("wind",)
    min_buildings = 2000

    def unavailable(self) -> str | None:
        """Without the Data Store's key every tile would fail alike. The plan
        job holds no key, only whether one is set (ADS_CONFIGURED)."""
        if os.environ.get("CDSAPI_KEY") or os.environ.get("ADS_CONFIGURED") == "true":
            return None
        if Path.home().joinpath(".cdsapirc").exists():
            return None
        return "no Atmosphere Data Store key (CDSAPI_KEY or ~/.cdsapirc)"

    def prepare(self, tiles: list[str], ctx: Context) -> None:
        """One fold for the tiles inside the European reanalysis's domain and
        one for the rest (the Canaries are Spain, and outside it)."""
        import air_cams_bulk as acb
        from tiles import Tile
        groups: dict[bool, list] = {}
        for t in tiles:
            tile = Tile.parse(t)
            groups.setdefault(acb.in_europe(tile.bounds), []).append(tile)
        folds = {}
        for europe, ts in groups.items():
            bbox = (min(t.bounds[0] for t in ts) - 0.2, min(t.bounds[1] for t in ts) - 0.2,
                    max(t.bounds[2] for t in ts) + 0.2, max(t.bounds[3] for t in ts) + 0.2)
            if europe:   # the ring around the tiles must not leave the domain either
                bbox = (max(bbox[0], acb.EUROPE[0]), max(bbox[1], acb.EUROPE[1]),
                        min(bbox[2], acb.EUROPE[2]), min(bbox[3], acb.EUROPE[3]))
            folds[europe] = acb.fold_region_cached(bbox, YEARS, ts)
        ctx.shared["air"] = folds

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import air_cams_bulk as acb
        from tiles import Tile
        t = Tile.parse(tile)
        fold, note = ctx.shared["air"][acb.in_europe(t.bounds)]
        return acb.build_tile(t, stage, YEARS, fold, note)

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
                if node[var]["annual_mean"] is None or not 0 <= node[var]["annual_mean"] < 400:
                    raise Invalid(f"{var} annual mean {node[var]['annual_mean']} at {node['requested']}")
