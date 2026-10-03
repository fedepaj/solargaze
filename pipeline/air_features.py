"""
What is around a point, as numbers: the predictors of the street-scale air
layer.

A station next to a dual carriageway reads more NO₂ than CAMS says, one in a
park less; how much more or less is what the land-use regression in
air_lur.py learns from these numbers, at every station, and then applies at
every 50 m cell of a tile. They have to be computed the same way in both
places, so they are computed here only.

Everything happens on a 10 m grid in EPSG:3035 (the European equal-area
projection the EEA's own air-quality maps use), so a buffer of 300 m is 300 m
anywhere in Italy:

- roads, from the Geofabrik extracts (osm_extract.py): metres of major,
  secondary and local carriageway within 50, 100, 300, 500 and 1000 m, and
  the distance to the nearest major road;
- buildings, from the same extracts: the share of ground covered, and the
  built volume, within the same radii;
- land cover, ESA WorldCover 2021 at 10 m (Planetary Computer, each 3°
  file kept under ``cache/rasters/``): the share of green, built-up and
  water within 300 and 1000 m;
- terrain, Copernicus DEM GLO-30 (Planetary Computer): the altitude, and the
  altitude against the mean within 1000 m, which is what tells a basin that
  holds its air from a ridge that sheds it.

Road length is counted as touched 10 m cells × 10 m, which is exact along
the grid and up to 41 % long on the diagonal; the regression only needs the
same bias at the stations and on the map, which it gets.

No population layer yet: built volume stands in for where people live, and a
census grid is the obvious next predictor if the fit asks for one.
"""

from __future__ import annotations

import functools
import logging
import math
import sys
import time
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from scipy.ndimage import distance_transform_edt
from scipy.signal import fftconvolve

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "wind"))
from tiles import STEP, Tile  # noqa: E402
import netdns  # noqa: E402,F401  (falls back to DNS over HTTPS when the system resolver drops a name)
import osm_extract  # noqa: E402
from buildings import building_height  # noqa: E402

log = logging.getLogger("air.features")

CRS = "EPSG:3035"
PX = 10.0
RADII = (50, 100, 300, 500, 1000)
COVER_RADII = (300, 1000)
PAD = 1100.0  # metres of context around what is asked for, for the widest buffer
TO_LAEA = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
TO_LL = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
ROAD_CLASSES = ("major", "secondary", "local")
WORLDCOVER = {"green": (10, 20, 30), "builtup": (50,), "water": (80,)}


from sg.errors import Upstream  # noqa: E402


class MissingData(Upstream):
    """The extracts do not cover the window, or a raster could not be read:
    an input that is not there, retried when it might be (an upstream
    failure), never mistaken for a bug in this code."""


class Window:
    """A north-up grid of PX-metre cells in EPSG:3035."""

    def __init__(self, x0: float, y1: float, nx: int, ny: int):
        self.x0, self.y1, self.nx, self.ny = x0, y1, nx, ny
        self.transform = from_origin(x0, y1, PX, PX)

    @classmethod
    def around(cls, xmin, ymin, xmax, ymax, pad: float = PAD) -> "Window":
        x0 = math.floor((xmin - pad) / PX) * PX
        y1 = math.ceil((ymax + pad) / PX) * PX
        nx = int(math.ceil((xmax + pad - x0) / PX))
        ny = int(math.ceil((y1 - (ymin - pad)) / PX))
        return cls(x0, y1, nx, ny)

    def lonlat_bounds(self) -> tuple[float, float, float, float]:
        xs = np.array([self.x0, self.x0 + self.nx * PX] * 2 + [self.x0 + self.nx * PX / 2] * 2)
        ys = np.array([self.y1, self.y1, self.y1 - self.ny * PX, self.y1 - self.ny * PX, self.y1, self.y1 - self.ny * PX])
        lon, lat = TO_LL.transform(xs, ys)
        return float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())

    def cells(self, x, y):
        """(row, col) of LAEA points, clipped to the grid."""
        col = np.clip(((np.asarray(x) - self.x0) / PX).astype(int), 0, self.nx - 1)
        row = np.clip(((self.y1 - np.asarray(y)) / PX).astype(int), 0, self.ny - 1)
        return row, col


# ---------------------------------------------------------------- sources

