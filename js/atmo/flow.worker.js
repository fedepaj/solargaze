/**
 * Street-level wind around the buildings, solved here and now.
 *
 * The pipeline once shipped sixteen precomputed flow fields per tile —
 * 75 MB. They were the solution of a linear problem on a 0.4 MB mask, so
 * the mask is what ships and this worker does the solving, for a window of
 * a few kilometres around the point and for the exact wind direction of
 * the moment. Same model as pipeline/wind/flow.py, and kept in step with
 * it: a mass-consistent correction to a uniform inflow,
 *
 *     u = u0 + ∇φ,   u·n = 0 on walls,   ∇²φ − φ/L² = 0 in the streets,
 *
 * cell-centred finite volumes on the tile's lat/lon cells, solid cells as
 * identity rows, open edges, Jacobi-preconditioned conjugate gradient. The
 * φ/L² term lets air escape over the roofs, which is what stops a dense
 * quarter turning into nozzles. Qualitative, no wakes, not CFD — see the
 * Python docstring for the honest version.
 *
 * Message in:  { id, rows, cols, dx, dy, L, u0, v0, solid: Uint8Array }
 * Message out: { id, u: Float32Array, v: Float32Array, iterations }
 */

self.onmessage = e => {
  const { id, rows, cols, dx, dy, L, u0, v0, solid } = e.data;
  const t0 = performance.now();
  const n = rows * cols;
  const cx = dy / dx;
  const cy = dx / dy;
  const screen = (dx * dy) / (L * L);

  const fluid = new Uint8Array(n);
  for (let k = 0; k < n; k++) fluid[k] = solid[k] ? 0 : 1;

  // Conductance across each cell's east and south face: only between two
  // fluid cells. The last column and row have none — the open edge.
  const cE = new Float32Array(n);
  const cS = new Float32Array(n);
  const diag = new Float32Array(n);
  const b = new Float32Array(n);
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const k = r * cols + c;
      if (!fluid[k]) { diag[k] = 1; continue; }
      if (c + 1 < cols && fluid[k + 1]) cE[k] = cx;
      if (r + 1 < rows && fluid[k + cols]) cS[k] = cy;
    }
  }
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const k = r * cols + c;
      if (!fluid[k]) continue;
      const w = c > 0 ? cE[k - 1] : 0;
      const nn = r > 0 ? cS[k - cols] : 0;
      diag[k] = cE[k] + w + cS[k] + nn + screen;
      // Walls: a building to the east of a cell facing an east wind sinks
      // air, one to the west sources it; likewise north and south. Row 0 is
      // north, so "north" is r - 1.
      const wallE = c + 1 < cols && !fluid[k + 1];
      const wallW = c > 0 && !fluid[k - 1];
      const wallN = r > 0 && !fluid[k - cols];
      const wallS = r + 1 < rows && !fluid[k + cols];
      b[k] = (wallE ? -u0 * dy : 0) + (wallW ? u0 * dy : 0) + (wallN ? -v0 * dx : 0) + (wallS ? v0 * dx : 0);
    }
  }

  // A·x for the five-band stencil.
  const Ax = (x, out) => {
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const k = r * cols + c;
        let s = diag[k] * x[k];
        if (c + 1 < cols) s -= cE[k] * x[k + 1];
        if (c > 0) s -= cE[k - 1] * x[k - 1];
        if (r + 1 < rows) s -= cS[k] * x[k + cols];
        if (r > 0) s -= cS[k - cols] * x[k - cols];
        out[k] = s;
      }
    }
  };

  // Jacobi-preconditioned CG. The system is symmetric positive definite.
  const phi = new Float32Array(n);
  const rvec = new Float32Array(b);
  const z = new Float32Array(n);
  const p = new Float32Array(n);
  const q = new Float32Array(n);
  let rz = 0;
  let bnorm = 0;
  for (let k = 0; k < n; k++) {
    z[k] = rvec[k] / diag[k];
    p[k] = z[k];
    rz += rvec[k] * z[k];
    bnorm += b[k] * b[k];
  }
  const tol2 = 1e-12 * bnorm;
  let it = 0;
  const maxIt = 600;
  if (bnorm > 0) {
    for (; it < maxIt; it++) {
      Ax(p, q);
      let pq = 0;
      for (let k = 0; k < n; k++) pq += p[k] * q[k];
      const alpha = rz / pq;
      let rr = 0;
      for (let k = 0; k < n; k++) {
        phi[k] += alpha * p[k];
        rvec[k] -= alpha * q[k];
        rr += rvec[k] * rvec[k];
      }
      if (rr < tol2) { it++; break; }
      let rzNew = 0;
      for (let k = 0; k < n; k++) {
        z[k] = rvec[k] / diag[k];
        rzNew += rvec[k] * z[k];
      }
      const beta = rzNew / rz;
      rz = rzNew;
      for (let k = 0; k < n; k++) p[k] = z[k] + beta * p[k];
    }
  }

  // Face velocities, then the cell mean of its two faces. A wall face is
  // zero; the open edge carries the undisturbed inflow.
  const u = new Float32Array(n);
  const v = new Float32Array(n);
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const k = r * cols + c;
      if (!fluid[k]) continue;
      const uE = c + 1 < cols ? (fluid[k + 1] ? u0 + (phi[k + 1] - phi[k]) / dx : 0) : u0;
      const uW = c > 0 ? (fluid[k - 1] ? u0 + (phi[k] - phi[k - 1]) / dx : 0) : u0;
      const vN = r > 0 ? (fluid[k - cols] ? v0 + (phi[k - cols] - phi[k]) / dy : 0) : v0;
      const vS = r + 1 < rows ? (fluid[k + cols] ? v0 + (phi[k] - phi[k + cols]) / dy : 0) : v0;
      u[k] = 0.5 * (uE + uW);
      v[k] = 0.5 * (vN + vS);
    }
  }

  self.postMessage({ id, u, v, iterations: it, ms: performance.now() - t0 }, [u.buffer, v.buffer]);
};
