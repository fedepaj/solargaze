/**
 * The tiles of a view that are still on their way, as a picture: one cell a
 * tile, and a wave of light running outward from the pin over the cells that
 * have not landed. Pure — the pixels are written here, the canvas and the
 * clock are the drape's (createPending in drape.js).
 */

/** Pixels a side for one tile's cell; the last one is the gap between tiles. */
export const CELL = 8;
const PERIOD_MS = 1600;      // one crest to the next, at a given tile
const WAVELENGTH = 5;        // tiles between two crests
const FLOOR = 0.07;          // a waiting tile between crests
const CREST = 0.3;           // and on one

/**
 * The cells of a rectangle of tiles (west, south, east, north in degrees,
 * snapped to tile edges): how many, and which cell a tile's bounds are.
 */
export function pendingGrid([west, south, east, north], step) {
  const cols = Math.max(1, Math.round((east - west) / step));
  const rows = Math.max(1, Math.round((north - south) / step));
  return {
    cols, rows,
    /** The cell of a tile, row 0 the northernmost; -1 for one outside the rectangle. */
    cellOf([w, s]) {
      const col = Math.round((w - west) / step);
      const row = rows - 1 - Math.round((s - south) / step);
      return col < 0 || col >= cols || row < 0 || row >= rows ? -1 : row * cols + col;
    },
    /** The cell a point is in, as fractions of a cell, for the wave to start from. */
    at(lat, lon) {
      return [(lon - west) / step - 0.5, (north - lat) / step - 0.5];
    },
  };
}

/** How bright a waiting tile is, 0..1, `d` tiles from where the wave starts, at `t` ms. */
export function glow(d, t, still = false) {
  if (still) return (FLOOR + CREST) / 2;
  const phase = t / PERIOD_MS - d / WAVELENGTH;
  const crest = (0.5 + 0.5 * Math.cos(2 * Math.PI * phase)) ** 3;
  return FLOOR + (CREST - FLOOR) * crest;
}

/**
 * One frame into `px` (RGBA, cols·CELL × rows·CELL): white at the glow of
 * each waiting cell, nothing where a tile has landed or there is none.
 * `still` is for someone who asked for less motion: the cells, without the wave.
 */
export function pendingFrame(px, grid, waiting, origin, t, still = false) {
  const { cols, rows } = grid;
  const W = cols * CELL;
  px.fill(0);
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < cols; col++) {
      if (!waiting[row * cols + col]) continue;
      const a = Math.round(255 * glow(Math.hypot(col - origin[0], row - origin[1]), t, still));
      for (let y = 0; y < CELL - 1; y++) {
        let o = ((row * CELL + y) * W + col * CELL) * 4;
        for (let x = 0; x < CELL - 1; x++, o += 4) {
          px[o] = px[o + 1] = px[o + 2] = 255;
          px[o + 3] = a;
        }
      }
    }
  }
}
