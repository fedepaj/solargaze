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

import stored
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
# the temperature climbs a quarter of a degree a step from T_MIN. That first
# scale stopped at 53.5 °C, and a July morning in Sicily has one roof in nine
# above it: tiles are now written on SCALE, three tenths of a degree a step,
# up to 66 °C. Each tile's meta says its scale and the app reads it from
# there; T_MIN and T_STEP stay for the tiles written with them.
T_MIN, T_STEP = -10.0, 0.25
SCALE = (-10.0, 0.3)
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


def read_scene(item, tile: Tile, shape: tuple[int, int], bounds=None, thermal_resampling=Resampling.bilinear):
    """Surface temperature in °C over the tile (or other bounds), NaN where
    clouded or missing."""
    west, south, east, north = bounds or tile.bounds
    transform = from_bounds(west, south, east, north, shape[1], shape[0])
    signed = planetary_computer.sign(item)
    out = {}
    thermal = thermal_key(item)
    for key, resampling in ((thermal, thermal_resampling), ("qa_pixel", Resampling.nearest)):
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
        # A file the archive holds broken (LC08_L2SP_187027_20220606 since
        # 2026-10) fails alike every night, on every tile it covers: a scene
        # missing, not the network failing, so it must not count towards the
        # failures that send a tile back as transient.
        if "not recognized as being in a supported file format" in str(e):
            return np.full(shape, np.nan, np.float16)
        return None
    np.save(p, arr.astype(np.float16))
    return arr.astype(np.float16)


# -- a day's warmth -----------------------------------------------------------
#
# A month is a median over the days that happened to be clear, and which days
# were clear differs across a tile: a swath edge, a scene that clips a corner.
# Where a warm week was clear on one side and a cold one on the other, the
# median steps along the swath (issue #24: 7 °C between neighbouring tiles
# near Stuttgart in October). Each scene's level — its day's warmth — is
# fitted over a window of the tile and its eight neighbours, read coarse
# (LEVEL_DEG, an overview of the file), from every scene that crosses the
# window: T[scene, pixel] = level[scene] + ground[pixel], by medians. The
# month is then the median of the scenes with their levels taken out, plus
# the level the plain median would have had, smoothed over LEVEL_SIGMA_PX.
# That last term is what keeps neighbouring tiles together: any constant in
# the fitted levels cancels in it pixel by pixel, and near an edge both tiles
# smooth the same window. Taking a constant out instead (centring the levels
# on the tile's own scenes, #25) left steps of 4 °C at tile edges.
#
# Measured over 20 tiles round Stuttgart (31 edges), October 2018–25: the
# spread between groups of pixels that saw different scenes 0.95 → 0.56 °C
# rms (coldest to warmest group 3.3 → 2.0 °C); the step at tile edges 1.03 →
# 1.00 °C at the median, 1.42 → 1.37 at the 90th percentile, 1.80 → 1.61 at
# the worst. April, with no such swath, is unchanged (0.63 → 0.61 °C). Tile
# medians move by 0.7 °C on average in October (1.8 at most), 0.2 in April.
LEVEL_DEG = 0.0081        # the window read at ~0.9 km
LEVEL_MARGIN = 0.25       # one tile each side
LEVEL_SIGMA_PX = 24       # ~22 km north–south, ~15 east–west


def cached_coarse(item, tile: Tile, bounds, shape) -> np.ndarray | None:
    """The scene over the window, coarse; cached like the scenes."""
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"{tile.id}_{item.id}_w.npy"
    if p.exists():
        return np.load(p).astype(np.float32)
    try:
        arr = read_scene(item, tile, shape, bounds, Resampling.average)
    except Exception as e:  # noqa: BLE001 — a level we do without: that scene is not corrected
        print(f"    skip window {item.id}: {e}", file=sys.stderr)
        return None
    np.save(p, arr.astype(np.float16))
    return arr


