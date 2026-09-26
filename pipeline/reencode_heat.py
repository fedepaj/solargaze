"""
Re-encode heat products written in the first format (30 m, RGBA, alpha for
no-data) into the current one (90 m, one channel, byte 0 for no-data). The
data are the same medians; only the pixel and the bytes change — see the
note on DOWNSAMPLE in heat_landsat.py.

    pipeline/.venv/bin/python pipeline/reencode_heat.py            # every tile still in format 1
    pipeline/.venv/bin/python pipeline/reencode_heat.py N41.75E12.25
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

from heat_landsat import DOWNSAMPLE, ENCODING, T_MIN, T_STEP, downsample, encode
from tiles import DATA_DIR, Tile, read_meta, write_meta


def reencode(tile: Tile) -> bool:
    meta = read_meta(tile)
    info = meta.get("products", {}).get("heat")
    if not info or info.get("encoding", {}).get("version") == ENCODING:
        return False
    lo, hi = info["encoding"]["byte0_c"], info["encoding"]["byte255_c"]
    before = after = 0
    for name, file in info["files"].items():
        path = tile.path / file
        a = np.array(Image.open(path).convert("RGBA"))
        t = lo + a[..., 0].astype(np.float32) / 255 * (hi - lo)
        t[a[..., 3] == 0] = np.nan
        before += path.stat().st_size
        encode(downsample(t)).save(path, optimize=True)
        after += path.stat().st_size
    rows, cols = info["rows"] // DOWNSAMPLE, info["cols"] // DOWNSAMPLE
    # A new stamp: the app keys its cache on it, and these are new files.
    from datetime import datetime, timezone
    info["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    info.update({
        "rows": rows, "cols": cols,
        "degrees_per_pixel": info["degrees_per_pixel"] * DOWNSAMPLE,
        "native_degrees_per_pixel": info["degrees_per_pixel"],
        "encoding": {"version": ENCODING, "channel": "L", "nodata_byte": 0, "byte1_c": T_MIN, "step_c": T_STEP,
                     "note": "median at 30 m, then a 3 x 3 mean; byte 0 = fewer than 3 clear scenes"},
    })
    write_meta(tile, "heat", info)
    print(f"{tile.id}: {before / 1e6:.1f} MB -> {after / 1e6:.2f} MB")
    return True


if __name__ == "__main__":
    ids = sys.argv[1:] or [p.name for p in sorted(DATA_DIR.glob("N*E*")) if (p / "heat").is_dir()]
    n = sum(reencode(Tile.parse(i)) for i in ids)
    print(f"{n} tile(s) re-encoded")
