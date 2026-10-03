"""
Pack heat products written a month to a file into one image per level.

Older tiles carry twelve PNGs per level (``m01.png`` … ``m12.o9.png``,
36 files); heat_landsat.py now writes three (``months.png``,
``months.o3.png``, ``months.o9.png``), the months stacked north to south.
This converts the old ones in place by stacking the bytes already there —
nothing is decoded and re-quantised — and computes any missing overview
from the 90 m month. The old files are removed and the product restamped so
the app's cache takes the new ones.

    pipeline/.venv/bin/python pipeline/pack_heat.py            # every tile not yet packed
    pipeline/.venv/bin/python pipeline/pack_heat.py N41.75E12.25
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import numpy as np
from PIL import Image

from heat_landsat import ENCODING, OVERVIEWS, PACKING, T_MIN, T_STEP, downsample, encode, stack_months
from tiles import DATA_DIR, Tile, read_meta, write_meta


def pack(tile: Tile) -> bool:
    info = read_meta(tile).get("products", {}).get("heat")
    if not info or info.get("packing") or info.get("encoding", {}).get("version") != ENCODING:
        return False
    old = dict(info["files"])
    base = {int(name[1:3]): np.array(Image.open(tile.path / f)) for name, f in old.items() if "." not in name}
    if not base:
        return False
    files = {}
    for k in (1, *OVERVIEWS):
        level = {}
        for m, b in base.items():
            name = f"m{m:02d}" if k == 1 else f"m{m:02d}.o{k}"
            if name in old:
                level[m] = np.array(Image.open(tile.path / old[name]))
            else:  # an overview the tile never had: from the 90 m month
                t = np.where(b > 0, T_MIN + (b.astype(np.float32) - 1) * T_STEP, np.nan)
                level[m] = np.asarray(encode(downsample(t, k)))
        r, c = next(iter(level.values())).shape
        out = "months" if k == 1 else f"months.o{k}"
        Image.fromarray(stack_months(level, r, c), "L").save(tile.path / "heat" / f"{out}.png", optimize=True)
        files[out] = f"heat/{out}.png"
    for f in old.values():
        (tile.path / f).unlink(missing_ok=True)
    info.update(files=files, packing=PACKING, overviews=list(OVERVIEWS),
                generated=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    write_meta(tile, "heat", info)
    return True


if __name__ == "__main__":
    ids = sys.argv[1:] or [p.name for p in sorted(DATA_DIR.glob("N*E*")) if (p / "heat").is_dir()]
    print(sum(pack(Tile.parse(i)) for i in ids), "tile(s) packed")
