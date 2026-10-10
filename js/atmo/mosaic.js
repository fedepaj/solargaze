/**
 * A set of tiles painted as one picture.
 *
 * A set of tiles becomes one texture on one ground primitive: a primitive
 * per tile is a draw call, a texture and a classification volume each, and
 * seventy of them stall a laptop and kill a phone. Every quarter-degree cell
 * of the rectangle that has no data is painted a translucent grey — "not
 * computed here yet" — in the same pass.
 *
 * Pure: no Cesium, no DOM. The engine (../atmo.js) makes the canvas and
 * hands its pixels over; what is written into them is this file's, so that
 * `node --test` can hold the ground to the same numbers as the pane.
 */

import { sampleClimatology, STREET_MODELLED, PPB_NO2, PPB_O3, yearPair, growthFrom } from './field.js';
import { GROWTH_MIN } from './scales.js';

const CELL = 0.25;
const MISSING = [128, 128, 128, 64];
/** The same grey as one 32-bit word, in whatever byte order this machine keeps its pixels. */
const MISSING_WORD = new Uint32Array(new Uint8Array(MISSING).buffer)[0];

/** Pixels a side each cell of coarse data is drawn with, so that a seam fits between two. */
const SEAM_PX = 8;
/** …while a tile of it stays this small: beyond, the cells are read in blocks instead (`ctx.blocky`). */
const SEAM_TILE_PX = 512;

/**
 * How a set is laid out in pixels: `nx × ny` cells of `P` pixels a side.
 * A cell gets its tile's own resolution (street air a share of it, on a
 * phone) unless the whole picture would then pass `maxTexture`. Coarse data
 * (`ctx.coarse`: the night sky's 460 m cells) is drawn larger than it is,
 * eight pixels a cell, so that the seams between its cells can be seen.
 */
export function mosaicLayout(series, ctx, { maxTexture, streetScale = 1 }) {
  const [west, south, east, north] = series.rect;
  const nx = Math.round((east - west) / CELL);
  const ny = Math.round((north - south) / CELL);
  const cols = series.tiles[0]?.cols;
  let native = cols ?? 8;
  if (series.kind === 'street-set') native = Math.round((cols ?? 64) * streetScale);
  else if (ctx.coarse && native * SEAM_PX <= SEAM_TILE_PX) native *= SEAM_PX;
  const P = Math.max(4, Math.min(native, Math.floor(maxTexture / Math.max(nx, ny))));
  return { nx, ny, P, W: nx * P, H: ny * P };
}

/**
 * Paint a set into `px` (RGBA, `layout.W × layout.H`, row 0 the north
 * edge): each tile through the colour table over `domain`, at `alpha`
 * (0..1), for the month, hour and year in `ctx`.
 */
export function paintMosaic(px, layout, series, ctx, lut, domain, alpha) {
  for (const paintNext of mosaicSteps(px, layout, series, ctx, lut, domain, alpha)) paintNext();
}

/**
 * The same painting as a list of steps, a tile each, for a caller that
 * cannot hold the page still for all of them at once: a view of 10 m noise
 * is twelve million pixels, a third of a second in one go. The picture is
 * whole once every step has run; the first call also lays the grey.
 */
export function mosaicSteps(px, layout, series, ctx, lut, domain, alpha) {
  const { P, W, H } = layout;
  const [west, , , north] = series.rect;
  const a = Math.round(alpha * 255);
  const fill = series.kind === 'street-set' ? fillStreet : fillRaster;
  const steps = [() => new Uint32Array(px.buffer, px.byteOffset, px.length >> 2).fill(MISSING_WORD)];
  for (const tile of series.tiles) {
    const x0 = Math.round((tile.bounds[0] - west) / CELL) * P;
    const y0 = Math.round((north - tile.bounds[3]) / CELL) * P;
    if (x0 < 0 || y0 < 0 || x0 >= W || y0 >= H) continue;
    steps.push(() => fill(tile, ctx, lut, domain, a, px, W, x0, y0, P));
  }
  return steps;
}

/** Source row/column for each of P output pixels across n source cells. */
const pick = (n, P) => Uint32Array.from({ length: P }, (_, k) => Math.min(n - 1, Math.floor(((k + 0.5) * n) / P)));

const paintPixel = (px, o, v, lut, lo, span, alpha) => {
  const li = Math.min(255, Math.max(0, Math.round(((v - lo) / span) * 255))) * 3;
  px[o] = lut[li];
  px[o + 1] = lut[li + 1];
  px[o + 2] = lut[li + 2];
  px[o + 3] = alpha;
};

/**
 * A raster tile's month, the two months around the date blended per pixel
 * as the layer's sampler does for the readout, so ground and pane agree.
 */
