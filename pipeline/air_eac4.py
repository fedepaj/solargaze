"""
The air by year, 2003–2024: monthly means from the CAMS global reanalysis
(EAC4), on its 0.75° grid (about 80 km), over Europe, North America and
Japan. In the app it is the air of earlier years everywhere, and the air of
the present wherever there is no street-scale layer — which outside the few
regions built at 50 m is everywhere.

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

Output: per area, one PNG per pollutant: the grid's rows for every
(year, month), stacked oldest first, a byte a cell (µg/m³ + 1, 0 = no
value) — Europe's under ``data/tiles/air_past/`` as it has always been, the
others under ``air_past/<area>/`` — and ``air_past/meta.json`` with every
area's grid (``areas``; Europe's also at the top, for the app of before).

    pipeline/.venv/bin/python pipeline/air_eac4.py            # fetch, fold, write
    pipeline/.venv/bin/python pipeline/air_eac4.py --publish  # and send to R2

The downloads are kept under ``pipeline/cache/eac4`` or, when the cache
disk is not there, wherever ``SG_EAC4_CACHE`` says.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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
YEARS = list(range(2003, 2025))
# north, west, south, east. Europe's is the European scope; North America's
# reaches from Hawaii to Puerto Rico and from Mexico's north to Canada's
# cities; Japan's from Kyushu's islands to Hokkaido.
AREAS = {
    "europe": (72.0, -25.0, 34.5, 45.0),
    "north_america": (55.5, -168.0, 15.0, -60.0),
    "japan": (46.5, 122.25, 23.25, 146.25),
}
# Outside Europe a cell is kept only where it touches a region the pipeline
# builds: the reanalysis reads Mexico City's PM2.5 at 128 µg/m³ and Seoul's
# at 112 (the stations, 20–25), and a grid there would show it to anyone.
KEEP = {"north_america": "north-america/us/", "japan": "asia/japan/"}
YEARS_A_REQUEST = 6                 # what the Data Store hands over in one go without a long queue
STEP = 0.75
AIR_DENSITY = 1.2                   # kg/m³, for a mixing ratio near the ground
VARS = {   # the app's name → (ADS variable, NetCDF variable, model level or None)
    "pm2_5": ("particulate_matter_2.5um", "pm2p5", None),
    "pm10": ("particulate_matter_10um", "pm10", None),
    "nitrogen_dioxide": ("nitrogen_dioxide", "no2", "60"),
    "ozone": ("ozone", "go3", "60"),
}
CACHE = Path(os.environ["SG_EAC4_CACHE"]) if os.environ.get("SG_EAC4_CACHE") else HERE / "cache" / "eac4"
OUT = HERE.parent / "data" / "tiles" / "air_past"


def fetch(area: str, years: list[int]) -> list[Path]:
    """Those years' monthly means over the area, every variable, as NetCDFs (cached)."""
    import cdsapi
    where = CACHE / area / f"{years[0]}-{years[-1]}"
    done = sorted(where.glob("*.nc"))
    if len(done) == 2:
        return done
    where.mkdir(parents=True, exist_ok=True)
    zpath = where.with_suffix(".zip")
    log.info("requesting %s %d–%d", area, years[0], years[-1])
    cdsapi.Client(quiet=True, timeout=300).retrieve(DATASET, {
        "variable": [v[0] for v in VARS.values()], "model_level": ["60"], "product_type": ["monthly_mean"],
        "year": [str(y) for y in years], "month": [f"{m:02d}" for m in range(1, 13)],
        "area": list(AREAS[area]), "data_format": "netcdf_zip",
    }, str(zpath))
    with zipfile.ZipFile(zpath) as z:
        z.extractall(where)
    zpath.unlink()
    return sorted(where.glob("*.nc"))


