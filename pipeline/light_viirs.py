"""
How dark the night sky is, from the year's night lights, at 15″ (about 460 m).

What the number is: the brightness of the sky overhead on a clear, moonless
night, in magnitudes per square arcsecond — the unit of a Sky Quality Meter,
where a pristine sky reads 22.0 and the centre of a big city 17 or less. It
is the natural sky plus the glow that a region's lights scatter back down,
which reaches tens of kilometres from where the lights are: a village under
a dark sky and a village in a big city's glow differ by more than their own
streetlights.

The lights: NASA's Black Marble, VNP46A4 v2 — VIIRS day/night band radiance,
one composite per year, moonlight and the angle of view taken out, snow-free
nights (LAADS DAAC, Earthdata login).

The glow: the artificial brightness at a point is the year's radiance around
it seen through a kernel of distance,

    B(x) = a · Σ R(y) · d(x, y)^−b · e^(−d/c) · ΔA(y),

fitted on the New World Atlas of Artificial Night Sky Brightness (Falchi et
al. 2016, Science Advances 2:e1600377) with the 2014 Black Marble that atlas
was made from: b 1.59, c 30 km. Over Italy, where it was fitted, the model
and the atlas agree within a factor of 1.5 on 97 cells in 100; over the
Balkans, never fitted, on 94. The atlas itself is used for that fit only and
is not redistributed (its licence forbids it).

What it does not know: VIIRS is blind to blue light, so a town that went
from sodium to white LEDs looks less bright to it than it is to the eye and
the sky; and its glow comes from a fitted kernel, not from a model of the
atmosphere and the terrain (a mountain top sees less of the glow below it).
"""

from __future__ import annotations

import logging
import math
import os
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger("light")

COLLECTION = "VNP46A4"
DATASET = "HDFEOS/GRIDS/VIIRS_Grid_DNB_2d/Data Fields/AllAngle_Composite_Snow_Free"
NATIVE = 240                 # cells per degree in Black Marble (15″)
FIT = 120                    # cells per degree the kernel was fitted at (30″)
KERNEL = {"b": 1.59, "c_km": 30.2, "log10_a": -2.4316, "self_km": 0.35, "reach_km": 200.0}
VALIDATION = {
    "reference": "Falchi et al. 2016, New World Atlas of Artificial Night Sky Brightness (2014 VIIRS)",
    "italy_fitted": {"cells": 1447635, "within_x1.5": 0.966, "within_x2": 0.997, "rms_dex": 0.079},
    "balkans_not_fitted": {"cells": 691200, "within_x1.5": 0.938, "within_x2": 0.996, "rms_dex": 0.090},
}
NATURAL_MCD = 0.174          # the natural zenith sky, mcd/m² (22.0 mag/arcsec²)
NATURAL_MAG = 22.0
ENCODING = {"value_at_byte1": 22.0, "step": -0.025, "unit": "mag/arcsec²", "nodata_byte": 0,
            "note": "zenith sky brightness, mag/arcsec² = 22.0 − (byte − 1) × 0.025; lower is brighter"}


def mag(artificial_mcd: np.ndarray) -> np.ndarray:
    """Artificial brightness (mcd/m²) → the whole sky's, in mag/arcsec²."""
    return NATURAL_MAG - 2.5 * np.log10((artificial_mcd + NATURAL_MCD) / NATURAL_MCD)


# ---------------------------------------------------------------- the lights

_session = None
_lock = threading.Lock()


def _get(url, **kw):
    import requests
    global _session
    if _session is None:
        import heat_ecostress
        _session = requests.Session()
        _session.headers["Authorization"] = "Bearer " + heat_ecostress.token()
    return _session.get(url, **kw)


def _cmr(params: dict) -> list:
    import requests
    from sg.errors import Transient
    try:
        r = requests.get("https://cmr.earthdata.nasa.gov/search/granules.json",
                         params={"short_name": COLLECTION, "version": "2", **params}, timeout=60)
        r.raise_for_status()
    except requests.RequestException as e:
        raise Transient(f"CMR: {e}") from e
    return r.json()["feed"]["entry"]


