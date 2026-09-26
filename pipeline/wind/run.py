"""
Build the wind product for one tile::

    pipeline/.venv/bin/python pipeline/wind/run.py N41.75E12.25 [--fields]

Fetches the buildings, rasterises them, and writes ``data/tiles/<id>/wind/
heights.png`` plus the ``wind`` entry of ``meta.json``. That mask — 0.4 MB
— is the whole product: the flow itself is a linear problem on it, and the
app solves it in a worker (js/atmo/flow.worker.js) for the window around
the point and the wind of the moment. Shipping the sixteen solved fields
instead cost 75 MB a tile.

``--fields`` still solves the sixteen compass directions here and writes
``d00.png`` … ``d15.png`` and ``preview_d04.png``, which is how the browser
solver is checked against this one: same operator, same answer. The
velocity PNGs use red for u (east), green for v (north), each
``byte = 128 + 40 × value`` for a unit inflow, blue 255 inside a building.

The grid is 2784 cells a side — 10 m north–south at this latitude, 7.4 m
east–west — because the streets of the centre are 6–8 m wide and a coarser
grid closes them.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import buildings  # noqa: E402
import flow  # noqa: E402
from tiles import STEP, Tile, write_meta  # noqa: E402

log = logging.getLogger("wind.run")

GRID_N = 2784
ENCODE_OFFSET = 128
ENCODE_SCALE = 40.0


def encode(u: np.ndarray, v: np.ndarray, solid: np.ndarray) -> np.ndarray:
    rgb = np.zeros(u.shape + (3,), np.uint8)
    rgb[..., 0] = np.clip(np.round(ENCODE_OFFSET + u * ENCODE_SCALE), 0, 255)
    rgb[..., 1] = np.clip(np.round(ENCODE_OFFSET + v * ENCODE_SCALE), 0, 255)
    rgb[..., 2] = np.where(solid, 255, 0)
    return rgb


def preview(u: np.ndarray, v: np.ndarray, solid: np.ndarray) -> np.ndarray:
    """Speed as blue (calm) → pale yellow (undisturbed 1 m/s) → red (2 m/s
    and above), buildings black. For eyeballing, not for the app."""
    t = np.clip(np.hypot(u, v) / 2.0, 0.0, 1.0)
    rgb = np.zeros(u.shape + (3,), np.uint8)
    rgb[..., 0] = np.clip(255 * 2 * t, 0, 255)
    rgb[..., 1] = np.clip(255 * (1 - np.abs(2 * t - 1)), 0, 255)
    rgb[..., 2] = np.clip(255 * (1 - t), 0, 255)
    rgb[solid] = 0
    return rgb


def main(tile_id: str, fields: bool = False) -> None:
    tile = Tile.parse(tile_id)
    out = tile.path / "wind"
    t_start = time.time()

    heights, binfo = buildings.build(tile, GRID_N, out)
    solid = heights > flow.SLICE_HEIGHT_M
    grid = flow.Grid.for_tile(tile.bounds, GRID_N, GRID_N)

    directions = []
    solver = flow.Solver(solid, grid) if fields else None
    for k, name in enumerate(flow.DIRECTIONS if fields else []):
        u0, v0 = flow.inflow(k)
        u, v, iters = solver.solve(u0, v0)
        Image.fromarray(encode(u, v, solid), "RGB").save(out / f"d{k:02d}.png", optimize=True)
        if k == 4:
            Image.fromarray(preview(u, v, solid), "RGB").save(out / "preview_d04.png", optimize=True)
        speed = np.hypot(u, v)[~solid]
        directions.append({
            "index": k, "name": name, "from_degrees": k * 22.5, "inflow_uv": [round(u0, 6), round(v0, 6)],
            "file": f"d{k:02d}.png", "cg_iterations": iters,
            "street_speed_mean": round(float(speed.mean()), 4),
            "street_speed_max": round(float(speed.max()), 3),
        })

    runtime = time.time() - t_start
    info = {
        "bounds": list(tile.bounds),
        "rows": GRID_N, "cols": GRID_N,
        "degrees_per_pixel": STEP / GRID_N,
        "cell_metres": {"east_west": round(grid.dx, 3), "north_south": round(grid.dy, 3)},
        "row0": "north",
        "slice_height_m": flow.SLICE_HEIGHT_M,
        "screening_length_m": flow.SCREEN_LENGTH_M,
        "inflow_speed": 1.0,
        "solved_in": "the browser (js/atmo/flow.worker.js), on a window around the point, for the wind of the moment",
        "directions": directions,
        "field_encoding_when_written": {
            "red": "u east", "green": "v north",
            "blue": "255 inside a building, 0 elsewhere; this is the authoritative obstacle mask (heights.png > 5 agrees)",
            "offset": ENCODE_OFFSET, "scale": ENCODE_SCALE,
            "decode": "value = (byte - 128) / 40  [m/s per 1 m/s of inflow]",
            "range_m_per_s": [-ENCODE_OFFSET / ENCODE_SCALE, (255 - ENCODE_OFFSET) / ENCODE_SCALE],
        },
        "method": flow.METHOD,
        "solve_domain": "whole tile; the screening term makes the far field exactly the inflow",
        "edge_condition": "open (Neumann): wind on the tile edge is the undisturbed inflow",
        "runtime_seconds": round(runtime),
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "caveats": [
            "Qualitative street-scale approximation, not CFD: potential flow with a vertical-escape term.",
            "No wakes or recirculation; the field is fore-aft symmetric, so the lee of a building looks like its windward stagnation.",
            "Speeds are relative to the undisturbed street-level wind, not the 10 m station value; multiply by a local reference speed.",
            "Corner speed-ups are the potential-flow kind and overshoot; values are clamped at +/-3.2 m/s per 1 m/s inflow.",
            "Buildings shorter than the 5 m slice (one OSM level = 3.2 m) are not obstacles; unmapped buildings are open ground.",
            "Terrain, trees and walls are ignored.",
        ],
        **binfo,
    }
    write_meta(tile, "wind", info)
    log.info("done in %.0f s -> %s", runtime, out)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    main(sys.argv[1], fields="--fields" in sys.argv[2:])
