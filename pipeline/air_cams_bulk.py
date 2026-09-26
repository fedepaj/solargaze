"""
Air quality climatology from CAMS, in bulk, from the Copernicus Atmosphere
Data Store — the way to do many tiles at once.

The Open-Meteo route (air_cams.py) asks for every node of every tile
separately and is throttled by the free tier to a few tiles a day. The ADS
hands over whole months of the European reanalysis for a bounding box as
one NetCDF each: 0.1°, hourly, every pollutant, the same ensemble model
Open-Meteo serves. One request per (year, month, variable) covers a whole
region; the folding into month × hour tables is then local and instant.

Output is identical to air_cams.py's — ``data/tiles/<id>/air/climatology.json``
and the ``air`` product entry — so the app cannot tell which script ran.

    pipeline/.venv/bin/python pipeline/air_cams_bulk.py --region italy
    pipeline/.venv/bin/python pipeline/air_cams_bulk.py --bbox 6.5,36.5,18.6,47.2 --years 2020-2024

Needs ``~/.cdsapirc`` with the ADS url and your personal key (never in the
repo). Requests queue on their side — a month of one variable over Italy
came back in about a minute in testing; a five-year region is a few hours of
mostly waiting, resumable: every downloaded month is kept under
``pipeline/cache/cams_bulk/`` and never asked for twice.

Validated reanalysis lags about two years; for later years the interim
reanalysis is used and said so in the meta.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiles import STEP, Tile, write_meta  # noqa: E402
from air_cams import VARS, WHO_DAILY, nodes_for  # noqa: E402
from run_region import REGIONS, tiles_in_bbox  # noqa: E402

log = logging.getLogger("cams-bulk")

DATASET = "cams-europe-air-quality-reanalyses"
CACHE = Path(__file__).resolve().parent / "cache" / "cams_bulk"
# Open-Meteo's names → ADS names → the variable inside the NetCDF.
ADS_VARS = {
    "pm2_5": ("particulate_matter_2.5um", "pm2p5"),
    "pm10": ("particulate_matter_10um", "pm10"),
    "nitrogen_dioxide": ("nitrogen_dioxide", "no2"),
    "ozone": ("ozone", "o3"),
}
# The validated reanalysis stops about two years back; interim fills the rest.
VALIDATED_UNTIL = datetime.now().year - 2


def fetch_month(bbox, year: int, month: int, var: str) -> Path:
    """One NetCDF of one variable for one month over the bbox, cached."""
    import cdsapi
    west, south, east, north = bbox
    tag = f"{var}_{year}-{month:02d}_{north}_{west}_{south}_{east}"
    out = CACHE / f"{tag}.nc"
    if out.exists():
        return out
    CACHE.mkdir(parents=True, exist_ok=True)
    zpath = CACHE / f"{tag}.zip"
    kind = "validated_reanalysis" if year <= VALIDATED_UNTIL else "interim_reanalysis"
    log.info("requesting %s %d-%02d (%s)", var, year, month, kind)
    t0 = time.time()
    cdsapi.Client(quiet=True).retrieve(DATASET, {
        "variable": [ADS_VARS[var][0]], "model": ["ensemble"], "level": ["0"], "type": [kind],
        "year": [str(year)], "month": [f"{month:02d}"], "area": [north, west, south, east],
    }, str(zpath))
    with zipfile.ZipFile(zpath) as z:
        name = next(n for n in z.namelist() if n.endswith(".nc"))
        z.extract(name, CACHE)
        (CACHE / name).rename(out)
    zpath.unlink()
    log.info("  %s in %.0f s", out.name, time.time() - t0)
    return out


def fold_region(bbox, years, tiles):
    """month × hour means per pollutant at every 0.1° node any tile wants, local time."""
    import xarray as xr
    from zoneinfo import ZoneInfo
    wanted = sorted({node for t in tiles for node in nodes_for(t)})
    lat_w = np.array([n[0] for n in wanted]); lon_w = np.array([n[1] for n in wanted])
    # A single zone for a region is a simplification the tables can afford:
    # the whole of Italy is one hour of the clock, and the tables are means.
    tz = ZoneInfo("Europe/Rome")
    result = {v: {"sum": np.zeros((len(wanted), 12, 24)), "n": np.zeros((len(wanted), 12, 24)), "daily": []} for v in VARS}

    for var in VARS:
        ncname = ADS_VARS[var][1]
        for year in years:
            for month in range(1, 13):
                ds = xr.open_dataset(fetch_month(bbox, year, month, var))
                # One variable per file; its name has changed before (pm2p5,
                # pm2p5_conc), so fall back to whatever the file holds.
                da = ds[ncname] if ncname in ds else ds[list(ds.data_vars)[0]]
                # nearest model cell for every wanted node: the grid is 0.1° at .05 offsets
                sel = da.sel(lat=xr.DataArray(lat_w, dims="node"), lon=xr.DataArray(lon_w, dims="node"), method="nearest")
                times = [t.astimezone(tz) for t in sel.time.to_index().tz_localize("UTC").to_pydatetime()]
                months = np.array([t.month - 1 for t in times]); hours = np.array([t.hour for t in times])
                vals = sel.values  # (time, node)
                ok = np.isfinite(vals)
                for k in range(len(wanted)):
                    np.add.at(result[var]["sum"][k], (months[ok[:, k]], hours[ok[:, k]]), vals[ok[:, k], k])
                    np.add.at(result[var]["n"][k], (months[ok[:, k]], hours[ok[:, k]]), 1)
                n = vals.shape[0] // 24 * 24
                result[var]["daily"].append(np.nanmean(vals[:n].reshape(-1, 24, len(wanted)), axis=1))
                ds.close()
                log.info("folded %s %d-%02d", var, year, month)
    return wanted, result


def write_tiles(tiles, years, wanted, result, source_note):
    index = {node: k for k, node in enumerate(wanted)}
    for tile in tiles:
        nodes = []
        for (lat, lon) in nodes_for(tile):
            k = index[(lat, lon)]
            entry = {"lat": lat, "lon": lon, "requested": [lat, lon]}
            for var in VARS:
                r = result[var]
                with np.errstate(invalid="ignore"):
                    mean = np.where(r["n"][k] > 0, r["sum"][k] / r["n"][k], np.nan)
                daily = np.concatenate([d[:, k] for d in r["daily"]])
                daily = daily[np.isfinite(daily)]
                entry[var] = {
                    "by_month_hour": [[None if np.isnan(x) else round(float(x), 1) for x in row] for row in mean],
                    "annual_mean": round(float(np.nanmean(mean)), 1),
                    "days_over_who": round(float((daily > WHO_DAILY[var]).mean()), 3) if len(daily) else None,
                }
            nodes.append(entry)
        out_dir = tile.path / "air"
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {"step": 0.1, "timezone": "Europe/Rome", "years": years, "vars": VARS,
                   "lats": sorted({n["requested"][0] for n in nodes}), "lons": sorted({n["requested"][1] for n in nodes}),
                   "nodes": nodes}
        (out_dir / "climatology.json").write_text(json.dumps(payload, separators=(",", ":")) + "\n")
        write_meta(tile, "air", {
            "product": "CAMS European air quality reanalysis (ensemble, surface): hourly means by calendar month, local time",
            "years": years, "timezone": "Europe/Rome", "step_degrees": 0.1, "nodes": len(nodes), "vars": VARS,
            "who_daily_guideline": WHO_DAILY, "file": "air/climatology.json",
            "source": source_note,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "caveat": "A ~11 km model grid: regional background, not the street. Means over the years listed, not any particular day.",
        })
        log.info("wrote %s", tile.id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", choices=sorted(REGIONS))
    ap.add_argument("--bbox", help="west,south,east,north")
    ap.add_argument("--years", default="2020-2024")
    ap.add_argument("--tiles", help="comma list of tile ids to write (default: all in the box)")
    ap.add_argument("--only-built", type=int, default=2000,
                    help="write only tiles whose wind product counts at least this many OSM buildings (0 = all)")
    args = ap.parse_args()
    bbox = tuple(map(float, args.bbox.split(","))) if args.bbox else REGIONS[args.region]
    y0, y1 = map(int, args.years.split("-"))
    years = list(range(y0, y1 + 1))
    tiles = [Tile.parse(t) for t in args.tiles.split(",")] if args.tiles else list(tiles_in_bbox(*bbox))
    # One 0.1° ring outside the box, for the tiles on its edge.
    fetch_box = (bbox[0] - 0.2, bbox[1] - 0.2, bbox[2] + 0.2, bbox[3] + 0.2)
    wanted, result = fold_region(fetch_box, years, tiles)
    if args.only_built and not args.tiles:
        # A 200 KB table for every square of sea and mountain is a repo nobody
        # wants; the mask run says where the buildings are. Decided now, after
        # the hours of downloading, so a mask run going on in parallel counts.
        from tiles import read_meta
        tiles = [t for t in tiles
                 if read_meta(t).get("products", {}).get("wind", {}).get("osm_buildings", 0) >= args.only_built]
        log.info("%d built tiles to write", len(tiles))
    kinds = "validated" if years[-1] <= VALIDATED_UNTIL else "validated to %d, interim after" % VALIDATED_UNTIL
    write_tiles(tiles, years, wanted, result, f"Copernicus Atmosphere Data Store, cams-europe-air-quality-reanalyses ({kinds})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    main()
