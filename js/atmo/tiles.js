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
import { TILES_BASE, TILES_REMOTE } from '../config.js';
import { profile } from '../device.js';
import { cardOf } from './catalog.js';

const STEP = 0.25;

let index = null;
const metas = new Map();
const products = new Map();

/** The ids of the tile holding a point and its eight neighbours. */
export function tileIdsAround(lat, lon) {
  const lat0 = Math.floor(lat / STEP) * STEP;
  const lon0 = Math.floor(lon / STEP) * STEP;
  const ids = [];
  for (const di of [-1, 0, 1]) {
    for (const dj of [-1, 0, 1]) {
      ids.push(`N${(lat0 + di * STEP).toFixed(2)}E${(lon0 + dj * STEP).toFixed(2)}`);
    }
  }
  return ids;
}

/**
 * Snap a view rectangle outward to tile edges, so that the plan — and the
 * cache key built from it — only changes when a new row or column of tiles
 * comes into view, not with every pixel of camera motion.
 */
export function snapRect([west, south, east, north]) {
  const f = x => Math.floor(x / STEP) * STEP;
  const c = x => Math.ceil(x / STEP) * STEP;
  return [f(west), f(south), c(east), c(north)].map(x => Math.round(x * 100) / 100);
}

/**
 * Which resolution for how many tiles. A handful fills the screen and gets
 * the 90 m months; a province gets 270 m; a country 810 m, where a month of
 * a tile is under a kilobyte and four hundred of them are still a quick
 * download.
 */
export function levelFor(rect) {
  const [west, south, east, north] = rect;
  const n = Math.max(1, Math.round(((east - west) / STEP) * ((north - south) / STEP)));
  const [full, mid] = profile.heatLevels;
  return n <= full ? 1 : n <= mid ? 3 : 9;
}

const MAX_TILES = 600;

/**
 * A raster product for every built tile inside a view rectangle, at the
 * level the rectangle earns, plus the tile under the pin at full resolution
 * as `centre` — the one the legend's medians and the pane's reading use.
 * Returns null when not even the centre exists.
 */
export async function tileProductsInView(rect, lat, lon, product) {
  const idx = await getIndex();
  const [west, south, east, north] = rect;
  const level = levelFor(rect);
  let ids = idx.tiles
    .filter(t => t.products.includes(product))
    .filter(t => t.bounds[0] < east && t.bounds[2] > west && t.bounds[1] < north && t.bounds[3] > south)
    .map(t => t.id);
  if (ids.length > MAX_TILES) {
    // Nearest to the pin first: a view of half a continent still draws
    // around the place being looked at.
    const d = id => { const m = /^N(-?[\d.]+)E(-?[\d.]+)$/.exec(id); return (Number(m[1]) + STEP / 2 - lat) ** 2 + (Number(m[2]) + STEP / 2 - lon) ** 2; };
    ids = ids.sort((a, b) => d(a) - d(b)).slice(0, MAX_TILES);
  }
  const centreId = tileIdFor(lat, lon);
  const [centre, ...rest] = await Promise.all([
    tileProduct(centreId, product, 1).catch(() => null),
    ...ids.map(id => tileProduct(id, product, level).catch(() => null)),
  ]);
  const tiles = rest.filter(Boolean);
  if (!centre && !tiles.length) return null;
  return { kind: 'raster-set', rect, tiles, centre: centre || tiles[0], level, key: `${product}-view|${level}|${rect.join(',')}`, meta: (centre || tiles[0]).meta };
}

export function tileIdFor(lat, lon) {
  const lat0 = Math.floor(lat / STEP) * STEP;
  const lon0 = Math.floor(lon / STEP) * STEP;
  return `N${lat0.toFixed(2)}E${lon0.toFixed(2)}`;
}

/**
 * Where the tiles are read from: the pipeline's own folder when served from
 * localhost (see config.js), unless there is none there — a fresh clone has
 * no data/tiles, which live in the bucket — in which case the bucket.
 */
let BASE = TILES_BASE;

async function getIndex() {
  if (index) return index;
  const read = base => fetch(`${base}/index.json`).then(r => (r.ok ? r.json() : null)).catch(() => null);
  index = (async () => {
    let idx = await read(BASE);
    const remote = TILES_REMOTE && TILES_REMOTE.replace(/\/$/, '');
    if (!idx && remote && BASE !== remote) {
      BASE = remote;
      idx = await read(BASE);
    }
    return idx || { tiles: [] };
  })();
  return index;
}

