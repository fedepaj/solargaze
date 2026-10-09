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
from sg.errors import NoData, Transient

# Scenes are read at 30 m — 0.00027°, the product's own pixel — and the
# monthly median is then averaged 3 × 3 down to 90 m before it is written.
# Nothing real is lost: Landsat's thermal band is acquired at 100 m and only
# resampled to 30 m by the USGS, and against a 90 m mean just 2 % of pixels
# differ by more than half a degree. The file is a ninth of the size, and the
# app interpolates it back up the way a terrain viewer interpolates a DEM.
DEG_PER_PX = 0.00027
DOWNSAMPLE = 3
YEARS = "2018-01-01/2025-12-31"
MAX_CLOUD = 40
CACHE = Path(__file__).resolve().parent / "cache" / "landsat"

# One 8-bit channel: byte 0 is "no cloud-free observation", and from 1 up
# the temperature climbs a quarter of a degree a step from T_MIN.
T_MIN, T_STEP = -10.0, 0.25
ENCODING = 2

# Windowed reads over HTTP: do not list directories, do not probe sidecars.
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "4")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")


def qa_is_clear(qa: np.ndarray) -> np.ndarray:
    """Collection 2 QA_PIXEL: bit 1 dilated cloud, 2 cirrus, 3 cloud, 4 shadow."""
    bad = (qa >> 1 | qa >> 2 | qa >> 3 | qa >> 4) & 1
    return (bad == 0) & (qa != 0) & (qa != 1)


def thermal_key(item) -> str | None:
    """The surface-temperature asset: lwir11 on Landsat 8/9 (band 10), lwir
    on Landsat 4–7 (band 6); None where the USGS published reflectance only."""
    return next((k for k in ("lwir11", "lwir") if k in item.assets), None)


def read_scene(item, tile: Tile, shape: tuple[int, int]):
    """Surface temperature in °C over the tile, NaN where clouded or missing."""
    west, south, east, north = tile.bounds
    transform = from_bounds(west, south, east, north, shape[1], shape[0])
    signed = planetary_computer.sign(item)
    out = {}
    thermal = thermal_key(item)
    for key, resampling in ((thermal, Resampling.bilinear), ("qa_pixel", Resampling.nearest)):
        with rasterio.open(signed.assets[key].href) as src:
            with WarpedVRT(src, crs="EPSG:4326", transform=transform, width=shape[1], height=shape[0],
                           resampling=resampling, nodata=0) as vrt:
                out[key] = vrt.read(1)
    st = out[thermal].astype(np.float32)
    band = item.assets[thermal].extra_fields["raster:bands"][0]
    kelvin = st * band["scale"] + band["offset"]
    celsius = kelvin - 273.15
    valid = (out[thermal] != 0) & qa_is_clear(out["qa_pixel"])
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


