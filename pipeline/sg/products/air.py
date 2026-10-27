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


def fold_for(tiles, points=None):
    """The folds for the tiles — one for those inside the European
    reanalysis's domain, one for the rest (the Canaries are Spain, and outside
    it) — with the nodes round the points (stations) added; and CAMS at the
    points, or None without points. {in Europe: (fold, note)}, at."""
    import air_cams_bulk as acb
    from air_cams import nodes_for
    from .. import stations
    groups: dict[bool, tuple[list, list]] = {}
    for t in tiles:
        groups.setdefault(acb.in_europe(t.bounds), ([], []))[0].append(t)
    pts = [] if points is None else list(points.itertuples())
    for p in pts:
        groups.setdefault(acb.in_europe((p.lon, p.lat, p.lon, p.lat)), ([], []))[1].append(p)
    folds, at = {}, []
    for europe, (ts, ps) in groups.items():
        nodes = {n for t in ts for n in nodes_for(t)} | {n for p in ps for n in stations.corners(p.lat, p.lon)}
        lats, lons = [n[0] for n in nodes], [n[1] for n in nodes]
        bbox = (min(lons) - 0.2, min(lats) - 0.2, max(lons) + 0.2, max(lats) + 0.2)
        if europe:   # the ring round the nodes must not leave the domain either
            bbox = (max(bbox[0], acb.EUROPE[0]), max(bbox[1], acb.EUROPE[1]),
                    min(bbox[2], acb.EUROPE[2]), min(bbox[3], acb.EUROPE[3]))
        folds[europe] = acb.fold_nodes(bbox, YEARS, nodes)
        if ps:
            import pandas as pd
            at.append(stations.cams_at(folds[europe][0], pd.DataFrame([p._asdict() for p in ps])))
    if points is None:
        return folds, None
    import pandas as pd
    return folds, (pd.concat(at, ignore_index=True) if at else pd.DataFrame(columns=["station", "var", "cams"]))


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
        from tiles import Tile
        folds, at = fold_for([Tile.parse(t) for t in tiles], ctx.shared.get("air_points"))
        ctx.shared["air"] = folds
        if at is not None:
            ctx.shared["air_at_points"] = at

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