function fillRaster(raster, ctx, lut, [lo, hi], alpha, px, W, x0, y0, P) {
  const mf = ((ctx.monthFrac % 12) + 12) % 12;
  const m0 = Math.floor(mf);
  const wm = mf - m0;
  // A raster by year or epoch (the night sky, the built ground) is read
  // between the two around the year on the slider; the built ground draws
  // what is there now and was not then (KINDS['built-epochs'] in layers.js).
  let a, b, w = wm, now = null;
  if (raster.years) {
    const growth = ctx.kind === 'built-epochs';
    const pair = yearPair(raster, growth ? growthFrom(raster, ctx.year) : ctx.year);
    a = pair.a.values; b = pair.b.values; w = pair.w;
    if (growth) now = raster.years[pair.last].values;
  } else {
    a = raster.months[m0]?.values;
    b = raster.months[(m0 + 1) % 12]?.values;
  }
  if (!a && !b) return;
  // A 256-entry table instead of a call per pixel.
  const dec = raster.decLut ??= Float32Array.from({ length: 256 }, (_, k) => (k ? raster.decode(k) : NaN));
  const span = hi - lo || 1;
  const seams = !!ctx.coarse && P >= SEAM_PX * raster.cols;
  const block = ctx.blocky && !seams && raster.level === 1 ? 2 : 1;   // an overview is coarse already
  const rows = pick(raster.rows, P).map(r => r - (r % block));
  const cols = pick(raster.cols, P).map(c => c - (c % block));
  // Coarse data: the first pixel row and column of each source cell is a
  // seam — a tile's first too, so that the grid runs unbroken across tiles.
  const seamAlpha = Math.round(alpha * 0.45);
  for (let y = 0; y < P; y++) {
    const base = rows[y] * raster.cols;
    const rowSeam = seams && (y === 0 || rows[y] !== rows[y - 1]);
    let o = ((y0 + y) * W + x0) * 4;
    for (let x = 0; x < P; x++, o += 4) {
      const i = base + cols[x];
      const va = a ? dec[a[i]] : NaN;
      const vb = b ? dec[b[i]] : NaN;
      let v = Number.isNaN(va) ? vb : Number.isNaN(vb) ? va : va * (1 - w) + vb * w;
      if (now) {
        v = dec[now[i]] - v;
        if (!(v >= GROWTH_MIN)) { px[o + 3] = 0; continue; }
      }
      if (Number.isNaN(v)) { px[o + 3] = 0; continue; }
      const seam = rowSeam || (seams && (x === 0 || cols[x] !== cols[x - 1]));
      paintPixel(px, o, v, lut, lo, span, seam ? seamAlpha : alpha);
    }
  }
}

/**
 * A street tile for the month and hour on the sliders. CAMS changes over
 * kilometres, the ratio over metres: CAMS is evaluated on a 32 × 32 lattice
 * and interpolated a row at a time, the ratio read per cell. The arithmetic
 * is streetValue's (atmo/field.js), written out flat because this loop runs
 * a few million times a repaint; the tests hold the two to each other.
 */
const STREET_LATTICE = 32;
function fillStreet(tile, ctx, lut, [lo, hi], alpha, px, W, x0, y0, P) {
  const { cols, rows, bounds: [west, south, east, north] } = tile;
  const option = ctx.option;
  const G = STREET_LATTICE;
  const lattice = name => {
    const g = new Float32Array(G * G);
    for (let j = 0; j < G; j++) {
      const lat = north - ((j * (rows - 1)) / (G - 1) + 0.5) * ((north - south) / rows);
      for (let i = 0; i < G; i++) {
        const lon = west + ((i * (cols - 1)) / (G - 1) + 0.5) * ((east - west) / cols);
        g[j * G + i] = sampleClimatology(tile.clim, name, lat, lon, ctx.monthFrac, ctx.hourFrac);
      }
    }
    return g;
  };
  const isOzone = option === 'ozone';
  const gMain = lattice(option);
  const gNo2 = isOzone ? lattice('nitrogen_dioxide') : null;
  const ratioVar = isOzone ? 'nitrogen_dioxide' : STREET_MODELLED.includes(option) ? option : null;
  const bytes = ratioVar ? tile.bytes[ratioVar] : null;
  const ratioOf = tile.ratioOf;
  const span = hi - lo || 1;
  const srcRow = pick(rows, P);
  const srcCol = pick(cols, P);
  const i0 = new Uint16Array(P);
  const fx = new Float32Array(P);
  for (let x = 0; x < P; x++) {
    const u = (srcCol[x] * (G - 1)) / (cols - 1);
    i0[x] = Math.min(G - 2, Math.floor(u));
    fx[x] = u - i0[x];
  }
  const rowMain = new Float32Array(P);
  const rowNo2 = new Float32Array(P);
  const fillRow = (g, out, j0, fy) => {
    for (let x = 0; x < P; x++) {
      const k = j0 * G + i0[x];
      const a = g[k] + (g[k + 1] - g[k]) * fx[x];
      const b = g[k + G] + (g[k + G + 1] - g[k + G]) * fx[x];
      out[x] = a + (b - a) * fy;
    }
  };
  for (let y = 0; y < P; y++) {
    const r = srcRow[y];
    const v0 = (r * (G - 1)) / (rows - 1);
    const j0 = Math.min(G - 2, Math.floor(v0));
    fillRow(gMain, rowMain, j0, v0 - j0);
    if (gNo2) fillRow(gNo2, rowNo2, j0, v0 - j0);
    const base = r * cols;
    let o = ((y0 + y) * W + x0) * 4;
    for (let x = 0; x < P; x++, o += 4) {
      const ratio = bytes ? ratioOf[bytes[base + srcCol[x]]] : NaN;
      let v;
      if (isOzone) {
        const no2 = rowNo2[x];
        const no2Street = Number.isNaN(ratio) ? no2 : (no2 + 1) * ratio - 1;
        v = Math.max(0, (rowMain[x] / PPB_O3 + no2 / PPB_NO2 - no2Street / PPB_NO2) * PPB_O3);
      } else {
        v = Number.isNaN(ratio) ? rowMain[x] : (rowMain[x] + 1) * ratio - 1;
      }
      if (Number.isNaN(v)) { px[o + 3] = 0; continue; }
      paintPixel(px, o, v, lut, lo, span, alpha);
    }
  }
}