/** The pipeline's catalog of products (atmo/catalog.js), from wherever the index came from; null if none. */
export async function getCatalog() {
  await getIndex();
  return fetch(`${BASE}/catalog.json`, { cache: 'no-cache' }).then(r => (r.ok ? r.json() : null)).catch(() => null);
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
 * An image's red channel (and its alpha, if asked) as bytes, read a band of
 * rows at a time: a 2784² tile read whole asks the browser for a 31 MB
 * ImageData at once, and on a phone with a few tiles in memory that is the
 * request that fails. The decoded bitmap is released as soon as it is read.
 */
const BAND = 256;
function channels(img, { alpha = false } = {}) {
  const w = img.width;
  const h = img.height;
  const red = new Uint8Array(w * h);
  const al = alpha ? new Uint8Array(w * h) : null;
  const cv = document.createElement('canvas');
  cv.width = w;
  cv.height = Math.min(BAND, h);
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  for (let y0 = 0; y0 < h; y0 += BAND) {
    const rows = Math.min(BAND, h - y0);
    ctx.clearRect(0, 0, w, cv.height);
    ctx.drawImage(img, 0, y0, w, rows, 0, 0, w, rows);
    const px = ctx.getImageData(0, 0, w, rows).data;
    for (let i = 0, o = 0, k = y0 * w; i < w * rows; i++, o += 4, k++) {
      red[k] = px[o];
      if (al) al[k] = px[o + 3];
    }
  }
  cv.width = 0;
  cv.height = 0;
  img.close?.();
  return { red, alpha: al, width: w, height: h };
}

/**
 * A product that is one file for the whole scope rather than tiles (the air
 * of earlier years, on an 80 km grid): its meta and every pollutant's image,
 * decoded once. Rows of the grid for each (year, month), oldest first; byte
 * = µg/m³ + 1, 0 = no value.
 */
const grids = new Map();
export function gridPast(product) {
  if (!grids.has(product)) {
    const load = async () => {
      const card = cardOf(product);
      const meta = await fetch(`${BASE}/${card.meta}`, { cache: 'no-cache' }).then(r => (r.ok ? r.json() : null)).catch(() => null);
      if (!meta) return null;
      const data = {};
      await Promise.all(Object.entries(meta.files).map(async ([name, file]) => {
        const img = await loadImage(`${BASE}/${file}?v=${encodeURIComponent(meta.generated || '')}`);
        data[name] = channels(img).red;
      }));
      return { meta, data };
    };
    grids.set(product, load().catch(err => { grids.delete(product); throw err; }));
  }
  return grids.get(product);
}

/** The grid of earlier years around a point: a few degrees each way, on its own cell edges. */
export async function gridPastAround(lat, lon, product) {
  const g = await gridPast(product);
  if (!g) return null;
  const { north, west, step, rows, cols } = g.meta.grid;
  const span = 4;
  const r0 = Math.max(0, Math.round((north - (lat + span)) / step));
  const r1 = Math.min(rows - 1, Math.round((north - (lat - span)) / step));
  const c0 = Math.max(0, Math.round((lon - span - west) / step));
  const c1 = Math.min(cols - 1, Math.round((lon + span - west) / step));
  if (r0 > r1 || c0 > c1) return null;
  const grid = {
    north: north - r0 * step + step / 2, south: north - r1 * step - step / 2,
    west: west + c0 * step - step / 2, east: west + c1 * step + step / 2,
    key: `${product}|${r0},${r1},${c0},${c1}`,
  };
  return { kind: 'grid-past', grid, data: g.data, meta: g.meta, key: grid.key };
}

/**
 * Load one product of one tile. Returns null when the tile or the product
 * is absent. Shapes:
 *   air  → a climatology Series: { kind: 'climatology', grid, vars, meta }
 *   heat → a raster stack:       { kind: 'raster', bounds, rows, cols, months, decode, meta }
 */
export async function tileProduct(tileId, product, level = 1) {
  const key = `${tileId}|${product}|${level}`;
  if (products.has(key)) return products.get(key);
  const promise = load(tileId, product, level).catch(err => { products.delete(key); throw err; });
  products.set(key, promise);
  return promise;
}

async function load(tileId, product, level) {
  const meta = await tileMeta(tileId);
  const info = meta?.products?.[product];
  if (!info) return null;
  // How to read a product is its kind's business (atmo/catalog.js), not its name's.
  switch (cardOf(product)?.kind) {
    case 'climatology': return loadClimatology(tileId, info, product);
    case 'raster-months': return loadRaster(tileId, info, level, product);
    case 'raster-static':
    case 'sky-brightness':
    case 'built-epochs': return loadStatic(tileId, info, level, product);
    case 'building-mask': return loadMask(tileId, info, product);
    case 'street-air': return loadStreet(tileId, info, product);
    default: throw new Error(`no reader for tile product ${product}`);
  }
}

async function loadClimatology(tileId, info, product) {
  const data = await cachedFetch(productUrl(tileId, info.file, info, product)).then(r => r.json());
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
async function loadRaster(tileId, info, level = 1, product = 'heat') {
  const enc = info.encoding || {};
  const v2 = enc.version === 2;
  const decode = v2
    ? byte => enc.byte1_c + (byte - 1) * enc.step_c
    : byte => enc.byte0_c + ((byte - 1) / 255) * (enc.byte255_c - enc.byte0_c);
  // Overviews are the 90 m month averaged 3 × 3 or 9 × 9; a tile without
  // them (an older product) is read at full resolution whatever was asked.
  const suffix = level > 1 && (info.overviews || []).includes(level) ? `.o${level}` : '';
  const factor = suffix ? level : 1;
  const raster = {
    kind: 'raster',
    level: factor,
    bounds: null,        // filled from the tile meta by the caller
    rows: Math.floor(info.rows / factor), cols: Math.floor(info.cols / factor),
    months: {},          // m (0..11) → { values, cols, rows }
    decode,
    tileMedian: m => info.months[String(m + 1).padStart(2, '0')]?.tile_median_c ?? null,
    scenes: m => info.months[String(m + 1).padStart(2, '0')]?.scenes ?? 0,
    meta: info,
    key: `${product}|${tileId}`,
  };
  const meta = await tileMeta(tileId);
  raster.bounds = meta.bounds;
  // Decode every month up front: the date slider would otherwise stall on
  // each new month it reaches.
  const fetchChecked = async (file, width, height) => {
    const url = productUrl(tileId, file, info, product);
    let img = await loadImage(url);
    // The cache key is the product's stamp; a file rewritten without a new
    // stamp would come back at the old size and be read with the wrong
    // stride. A size that disagrees with the meta is refetched, not trusted.
    if (img.width !== width || img.height !== height) {
      img = await loadImage(url, { reload: true });
      if (img.width !== width || img.height !== height) {
        throw new Error(`${file} is ${img.width}×${img.height}, meta says ${width}×${height}`);
      }
    }
    const { red, alpha } = channels(img, { alpha: !v2 });
    if (v2) return red;
    for (let i = 0; i < red.length; i++) red[i] = alpha[i] ? red[i] + 1 : 0;
    return red;
  };
  if (info.packing) {
    // One image per level, the twelve months stacked north to south.
    const name = suffix ? `months${suffix}` : 'months';
    const { rows, cols } = raster;
    const all = await fetchChecked(info.files[name], cols, rows * 12);
    for (const key of Object.keys(info.months)) {
      const m = Number(key) - 1;
      raster.months[m] = { values: all.subarray(m * rows * cols, (m + 1) * rows * cols), cols, rows };
    }
    return raster;
  }
  const wanted = Object.entries(info.files).filter(([name]) => (suffix ? name.endsWith(suffix) : !name.includes('.o')));
  await Promise.all(wanted.map(async ([name, file]) => {
    const m = Number(name.slice(1, 3)) - 1;
    const values = await fetchChecked(file, raster.cols, raster.rows);
    raster.months[m] = { values, cols: raster.cols, rows: raster.rows };
  }));
  return raster;
}

/**
 * One raster with no months (noise, the night sky): read like a month raster
 * whose every month is the same image, so the samplers and the mosaic need
 * nothing new. Byte 0 is no value (inside a building); the rest move by
 * `step` from `value_at_byte1` (noise says `step_db` and `byte1_db`). The
 * image is the product's one file without an overview suffix.
 */
async function loadStatic(tileId, info, level = 1, product = 'noise') {
  const enc = info.encoding;
  const suffix = level > 1 && (info.overviews || []).includes(level) ? `.o${level}` : '';
  const factor = suffix ? level : 1;
  const meta = await tileMeta(tileId);
  const main = Object.keys(info.files).find(name => !name.includes('.o'));
  const file = info.files[`${main}${suffix}`] || info.files[main];
  const v1 = enc.value_at_byte1 ?? enc.byte1_db;
  const step = enc.step ?? enc.step_db;
  const img = await loadImage(productUrl(tileId, file, info, product));
  const { red: values, width, height } = channels(img);
  const cv = { width, height };
  // A stack of years (the night sky) or epochs (the built ground): one image
  // each, oldest on top — an overview's rows are its height over the count.
  // Epochs written as changes from the one before are summed back here.
  const stacked = info.stack === 'years';
  const rows = stacked ? cv.height / info.years.length : cv.height;
  const n = rows * cv.width;
  if (stacked && enc.deltas) {
    for (let i = n; i < values.length; i++) values[i] = values[i - n] + values[i] - enc.delta_zero_byte;
  }
  const years = stacked
    ? Object.fromEntries(info.years.map((y, k) => [y, { values: values.subarray(k * n, (k + 1) * n), cols: cv.width, rows }]))
    : null;
  const image = years ? years[info.years[info.years.length - 1]] : { values, cols: cv.width, rows: cv.height };
  return {
    kind: 'raster', level: factor, bounds: meta.bounds, rows, cols: cv.width, years,
    months: Object.fromEntries([...Array(12).keys()].map(m => [m, image])),
    decode: byte => v1 + (byte - 1) * step,
    tileMedian: () => null, scenes: () => 0, meta: info, key: `${product}|${tileId}`,
  };
}

/**
 * The building mask for the street-level wind: one byte per cell, the
 * building height in metres rounded up, and a cell is an obstacle above the
 * slice height the solver works at. The image is decoded once into a flat
 * Uint8Array of 0/1; at 2784² that is 7.7 MB, kept for as long as the pin
 * stays in the tile.
 */
async function loadMask(tileId, info, product) {
  const img = await loadImage(productUrl(tileId, info.heights_png.file, info, product));
  const { red: solid, width, height } = channels(img);
  const cv = { width, height };
  const slice = info.slice_height_m ?? 5;
  const perLsb = info.heights_png.metres_per_lsb ?? 1;
  for (let i = 0; i < solid.length; i++) solid[i] = solid[i] * perLsb > slice ? 1 : 0;
  const meta = await tileMeta(tileId);
  return {
    kind: 'mask', bounds: meta.bounds, rows: cv.height, cols: cv.width, solid,
    sliceHeight: slice, buildings: info.osm_buildings, meta: info, key: `tile-wind|${tileId}`,
  };
}

/**
 * The street-scale air correction of one tile: for each pollutant the
 * pipeline modelled (NO₂ and PM10), a byte per 50 m cell encoding the
 * log-ratio to CAMS, decoded once into a 256-entry table of ratios.
 */
async function loadStreet(tileId, info, product) {
  const enc = info.encoding;
  const ratioOf = new Float32Array(256).fill(NaN);
  for (let b = 1; b < 256; b++) ratioOf[b] = Math.exp(enc.byte1_ln_ratio + (b - 1) * enc.step_ln_ratio);
  const bytes = {};
  await Promise.all(Object.entries(info.files).map(async ([name, file]) => {
    const img = await loadImage(productUrl(tileId, file, info, product));
    if (img.width !== info.cols || img.height !== info.rows) throw new Error(`${file} is ${img.width}×${img.height}`);
    bytes[name] = channels(img).red;
  }));
  const meta = await tileMeta(tileId);
  return { kind: 'street', bounds: meta.bounds, rows: info.rows, cols: info.cols, bytes, ratioOf, meta: info, key: `${product}|${tileId}` };
}

/**
 * Street-scale air around a point: the tile under it and its eight
 * neighbours, each with its 50 m ratios and the CAMS climatology they
 * multiply. `centre` is the tile under the point. Null when that tile has
 * no street product.
 */
export async function streetSetAround(lat, lon, product = 'air_street') {
  const base = cardOf(product)?.base || 'air';
  const pair = id => Promise.all([
    tileProduct(id, product).catch(() => null),
    tileProduct(id, base).catch(() => null),
  ]).then(([street, clim]) => (street && clim ? { ...street, clim } : null));
  const centreId = tileIdFor(lat, lon);
  const all = await Promise.all(tileIdsAround(lat, lon).map(pair));
  const tiles = all.filter(Boolean);
  const centre = tiles.find(t => t.key === `${product}|${centreId}`);
  if (!centre) return null;
  const [w, south, e, n] = centre.bounds;
  const rect = [w - STEP, south - STEP, e + STEP, n + STEP];
  return { kind: 'street-set', rect, tiles, centre, meta: centre.meta, key: `${product}|${centreId}` };
}

/**
 * A product's url, stamped with when it was generated: the stamp is what
 * makes a rebuilt tile a different cache entry, while the meta and index
 * that carry the stamps are always fetched fresh (they are tiny).
 */
const productUrl = (tileId, file, info, product = '') =>
  `${BASE}/${tileId}/${file.includes('/') ? file : `${product}/${file}`}?v=${encodeURIComponent(info.generated || '')}`;

/** An image through the cache: fetch the blob, then decode it. */
const loadImage = async (src, { reload = false } = {}) => {
  const res = await cachedFetch(src, { reload });
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
