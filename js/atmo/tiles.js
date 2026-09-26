/**
 * The precomputed tiles: what the offline pipeline (pipeline/) left under
 * data/tiles/, read back.
 *
 * Italy is cut into quarter-degree squares keyed by their south-west corner
 * (`N41.75E12.25` holds Rome — see pipeline/tiles.py, which is the authority
 * on the scheme). A tile's meta.json lists its products; each product is
 * loaded on first use and turned into something the samplers in field.js
 * understand: a climatology Series for the CAMS tables, a raster stack for
 * the Landsat months. Nothing here is fetched until a layer or the pane
 * asks, and a tile that does not exist is simply "no data here", not an
 * error worth a red line.
 */

import { cachedFetch } from './cache.js';

const STEP = 0.25;
const BASE = './data/tiles';

let index = null;
const metas = new Map();
const products = new Map();

export function tileIdFor(lat, lon) {
  const lat0 = Math.floor(lat / STEP) * STEP;
  const lon0 = Math.floor(lon / STEP) * STEP;
  return `N${lat0.toFixed(2)}E${lon0.toFixed(2)}`;
}

async function getIndex() {
  if (index) return index;
  index = fetch(`${BASE}/index.json`).then(r => (r.ok ? r.json() : { tiles: [] })).catch(() => ({ tiles: [] }));
  return index;
}

/** The tile's meta.json, or null when no tile has been computed there. */
export async function tileMeta(tileId) {
  if (metas.has(tileId)) return metas.get(tileId);
  const idx = await getIndex();
  if (!idx.tiles.some(t => t.id === tileId)) { metas.set(tileId, null); return null; }
  const meta = await fetch(`${BASE}/${tileId}/meta.json`).then(r => (r.ok ? r.json() : null)).catch(() => null);
  metas.set(tileId, meta);
  return meta;
}

/**
 * Load one product of one tile. Returns null when the tile or the product
 * is absent. Shapes:
 *   air  → a climatology Series: { kind: 'climatology', grid, vars, meta }
 *   heat → a raster stack:       { kind: 'raster', bounds, rows, cols, months, decode, meta }
 */
export async function tileProduct(tileId, product) {
  const key = `${tileId}|${product}`;
  if (products.has(key)) return products.get(key);
  const promise = load(tileId, product).catch(err => { products.delete(key); throw err; });
  products.set(key, promise);
  return promise;
}

async function load(tileId, product) {
  const meta = await tileMeta(tileId);
  const info = meta?.products?.[product];
  if (!info) return null;
  if (product === 'air') return loadClimatology(tileId, info);
  if (product === 'heat') return loadRaster(tileId, info);
  if (product === 'wind') return loadMask(tileId, info);
  throw new Error(`unknown tile product ${product}`);
}

async function loadClimatology(tileId, info) {
  const data = await cachedFetch(productUrl(tileId, info.file, info, 'air')).then(r => r.json());
  const { lats, lons, nodes, step } = data;
  const rows = lats.length;
  const cols = lons.length;
  const grid = {
    source: 'tile-air', step, rows, cols, n: cols,
    south: lats[0], north: lats[rows - 1], west: lons[0], east: lons[cols - 1],
    key: `tile-air|${tileId}`,
  };
  // Nodes were requested row-major, south to north, west to east — the same
  // order gridFor lists them — but say so with a lookup rather than trust it.
  const at = new Map(nodes.map(n => [`${n.requested[0]},${n.requested[1]}`, n]));
  const vars = {};
  for (const name of data.vars) {
    const arr = new Float32Array(288 * rows * cols).fill(NaN);
    for (let i = 0; i < rows; i++) {
      for (let j = 0; j < cols; j++) {
        const node = at.get(`${lats[i]},${lons[j]}`);
        const table = node?.[name]?.by_month_hour;
        if (!table) continue;
        for (let m = 0; m < 12; m++) {
          for (let h = 0; h < 24; h++) {
            const v = table[m][h];
            if (typeof v === 'number') arr[(m * 24 + h) * rows * cols + i * cols + j] = v;
          }
        }
      }
    }
    vars[name] = arr;
  }
  return { kind: 'climatology', grid, vars, hours: 288, nodes, lats, lons, meta: info, key: grid.key };
}

