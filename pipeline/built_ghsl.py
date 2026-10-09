"""
Where a place was built, and when: the share of the ground covered by
buildings in each five-year epoch from 1975 to 2020, at 3″ (about 90 m).

What the number is: the built-up surface of a cell — the footprint of its
buildings, in m² — over the cell's area, for the epochs 1975, 1980 … 2020,
from the Global Human Settlement Layer (GHS-BUILT-S R2023A, European
Commission JRC, CC BY 4.0). The epochs are modelled by the JRC from Landsat
(1975–2010) and Sentinel-2 (2018) with a consistent method, so they are
comparable with one another; a single 90 m cell in a single epoch is not a
survey, and the earliest epochs are the least certain. The 2025 and 2030
epochs are projections and are left out.

The app draws the difference between now (2020) and the year on the slider:
what was built after that year, over the 3D city of today.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger("built")

EPOCHS = list(range(1975, 2021, 5))
PER_DEG = 1200                        # 3″
URL = ("https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_BUILT_S_GLOBE_R2023A/"
       "GHS_BUILT_S_E{e}_GLOBE_R2023A_4326_3ss/V1-0/tiles/{name}.zip")
# The first epoch as a share, each later one as its change from the one
# before (most cells do not change, and a PNG of zeros is small): 0.01 a step.
ENCODING = {"value_at_byte1": 0.0, "step": 0.01, "unit": "share of the cell built", "nodata_byte": 0,
            "deltas": True, "delta_zero_byte": 128,
            "note": "first epoch: share = (byte − 1) × 0.01; each later epoch: the one before + (byte − 128) × 0.01"}
OVERVIEWS = (3, 9)

_lock = threading.Lock()


# The JRC's 10° tiles are not on round degrees: R1 starts just under 89.1°N,
# C1 a hair west of 180°W (from the tiles' own georeferencing).
TOP, LEFT = 89.09958333862926, -180.00791668384880


def cell_of(lat: float, lon: float) -> tuple[int, int]:
    """The GHSL 10° tile (row, column) holding a point."""
    return int((TOP - lat) // 10) + 1, int((lon - LEFT) // 10) + 1


def cells_for(bounds) -> list[tuple[int, int]]:
    w, s, e, n = bounds
    rows = range(cell_of(n - 1e-9, w)[0], cell_of(s + 1e-9, w)[0] + 1)
    cols = range(cell_of(s, w + 1e-9)[1], cell_of(s, e - 1e-9)[1] + 1)
    return [(r, c) for r in rows for c in cols]


def _name(epoch: int, r: int, c: int) -> str:
    return f"GHS_BUILT_S_E{epoch}_GLOBE_R2023A_4326_3ss_V1_0_R{r}_C{c}"


def tif(epoch: int, r: int, c: int, cache: Path) -> Path | None:
    """One epoch's 10° tile, downloaded once and unzipped; None where the
    JRC has none (open ocean)."""
    import requests
    from sg.errors import Transient
    name = _name(epoch, r, c)
    out = cache / f"{name}.tif"
    none = cache / f"{name}.none"
    with _lock:
        if out.exists():
            return out
        if none.exists():
            return None
        cache.mkdir(parents=True, exist_ok=True)
        part = cache / f"{name}.zip.part"
        try:
            with requests.get(URL.format(e=epoch, name=name), stream=True, timeout=300) as resp:
                if resp.status_code == 404:
                    none.touch()
                    return None
                resp.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in resp.iter_content(1 << 20):
                        f.write(chunk)
            with zipfile.ZipFile(part) as z:
                member = next(m for m in z.namelist() if m.endswith(".tif"))
                with z.open(member) as src, open(out.with_suffix(".tif.part"), "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
            os.replace(out.with_suffix(".tif.part"), out)
        except (requests.RequestException, OSError, zipfile.BadZipFile) as e:
            raise Transient(f"GHSL {name}: {e}") from e
        finally:
            part.unlink(missing_ok=True)
        log.info("downloaded %s (%.0f MB)", out.name, out.stat().st_size / 1e6)
        return out


def surface(epoch: int, bounds, cache: Path) -> np.ndarray:
    """Built-up surface (m²) per 3″ cell over bounds, which lie on the 3″ grid."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT
    w, s, e, n = bounds
    rows, cols = round((n - s) * PER_DEG), round((e - w) * PER_DEG)
    out = np.zeros((rows, cols), np.float32)
    for r, c in cells_for(bounds):
        path = tif(epoch, r, c, cache)
        if path is None:
            continue
        # The JRC grid sits a fraction of a cell off the round degrees: warp
        # onto ours (nearest — the cells are the same size). Sea and no data
        # are 0, outside a tile too, so the tiles combine by their maximum.
        with rasterio.open(path) as src, WarpedVRT(src, crs="EPSG:4326", transform=from_bounds(w, s, e, n, cols, rows),
                                                   width=cols, height=rows, resampling=Resampling.nearest) as vrt:
            out = np.maximum(out, vrt.read(1).astype(np.float32))
    return out


