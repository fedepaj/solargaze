"""
Surface temperature at night, by month, from ECOSTRESS, at 70 m.

What the number is: the temperature of the surface — roof, asphalt, canopy,
river — that ECOSTRESS's thermal radiometer sees between 21:00 and 05:00
local solar time on clear nights, as delivered in its Level-2 tiled product
(ECO_L2T_LSTE v002: Kelvin on 70 m UTM tiles). For a tile, every night pass
since mid-2018 is read, cloud and unproduced pixels are dropped, and the
per-pixel *median* over all passes of each calendar month is kept, on the
same 90 m grid and in the same packing as the morning product
(heat_landsat.py), so the app reads both alike.

Why night: a city's surfaces give back at night the heat they took in by
day, and the gap between a dense block and a park is widest then — the
nights that do not cool are what an urban heat wave is. Landsat passes at
10:30 and never sees it. ECOSTRESS rides the International Space Station,
whose orbit drifts through the hours of the day, so it sees each place at
night too; it also only sees between about 52° south and 52° north.

What it is not: the air temperature, and not any particular night. The
passes fall at different hours of the night, so the median mixes late
evening and the hour before dawn.

Source: NASA LP DAAC (Earthdata Cloud), public domain. Reading needs a free
Earthdata login: EARTHDATA_TOKEN (a user token, 60 days), or
EARTHDATA_USERNAME and EARTHDATA_PASSWORD, from which a token is made.
Passes are found through NASA's CMR, which needs no login. Each file is read
by HTTP range requests made from Python, not by GDAL: only the window over
the tile crosses the wire, the redirect to the archive's CDN drops the token
as it should, and the DNS fallback (netdns.py) covers it.
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT

sys.path.insert(0, str(Path(__file__).resolve().parent))
import heat_landsat  # noqa: E402
from sg.errors import NoData, Transient, Upstream  # noqa: E402
from tiles import Tile  # noqa: E402

CMR = "https://cmr.earthdata.nasa.gov/search/granules.json"
COLLECTION = ("ECO_L2T_LSTE", "002")
START, END = "2018-07-01", "2025-12-31"
NIGHT_FROM, NIGHT_TO = 21, 5          # local solar hours, [21, 24) and [0, 5)
MAX_LAT = 52.0                         # the ISS's reach, rounded out
MIN_LOOKS = 3                          # clear nights per pixel and month, as for Landsat
# Nights are colder than mornings: the byte scale starts lower.
T_MIN, T_STEP = -30.0, 0.25
SUBDIR = "heat_night"
CACHE = Path(__file__).resolve().parent / "cache" / "ecostress"
WORKERS = int(os.environ.get("SG_SCENE_WORKERS", "8"))


# ---------------------------------------------------------------- credentials

_token_lock = threading.Lock()
_token: str | None = None


def token() -> str:
    """A bearer token for the archive: EARTHDATA_TOKEN as given, or one made
    from EARTHDATA_USERNAME and EARTHDATA_PASSWORD (which never expires on
    us: a new one is made when the old one lapses)."""
    global _token
    with _token_lock:
        if _token:
            return _token
        if os.environ.get("EARTHDATA_USERNAME") and os.environ.get("EARTHDATA_PASSWORD"):
            r = requests.post("https://urs.earthdata.nasa.gov/api/users/find_or_create_token",
                              auth=(os.environ["EARTHDATA_USERNAME"], os.environ["EARTHDATA_PASSWORD"]), timeout=60)
            if r.status_code == 401:
                raise Upstream("Earthdata refused the username and password")
            r.raise_for_status()
            _token = r.json()["access_token"]
        elif os.environ.get("EARTHDATA_TOKEN"):
            _token = os.environ["EARTHDATA_TOKEN"]
        else:
            raise Upstream("no Earthdata credentials: set EARTHDATA_TOKEN, or EARTHDATA_USERNAME and EARTHDATA_PASSWORD")
        return _token


_local = threading.local()


def _session() -> requests.Session:
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
        _local.s.headers["Authorization"] = f"Bearer {token()}"
    return _local.s


class RangeFile(io.RawIOBase):
    """A remote file read in 512 KB blocks by HTTP range requests. The first
    request follows the archive's redirect to its CDN; the rest go straight
    there, signed, without the token."""
    BLOCK = 1 << 19

    def __init__(self, url: str):
        r = _session().get(url, headers={"Range": "bytes=0-0"}, timeout=60)
        if r.status_code in (401, 403):
            raise Upstream(f"Earthdata: HTTP {r.status_code} for {url.rsplit('/', 1)[-1]} (token expired or not authorised)")
        r.raise_for_status()
        self.url = r.url
        self.size = int(r.headers["Content-Range"].rsplit("/", 1)[1])
        self.pos = 0
        self.blocks: dict[int, bytes] = {}

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def _block(self, i: int) -> bytes:
        if i not in self.blocks:
            a = i * self.BLOCK
            b = min(self.size, a + self.BLOCK) - 1
            r = requests.get(self.url, headers={"Range": f"bytes={a}-{b}"}, timeout=120)
            r.raise_for_status()
            self.blocks[i] = r.content
        return self.blocks[i]

    def readinto(self, buf) -> int:
        n = min(len(buf), self.size - self.pos)
        if n <= 0:
            return 0
        out = bytearray()
        while len(out) < n:
            i, off = divmod(self.pos + len(out), self.BLOCK)
            out += self._block(i)[off: off + n - len(out)]
        buf[:n] = out
        self.pos += n
        return n


def _opener(path: str, mode: str = "rb"):
    # rasterio probes an opener with a path of its own; only URLs are ours.
    if not path.startswith("http"):
        raise FileNotFoundError(path)
    return RangeFile(path)


# ---------------------------------------------------------------- passes

def solar_hour(utc: datetime, lon: float) -> float:
    return (utc.hour + utc.minute / 60 + lon / 15) % 24


def is_night(utc: datetime, lon: float) -> bool:
    h = solar_hour(utc, lon)
    return h >= NIGHT_FROM or h < NIGHT_TO


def passes(tile: Tile) -> list[dict]:
    """Night passes over the tile: one entry per pass (orbit and scene),
    with the urls of every 110 km granule of it that touches the tile —
    a tile on the edge of two granules is read from both."""
    w, s, e, n = tile.bounds
    lon = (w + e) / 2
    found: dict[str, dict] = {}
    page = 1
    while True:
        r = requests.get(CMR, timeout=120, params={
            "short_name": COLLECTION[0], "version": COLLECTION[1], "bounding_box": f"{w},{s},{e},{n}",
            "temporal": f"{START}T00:00:00Z,{END}T23:59:59Z", "page_size": 2000, "page_num": page})
        r.raise_for_status()
        entries = r.json()["feed"]["entry"]
        for g in entries:
            t = datetime.fromisoformat(g["time_start"].replace("Z", "+00:00"))
            if not is_night(t, lon):
                continue
            # ECOv002_L2T_LSTE_<orbit>_<scene>_<mgrs>_<time>_...: a pass is orbit and scene.
            parts = g["title"].split("_")
            key = f"{parts[3]}_{parts[4]}"
            urls = {}
            for link in g.get("links", []):
                href = link.get("href", "")
                if href.startswith("https") and href.endswith(".tif"):
                    layer = href.rsplit("_", 1)[-1].removesuffix(".tif")
                    if layer in ("LST", "cloud"):
                        urls[layer] = href
            if len(urls) == 2:
                found.setdefault(key, {"id": key, "time": t, "granules": []})["granules"].append(urls)
        if len(entries) < 2000:
            break
        page += 1
    return sorted(found.values(), key=lambda p: p["time"])


def _signed(url: str) -> str:
    """The archive's url → the CDN url it redirects to, signed for an hour:
    readable by GDAL's own HTTP reader without the token."""
    r = _session().get(url, headers={"Range": "bytes=0-0"}, timeout=60)
    if r.status_code in (401, 403):
        raise Upstream(f"Earthdata: HTTP {r.status_code} for {url.rsplit('/', 1)[-1]} (token expired or not authorised)")
    r.raise_for_status()
    return r.url