@functools.lru_cache(maxsize=16)
def _osm(tile_id: str):
    t = Tile.parse(tile_id)
    return osm_extract.read_roads(t, whole=False), osm_extract.read_tile(t, whole=False)


def _tiles(bounds):
    w, s, e, n = bounds
    for lat in range(math.floor(s / STEP), math.floor(n / STEP) + 1):
        for lon in range(math.floor(w / STEP), math.floor(e / STEP) + 1):
            yield Tile(lat * STEP, lon * STEP).id


def _laea(coords):
    a = np.asarray(coords, dtype=float)
    x, y = TO_LAEA.transform(a[:, 0], a[:, 1])
    return list(zip(x.tolist(), y.tolist()))


def osm_layers(win: Window) -> dict[str, np.ndarray]:
    """Road metres per cell by class, and the tallest building per cell (0 = none)."""
    roads, blds, seen_r, seen_b = [], [], set(), set()
    # What matters is the land this window sees, not the tiles it borrows
    # from: a station in Trieste is covered though its tile runs into Slovenia.
    if not osm_extract.covered_box(win.lonlat_bounds()):
        raise MissingData(f"the window {tuple(round(v, 3) for v in win.lonlat_bounds())} runs outside the indexed extracts")
    for tid in _tiles(win.lonlat_bounds()):
        r, b = _osm(tid)
        if r is None or b is None:
            raise MissingData("the extracts were indexed without roads")
        roads += [e for e in r if e["id"] not in seen_r and not seen_r.add(e["id"])]
        blds += [e for e in b if e["id"] not in seen_b and not seen_b.add(e["id"])]
    shape = (win.ny, win.nx)
    out = {}
    for cls in ROAD_CLASSES:
        shapes = [({"type": "LineString", "coordinates": _laea(e["coordinates"])}, 1) for e in roads if e["c"] == cls]
        out[f"road_{cls}"] = (rasterize(shapes, out_shape=shape, transform=win.transform, all_touched=True,
                                        dtype="uint8") * PX if shapes else np.zeros(shape)).astype(np.float32)
    shapes = []
    for e in blds:
        if e.get("type") == "polygon":
            rings = [_laea(r) for r in e["coordinates"]]
        else:  # an Overpass way, should one ever reach here
            continue
        shapes.append(({"type": "Polygon", "coordinates": rings}, building_height(e.get("tags") or {})[0]))
    shapes.sort(key=lambda s: s[1])  # burned in order: the tallest wins a shared cell
    out["bld_h"] = (rasterize(shapes, out_shape=shape, transform=win.transform, dtype="float32")
                    if shapes else np.zeros(shape, np.float32))
    return out


def _patiently(fn, *args, waits=(10, 30, 90, 270, 600)):
    """Call fn, waiting out a DNS or connection failure; this network has them."""
    for wait in (*waits, None):
        try:
            return fn(*args)
        except MissingData:
            raise
        except Exception as e:
            text = str(e)
            if wait is None or not any(h in text for h in ("resolve", "Resolution", "Connection", "timed out", "CURL", "403")):
                raise
            log.warning("network trouble (%s); again in %d s", text[:100], wait)
            time.sleep(wait)


@functools.lru_cache(maxsize=1)
def _catalog():
    return pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1",
                                     modifier=planetary_computer.sign_inplace)


# Both collections are cut on a fixed grid of whole degrees, so the files a
# window needs follow from its bounds; each is fetched once into the cache
# and read locally after that, which on a network that loses its DNS every
# hour is the difference between a run that finishes and one that does not.
RASTERS = Path(__file__).resolve().parent / "cache" / "rasters"
GRIDS = {
    "esa-worldcover": (3, "map", lambda la, lo: f"ESA_WorldCover_10m_2021_v200_N{la:02d}E{lo:03d}_Map.tif"),
    "cop-dem-glo-30": (1, "data", lambda la, lo: f"Copernicus_DSM_COG_10_N{la:02d}_00_E{lo:03d}_00_DEM.tif"),
}


def _download(href: str, dest: Path) -> None:
    import requests
    tmp = dest.with_suffix(".part")
    with requests.get(href, stream=True, timeout=(30, 600)) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 22):
                f.write(chunk)
    tmp.rename(dest)