def shares(bounds, cache: Path) -> np.ndarray:
    """The built share of every cell for every epoch, (epochs, rows, cols)."""
    w, s, e, n = bounds
    rows = round((n - s) * PER_DEG)
    lat = n - (np.arange(rows) + 0.5) / PER_DEG
    area = (111_320 / PER_DEG) ** 2 * np.cos(np.radians(lat))[:, None]
    return np.stack([np.clip(surface(ep, bounds, cache) / area, 0, 1) for ep in EPOCHS]).astype(np.float32)


def _encode(f: np.ndarray) -> Image.Image:
    """(epochs, rows, cols) shares → one image, epochs stacked, as deltas."""
    q = np.round(np.clip(f, 0, 1) / ENCODING["step"]).astype(np.int16)
    d = np.concatenate([q[:1] + 1, np.diff(q, axis=0) + ENCODING["delta_zero_byte"]])
    return Image.fromarray(d.reshape(-1, f.shape[2]).astype(np.uint8), "L")


def decode(img: np.ndarray, epochs: int) -> np.ndarray:
    """The inverse of _encode: (epochs, rows, cols) shares."""
    d = img.astype(np.int16).reshape(epochs, -1, img.shape[1])
    q = np.concatenate([d[:1] - 1, d[1:] - ENCODING["delta_zero_byte"]]).cumsum(axis=0)
    return q.astype(np.float32) * ENCODING["step"]


def _overview(f: np.ndarray, k: int) -> np.ndarray:
    e, rows, cols = f.shape
    r, c = rows // k * k, cols // k * k
    return f[:, :r, :c].reshape(e, r // k, k, c // k, k).mean(axis=(2, 4))


def build(tile, out_dir: Path, cache: Path) -> dict:
    """built.png — every epoch's built share, stacked top to bottom, oldest
    first — and the meta entry."""
    f = shares(tile.bounds, cache)
    rows, cols = f.shape[1:]
    out_dir.mkdir(parents=True, exist_ok=True)
    _encode(f).save(out_dir / "built.png", optimize=True)
    files = {"built": "built/built.png"}
    for k in OVERVIEWS:
        _encode(_overview(f, k)).save(out_dir / f"built.o{k}.png", optimize=True)
        files[f"built.o{k}"] = f"built/built.o{k}.png"
    built = {str(ep): round(float(a.mean()), 4) for ep, a in zip(EPOCHS, f)}
    return {
        "product": "Built-up share of the ground by epoch, 1975–2020 (GHS-BUILT-S R2023A)",
        "years": EPOCHS, "stack": "years", "rows": int(rows), "cols": int(cols), "arcsec_per_pixel": 3,
        "encoding": ENCODING, "files": files, "overviews": list(OVERVIEWS),
        "built_share_by_year": built,
        "grown_since_1975": round(built["2020"] - built["1975"], 4),
        "source": "GHS-BUILT-S R2023A, European Commission, Joint Research Centre (JRC)",
        "licence": "CC BY 4.0 (© European Union)",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caveat": "Modelled from Landsat and Sentinel-2 by the JRC: comparable between epochs, but a 90 m cell in a "
                  "single epoch is an estimate, and the 1970s and 1980s the least certain.",
    }