/**
 * Two encodings exist. The current one is a single channel where byte 0 is
 * "no observation" and the rest climb `step_c` a step from `byte1_c`; the
 * first was RGBA with the value in every colour channel and alpha 0 for no
 * data. Both are normalised here into a `values` byte per pixel, 0 for no
 * data, and one `decode` — so that nothing downstream knows there were two.
 */
async function loadRaster(tileId, info) {
  const enc = info.encoding || {};
  const v2 = enc.version === 2;
  const decode = v2
    ? byte => enc.byte1_c + (byte - 1) * enc.step_c
    : byte => enc.byte0_c + ((byte - 1) / 255) * (enc.byte255_c - enc.byte0_c);
  const raster = {
    kind: 'raster',
    bounds: null,        // filled from the tile meta by the caller
    rows: info.rows, cols: info.cols,
    months: {},          // m (0..11) → { values, cols, rows }
    decode,
    tileMedian: m => info.months[String(m + 1).padStart(2, '0')]?.tile_median_c ?? null,
    scenes: m => info.months[String(m + 1).padStart(2, '0')]?.scenes ?? 0,
    meta: info,
    key: `tile-heat|${tileId}`,
  };
  const meta = await tileMeta(tileId);
  raster.bounds = meta.bounds;
  // Decode every month up front: twelve ~200 KB PNGs, and the date slider
  // would otherwise stall on each new month it reaches.
  await Promise.all(Object.entries(info.files).map(async ([name, file]) => {
    const m = Number(name.slice(1)) - 1;
    const img = await loadImage(productUrl(tileId, file, info, 'heat'));
    const cv = document.createElement('canvas');
    cv.width = img.width;
    cv.height = img.height;
    const ctx = cv.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    const px = ctx.getImageData(0, 0, cv.width, cv.height).data;
    const values = new Uint8Array(cv.width * cv.height);
    for (let i = 0, o = 0; i < values.length; i++, o += 4) {
      values[i] = v2 ? px[o] : (px[o + 3] ? px[o] + 1 : 0);
    }
    raster.months[m] = { values, cols: cv.width, rows: cv.height };
  }));
  return raster;
}

/**
 * The building mask for the street-level wind: one byte per cell, the
 * building height in metres rounded up, and a cell is an obstacle above the
 * slice height the solver works at. The image is decoded once into a flat
 * Uint8Array of 0/1; at 2784² that is 7.7 MB, kept for as long as the pin
 * stays in the tile.
 */
async function loadMask(tileId, info) {
  const img = await loadImage(productUrl(tileId, info.heights_png.file, info, 'wind'));
  const cv = document.createElement('canvas');
  cv.width = img.width;
  cv.height = img.height;
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const px = ctx.getImageData(0, 0, cv.width, cv.height).data;
  const solid = new Uint8Array(cv.width * cv.height);
  const slice = info.slice_height_m ?? 5;
  const perLsb = info.heights_png.metres_per_lsb ?? 1;
  for (let i = 0, o = 0; i < solid.length; i++, o += 4) solid[i] = px[o] * perLsb > slice ? 1 : 0;
  const meta = await tileMeta(tileId);
  return {
    kind: 'mask', bounds: meta.bounds, rows: cv.height, cols: cv.width, solid,
    sliceHeight: slice, buildings: info.osm_buildings, meta: info, key: `tile-wind|${tileId}`,
  };
}

/**
 * A product's url, stamped with when it was generated: the stamp is what
 * makes a rebuilt tile a different cache entry, while the meta and index
 * that carry the stamps are always fetched fresh (they are tiny).
 */
const productUrl = (tileId, file, info, product = '') =>
  `${BASE}/${tileId}/${file.includes('/') ? file : `${product}/${file}`}?v=${encodeURIComponent(info.generated || '')}`;

/** An image through the cache: fetch the blob, then decode it. */
const loadImage = async src => {
  const res = await cachedFetch(src);
  if (!res.ok) throw new Error(`could not load ${src}`);
  const blob = await res.blob();
  if (typeof createImageBitmap === 'function') return createImageBitmap(blob);
  return new Promise((ok, fail) => {
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(img.src); ok(img); };
    img.onerror = () => fail(new Error(`could not decode ${src}`));
    img.src = URL.createObjectURL(blob);
  });
};