@lru_cache(maxsize=1)
def latest_year() -> int:
    """The last year with a composite (one over Europe, which has them all)."""
    from sg.errors import Upstream
    e = _cmr({"readable_granule_name": f"{COLLECTION}.A*.h18v04*", "options[readable_granule_name][pattern]": "true",
              "sort_key": "-start_date", "page_size": 1})
    if not e:
        raise Upstream(f"CMR lists no {COLLECTION} composite")
    return int(e[0]["time_start"][:4])


def _file(year: int, h: int, v: int, cache: Path) -> Path | None:
    """The year's composite for one 10° cell, downloaded once; None where
    there is none (open ocean)."""
    from sg.errors import Transient, Upstream
    out = cache / f"{COLLECTION}.{year}.h{h:02d}v{v:02d}.h5"
    none = out.with_suffix(".none")
    with _lock:
        if out.exists():
            return out
        if none.exists():
            return None
        e = _cmr({"readable_granule_name": f"{COLLECTION}.A{year}001.h{h:02d}v{v:02d}*",
                  "options[readable_granule_name][pattern]": "true", "page_size": 1})
        if not e:
            cache.mkdir(parents=True, exist_ok=True)
            none.touch()
            return None
        href = next((l["href"] for l in e[0]["links"] if l["href"].startswith("https") and l["href"].endswith(".h5")), None)
        if not href:
            raise Upstream(f"{COLLECTION} {year} h{h:02d}v{v:02d}: no https link in CMR")
        cache.mkdir(parents=True, exist_ok=True)
        part = out.with_suffix(".part")
        try:
            with _get(href, stream=True, timeout=600) as r:
                if r.status_code in (401, 403):
                    raise Upstream(f"Earthdata: HTTP {r.status_code} for {href.rsplit('/', 1)[-1]} (token expired or not authorised)")
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
        except OSError as e:
            part.unlink(missing_ok=True)
            raise Transient(f"{href.rsplit('/', 1)[-1]}: {e}") from e
        os.replace(part, out)
        log.info("downloaded %s (%.0f MB)", out.name, out.stat().st_size / 1e6)
        return out


@lru_cache(maxsize=6)
def _cell(path: Path | None) -> np.ndarray:
    if path is None:
        return np.zeros((10 * NATIVE, 10 * NATIVE), np.float32)
    import h5py
    with h5py.File(path) as f:
        a = f[DATASET][:]
    return np.where(a > 0, a, 0).astype(np.float32)   # fill (−999.9) and noise below zero: dark


def cells_for(bounds) -> list[tuple[int, int]]:
    w, s, e, n = bounds
    return [(h, v) for h in range(math.floor((w + 180) / 10), math.floor((e + 180 - 1e-9) / 10) + 1)
            for v in range(math.floor((90 - n) / 10), math.floor((90 - s - 1e-9) / 10) + 1)]


def radiance(year: int, bounds, cache: Path) -> np.ndarray:
    """Radiance (nW/cm²/sr) over bounds, which lie on the 15″ grid."""
    w, s, e, n = bounds
    c0, r0 = round((w + 180) * NATIVE), round((90 - n) * NATIVE)
    out = np.zeros((round((n - s) * NATIVE), round((e - w) * NATIVE)), np.float32)
    for h, v in cells_for(bounds):
        a = _cell(_file(year, h, v, cache))
        hc0, hr0 = h * 10 * NATIVE, v * 10 * NATIVE                    # the cell's corner on the global grid
        rr0, rr1 = max(r0, hr0), min(r0 + out.shape[0], hr0 + 10 * NATIVE)
        cc0, cc1 = max(c0, hc0), min(c0 + out.shape[1], hc0 + 10 * NATIVE)
        if rr0 < rr1 and cc0 < cc1:
            out[rr0 - r0:rr1 - r0, cc0 - c0:cc1 - c0] = a[rr0 - hr0:rr1 - hr0, cc0 - hc0:cc1 - hc0]
    return out


# ---------------------------------------------------------------- the glow

