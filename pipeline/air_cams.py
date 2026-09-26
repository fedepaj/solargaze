"""
Air quality as a climatology: what the air over a tile is typically like at
each hour of each month, from CAMS reanalysis and analysis fields served by
Open-Meteo's air-quality archive (0.1°, ~11 km, from 2013).

For every model node touching the tile — plus one ring outside it, so the
app's interpolation has neighbours at the edges — five years of hourly
values are folded into a 12 × 24 table of means per pollutant, in the tile's
local time. The app indexes that table with the two sliders: the date picks
the month (blended with its neighbour), the clock picks the hour. Two annual
figures per node come along for the pane: the mean, and how often a day
exceeds the WHO 24-hour guideline.

This is the coarse, honest layer: it says how bad the air *regionally* tends
to be at 8 in the morning in January. The street-level correction from
monitoring stations is a separate product that adds to it.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

from tiles import Tile, write_meta

API = "https://air-quality-api.open-meteo.com/v1/air-quality"
STEP = 0.1
YEARS = [2020, 2021, 2022, 2023, 2024]
VARS = ["pm2_5", "pm10", "nitrogen_dioxide", "ozone"]
# WHO 2021 guideline, 24-hour mean, µg/m³.
WHO_DAILY = {"pm2_5": 15, "pm10": 45, "nitrogen_dioxide": 25, "ozone": 100}
CACHE = Path(__file__).resolve().parent / "cache" / "cams"


def nodes_for(tile: Tile) -> list[tuple[float, float]]:
    west, south, east, north = tile.bounds
    lats = np.arange(np.floor(south / STEP) * STEP - STEP, north + STEP + 1e-9, STEP)
    lons = np.arange(np.floor(west / STEP) * STEP - STEP, east + STEP + 1e-9, STEP)
    return [(round(float(a), 2), round(float(o), 2)) for a in lats for o in lons]


def fetch_year(lat: float, lon: float, year: int) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"{lat}_{lon}_{year}.json"
    if p.exists():
        return json.loads(p.read_text())
    for attempt in range(5):
        r = requests.get(API, params={
            "latitude": lat, "longitude": lon, "hourly": ",".join(VARS),
            "start_date": f"{year}-01-01", "end_date": f"{year}-12-31", "timezone": "auto",
        }, timeout=60)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        data = r.json()
        p.write_text(json.dumps(data))
        return data
    raise RuntimeError(f"rate limited at {lat},{lon} {year}")


def fold(years: list[dict]) -> dict:
    """12×24 means per variable, annual mean, and WHO exceedance share."""
    out = {}
    for var in VARS:
        table = np.zeros((12, 24)); count = np.zeros((12, 24))
        daily = []
        for data in years:
            times = data["hourly"]["time"]
            vals = data["hourly"][var]
            months = np.array([int(t[5:7]) - 1 for t in times])
            hours = np.array([int(t[11:13]) for t in times])
            v = np.array([np.nan if x is None else x for x in vals], dtype=float)
            ok = np.isfinite(v)
            np.add.at(table, (months[ok], hours[ok]), v[ok])
            np.add.at(count, (months[ok], hours[ok]), 1)
            # Daily means for the guideline count: 24 consecutive hours, local time.
            n = len(v) // 24 * 24
            daily.append(np.nanmean(v[:n].reshape(-1, 24), axis=1))
        daily = np.concatenate(daily)
        daily = daily[np.isfinite(daily)]
        with np.errstate(invalid="ignore"):
            mean = np.where(count > 0, table / count, np.nan)
        out[var] = {
            "by_month_hour": [[None if np.isnan(x) else round(float(x), 1) for x in row] for row in mean],
            "annual_mean": round(float(np.nanmean(mean)), 1),
            "days_over_who": round(float((daily > WHO_DAILY[var]).mean()), 3) if len(daily) else None,
        }
    return out


def run(tile: Tile) -> None:
    nodes = nodes_for(tile)
    print(f"{tile.id}: {len(nodes)} CAMS nodes × {len(YEARS)} years")
    t0 = time.time()
    result = []
    tz = None
    for i, (lat, lon) in enumerate(nodes):
        years = [fetch_year(lat, lon, y) for y in YEARS]
        tz = tz or years[0].get("timezone")
        result.append({"lat": years[0]["latitude"], "lon": years[0]["longitude"], "requested": [lat, lon], **fold(years)})
        print(f"  node {i + 1}/{len(nodes)} ({lat}, {lon}) {time.time() - t0:.0f}s")

    out_dir = tile.path / "air"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "step": STEP, "timezone": tz, "years": YEARS, "vars": VARS,
        "lats": sorted({n["requested"][0] for n in result}),
        "lons": sorted({n["requested"][1] for n in result}),
        "nodes": result,
    }
    (out_dir / "climatology.json").write_text(json.dumps(payload, separators=(",", ":")) + "\n")

    write_meta(tile, "air", {
        "product": "CAMS (Copernicus) via Open-Meteo air-quality archive: hourly means by calendar month, local time",
        "years": YEARS, "timezone": tz, "step_degrees": STEP, "nodes": len(result), "vars": VARS,
        "who_daily_guideline": WHO_DAILY,
        "file": "air/climatology.json",
        "source": "Open-Meteo (CC-BY 4.0), CAMS European air quality reanalysis/analysis",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caveat": "A ~11 km model grid: regional background, not the street. Means over five years, not any particular day.",
    })
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    tile = Tile.parse(sys.argv[1]) if len(sys.argv) > 1 else Tile.containing(41.8905, 12.4924)
    run(tile)