def fold(area: str) -> tuple[dict, dict[str, np.ndarray]]:
    """(grid, {pollutant: (years, 12, rows, cols) µg/m³}) for one area."""
    import xarray as xr
    out = {v: [] for v in VARS}
    grid = None
    for k in range(0, len(YEARS), YEARS_A_REQUEST):
        years = YEARS[k:k + YEARS_A_REQUEST]
        # Surface fields and the lowest model level come as two files, stamped
        # at different hours of the month's first day: read each on its own.
        files = [xr.open_dataset(p) for p in fetch(area, years)]
        for name, (_, nc, level) in VARS.items():
            ds = next(f for f in files if nc in f)
            a = ds[nc]
            if level is not None:
                a = a.isel(model_level=0) * AIR_DENSITY
            a = a.sortby("valid_time").values * 1e9            # kg/m³ → µg/m³
            if a.shape[0] != 12 * len(years):
                raise ValueError(f"{area} {years[0]}–{years[-1]} {name}: {a.shape[0]} months")
            out[name].append(a.astype(np.float32).reshape(len(years), 12, *a.shape[1:]))
        if grid is None:
            lats, lons = ds.latitude.values, ds.longitude.values
            # West of Greenwich the reanalysis counts longitude from 0 to 360.
            grid = {"north": float(lats[0]), "west": float((lons[0] + 180) % 360 - 180), "step": STEP,
                    "rows": int(len(lats)), "cols": int(len(lons)),
                    "note": "cell centres: row 0 is `north`, column 0 is `west`; a cell spans ±step/2"}
        for f in files:
            f.close()
    return grid, {k: np.concatenate(v) for k, v in out.items()}


def kept(area: str, grid: dict) -> np.ndarray | None:
    """(rows, cols): the cells whose square touches one of the area's regions, or None to keep all."""
    if area not in KEEP:
        return None
    import requests
    from shapely.geometry import box
    from shapely.ops import unary_union
    import osm_extract
    from sg import regions
    shapes = []
    for path in (p for p in regions.SCOPES["world"] if p.startswith(KEEP[area])):
        poly = CACHE / "polys" / f"{regions.name_of(path)}.poly"
        if not poly.exists():
            poly.parent.mkdir(parents=True, exist_ok=True)
            r = requests.get(f"{regions.GEOFABRIK}/{path}.poly", timeout=60)
            r.raise_for_status()
            poly.write_bytes(r.content)
        shapes.append(osm_extract.read_poly(poly))
    land = unary_union(shapes)
    h = STEP / 2
    return np.array([[land.intersects(box(grid["west"] + c * STEP - h, grid["north"] - r * STEP - h,
                                          grid["west"] + c * STEP + h, grid["north"] - r * STEP + h))
                      for c in range(grid["cols"])] for r in range(grid["rows"])])


def write(folded: dict[str, tuple[dict, dict[str, np.ndarray]]]) -> dict:
    areas = []
    for area, (grid, data) in folded.items():
        keep = kept(area, grid)
        where = OUT if area == "europe" else OUT / area
        where.mkdir(parents=True, exist_ok=True)
        files = {}
        for name, a in data.items():
            ok = np.isfinite(a) if keep is None else np.isfinite(a) & keep
            byte = np.where(ok, np.clip(np.round(a) + 1, 1, 255), 0).astype(np.uint8)
            Image.fromarray(byte.reshape(-1, grid["cols"]), "L").save(where / f"{name}.png", optimize=True)
            files[name] = (where / f"{name}.png").relative_to(OUT.parent).as_posix()
        areas.append({"name": area, "grid": grid, "files": files})
    europe = next(a for a in areas if a["name"] == "europe")
    grid, data = folded["europe"]
    rome = (int(round((grid["north"] - 41.9) / STEP)), int(round((12.5 - grid["west"]) / STEP)))
    meta = {
        "product": "CAMS global reanalysis (EAC4), monthly means at the surface, 0.75°",
        "years": YEARS, "areas": areas,
        # Europe's grid where the app of before looks for it.
        "grid": europe["grid"], "files": europe["files"], "vars": list(VARS),
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
    meta = write({area: fold(area) for area in AREAS})
    log.info("wrote %s: NO₂ over Rome by year %s", OUT, meta["rome_annual_no2"])
    if args.publish:
        from sg.remote import Remote
        remote = Remote.maybe(HERE.parent / "data" / "tiles", HERE / "cache" / "state")
        # The images first and the meta last: an app that reads the new meta
        # finds every file it names.
        for p in sorted((p for p in OUT.rglob("*") if p.is_file()), key=lambda p: p.name == "meta.json"):
            remote.publish_file(p)
            log.info("published %s", p.relative_to(OUT))


if __name__ == "__main__":
    main()