def _smooth(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian mean of the finite cells; NaN where there is next to nothing."""
    from scipy.ndimage import gaussian_filter
    ok = np.isfinite(a)
    num = gaussian_filter(np.where(ok, a, 0.0), sigma, mode="constant")
    den = gaussian_filter(ok.astype(np.float32), sigma, mode="constant")
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    out[den < 0.05] = np.nan
    return out


def scene_levels(cube: np.ndarray, min_px: int = 30, passes: int = 3, soft_c: float = 3.0) -> np.ndarray:
    """Each scene's level over a (scenes, rows, cols) cube: T[s, p] = level[s]
    + ground[p], solved for the levels with the grounds eliminated (least
    squares, then reweighted so that a cloud the mask missed or a field
    ploughed between two passes weighs little). A cell seen by one scene, or
    only by scenes that see all the same cells, says nothing about levels and
    weighs nothing — a median fit gets stuck there, where two swaths overlap
    by a sliver. A scene with fewer than min_px shared cells gets the mean
    level: not corrected. The levels are known up to a constant."""
    n = cube.shape[0]
    x = cube.reshape(n, -1).astype(np.float64)
    seen = np.isfinite(x)
    shared = seen.sum(axis=0) >= 2
    x, seen = np.where(seen, x, 0.0)[:, shared], seen[:, shared]
    w = seen.astype(np.float64)
    level = np.zeros(n)
    for _ in range(passes):
        k = w.sum(axis=0)
        k[k == 0] = 1.0
        mean = (w * x).sum(axis=0) / k
        a = np.diag(w.sum(axis=1)) - (w / k) @ w.T
        b = (w * (x - mean)).sum(axis=1)
        level = np.linalg.lstsq(a, b, rcond=None)[0]
        ground = (w * (x - level[:, None])).sum(axis=0) / k
        resid = np.where(seen, x - level[:, None] - ground, 0.0)
        w = seen / (1.0 + (resid / soft_c) ** 2)
    level[seen.sum(axis=1) < min_px] = 0.0
    return level.astype(np.float32)


def level_field(cube: np.ndarray, level: np.ndarray, sigma: float = LEVEL_SIGMA_PX) -> np.ndarray:
    """What the plain median adds over the levelled one, smoothed: the warmth
    of the days a place typically saw, without the step at a swath."""
    with np.errstate(all="ignore"):
        plain = np.nanmedian(cube, axis=0)
        levelled = np.nanmedian(cube - level[:, None, None], axis=0)
    field = _smooth(plain - levelled, sigma)
    return np.where(np.isfinite(field), field, np.nanmedian(field) if np.isfinite(field).any() else 0.0)


def onto(field: np.ndarray, window, tile_bounds, shape) -> np.ndarray:
    """A coarse field over the window, sampled at the centres of the tile's pixels."""
    from scipy.ndimage import map_coordinates
    w, s, e, n = window
    tw, ts, te, tn = tile_bounds
    rows = (n - (tn - (np.arange(shape[0]) + 0.5) * (tn - ts) / shape[0])) / ((n - s) / field.shape[0]) - 0.5
    cols = ((tw + (np.arange(shape[1]) + 0.5) * (te - tw) / shape[1]) - w) / ((e - w) / field.shape[1]) - 0.5
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    return map_coordinates(field, [rr, cc], order=1, mode="nearest").astype(np.float32)


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
        stored.lossy(stack_months(level, r, c), out_dir / f"{name}.avif")
        files[name] = f"{subdir}/{name}.avif"
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


# Landsat 5 and 7 take whole decks of low cloud for clear ground: their cloud
# mask has no cirrus band and leans on a thermal test that a winter stratus
# passes, so a scene can read -50 °C over a tile at "0 % cloud" — and ranking
# scenes by their cloud cover puts exactly those first. An earlier decade
# therefore checks each scene against the present: a scene whose clear pixels
# have a median more than COLDER_C under the same month of the present heat
# (Landsat 8/9, a better mask and thirty-odd scenes) is a cloud, and is not
# used. Measured on 34 tiles across Europe, 4657 scenes of 1984–93: one in
# fifteen sits more than 25 °C under the present (the worst 167 °C under),
# while the ground sits 3.7 °C under at the median and one scene in twenty
# 11 °C or more under (the earlier overpass, the snow of the winters of
# 1985–87). With the check, months more than 12 °C under the present went
# from 18 of 408 to 2, and the share of pixels with a value did not drop.
COLDER_C = 15.0


def _window(cat, tile: Tile, years: str, platforms, wanted: set[int]):
    """Every scene of the wanted months over the tile and its neighbours,
    coarse: month → [(scene id, array)], and the window's bounds."""
    w, s, e, n = tile.bounds
    window = (w - LEVEL_MARGIN, s - LEVEL_MARGIN, e + LEVEL_MARGIN, n + LEVEL_MARGIN)
    shape = (round((window[3] - window[1]) / LEVEL_DEG), round((window[2] - window[0]) / LEVEL_DEG))
    from shapely.geometry import box, shape as footprint
    frame = box(*window)
    # The catalogue answers for any scene whose outline touches the window,
    # and half of those only graze it with the empty margin of the file:
    # nothing to read there.
    items = [it for it in cat.search(
        collections=["landsat-c2-l2"], bbox=list(window), datetime=years,
        query={"eo:cloud_cover": {"lt": MAX_CLOUD}, "platform": {"in": list(platforms)}},
    ).items() if thermal_key(it) and it.datetime.month in wanted
        and (not getattr(it, "geometry", None) or footprint(it.geometry).intersection(frame).area > 0.02 * frame.area)]
    t0 = time.time()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(SCENE_WORKERS) as pool:
        arrays = list(pool.map(lambda it: cached_coarse(it, tile, window, shape), items))
    out: dict[int, list] = {}
    for it, arr in zip(items, arrays):
        if arr is not None and np.isfinite(arr).mean() >= 0.01:
            out.setdefault(it.datetime.month, []).append((it.id, arr))
    print(f"  window: {len(items)} scenes, {sum(map(len, out.values()))} with clear ground, {time.time() - t0:.0f}s")
    return window, out


def build(tile: Tile, out_dir: Path, years: str = YEARS, platforms: tuple[str, ...] = PLATFORMS,
          max_per_month: int | None = None, subdir: str = "heat",
          reference_c: dict[int, float] | None = None, levels: bool = False,
          cell: int = DOWNSAMPLE) -> dict:
    """The packed months into out_dir; returns the meta entry, writes nothing
    else. NoData where no scene covers the tile or none carries surface
    temperature; Transient when too many scenes would not read (the network,
    not the tile). An earlier decade (heat_past) passes its own years and
    satellites, a cap on the scenes a month — the clearest are kept — and
    each month's tile median of the present, against which a scene that
    reads far colder is dropped as cloud (COLDER_C). With levels, each
    scene's warmth is taken out before the median (see LEVEL_DEG)."""
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
    cover = lambda it: it.properties.get("eo:cloud_cover", 100)  # noqa: E731
    # Each month's scenes, clearest first. Without a cap every one is read in
    # one go; with one, the cap's worth first, and for a month that lost some
    # (a cloud, a scene that only clips the tile) the next ones, until the cap
    # is full or the month has none left.
    queue: dict[int, list] = {m: [] for m in range(1, 13)}
    for it in sorted(items, key=cover):
        queue[it.datetime.month].append(it)

    def take(m: int, n: int | None) -> list:
        out, queue[m] = (queue[m], []) if n is None else (queue[m][:n], queue[m][n:])
        return out

    by_month: dict[int, list[tuple[str, np.ndarray]]] = {m: [] for m in range(1, 13)}
    clouds = {m: 0 for m in range(1, 13)}
    t0 = time.time()
    failed = read = 0
    # Reading a scene is a few windowed requests to the archive and mostly
    # waiting, so several go at once: a runner far from the archive spent
    # ten minutes a tile reading them one by one.
    from concurrent.futures import ThreadPoolExecutor
    batch = [it for m in queue for it in take(m, max_per_month)]
    while batch:
        with ThreadPoolExecutor(SCENE_WORKERS) as pool:
            pairs = list(zip(batch, pool.map(lambda it: cached_scene(it, tile, shape), batch)))
        read += len(pairs)
        for item, arr in sorted(pairs, key=lambda p: cover(p[0])):
            if arr is None:
                failed += 1
                continue
            # A scene that only clips the tile's corner adds noise, not signal.
            if np.isfinite(arr).mean() < 0.05:
                continue
            m = item.datetime.month
            if reference_c and m in reference_c and float(np.nanmedian(arr)) < reference_c[m] - COLDER_C:
                clouds[m] += 1
                continue
            if max_per_month and len(by_month[m]) >= max_per_month:
                continue
            by_month[m].append((item.id, arr))
        batch = [it for m in queue for it in take(m, max_per_month - len(by_month[m]))] if max_per_month else []
    print(f"  {read} scenes read, {time.time() - t0:.0f}s" + (f", {sum(clouds.values())} dropped as cloud" if reference_c else ""))

    # A scene or two that will not read is the archive's; many is the
    # network's, and a tile written from what happened to get through would
    # look finished while missing most of its months. Keep the scene cache,
    # write nothing, and let the next run pick it up.
    if not items:
        raise NoData(f"{tile.id}: no Landsat scene under {MAX_CLOUD}% cloud")
    if failed > max(3, 0.05 * read):
        raise Transient(f"{tile.id}: {failed} of {read} scenes failed to read; tile not written")

    window = coarse = None
    if levels:
        window, coarse = _window(cat, tile, years, platforms, {m for m, st in by_month.items() if st})

    out_dir.mkdir(parents=True, exist_ok=True)
    months = {}
    medians = {}
    for m, stack in by_month.items():
        if not stack:
            continue
        cube = np.stack([a for _, a in stack]).astype(np.float32)
        if coarse and coarse.get(m):
            ids, wcube = zip(*coarse[m])
            wcube = np.stack(wcube)
            level = scene_levels(wcube)
            of = dict(zip(ids, level))
            here = np.array([of.get(i, 0.0) for i, _ in stack], np.float32)
            with np.errstate(all="ignore"):
                median = np.nanmedian(cube - here[:, None, None], axis=0)
            median += onto(level_field(wcube, level), window, tile.bounds, shape)
        else:
            with np.errstate(all="ignore"):
                median = np.nanmedian(cube, axis=0)
        count = np.isfinite(cube).sum(axis=0)
        # One clear look is not a climatology; ask for at least three.
        median[count < 3] = np.nan
        median = downsample(median, cell)
        medians[m] = median
        tile_median = float(np.nanmedian(median)) if np.isfinite(median).any() else None
        months[f"{m:02d}"] = {
            "scenes": len(stack),
            "coverage": round(float(np.isfinite(median).mean()), 3),
            "tile_median_c": None if tile_median is None else round(tile_median, 2),
        }
        if reference_c:
            months[f"{m:02d}"]["clouds_dropped"] = clouds[m]
        print(f"  month {m:02d}: {len(stack)} scenes, coverage {months[f'{m:02d}']['coverage']:.0%}"
              + (f", {clouds[m]} dropped as cloud" if clouds[m] else ""))

    # Scenes, but no pixel with three clear looks in any month (an island
    # under its weather): nothing to show, which is not a fault.
    if not any(v["tile_median_c"] is not None for v in months.values()):
        raise NoData(f"{tile.id}: no month with three clear looks")
    files = write_packed(medians, out_dir, subdir, *SCALE)

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
        "rows": shape[0] // cell, "cols": shape[1] // cell,
        "degrees_per_pixel": DEG_PER_PX * cell,
        "native_degrees_per_pixel": DEG_PER_PX,
        "encoding": {"version": ENCODING, "channel": "L", "nodata_byte": 0, "lossy": stored.LOSSY,
                     "byte1_c": SCALE[0], "step_c": SCALE[1],
                     "note": f"median at 30 m, then a {cell} x {cell} mean; byte 0 = fewer than 3 clear scenes"},
        "files": files,
        **({"levels": {"window_deg": round(tile.bounds[2] - tile.bounds[0] + 2 * LEVEL_MARGIN, 4),
                       "read_deg": LEVEL_DEG, "sigma_px": LEVEL_SIGMA_PX,
                       "note": "each scene's level fitted over the window and taken out before the median; "
                               "the plain median's level put back smoothed"}} if levels else {}),
        **({"cloud_check": {"reference": "the present heat's tile median of the same month",
                            "colder_c": COLDER_C}} if reference_c else {}),
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
