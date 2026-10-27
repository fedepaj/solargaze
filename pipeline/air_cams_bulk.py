"""
Air quality climatology from CAMS, in bulk, from the Copernicus Atmosphere
Data Store — the way to do many tiles at once.

The Open-Meteo route (air_cams.py) asks for every node of every tile
separately and is throttled by the free tier to a few tiles a day. The ADS
hands over whole months for a bounding box as one NetCDF each, and the
folding into month × hour tables is then local:

- inside Europe, the CAMS European reanalysis: 0.1°, hourly, the same
  ensemble model Open-Meteo serves; one request per (year, month, variable);
- elsewhere, the CAMS global reanalysis (EAC4): 0.75°, every three hours,
  read at the same 0.1° nodes by bilinear interpolation and between its
  hours linearly; one request per year for every variable. Coarser by far —
  a region's background — and said so in each tile's meta.

The tables are by local time at each node — the wall clock the app reads,
summer time included — so a tile in Lisbon, one in Bucharest and one in Los
Angeles each read their own hours.

Output is identical to air_cams.py's — ``data/tiles/<id>/air/climatology.json``
and the ``air`` product entry — so the app cannot tell which script ran.

    pipeline/.venv/bin/python pipeline/air_cams_bulk.py --region italy
    pipeline/.venv/bin/python pipeline/air_cams_bulk.py --bbox 6.5,36.5,18.6,47.2 --years 2020-2024

Needs the ADS key: ``~/.cdsapirc`` on a machine, ``CDSAPI_URL`` and
``CDSAPI_KEY`` on a runner (never in the repo). Requests queue on their side —
a month of one variable over Italy comes back in about a minute — so several
are asked at once. Every downloaded file is folded and thrown away.

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiles import Tile, write_meta  # noqa: E402
import netdns  # noqa: E402,F401  (falls back to DNS over HTTPS when the system resolver drops a name)
from air_cams import VARS, WHO_DAILY, nodes_for  # noqa: E402

log = logging.getLogger("cams-bulk")

DATASET = "cams-europe-air-quality-reanalyses"
EAC4 = "cams-global-reanalysis-eac4"
CACHE = Path(__file__).resolve().parent / "cache" / "cams_bulk"
# Open-Meteo's names → ADS names → the variable inside the NetCDF.
ADS_VARS = {
    "pm2_5": ("particulate_matter_2.5um", "pm2p5"),
    "pm10": ("particulate_matter_10um", "pm10"),
    "nitrogen_dioxide": ("nitrogen_dioxide", "no2"),
    "ozone": ("ozone", "o3"),
}
# EAC4: surface particulate matter as delivered (kg/m³); NO₂ and ozone from
# the lowest model level's mass mixing ratio times a standard air density.
EAC4_VARS = {"pm2_5": ("pm2p5", False), "pm10": ("pm10", False),
             "nitrogen_dioxide": ("no2", True), "ozone": ("go3", True)}
AIR_DENSITY = 1.2       # kg/m³
EAC4_STEP = 0.75
# The validated reanalysis stops about two years back; interim fills the rest.
VALIDATED_UNTIL = datetime.now().year - 2
# The European reanalysis's domain: west, south, east, north.
EUROPE = (-25.0, 30.0, 45.0, 72.0)
WORKERS = 6             # requests in the Data Store's queue at once


def in_europe(bbox) -> bool:
    w, s, e, n = bbox
    return w >= EUROPE[0] and s >= EUROPE[1] and e <= EUROPE[2] and n <= EUROPE[3]


# ---------------------------------------------------------------- time zones

@lru_cache(maxsize=1)
def _finder():
    from timezonefinder import TimezoneFinder
    return TimezoneFinder(in_memory=True)


def zone_at(lat: float, lon: float) -> str:
    """The IANA zone of a place; at sea, the nautical zone of its longitude."""
    z = _finder().timezone_at(lat=lat, lng=lon)
    if z:
        return z
    off = int(round(lon / 15))
    return "Etc/GMT" + (f"{-off:+d}" if off else "")


# ---------------------------------------------------------------- folding

class Fold:
    """Hourly values at a set of nodes, folded into means by local calendar
    month × local hour, and the share of local days whose mean is over the
    WHO daily guideline."""

    def __init__(self, wanted: list[tuple[float, float]]):
        self.wanted = list(wanted)
        k = len(self.wanted)
        zones = [zone_at(a, o) for a, o in self.wanted]
        self.groups = {z: np.array([i for i, x in enumerate(zones) if x == z]) for z in sorted(set(zones))}
        self.zones = zones
        self.sum = {v: np.zeros((k, 12, 24)) for v in VARS}
        self.n = {v: np.zeros((k, 12, 24)) for v in VARS}
        self.over = {v: np.zeros(k) for v in VARS}
        self.days = {v: np.zeros(k) for v in VARS}

    def add(self, var: str, times, vals: np.ndarray) -> None:
        """times: a tz-aware hourly pandas DatetimeIndex (UTC); vals: (time, node)."""
        import pandas as pd
        vals = np.asarray(vals, dtype=np.float64)
        ok = np.isfinite(vals)
        v0 = np.where(ok, vals, 0.0)
        for zone, idx in self.groups.items():
            local = times.tz_convert(zone)
            cell = (local.month.to_numpy() - 1) * 24 + local.hour.to_numpy()
            onehot = np.zeros((len(cell), 288))
            onehot[np.arange(len(cell)), cell] = 1
            self.sum[var][idx] += (onehot.T @ v0[:, idx]).T.reshape(len(idx), 12, 24)
            self.n[var][idx] += (onehot.T @ ok[:, idx]).T.reshape(len(idx), 12, 24)
            day = pd.factorize(local.normalize())[0]
            dh = np.zeros((len(day), day.max() + 1))
            dh[np.arange(len(day)), day] = 1
            dsum, dn = dh.T @ v0[:, idx], dh.T @ ok[:, idx]
            seen = dn > 0
            with np.errstate(invalid="ignore", divide="ignore"):
                mean = dsum / dn
            self.over[var][idx] += ((mean > WHO_DAILY[var]) & seen).sum(axis=0)
            self.days[var][idx] += seen.sum(axis=0)

    def node(self, k: int) -> dict:
        out = {}
        for var in VARS:
            with np.errstate(invalid="ignore", divide="ignore"):
                mean = np.where(self.n[var][k] > 0, self.sum[var][k] / self.n[var][k], np.nan)
            out[var] = {
                "by_month_hour": [[None if np.isnan(x) else round(float(x), 1) for x in row] for row in mean],
                "annual_mean": round(float(np.nanmean(mean)), 1) if np.isfinite(mean).any() else None,
                "days_over_who": round(float(self.over[var][k] / self.days[var][k]), 3) if self.days[var][k] else None,
            }
        return out

    def save(self, path: Path) -> None:
        np.savez_compressed(path, wanted=np.array(self.wanted), **{
            f"{v}_{k}": getattr(self, k)[v] for v in VARS for k in ("sum", "n", "over", "days")})

    @classmethod
    def load(cls, path: Path) -> "Fold":
        z = np.load(path, allow_pickle=True)
        f = cls([tuple(x) for x in z["wanted"]])
        for v in VARS:
            for k in ("sum", "n", "over", "days"):
                if f"{v}_{k}" in z:
                    getattr(f, k)[v] = z[f"{v}_{k}"]
        return f


# ---------------------------------------------------------------- the European reanalysis

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
        z.extract(name, CACHE / tag)
        (CACHE / tag / name).rename(out)
        (CACHE / tag).rmdir()
    zpath.unlink()
    log.info("  %s in %.0f s", out.name, time.time() - t0)
    return out


def fold_europe(bbox, years, fold: Fold) -> None:
    """The European reanalysis over bbox into fold, every file read once and deleted."""
    import pandas as pd
    import xarray as xr
    lat_w = xr.DataArray([n[0] for n in fold.wanted], dims="node")
    lon_w = xr.DataArray([n[1] for n in fold.wanted], dims="node")
    asks = [(year, month, var) for var in VARS for year in years for month in range(1, 13)]
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(fetch_month, bbox, *a): a for a in asks}
        for k, done in enumerate(as_completed(futures), 1):
            year, month, var = futures[done]
            path = done.result()
            with xr.open_dataset(path) as ds:
                # One variable per file; its name has changed before (pm2p5,
                # pm2p5_conc), so fall back to whatever the file holds.
                ncname = ADS_VARS[var][1]
                da = ds[ncname] if ncname in ds else ds[list(ds.data_vars)[0]]
                sel = da.sel(lat=lat_w, lon=lon_w, method="nearest")   # the grid is 0.1° at .05 offsets
                times = pd.DatetimeIndex(sel.time.values).tz_localize("UTC")
                fold.add(var, times, sel.values)
            path.unlink()
            if k % 24 == 0:
                log.info("folded %d of %d months", k, len(asks))


# ---------------------------------------------------------------- the global reanalysis

def fetch_eac4_year(bbox, year: int) -> list[Path]:
    """A year of EAC4 every three hours over bbox, every variable: the surface
    file and the model-level one."""
    import cdsapi
    west, south, east, north = bbox
    where = CACHE / f"eac4_{year}_{north}_{west}_{south}_{east}"
    done = sorted(where.glob("*.nc"))
    if len(done) == 2:
        return done
    where.mkdir(parents=True, exist_ok=True)
    zpath = where.with_suffix(".zip")
    log.info("requesting EAC4 %d", year)
    cdsapi.Client(quiet=True, timeout=600).retrieve(EAC4, {
        "variable": [ADS_VARS[v][0] for v in VARS], "model_level": ["60"],
        "date": [f"{year}-01-01/{year}-12-31"], "time": [f"{h:02d}:00" for h in range(0, 24, 3)],
        "area": [north, west, south, east], "data_format": "netcdf_zip",
    }, str(zpath))
    with zipfile.ZipFile(zpath) as z:
        z.extractall(where)
    zpath.unlink()
    return sorted(where.glob("*.nc"))


def fold_eac4(bbox, years, fold: Fold) -> None:
    """EAC4 over bbox into fold: bilinear at the 0.1° nodes, linear between its
    three-hourly fields."""
    import math
    import pandas as pd
    import xarray as xr
    lat_w = xr.DataArray([n[0] for n in fold.wanted], dims="node")
    lon_w = xr.DataArray([n[1] for n in fold.wanted], dims="node")
    # On EAC4's own 0.75° grid, a cell beyond the box each way: a smaller box
    # may hold no grid point at all, and the interpolation wants them round.
    g = EAC4_STEP
    bbox = (math.floor(bbox[0] / g) * g - g, math.floor(bbox[1] / g) * g - g,
            math.ceil(bbox[2] / g) * g + g, math.ceil(bbox[3] / g) * g + g)
    with ThreadPoolExecutor(min(WORKERS, len(years))) as pool:
        futures = {pool.submit(fetch_eac4_year, bbox, y): y for y in years}
        for done in as_completed(futures):
            files = [xr.open_dataset(p) for p in done.result()]
            for var, (nc, level) in EAC4_VARS.items():
                ds = next(f for f in files if nc in f)
                da = ds[nc]
                if level:
                    da = da.isel(model_level=0) * AIR_DENSITY
                # West of Greenwich the reanalysis counts longitude to 360.
                da = da.assign_coords(longitude=(da.longitude + 180) % 360 - 180).sortby("longitude").sortby("latitude")
                at = da.interp(latitude=lat_w, longitude=lon_w) * 1e9                    # kg/m³ → µg/m³
                tname = "valid_time" if "valid_time" in at.dims else "time"
                series = pd.DataFrame(at.transpose(tname, "node").values,
                                      index=pd.DatetimeIndex(at[tname].values).tz_localize("UTC")).sort_index()
                hourly = series.resample("1h").asfreq().interpolate(limit=2, limit_area="inside")
                fold.add(var, hourly.index, hourly.to_numpy())
            for f in files:
                f.close()
            for p in done.result():
                p.unlink()
            log.info("folded EAC4 %d", futures[done])


def fold_region(bbox, years, tiles) -> tuple[Fold, str]:
    """The tables for every 0.1° node the tiles want, and a note on their source."""
    return fold_nodes(bbox, years, {node for t in tiles for node in nodes_for(t)})


def fold_nodes(bbox, years, nodes) -> tuple[Fold, str]:
    """The tables at these 0.1° nodes, from the source whose domain holds the box."""
    fold = Fold(sorted(nodes))
    if in_europe(bbox):
        fold_europe(bbox, years, fold)
        kinds = "validated" if years[-1] <= VALIDATED_UNTIL else f"validated to {VALIDATED_UNTIL}, interim after"
        note = f"Copernicus Atmosphere Data Store, {DATASET} ({kinds})"
    else:
        fold_eac4(bbox, years, fold)
        note = f"Copernicus Atmosphere Data Store, {EAC4} (0.75°, every 3 h, read at 0.1° nodes)"
    return fold, note


def fold_region_cached(bbox, years, tiles) -> tuple[Fold, str]:
    """The folded tables for a box, kept as one small file, so that tiles
    built later still get their air; any earlier fold over the same years
    that holds every node will do."""
    tag = f"folded_{'_'.join(f'{v:.2f}' for v in bbox)}_{years[0]}-{years[-1]}.npz"
    path = CACHE / tag
    wanted = {node for t in tiles for node in nodes_for(t)}
    for p in sorted(CACHE.glob(f"folded_*_{years[0]}-{years[-1]}.npz"), key=lambda p: p != path):
        f = Fold.load(p)
        if wanted <= set(f.wanted) and next(iter(f.days.values())).any():
            log.info("folded tables from %s", p.name)
            return f, json.loads(p.with_suffix(".json").read_text())["note"] if p.with_suffix(".json").exists() else ""
    fold, note = fold_region(bbox, years, tiles)
    CACHE.mkdir(parents=True, exist_ok=True)
    fold.save(path)
    path.with_suffix(".json").write_text(json.dumps({"note": note}))
    log.info("folded tables saved to %s", path.name)
    return fold, note


# ---------------------------------------------------------------- tiles

def build_tile(tile, out_dir: Path, years, fold: Fold, source_note: str) -> dict:
    """One tile's month × hour tables into out_dir/climatology.json; returns
    the meta entry. Writes nothing else."""
    index = {node: k for k, node in enumerate(fold.wanted)}
    zone = zone_at(*tile.centre)
    nodes = []
    for (lat, lon) in nodes_for(tile):
        nodes.append({"lat": lat, "lon": lon, "requested": [lat, lon], **fold.node(index[(lat, lon)])})
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"step": 0.1, "timezone": zone, "years": years, "vars": VARS,
               "lats": sorted({n["requested"][0] for n in nodes}), "lons": sorted({n["requested"][1] for n in nodes}),
               "nodes": nodes}
    (out_dir / "climatology.json").write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    europe = DATASET in source_note
    return {
        "product": ("CAMS European air quality reanalysis (ensemble, surface)" if europe else
                    "CAMS global reanalysis (EAC4, surface and lowest model level)")
                   + ": hourly means by calendar month, local time",
        "years": years, "timezone": zone, "step_degrees": 0.1, "nodes": len(nodes), "vars": VARS,
        "who_daily_guideline": WHO_DAILY, "file": "air/climatology.json",
        "source": source_note,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caveat": ("A ~11 km model grid: regional background, not the street." if europe else
                   "An ~80 km model grid read at 0.1°: a region's background, far from the street; NO₂ is diluted well "
                   "below what a road sees.") + " Means over the years listed, not any particular day.",
    }


def write_tiles(tiles, years, fold, source_note):
    """The old command line: build straight into each tile's folder."""
    for tile in tiles:
        write_meta(tile, "air", build_tile(tile, tile.path / "air", years, fold, source_note))
        log.info("wrote %s", tile.id)


def main():
    from run_region import REGIONS, tiles_in_bbox
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
    fold, note = fold_region_cached(fetch_box, years, tiles)
    if args.only_built and not args.tiles:
        from tiles import read_meta
        tiles = [t for t in tiles
                 if read_meta(t).get("products", {}).get("wind", {}).get("osm_buildings", 0) >= args.only_built]
        log.info("%d built tiles to write", len(tiles))
    write_tiles(tiles, years, fold, note)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    main()
