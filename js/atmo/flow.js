/**
 * The street-level flow field, kept solved for the neighbourhood of the pin.
 *
 * Owns the worker (flow.worker.js), decides when a fresh solve is worth it
 * — the pin has left the middle of the window, or the wind has swung more
 * than a few degrees — and answers `sample(lat, lon)` with the flow for a
 * unit inflow, or null outside the window. The particles multiply that by
 * the regional wind speed.
 *
 * The window is WINDOW cells a side on the tile's grid: about 5 km × 3.8 km
 * at Rome's 7.4 × 10 m cells, a quarter of a million unknowns, which the
 * worker solves in well under a second. Its edges carry the undisturbed
 * inflow, which is exact once you are a screening length or two from the
 * last building; the window is re-centred long before the pin gets near.
 */

const WINDOW = 512;
const SCREEN_LENGTH_M = 60;
const EARTH_M_PER_DEG = 111320;
/** Degrees of wind swing that earn a re-solve. */
const SWING_DEG = 6;

export function createFlow() {
  let worker = null;
  let mask = null;        // { bounds, rows, cols, solid: Uint8Array }
  let field = null;       // { r0, c0, rows, cols, u, v, from }
  let pending = null;
  let nextId = 0;

  function ensureWorker() {
    if (worker) return worker;
    worker = new Worker(new URL('./flow.worker.js', import.meta.url), { type: 'module' });
    worker.onmessage = e => {
      const { id, u, v, iterations, ms } = e.data;
      if (!pending || pending.id !== id) return;
      field = { ...pending, u, v, iterations, ms };
      pending = null;
    };
    return worker;
  }

  /** Tile pixel of a lat/lon on the mask's grid (row 0 north). */
  const toCell = (lat, lon) => {
    const [west, south, east, north] = mask.bounds;
    return {
      r: ((north - lat) / (north - south)) * mask.rows,
      c: ((lon - west) / (east - west)) * mask.cols,
    };
  };

  /** Solve a window centred on (lat, lon) for a wind *from* `fromDeg`. */
  function solveAround(lat, lon, fromDeg) {
    if (!mask) return;
    const { r, c } = toCell(lat, lon);
    const half = WINDOW / 2;
    const r0 = Math.max(0, Math.min(mask.rows - WINDOW, Math.round(r) - half));
    const c0 = Math.max(0, Math.min(mask.cols - WINDOW, Math.round(c) - half));
    const rows = Math.min(WINDOW, mask.rows);
    const cols = Math.min(WINDOW, mask.cols);

    const solid = new Uint8Array(rows * cols);
    for (let i = 0; i < rows; i++) {
      solid.set(mask.solid.subarray((r0 + i) * mask.cols + c0, (r0 + i) * mask.cols + c0 + cols), i * cols);
    }
    const [west, south, east, north] = mask.bounds;
    const latMid = ((south + north) / 2) * Math.PI / 180;
    const dy = ((north - south) / mask.rows) * EARTH_M_PER_DEG;
    const dx = ((east - west) / mask.cols) * EARTH_M_PER_DEG * Math.cos(latMid);
    const theta = fromDeg * Math.PI / 180;
    const u0 = -Math.sin(theta);
    const v0 = -Math.cos(theta);

    const id = ++nextId;
    pending = { id, r0, c0, rows, cols, from: fromDeg, centre: { lat, lon } };
    ensureWorker().postMessage(
      { id, rows, cols, dx, dy, L: SCREEN_LENGTH_M, u0, v0, solid },
      [solid.buffer],
    );
  }

  /** Is the current field still good for this place and wind? */
  function fresh(lat, lon, fromDeg) {
    const f = field || pending;
    if (!f || !mask) return false;
    const swing = Math.abs(((fromDeg - f.from + 540) % 360) - 180);
    if (swing > SWING_DEG) return false;
    const { r, c } = toCell(lat, lon);
    const quarter = WINDOW / 4;
    return Math.abs(r - (f.r0 + f.rows / 2)) < quarter && Math.abs(c - (f.c0 + f.cols / 2)) < quarter;
  }

  return {
    /** Hand over a tile's mask product, or null to forget it. */
    setMask(m) {
      mask = m ? { bounds: m.bounds, rows: m.rows, cols: m.cols, solid: m.solid } : null;
      field = null;
      pending = null;
    },
    get hasMask() { return !!mask; },
    get ready() { return !!field; },
    /** Make sure a field exists for here and this wind; solves if not. */
    ensure(lat, lon, fromDeg) {
      if (!mask || fresh(lat, lon, fromDeg)) return;
      solveAround(lat, lon, fromDeg);
    },
    /**
     * Unit-inflow flow at a point: { u, v, solid } or null outside the
     * window. Bilinear between cell centres, so a particle gliding along a
     * wall sees the wall's zero coming.
     */
    sample(lat, lon) {
      if (!field) return null;
      const { r, c } = toCell(lat, lon);
      const fr = r - field.r0 - 0.5;
      const fc = c - field.c0 - 0.5;
      if (fr < 0 || fc < 0 || fr > field.rows - 1 || fc > field.cols - 1) return null;
      const i0 = Math.min(Math.floor(fr), field.rows - 2);
      const j0 = Math.min(Math.floor(fc), field.cols - 2);
      const wi = fr - i0;
      const wj = fc - j0;
      const k = i0 * field.cols + j0;
      const lerp = a => (a[k] * (1 - wj) + a[k + 1] * wj) * (1 - wi) + (a[k + field.cols] * (1 - wj) + a[k + field.cols + 1] * wj) * wi;
      const rr = Math.min(Math.round(r) - field.r0, field.rows - 1);
      const cc = Math.min(Math.round(c) - field.c0, field.cols - 1);
      const solid = !!mask.solid[(field.r0 + Math.max(0, rr)) * mask.cols + field.c0 + Math.max(0, cc)];
      return { u: lerp(field.u), v: lerp(field.v), solid };
    },
  };
}