def downsample(t: np.ndarray, k: int = DOWNSAMPLE) -> np.ndarray:
    """k × k mean ignoring NaN; a block with no valid pixel stays NaN."""
    rows, cols = (t.shape[0] // k) * k, (t.shape[1] // k) * k
    blocks = t[:rows, :cols].reshape(rows // k, k, cols // k, k)
    with np.errstate(invalid="ignore"):
        return np.nanmean(blocks, axis=(1, 3))


SCENE_WORKERS = int(os.environ.get("SG_SCENE_WORKERS", "8"))

# Overviews: the 90 m month averaged 3 × 3 and 9 × 9 again, for the view
# from far away. The app picks the level by how many tiles are on screen;
# a whole region at 810 m is a few kilobytes a tile.
OVERVIEWS = (3, 9)


# The twelve months of a level travel as one image, stacked north to south
# from January: one request and one object instead of twelve. A month with
# no data is a band of byte 0, like any pixel without one.
PACKING = "months stacked north to south, January on top, each `rows` tall; byte 0 = no data"


def stack_months(by_month: dict[int, np.ndarray], rows: int, cols: int) -> np.ndarray:
    """Byte images of the months present → one (12·rows) × cols image."""
    out = np.zeros((12 * rows, cols), np.uint8)
    for m, b in by_month.items():
        out[(m - 1) * rows:m * rows] = b
    return out


def write_packed(t90: dict[int, np.ndarray], out_dir: Path, subdir: str = "heat",
                 t_min: float = T_MIN, t_step: float = T_STEP) -> dict:
    """The 90 m months and their overviews, packed; returns the file map.
    Other surface-temperature products (heat_ecostress.py) write the same
    way under their own folder, with their own scale."""
    rows, cols = next(iter(t90.values())).shape
    files = {}
    for k in (1, *OVERVIEWS):
        level = {m: np.asarray(encode(t if k == 1 else downsample(t, k), t_min, t_step)) for m, t in t90.items()}
        r, c = next(iter(level.values())).shape
        name = "months" if k == 1 else f"months.o{k}"
        Image.fromarray(stack_months(level, r, c), "L").save(out_dir / f"{name}.png", optimize=True)
        files[name] = f"{subdir}/{name}.png"
    return files


def encode(t: np.ndarray, t_min: float = T_MIN, t_step: float = T_STEP) -> Image.Image:
    valid = np.isfinite(t)
    byte = np.clip(np.round((t - t_min) / t_step) + 1, 1, 255)
    return Image.fromarray(np.where(valid, byte, 0).astype(np.uint8), "L")


def run(tile: Tile) -> None:
    """The old command line: build straight into the tile's folder."""
    info = build(tile, tile.path / "heat")
    write_meta(tile, "heat", info)


PLATFORMS = ("landsat-8", "landsat-9")


def build(tile: Tile, out_dir: Path, years: str = YEARS, platforms: tuple[str, ...] = PLATFORMS,
          max_per_month: int | None = None, subdir: str = "heat") -> dict:
    """The packed months into out_dir; returns the meta entry, writes nothing
    else. NoData where no scene covers the tile or none carries surface
    temperature; Transient when too many scenes would not read (the network,
    not the tile). An earlier decade (heat_past) passes its own years and
    satellites, and a cap on the scenes a month: the clearest are kept."""
    shape = tile.shape(DEG_PER_PX)
    print(f"{tile.id}: {shape[0]}×{shape[1]} px at {DEG_PER_PX}°")
    cat = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1")
    items = list(cat.search(
        collections=["landsat-c2-l2"], bbox=list(tile.bounds), datetime=years,
        query={"eo:cloud_cover": {"lt": MAX_CLOUD}, "platform": {"in": list(platforms)}},
    ).items())
    print(f"  {len(items)} scenes under {MAX_CLOUD}% cloud")
    # Where the USGS lacks the ancillary data for surface temperature (the
    # Azores, ocean islands) it publishes reflectance only (L2SR, no lwir11):
    # nothing to read there, however often it is asked.
    with_st = [it for it in items if thermal_key(it)]
    if items and not with_st:
        raise NoData(f"{tile.id}: Landsat has no surface temperature here ({len(items)} scenes, reflectance only)")
    items = with_st
    if max_per_month:
        kept: dict[int, list] = {}
        for it in sorted(items, key=lambda it: it.properties.get("eo:cloud_cover", 100)):
            if len(kept.setdefault(it.datetime.month, [])) < max_per_month:
                kept[it.datetime.month].append(it)
        items = [it for month in kept.values() for it in month]
        print(f"  kept the clearest {max_per_month} a month: {len(items)} scenes")

    by_month: dict[int, list[np.ndarray]] = {m: [] for m in range(1, 13)}
    t0 = time.time()
    failed = 0
    # Reading a scene is a few windowed requests to the archive and mostly
    # waiting, so several go at once: a runner far from the archive spent
    # ten minutes a tile reading them one by one.
    from concurrent.futures import ThreadPoolExecutor
    ordered = sorted(items, key=lambda it: it.datetime)
    with ThreadPoolExecutor(SCENE_WORKERS) as pool:
        arrays = pool.map(lambda it: cached_scene(it, tile, shape), ordered)
        pairs = list(zip(ordered, arrays))
    for i, (item, arr) in enumerate(pairs):
        if arr is None:
            failed += 1
            continue
        # A scene that only clips the tile's corner adds noise, not signal.
        if np.isfinite(arr).mean() < 0.05:
            continue
        by_month[item.datetime.month].append(arr)
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(items)} scenes, {time.time() - t0:.0f}s")

    # A scene or two that will not read is the archive's; many is the
    # network's, and a tile written from what happened to get through would
    # look finished while missing most of its months. Keep the scene cache,
    # write nothing, and let the next run pick it up.
    if not items:
        raise NoData(f"{tile.id}: no Landsat scene under {MAX_CLOUD}% cloud")
    if failed > max(3, 0.05 * len(items)):
        raise Transient(f"{tile.id}: {failed} of {len(items)} scenes failed to read; tile not written")

    out_dir.mkdir(parents=True, exist_ok=True)
    months = {}
    medians = {}
    for m, stack in by_month.items():
        if not stack:
            continue
        cube = np.stack(stack).astype(np.float32)
        median = np.nanmedian(cube, axis=0)
        count = np.isfinite(cube).sum(axis=0)
        # One clear look is not a climatology; ask for at least three.
        median[count < 3] = np.nan
        median = downsample(median)
        medians[m] = median
        tile_median = float(np.nanmedian(median)) if np.isfinite(median).any() else None
        months[f"{m:02d}"] = {
            "scenes": len(stack),
            "coverage": round(float(np.isfinite(median).mean()), 3),
            "tile_median_c": None if tile_median is None else round(tile_median, 2),
        }
        print(f"  month {m:02d}: {len(stack)} scenes, coverage {months[f'{m:02d}']['coverage']:.0%}")

    if not medians:
        raise NoData(f"{tile.id}: no month with a clear look")
    files = write_packed(medians, out_dir, subdir)

    # The per-scene cache exists to resume an interrupted tile, not to keep
    # half a gigabyte per tile around: drop it once the months are written.
    for p in CACHE.glob(f"{tile.id}_*.npy"):
        p.unlink()

    info = {
        "product": f"Landsat {'/'.join(p.split('-')[1] for p in platforms)} Collection 2 Level-2 surface temperature, "
                   "per-pixel median by calendar month",
        "years": years, "platforms": list(platforms), "max_per_month": max_per_month,
        "max_cloud_percent": MAX_CLOUD,
        "overpass_local_time": "~10:30",
        "rows": shape[0] // DOWNSAMPLE, "cols": shape[1] // DOWNSAMPLE,
        "degrees_per_pixel": DEG_PER_PX * DOWNSAMPLE,
        "native_degrees_per_pixel": DEG_PER_PX,
        "encoding": {"version": ENCODING, "channel": "L", "nodata_byte": 0, "byte1_c": T_MIN, "step_c": T_STEP,
                     "note": "median at 30 m, then a 3 x 3 mean; byte 0 = fewer than 3 clear scenes"},
        "files": files,
        "packing": PACKING,
        "overviews": list(OVERVIEWS),
        "months": months,
        "source": "USGS via Microsoft Planetary Computer STAC (landsat-c2-l2)",
        "licence": "Landsat data are in the public domain (USGS)",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caveat": "Surface temperature at a mid-morning overpass on clear days, not air temperature; a median over several years, not any particular day.",
    }
    print(f"done in {time.time() - t0:.0f}s")
    return info


if __name__ == "__main__":
    tile = Tile.parse(sys.argv[1]) if len(sys.argv) > 1 else Tile.containing(41.8905, 12.4924)
    run(tile)