# GDAL reads the signed urls itself, which is safe from several threads; the
# Python opener (RangeFile) is kept for a machine whose system resolver drops
# names (SG_ECOSTRESS_READER=python), since only Python has the DNS fallback.
# Under eight threads on the runners it aborted the process in GDAL
# (std::length_error) every few tiles.
PYTHON_READER = os.environ.get("SG_ECOSTRESS_READER") == "python"
GDAL_ENV = {"CPL_VSIL_CURL_USE_HEAD": "NO", "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif", "GDAL_HTTP_MAX_RETRY": "4", "GDAL_HTTP_RETRY_DELAY": "2"}


def _read(url: str, tile: Tile, shape, resampling) -> np.ndarray:
    w, s, e, n = tile.bounds
    transform = from_bounds(w, s, e, n, shape[1], shape[0])
    if PYTHON_READER:
        ctx, path, kw = rasterio.Env(), url, {"opener": _opener}
    else:
        ctx, path, kw = rasterio.Env(**GDAL_ENV), "/vsicurl/" + _signed(url), {}
    with ctx, rasterio.open(path, **kw) as src:
        with WarpedVRT(src, crs="EPSG:4326", transform=transform, width=shape[1], height=shape[0],
                       resampling=resampling, src_nodata=src.nodata, nodata=np.nan if src.dtypes[0].startswith("float") else 255) as vrt:
            return vrt.read(1)


