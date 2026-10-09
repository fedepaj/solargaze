"""
The air of earlier years, 2003–2019: monthly means from the CAMS global
reanalysis (EAC4), on its 0.75° grid (about 80 km) over Europe.

What the number is: the model's mean at the surface for a calendar month of
a given year — PM2.5 and PM10 as delivered (kg/m³), NO₂ and ozone from the
lowest model level's mass mixing ratio times a standard air density
(1.2 kg/m³). A cell is a region's background, a city and its countryside
together: NO₂ in particular is diluted over 80 km and reads far below what a
street saw. It is there to show how the air of a region changed year by
year since 2003 — not what any street breathed.

Source: Copernicus Atmosphere Monitoring Service, CAMS global reanalysis
(EAC4) monthly averaged fields, from the Atmosphere Data Store (needs
``~/.cdsapirc`` with an ADS key, which only the machine that builds this
has: the nightly pipeline does not run it). Generated using Copernicus
Atmosphere Monitoring Service information; neither the European Commission
nor ECMWF is responsible for any use of it.

Output: one PNG per pollutant under ``data/tiles/air_past/``: the grid's
rows for every (year, month), stacked oldest first, a byte a cell
(µg/m³ + 1, 0 = no value), and ``air_past/meta.json`` with the grid.

    pipeline/.venv/bin/python pipeline/air_eac4.py            # fetch, fold, write
    pipeline/.venv/bin/python pipeline/air_eac4.py --publish  # and send to R2
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "wind")]

log = logging.getLogger("eac4")

DATASET = "cams-global-reanalysis-eac4-monthly"
YEARS = list(range(2003, 2020))     # the present air (CAMS Europe) begins in 2020
AREA = (72.0, -25.0, 34.5, 45.0)    # north, west, south, east: the European scope
STEP = 0.75
AIR_DENSITY = 1.2                   # kg/m³, for a mixing ratio near the ground
VARS = {   # the app's name → (ADS variable, NetCDF variable, model level or None)
    "pm2_5": ("particulate_matter_2.5um", "pm2p5", None),
    "pm10": ("particulate_matter_10um", "pm10", None),
    "nitrogen_dioxide": ("nitrogen_dioxide", "no2", "60"),
    "ozone": ("ozone", "go3", "60"),
}
CACHE = HERE / "cache" / "eac4"
OUT = HERE.parent / "data" / "tiles" / "air_past"


def fetch_year(year: int) -> list[Path]:
    """The year's twelve monthly means, every variable, as NetCDFs (cached)."""
    import cdsapi
    done = sorted(CACHE.glob(f"{year}/*.nc"))
    if len(done) == 2:
        return done
    (CACHE / str(year)).mkdir(parents=True, exist_ok=True)
    zpath = CACHE / f"{year}.zip"
    log.info("requesting %d", year)
    cdsapi.Client(quiet=True, timeout=300).retrieve(DATASET, {
        "variable": [v[0] for v in VARS.values()], "model_level": ["60"], "product_type": ["monthly_mean"],
        "year": [str(year)], "month": [f"{m:02d}" for m in range(1, 13)],
        "area": list(AREA), "data_format": "netcdf_zip",
    }, str(zpath))
    with zipfile.ZipFile(zpath) as z:
        z.extractall(CACHE / str(year))
    zpath.unlink()
    return sorted(CACHE.glob(f"{year}/*.nc"))


def fold() -> tuple[dict, dict[str, np.ndarray]]:
    """(grid, {pollutant: (years, 12, rows, cols) µg/m³})."""
    import xarray as xr
    out = {v: [] for v in VARS}
    grid = None
    for year in YEARS:
        # Surface fields and the lowest model level come as two files, stamped
        # at different hours of the month's first day: read each on its own.
        files = [xr.open_dataset(p) for p in fetch_year(year)]
        for name, (_, nc, level) in VARS.items():
            ds = next(f for f in files if nc in f)
            a = ds[nc]
            if level is not None:
                a = a.isel(model_level=0) * AIR_DENSITY
            a = a.sortby("valid_time").values * 1e9            # kg/m³ → µg/m³
            if a.shape[0] != 12:
                raise ValueError(f"{year} {name}: {a.shape[0]} months")
            out[name].append(a.astype(np.float32))
        if grid is None:
            lats, lons = ds.latitude.values, ds.longitude.values
            grid = {"north": float(lats[0]), "west": float(lons[0]), "step": STEP,
                    "rows": int(len(lats)), "cols": int(len(lons)),
                    "note": "cell centres: row 0 is `north`, column 0 is `west`; a cell spans ±step/2"}
        for f in files:
            f.close()
    return grid, {k: np.stack(v) for k, v in out.items()}


def write(grid: dict, data: dict[str, np.ndarray]) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, a in data.items():
        byte = np.where(np.isfinite(a), np.clip(np.round(a) + 1, 1, 255), 0).astype(np.uint8)
        Image.fromarray(byte.reshape(-1, grid["cols"]), "L").save(OUT / f"{name}.png", optimize=True)
        files[name] = f"air_past/{name}.png"
    rome = (int(round((grid["north"] - 41.9) / STEP)), int(round((12.5 - grid["west"]) / STEP)))
    meta = {
        "product": "CAMS global reanalysis (EAC4), monthly means at the surface, 0.75°",
        "years": YEARS, "grid": grid, "files": files, "vars": list(VARS),
        "packing": "rows of the grid for each (year, month), stacked oldest first; byte = µg/m³ + 1, 0 = no value",
        "rome_annual_no2": {str(y): round(float(data["nitrogen_dioxide"][k, :, rome[0], rome[1]].mean()), 1)
                            for k, y in enumerate(YEARS)},
        "air_density_kg_m3": AIR_DENSITY,
        "source": "Copernicus Atmosphere Monitoring Service, CAMS global reanalysis (EAC4) monthly averaged fields",
        "licence": "Copernicus licence",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caveat": "An 80 km model grid: a region's background, not a street; NO₂ is diluted far below what a road sees.",
    }
    (OUT / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--publish", action="store_true", help="send air_past/ to the tiles bucket")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    import socket
    import netdns
    socket.getaddrinfo = netdns.getaddrinfo
    grid, data = fold()
    meta = write(grid, data)
    log.info("wrote %s: NO₂ over Rome by year %s", OUT, meta["rome_annual_no2"])
    if args.publish:
        from sg.remote import Remote
        remote = Remote.maybe(HERE.parent / "data" / "tiles", HERE / "cache" / "state")
        for p in sorted(OUT.iterdir()):
            remote.publish_file(p)
            log.info("published %s", p.name)


if __name__ == "__main__":
    main()