def _local(collection: str, la: int, lo: int) -> Path | None:
    """The cell's file on disk, fetched the first time; None where the
    collection has no file (open sea)."""
    step, asset, name = GRIDS[collection]
    path = RASTERS / collection / name(la, lo)
    none = path.with_suffix(".none")
    if path.exists():
        return path
    if none.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    c = (lo + step / 2, la + step / 2)
    items = _patiently(lambda: list(_catalog().search(
        collections=[collection], bbox=[c[0] - .01, c[1] - .01, c[0] + .01, c[1] + .01]).items()))
    match = [it for it in items if it.assets[asset].href.split("?")[0].endswith(name(la, lo))]
    if not match:
        none.touch()
        return None
    log.info("fetching %s", path.name)
    _patiently(_download, match[0].assets[asset].href, path)
    return path


def _mosaic(collection: str, win: Window, resampling, nodata) -> np.ndarray:
    step = GRIDS[collection][0]
    w, s, e, n = win.lonlat_bounds()
    out = np.full((win.ny, win.nx), np.nan, np.float32)
    for la in range(math.floor(s / step) * step, math.floor(n / step) * step + 1, step):
        for lo in range(math.floor(w / step) * step, math.floor(e / step) * step + 1, step):
            path = _local(collection, la, lo)
            if path is None:
                continue
            with rasterio.open(path) as src:
                with WarpedVRT(src, crs=CRS, transform=win.transform, width=win.nx, height=win.ny,
                               resampling=resampling, nodata=nodata) as vrt:
                    a = vrt.read(1).astype(np.float32)
            a[a == nodata] = np.nan
            out = np.where(np.isnan(out), a, out)
    return out


def worldcover(win: Window) -> np.ndarray:
    return _mosaic("esa-worldcover", win, Resampling.nearest, 0)


def dem(win: Window) -> np.ndarray:
    return _mosaic("cop-dem-glo-30", win, Resampling.bilinear, -32767)


# ---------------------------------------------------------------- predictors

@functools.lru_cache(maxsize=None)
def _disk(radius_m: float) -> np.ndarray:
    r = radius_m / PX
    n = int(math.ceil(r))
    yy, xx = np.mgrid[-n:n + 1, -n:n + 1]
    return ((xx ** 2 + yy ** 2) <= r * r).astype(np.float32)


def _within(a: np.ndarray, radius_m: float) -> np.ndarray:
    """Sum of a over a disk of the radius, at every cell (FFT round-off,
    which leaves -1e-12 where the answer is zero, clipped away)."""
    return np.clip(fftconvolve(a, _disk(radius_m), mode="same"), 0, None)


def features(win: Window, x, y) -> dict[str, np.ndarray]:
    """Every predictor at the LAEA points (x, y), which must lie at least
    PAD inside the window for the buffers to be whole."""
    row, col = win.cells(x, y)
    at = lambda a: np.asarray(a[row, col], dtype=np.float32)  # noqa: E731
    osm = osm_layers(win)
    f: dict[str, np.ndarray] = {}
    for cls in ROAD_CLASSES:
        for r in RADII:
            f[f"road_{cls}_{r}"] = at(_within(osm[f"road_{cls}"], r))
    major = osm["road_major"] > 0
    f["dist_major"] = at(distance_transform_edt(~major) * PX) if major.any() else np.full(len(row), 5000, np.float32)
    covered = (osm["bld_h"] > 0).astype(np.float32)
    for r in RADII:
        area = _disk(r).sum()
        f[f"bld_frac_{r}"] = at(_within(covered, r) / area)
        f[f"bld_vol_{r}"] = at(_within(osm["bld_h"] * PX * PX, r))
    wc = worldcover(win)
    for name, classes in WORLDCOVER.items():
        layer = np.isin(wc, classes).astype(np.float32)
        for r in COVER_RADII:
            f[f"{name}_{r}"] = at(_within(layer, r) / _disk(r).sum())
    z = dem(win)
    z_filled = np.where(np.isnan(z), np.nanmean(z) if np.isfinite(z).any() else 0, z)
    f["elev"] = at(z_filled)
    f["elev_rel_1000"] = at(z_filled - _within(z_filled, 1000) / _disk(1000).sum())
    return f


def features_at(lat: float, lon: float) -> dict[str, float]:
    """The predictors at one point, on a window just big enough for it."""
    x, y = TO_LAEA.transform(lon, lat)
    win = Window.around(x, y, x, y)
    return {k: float(v[0]) for k, v in features(win, np.array([x]), np.array([y])).items()}
