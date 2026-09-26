"""
Surface temperature by month, from Landsat 8 and 9, at 30 m.

What the number is: the temperature of the surface — roof, asphalt, canopy,
river — that the satellite's thermal band sees at its overpass, about 10:30
local solar time, on cloud-free days, as delivered in the Collection 2
Level-2 ST_B10 product (Kelvin, scaled). For a tile, every scene since 2018
with less than 40 % cloud is read, cloud and shadow pixels are dropped using
the QA band, and the per-pixel *median* over all scenes of each calendar
month is kept. Twelve rasters per tile: the date slider moves between them.

What it is not: the air temperature at any hour. A black roof in July reads
50 °C at 10:30 while the air is 30 °C; that difference between one pixel and
the next — the part the coarse weather models cannot see — is the point of
this product, and the app labels it as surface, mid-morning.

Source: Microsoft Planetary Computer's STAC mirror of USGS Landsat C2 L2.
No account, no key; the SAS signing is anonymous and rate-limited. Public
domain data (USGS). Each scene is read through a warped VRT straight into
the tile's lat/lon grid, so only the window over the tile crosses the wire.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT

from tiles import Tile, write_meta

# 30 m at the equator is 0.00027°; the same step in latitude everywhere, and
# a little coarser than 30 m in longitude at Italian latitudes. Close enough.
DEG_PER_PX = 0.00027
YEARS = "2018-01-01/2025-12-31"
MAX_CLOUD = 40
CACHE = Path(__file__).resolve().parent / "cache" / "landsat"

# Temperature is stored as a byte: -10 °C → 0, +54 °C → 255, a quarter of a
# degree per step. Alpha 0 means no cloud-free observation there.
T_MIN, T_MAX = -10.0, 54.0

# Windowed reads over HTTP: do not list directories, do not probe sidecars.
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "4")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")


def qa_is_clear(qa: np.ndarray) -> np.ndarray:
    """Collection 2 QA_PIXEL: bit 1 dilated cloud, 2 cirrus, 3 cloud, 4 shadow."""
    bad = (qa >> 1 | qa >> 2 | qa >> 3 | qa >> 4) & 1
    return (bad == 0) & (qa != 0) & (qa != 1)


def read_scene(item, tile: Tile, shape: tuple[int, int]):
    """Surface temperature in °C over the tile, NaN where clouded or missing."""
    west, south, east, north = tile.bounds
    transform = from_bounds(west, south, east, north, shape[1], shape[0])
    signed = planetary_computer.sign(item)
    out = {}
    for key, resampling in (("lwir11", Resampling.bilinear), ("qa_pixel", Resampling.nearest)):
        with rasterio.open(signed.assets[key].href) as src:
            with WarpedVRT(src, crs="EPSG:4326", transform=transform, width=shape[1], height=shape[0],
                           resampling=resampling, nodata=0) as vrt:
                out[key] = vrt.read(1)
    st = out["lwir11"].astype(np.float32)
    band = item.assets["lwir11"].extra_fields["raster:bands"][0]
    kelvin = st * band["scale"] + band["offset"]
    celsius = kelvin - 273.15
    valid = (out["lwir11"] != 0) & qa_is_clear(out["qa_pixel"])
    celsius[~valid] = np.nan
    return celsius


def cached_scene(item, tile: Tile, shape) -> np.ndarray | None:
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"{tile.id}_{item.id}.npy"
    if p.exists():
        return np.load(p)
    try:
        arr = read_scene(item, tile, shape)
    except Exception as e:  # a scene that will not read is a scene we do without
        print(f"    skip {item.id}: {e}", file=sys.stderr)
        return None
    np.save(p, arr.astype(np.float16))
    return arr.astype(np.float16)


def encode(t: np.ndarray) -> Image.Image:
    valid = np.isfinite(t)
    byte = np.clip(np.round((t - T_MIN) / (T_MAX - T_MIN) * 255), 0, 255)
    byte = np.where(valid, byte, 0).astype(np.uint8)
    alpha = np.where(valid, 255, 0).astype(np.uint8)
    return Image.fromarray(np.dstack([byte, byte, byte, alpha]), "RGBA")


def run(tile: Tile) -> None:
    shape = tile.shape(DEG_PER_PX)
    print(f"{tile.id}: {shape[0]}×{shape[1]} px at {DEG_PER_PX}°")
    cat = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1")
    items = list(cat.search(
        collections=["landsat-c2-l2"], bbox=list(tile.bounds), datetime=YEARS,
        query={"eo:cloud_cover": {"lt": MAX_CLOUD}, "platform": {"in": ["landsat-8", "landsat-9"]}},
    ).items())
    print(f"  {len(items)} scenes under {MAX_CLOUD}% cloud")

    by_month: dict[int, list[np.ndarray]] = {m: [] for m in range(1, 13)}
    t0 = time.time()
    for i, item in enumerate(sorted(items, key=lambda it: it.datetime)):
        arr = cached_scene(item, tile, shape)
        if arr is None:
            continue
        # A scene that only clips the tile's corner adds noise, not signal.
        if np.isfinite(arr).mean() < 0.05:
            continue
        by_month[item.datetime.month].append(arr)
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(items)} scenes, {time.time() - t0:.0f}s")

    out_dir = tile.path / "heat"
    out_dir.mkdir(parents=True, exist_ok=True)
    months = {}
    for m, stack in by_month.items():
        if not stack:
            continue
        cube = np.stack(stack).astype(np.float32)
        median = np.nanmedian(cube, axis=0)
        count = np.isfinite(cube).sum(axis=0)
        # One clear look is not a climatology; ask for at least three.
        median[count < 3] = np.nan
        encode(median).save(out_dir / f"m{m:02d}.png", optimize=True)
        tile_median = float(np.nanmedian(median)) if np.isfinite(median).any() else None
        months[f"{m:02d}"] = {
            "scenes": len(stack),
            "coverage": round(float(np.isfinite(median).mean()), 3),
            "tile_median_c": None if tile_median is None else round(tile_median, 2),
        }
        print(f"  month {m:02d}: {len(stack)} scenes, coverage {months[f'{m:02d}']['coverage']:.0%}")

    write_meta(tile, "heat", {
        "product": "Landsat 8/9 Collection 2 Level-2 surface temperature (ST_B10), per-pixel median by calendar month",
        "years": YEARS,
        "max_cloud_percent": MAX_CLOUD,
        "overpass_local_time": "~10:30",
        "rows": shape[0], "cols": shape[1], "degrees_per_pixel": DEG_PER_PX,
        "encoding": {"byte0_c": T_MIN, "byte255_c": T_MAX, "alpha0": "no cloud-free observation (fewer than 3 clear scenes)"},
        "files": {f"m{m:02d}": f"heat/m{m:02d}.png" for m in range(1, 13) if f"{m:02d}" in months},
        "months": months,
        "source": "USGS via Microsoft Planetary Computer STAC (landsat-c2-l2)",
        "licence": "Landsat data are in the public domain (USGS)",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caveat": "Surface temperature at a mid-morning overpass on clear days, not air temperature; a median over several years, not any particular day.",
    })
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    tile = Tile.parse(sys.argv[1]) if len(sys.argv) > 1 else Tile.containing(41.8905, 12.4924)
    run(tile)