def read_pass(p: dict, tile: Tile, shape) -> np.ndarray:
    """Surface temperature in °C over the tile for one pass, NaN where it is
    clouded, not produced, or outside every granule of the pass. The QC
    layer is not read: of its flags, "cloud" is the cloud layer's and "not
    produced" leaves the temperature empty, so it would only cost a third
    more requests."""
    out = np.full(shape, np.nan, np.float32)
    for g in p["granules"]:
        lst = _read(g["LST"], tile, shape, Resampling.bilinear).astype(np.float32)
        cloud = _read(g["cloud"], tile, shape, Resampling.nearest)
        ok = np.isfinite(lst) & (lst > 150) & (cloud == 0)
        c = np.where(ok, lst - 273.15, np.nan)
        fill = np.isnan(out) & np.isfinite(c)
        out[fill] = c[fill]
    return out


def cached_pass(p: dict, tile: Tile, shape) -> np.ndarray | None:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{tile.id}_{p['id']}.npy"
    if path.exists():
        return np.load(path).astype(np.float32)
    try:
        arr = read_pass(p, tile, shape)
    except Upstream:
        raise
    except Exception as e:  # a pass that will not read is a pass we do without
        print(f"    skip {p['id']}: {e}", file=sys.stderr)
        return None
    np.save(path, arr.astype(np.float16))
    return arr


# ---------------------------------------------------------------- build