def kernel(lat0: float, per_deg: int = FIT, k: dict = KERNEL) -> np.ndarray:
    dy = 111.32 / per_deg
    dx = dy * math.cos(math.radians(lat0))
    ny, nx = int(k["reach_km"] / dy), int(k["reach_km"] / dx)
    y, x = np.mgrid[-ny:ny + 1, -nx:nx + 1]
    d = np.maximum(np.hypot(x * dx, y * dy), k["self_km"])
    out = d ** -k["b"] * np.exp(-d / k["c_km"]) * dx * dy
    out[d > k["reach_km"]] = 0
    return (out * 10 ** k["log10_a"]).astype(np.float32)


def artificial(year: int, bounds, cache: Path) -> tuple[np.ndarray, np.ndarray]:
    """The artificial zenith brightness (mcd/m²) over bounds at 15″, and the
    radiance there. Computed at 30″, as fitted, on a window that reaches the
    kernel's full range, then interpolated to 15″."""
    from scipy import ndimage, signal
    w, s, e, n = bounds
    lat0 = (s + n) / 2
    my = KERNEL["reach_km"] / 111.32
    mx = my / math.cos(math.radians(lat0))
    snap = lambda x, f: math.floor(x * FIT) / FIT if f else math.ceil(x * FIT) / FIT
    win = (snap(w - mx, 1), snap(max(s - my, -89.9), 1), snap(e + mx, 0), snap(min(n + my, 89.9), 0))
    r = radiance(year, win, cache)
    r30 = r.reshape(r.shape[0] // 2, 2, r.shape[1] // 2, 2).mean((1, 3))
    glow = signal.fftconvolve(r30, kernel(lat0), mode="same")
    glow = np.clip(glow, 0, None)
    # 30″ cell centres → the 15″ cell centres of bounds
    rows = round((n - s) * NATIVE); cols = round((e - w) * NATIVE)
    yc = ((win[3] - n) * FIT - 0.5) + (np.arange(rows) + 0.5) / 2
    xc = ((w - win[0]) * FIT - 0.5) + (np.arange(cols) + 0.5) / 2
    yy, xx = np.meshgrid(yc, xc, indexing="ij")
    art = ndimage.map_coordinates(glow, [yy, xx], order=1, mode="nearest").astype(np.float32)
    r0, c0 = round((win[3] - n) * NATIVE), round((w - win[0]) * NATIVE)
    return art, r[r0:r0 + rows, c0:c0 + cols]


# ---------------------------------------------------------------- a tile

def _encode(m: np.ndarray) -> Image.Image:
    byte = np.clip(np.round((m - ENCODING["value_at_byte1"]) / ENCODING["step"]) + 1, 1, 255)
    return Image.fromarray(np.where(np.isfinite(m), byte, 0).astype(np.uint8), "L")


def build(tile, out_dir: Path, cache: Path, year: int | None = None) -> dict:
    """sky.png into out_dir; returns the meta entry."""
    year = year or latest_year()
    art, rad = artificial(year, tile.bounds, cache)
    m = mag(art)
    out_dir.mkdir(parents=True, exist_ok=True)
    _encode(m).save(out_dir / "sky.png", optimize=True)
    ratio = art / NATURAL_MCD
    return {
        "product": "Zenith night-sky brightness from VIIRS night lights (Black Marble) and a fitted glow kernel",
        "year": year, "rows": int(m.shape[0]), "cols": int(m.shape[1]), "arcsec_per_pixel": 3600 // NATIVE,
        "encoding": ENCODING, "files": {"sky": "light/sky.png"},
        "mag_min": round(float(m.min()), 2), "mag_median": round(float(np.median(m)), 2), "mag_max": round(float(m.max()), 2),
        "share_milky_way_hidden": round(float((m < 20.5).mean()), 3),
        "share_pristine": round(float((ratio < 0.01).mean()), 3),
        "radiance_median_nw": round(float(np.median(rad)), 2),
        "model": KERNEL, "validation": VALIDATION,
        "source": f"NASA Black Marble {COLLECTION} v2 ({year}), LAADS DAAC; glow kernel fitted on Falchi et al. 2016",
        "licence": "NASA Black Marble: public domain",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caveat": "VIIRS does not see blue light: white LEDs are undercounted. A fitted kernel, not an atmosphere: "
                  "terrain and altitude are not modelled.",
    }
