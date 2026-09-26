# Wind: street-level flow around the buildings of a tile

Prepares, for one quarter-degree tile, what the app needs to show a steady
wind threading between the real buildings: the building mask. The flow
itself is solved in the browser (`js/atmo/flow.worker.js`), on a window of a
few kilometres around the point, for the wind direction of the moment —
the same model as `flow.py` here, which stays as the reference and, with
`--fields`, still writes the sixteen solved compass directions to check the
browser against. Shipping those fields was 75 MB a tile; the mask is 0.4.

## Method

A mass-consistent diagnostic model (Sherman 1978 / MATHEW family) reduced
to the pedestrian layer. Start from a uniform 1 m/s wind and find the
smallest potential correction that makes it respect the buildings:

    u = u0 + ∇φ,   u·n = 0 on walls,   ∇²φ − φ/L² = 0 in the streets.

The `φ/L²` term is the honest part: it lets a street cell exchange air with
the layer above the roofs at a cost, so a dense quarter does not become a
set of nozzles, as it would in pure 2D potential flow. `L` (60 m, a few
building heights) is how far a building's influence reaches horizontally
before the air has gone over the top instead. This gives channelling along
streets aligned with the wind, shelter in the ones across it, slowing in
front of walls, acceleration at corners and gaps, calm in small courtyards.
It gives no wakes or recirculation — the field is fore–aft symmetric — and
nothing in it is a number to quote. It is a qualitative picture, not CFD.
The physics and its limits are spelled out in `flow.py`'s docstring.

Numerically: cell-centred finite volumes on the tile's lat/lon grid (cells
are 10 m tall and 7.4 m wide at Rome's latitude), obstacles = cells whose
building is taller than the 5 m slice, open (Neumann) tile edges. One
symmetric positive-definite operator serves all sixteen directions — only
the right-hand side changes — solved by Jacobi-preconditioned conjugate
gradient. Two analytic checks were run against it (a closed channel, whose
exact solution is a cosh profile, agrees to 3e-3; a lone block stagnates
fore and aft, speeds up 14 % along its flanks and 44 % at its corners,
mirror-symmetrically, and is exactly zero inside).

Buildings come from OpenStreetMap via Overpass, one query for the tile
bbox, cached under `pipeline/cache/`. Height: `height` tag, else
`building:levels` × 3.2 m, else 10 m (`buildings.py` says why). A cell is
a building when more than half of it is covered.

## Files (`data/tiles/<id>/wind/`)

- `heights.png` — the product: building height in whole metres (rounded
  up), one 8-bit channel, 2784 × 2784, row 0 north. Cells above the 5 m
  slice are obstacles.
- `../meta.json` → `products.wind` — bounds, grid, slice and screening
  lengths, building counts and height rule, caveats.

With `--fields` (validation only, not committed):

- `d00.png` … `d15.png` — wind from N, NNE, NE … NNW (clockwise, 22.5°).
  RGB, 2784 × 2784, row 0 north. Red = u (east), green = v (north), each
  `byte = round(128 + 40 × value)` for a 1 m/s inflow, so
  `value = (byte − 128) / 40` and ±3.2 m/s fits. Blue = 255 inside a
  building (u = v = 0 there), 0 elsewhere.
- `heights.png` — building height in whole metres, one 8-bit channel.
- `preview_d04.png` — east wind, speed as blue (calm) → pale yellow
  (undisturbed) → red (2× and above), buildings black. For eyeballing.
- `../meta.json` → `products.wind` — bounds, grid, encoding, directions
  with per-direction statistics, method, building counts, caveats.

## How to run

    pipeline/.venv/bin/python pipeline/wind/run.py N41.75E12.25            # mask only, ~20 s
    pipeline/.venv/bin/python pipeline/wind/run.py N41.75E12.25 --fields   # plus the 16 fields, ~4 min

Dependencies are the venv's numpy, scipy, pillow, requests, rasterio;
nothing else was added.

## Runtime observed (Rome, N41.75E12.25, M1 Pro, 16 GB)

About 3.5 minutes end to end, once the Overpass answer is cached (the
first fetch took 13 s for 76 MB): 3 s to parse and rasterise 98 138
footprints, 1 s to assemble the 7.75 M-cell operator, then ~10 s of
conjugate gradient (about 133 iterations) plus ~3 s of PNG encoding per
direction. Peak memory is around 3 GB. The whole tile is solved — no
built-up bounding box — since the screening makes the far field exact.

Output size: 16 × 4.4–4.5 MB for the direction PNGs, 0.4 MB heights,
7 MB preview: 75 MB per tile. The model is linear, so the field for a wind
from S is the exact negative of the one from N (`d08 = −d00`, and so on);
an app could ship eight PNGs and flip the sign to halve that.

## Limitations

- No wakes, no vortices, no turbulence, no dependence on wind speed
  (linear model: the field for 5 m/s is five times the field for 1 m/s).
- Speeds are relative to the undisturbed street-level wind, not the 10 m
  station value the forecast quotes; the app should pick a reference.
- Potential-flow corner speed-ups overshoot; clamped at 3.2× on encoding.
- Buildings shorter than 5 m (one OSM level = 3.2 m) do not block; areas
  where OSM has no buildings are open ground. Terrain, trees and walls are
  ignored. Heights are mostly the 10 m default (see meta for the counts).
- The 16 fields are 2784² PNGs; see meta and the sizes below for the
  download cost per tile.
