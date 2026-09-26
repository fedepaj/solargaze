"""
Steady street-level wind around the buildings of a tile, one field per
inflow direction.

What it is
----------
A mass-consistent diagnostic model in the family of Sherman (1978) and
MATHEW, collapsed to the pedestrian layer. Start from a uniform 1 m/s wind
``u0`` blowing from one of 16 compass directions, then find the smallest
correction ``∇φ`` that makes the flow respect the buildings::

    u = u0 + ∇φ            no flow through a building wall:  u · n = 0
    ∇²φ − φ / L² = 0       in the streets

The ``φ/L²`` term is the one piece that is not textbook 2D potential flow,
and it is what makes the picture believable in a dense city. In a pure 2D
model air can only get past a block by squeezing through the streets, so
Rome's centro storico would turn into a set of nozzles blowing at several
times the free-stream speed. In reality most of that air goes over the
roofs. The model lets it: a street-layer cell may exchange air with the
layer above at a cost, and ``L`` — the screening length, a few building
heights — is how far the buildings' influence reaches horizontally before
the vertical escape has taken over. The Lagrangian behind it is the usual
one, ``∫ (u − u0)² + β w²`` with continuity as the constraint, with
``L² = β h²`` for a layer of thickness ``h``.

What it gives, honestly
-----------------------
- channelling along streets aligned with the wind and quiet in the ones
  across it, with the transition over a scale ``L``;
- slowing in front of a wall, acceleration around a corner and through a
  gap, near-calm in small enclosed courtyards;
- no wakes, no recirculation, no turbulence: the solution is fore–aft
  symmetric, so the shelter behind a building looks like the stagnation in
  front of it. Speeds are relative to the undisturbed street-level wind,
  not to the 10 m weather-station value.

It is a qualitative street-scale approximation for showing wind moving
between buildings, not CFD, and no number from it should be quoted.

Numerics
--------
Cell-centred finite volumes on the tile's lat/lon grid — cells are ``dy``
tall and ``dx`` wide in metres, not square, and the conductances say so.
Cells whose building height exceeds the slice height are solid; the wall
condition enters the right-hand side, the tile edges are open (Neumann,
wind there is ``u0``). The operator is symmetric positive definite and
the same for every direction — only the right-hand side changes — so it
is assembled once and each direction is a Jacobi-preconditioned conjugate
gradient solve. Face velocities are exact on walls; the cell value the
app reads is the mean of its two faces.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

log = logging.getLogger("wind.flow")

METHOD = "mass-consistent 2D potential flow with vertical escape (screened Poisson), Sherman/MATHEW-style"
SLICE_HEIGHT_M = 5.0
SCREEN_LENGTH_M = 60.0
EARTH_M_PER_DEG = 111_320.0

DIRECTIONS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def inflow(k: int) -> tuple[float, float]:
    """(u east, v north) of a 1 m/s wind blowing FROM compass sector k."""
    theta = math.radians(k * 22.5)
    return (-math.sin(theta), -math.cos(theta))


@dataclass
class Grid:
    rows: int
    cols: int
    dx: float  # metres, east–west cell size
    dy: float  # metres, north–south cell size

    @staticmethod
    def for_tile(bounds, rows: int, cols: int) -> "Grid":
        w, s, e, n = bounds
        lat = math.radians((s + n) / 2)
        dy = (n - s) / rows * EARTH_M_PER_DEG
        dx = (e - w) / cols * EARTH_M_PER_DEG * math.cos(lat)
        return Grid(rows, cols, dx, dy)


class Solver:
    """Geometry-dependent state: the operator, its preconditioner, and the
    face masks that turn a direction into a right-hand side."""

    def __init__(self, solid: np.ndarray, grid: Grid, screen_length_m: float = SCREEN_LENGTH_M):
        t0 = time.time()
        self.grid = grid
        self.solid = solid.astype(bool)
        fluid = ~self.solid
        rows, cols = fluid.shape
        n = rows * cols
        cx, cy = grid.dy / grid.dx, grid.dx / grid.dy  # face length / distance
        area = grid.dx * grid.dy

        # Conductance across the east face of each cell and the south face
        # of each cell: nonzero only between two fluid cells. The last
        # column / row has no east / south face — that is the open edge.
        cE = np.zeros((rows, cols)); cE[:, :-1] = cx * (fluid[:, :-1] & fluid[:, 1:])
        cS = np.zeros((rows, cols)); cS[:-1, :] = cy * (fluid[:-1, :] & fluid[1:, :])
        cW = np.zeros_like(cE); cW[:, 1:] = cE[:, :-1]
        cN = np.zeros_like(cS); cN[1:, :] = cS[:-1, :]
        diag = cE + cW + cS + cN + area / screen_length_m ** 2
        # Solid cells stay in the system as identity rows with zero
        # right-hand side, which keeps the matrix a plain five-band stencil.
        diag[self.solid] = 1.0

        e = cE.ravel()[:-1]
        s = cS.ravel()[:-cols]
        A = sp.diags_array([diag.ravel(), -e, -e, -s, -s], offsets=[0, 1, -1, cols, -cols],
                           shape=(n, n), format="csr")
        self.A = A
        self.M = sp.diags_array(1.0 / diag.ravel())
        # Which walls each fluid cell touches, for the right-hand side.
        pad = np.pad(self.solid, 1, constant_values=False)
        self.wall_E = fluid & pad[1:-1, 2:]
        self.wall_W = fluid & pad[1:-1, :-2]
        self.wall_N = fluid & pad[:-2, 1:-1]
        self.wall_S = fluid & pad[2:, 1:-1]
        self.fluid = fluid
        log.info("operator %d x %d (%.1f M unknowns, %.1f%% solid) in %.1f s",
                 n, n, n / 1e6, 100 * self.solid.mean(), time.time() - t0)

    def solve(self, u0: float, v0: float, rtol: float = 1e-6) -> tuple[np.ndarray, np.ndarray, int]:
        """Cell-centred (u, v) in m/s for a unit inflow (u0, v0); zero inside buildings."""
        g = self.grid
        t0 = time.time()
        # b_i = −Σ over wall faces of (u0 · n_out) × face length: a wall to
        # the east of a cell facing an east wind sinks air, one to the west
        # sources it, and the sign flips likewise north/south.
        b = (-u0 * g.dy) * self.wall_E + (u0 * g.dy) * self.wall_W \
            + (-v0 * g.dx) * self.wall_N + (v0 * g.dx) * self.wall_S
        count = [0]

        def cb(_):
            count[0] += 1
        phi, code = spla.cg(self.A, b.ravel(), M=self.M, rtol=rtol, maxiter=2000, callback=cb)
        if code != 0:
            log.warning("cg did not converge: code %d after %d iterations", code, count[0])
        phi = phi.reshape(self.solid.shape)

        # Face velocities: u0 plus the potential gradient where both cells
        # are fluid, zero on a wall, u0 on the open tile edge.
        rows, cols = self.solid.shape
        fl = self.fluid
        uE = np.full((rows, cols), u0)
        both = fl[:, :-1] & fl[:, 1:]
        uE[:, :-1] = np.where(both, u0 + (phi[:, 1:] - phi[:, :-1]) / g.dx, 0.0)
        uW = np.full((rows, cols), u0)
        uW[:, 1:] = uE[:, :-1]
        # North face of row r sits between rows r−1 and r; north is −row.
        vN = np.full((rows, cols), v0)
        both = fl[:-1, :] & fl[1:, :]
        vN[1:, :] = np.where(both, v0 + (phi[:-1, :] - phi[1:, :]) / g.dy, 0.0)
        vS = np.full((rows, cols), v0)
        vS[:-1, :] = vN[1:, :]
        u = np.where(fl, 0.5 * (uE + uW), 0.0).astype(np.float32)
        v = np.where(fl, 0.5 * (vN + vS), 0.0).astype(np.float32)
        log.info("direction (%+.2f, %+.2f): %d cg iterations, %.1f s", u0, v0, count[0], time.time() - t0)
        return u, v, count[0]