def build(tile: Tile, out_dir: Path) -> dict:
    """The packed night months into out_dir; returns the meta entry. NoData
    outside the ISS's reach or where no clear night was seen; Transient when
    too many passes would not read; Upstream without credentials."""
    w, s, e, n = tile.bounds
    if max(abs(s), abs(n)) > MAX_LAT:
        raise NoData(f"{tile.id}: beyond ECOSTRESS's reach (the ISS flies between about {MAX_LAT:.0f}° S and N)")
    token()   # no credentials: say so before searching
    full = tile.shape(heat_landsat.DEG_PER_PX)
    shape = (full[0] // heat_landsat.DOWNSAMPLE, full[1] // heat_landsat.DOWNSAMPLE)
    t0 = time.time()
    ps = passes(tile)
    print(f"{tile.id}: {len(ps)} night passes, {shape[0]}×{shape[1]} px at 90 m")
    if not ps:
        raise NoData(f"{tile.id}: no ECOSTRESS night pass")

    with ThreadPoolExecutor(WORKERS) as pool:
        arrays = list(pool.map(lambda p: cached_pass(p, tile, shape), ps))
    failed = sum(a is None for a in arrays)
    if failed > max(3, 0.05 * len(ps)):
        raise Transient(f"{tile.id}: {failed} of {len(ps)} night passes failed to read; tile not written")

    by_month: dict[int, list[np.ndarray]] = defaultdict(list)
    for p, arr in zip(ps, arrays):
        if arr is not None and np.isfinite(arr).mean() >= 0.05:
            by_month[p["time"].month].append(arr)

    out_dir.mkdir(parents=True, exist_ok=True)
    months, medians = {}, {}
    for m in sorted(by_month):
        cube = np.stack(by_month[m])
        with np.errstate(all="ignore"):
            median = np.nanmedian(cube, axis=0)
        median[np.isfinite(cube).sum(axis=0) < MIN_LOOKS] = np.nan
        if not np.isfinite(median).any():
            continue
        medians[m] = median
        months[f"{m:02d}"] = {
            "scenes": len(by_month[m]),
            "coverage": round(float(np.isfinite(median).mean()), 3),
            "tile_median_c": round(float(np.nanmedian(median)), 2),
        }
        print(f"  month {m:02d}: {len(by_month[m])} passes, coverage {months[f'{m:02d}']['coverage']:.0%}")
    if not medians:
        raise NoData(f"{tile.id}: no month with {MIN_LOOKS} clear nights")
    files = heat_landsat.write_packed(medians, out_dir, SUBDIR, T_MIN, T_STEP)
    for p in CACHE.glob(f"{tile.id}_*.npy"):
        p.unlink()

    print(f"done in {time.time() - t0:.0f}s")
    return {
        "product": "ECOSTRESS ECO_L2T_LSTE v002 land surface temperature at night, per-pixel median by calendar month",
        "years": f"{START}/{END}",
        "overpass_local_time": f"{NIGHT_FROM}:00–{NIGHT_TO:02d}:00 local solar time",
        "rows": shape[0], "cols": shape[1],
        "degrees_per_pixel": heat_landsat.DEG_PER_PX * heat_landsat.DOWNSAMPLE,
        "encoding": {"version": heat_landsat.ENCODING, "channel": "L", "nodata_byte": 0, "byte1_c": T_MIN,
                     "step_c": T_STEP, "note": f"70 m read onto 90 m; byte 0 = fewer than {MIN_LOOKS} clear nights"},
        "files": files,
        "packing": heat_landsat.PACKING,
        "overviews": list(heat_landsat.OVERVIEWS),
        "months": months,
        "passes": len(ps),
        "source": "NASA LP DAAC, ECOSTRESS ECO_L2T_LSTE v002 (Earthdata Cloud)",
        "licence": "NASA data, public domain",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caveat": "Surface temperature on clear nights at varying hours between 21:00 and 05:00, not air temperature; a median over several years, not any particular night.",
    }


def _main(argv: list[str]) -> int:
    """One tile, in a process of its own (sg/products/heat_night.py runs it
    so): the meta entry, or the error's class and message, go to --info as
    JSON; a crash in a native library takes only this process with it."""
    import argparse
    import json
    import publish_tiles
    import netdns  # noqa: F401
    from sg.errors import NotCovered, PipelineError, classify
    ap = argparse.ArgumentParser()
    ap.add_argument("tile")
    ap.add_argument("out")
    ap.add_argument("--info", help="write the meta entry, or the error, here as JSON")
    args = ap.parse_args(argv)
    publish_tiles.load_env()
    t = Tile.parse(args.tile)
    try:
        info = build(t, Path(args.out))
        result = {"info": info}
    except Exception as exc:  # noqa: BLE001 — classified here, where the type is known
        result = {"kind": classify(exc), "per_tile": isinstance(exc, NotCovered),
                  "msg": str(exc) if isinstance(exc, PipelineError) else f"{type(exc).__name__}: {exc}"}
    if args.info:
        Path(args.info).write_text(json.dumps(result))
    else:
        print(result.get("info") and {k: v for k, v in result["info"].items() if k != "months"} or result)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
