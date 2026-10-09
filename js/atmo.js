/**
 * The atmosphere layers, wired to the app.
 *
 * Three inputs drive everything, the same three as the sun: where the pin is,
 * which day, what time. Place and day decide *which data* — a grid, a date
 * and an endpoint per source, see atmo/sources.js — while time only decides
 * where in the fetched three days to read, which is free. So a fetch happens
 * when the pin crosses into a new grid or the calendar day changes, and
 * scrubbing the clock or playing the day through repaints from memory.
 *
 * What gets drawn, and how, is the layer registry's business
 * (atmo/layers.js). This file is the engine underneath it: it fetches the
 * sources the enabled layers need, keeps one renderer of each kind, and hands
 * the right layer to the right renderer whenever anything changes.
 */

import { state, on, emit, setPref } from './state.js';
import { viewer } from './scene.js';
import { wallToUtc } from './timezone.js';
import { profile } from './device.js';
import { gridFor, monthFraction, sampleClimatology, STREET_MODELLED, yearPair } from './atmo/field.js';
import { rampLut, GROWTH_MIN } from './atmo/scales.js';
import { dayOfYear, daysInYear } from './solar.js';
import { tileIdFor, tileProduct, tileProductsInView, snapRect, levelFor, streetSetAround, gridPastAround, maskAround, maskSpot, getCatalog } from './atmo/tiles.js';
import { SOURCES, resolveDate, registerTileSources } from './atmo/sources.js';
import { LAYERS, layerById, rivalsOf, modeOf, sourceOf, optionsOf, applyCatalog, available, setDaylight, setViewYear } from './atmo/layers.js';
import { setCatalog, cardOf } from './atmo/catalog.js';
import { fetchSeries, seriesKey } from './atmo/openmeteo.js';
import { createDrape } from './atmo/drape.js';
import { createWind } from './atmo/wind.js';

/** Read-mostly view of the data, for the pane. */
export const atmo = {
  /** A Series per source id, once fetched. */
  series: {},
  /** idle | loading | ready | none | error, per source id. `none` is a precomputed tile that does not exist here. */
  status: {},
  error: {},
};
for (const id of Object.keys(SOURCES)) {
  atmo.series[id] = null;
  atmo.status[id] = 'idle';
  atmo.error[id] = '';
}

/**
 * One renderer per kind of picture, built once the scene exists. A renderer
 * is `{ show(layer, series, ctx), hide() }`; the drape additionally exposes
 * `paint` so the engine can repaint it as the clock moves.
 */
const RENDERERS = {};

let fetchTimer = null;
const inflight = {};
const luts = new Map();

export function initAtmo() {
  RENDERERS.drape = drapeRenderer(createDrape(viewer.scene));
  RENDERERS.particles = particlesRenderer(createWind(viewer.scene, { count: profile.windParticles }));

  // Sunrise and sunset at the point switch the heat between its morning and
  // its night; anything else the clock does is a repaint from memory.
  // So does the year: a theme with earlier decades (the mornings since 1984)
  // shows the one the year slider is in.
  const daypart = () => {
    const sun = setDaylight(state.sun.elevation > 0);
    const year = setViewYear(state.y);
    if (sun || year) { apply(); scheduleFetch(true); emit('atmo', { source: 'daypart' }); }
  };
  setDaylight(state.sun.elevation > 0);
  setViewYear(state.y);
  on('location', () => { daypart(); scheduleFetch(); schedulePaint(); });
  // The surface mosaic follows the view: a new row of tiles in sight, or a
  // zoom across a level boundary, is a new fetch (cheap, mostly cached).
  // The camera event fires every few pixels of motion; the scale is read
  // again only once the view has settled, so nothing repaints mid-gesture.
  let settle = null;
  on('camera', () => {
    if (needsView()) scheduleFetch();
    clearTimeout(settle);
    settle = setTimeout(() => {
      cameraRect = readCameraRect();
      if (needsView()) schedulePaint();
    }, 300);
  });
  on('date', () => { daypart(); scheduleFetch(); schedulePaint(); });
  on('time', () => { daypart(); schedulePaint(); });
  on('pref', ({ key }) => {
    if (key === 'layers' || layerPrefs().has(key)) {
      apply();
      scheduleFetch(true);
    }
  });

  apply();
  scheduleFetch(true);

  // The catalog the tiles were built with: new variants of a theme, new
  // words. The built-in copy stands in until it lands, or if it never does.
  getCatalog().then(cat => {
    if (!setCatalog(cat)) return;
    for (const id of registerTileSources()) {
      atmo.series[id] = null;
      atmo.status[id] = 'idle';
      atmo.error[id] = '';
    }
    applyCatalog();
    emit('catalog');
    apply();
    scheduleFetch(true);
  }).catch(() => { /* the built-in catalog stays */ });
}

/* ── switches ──────────────────────────────────────────────────────── */

export const isOn = id => !!state.prefs.layers?.[id];

export function setLayer(id, on) {
  const layer = layerById(id);
  if (!layer) return;
  const next = { ...state.prefs.layers };
  // Layers sharing a slot share the picture; one replaces the other.
  if (on) for (const rival of rivalsOf(layer)) next[rival.id] = false;
  next[id] = on;
  if (Object.keys(next).some(k => next[k] !== state.prefs.layers?.[k])) setPref('layers', next);
}

export const toggleLayer = id => setLayer(id, !isOn(id));

/** Every preference key that changes what a layer draws: modes and options. */
export function layerPrefs() {
  const keys = new Set();
  for (const l of LAYERS) {
    if (l.modes) keys.add(l.modes.pref);
    for (const c of l.modes?.choices || [null]) {
      const group = optionsOf(l, c?.key ?? null);
      if (group) keys.add(group.pref);
    }
  }
  return keys;
}

/** The layer's chosen option value, or null when it has none. */
export function optionOf(layer, mode = modeOf(layer, state.prefs)) {
  const group = optionsOf(layer, mode);
  if (!group) return null;
  const value = state.prefs[group.pref];
  return group.choices.some(c => c.key === value) ? value : group.fallback;
}

/** The source id a layer reads right now. */
export const currentSource = layer => sourceOf(layer, state.prefs);

/* ── what we need ──────────────────────────────────────────────────── */

const enabledLayers = () => LAYERS.filter(l => available(l) && isOn(l.id));

/** Sources to have in hand: every layer's, since the panel reads them all at the point, drawn or not. */
function neededSources() {
  return [...new Set(LAYERS.filter(available).flatMap(l => [currentSource(l), ...(l.also || [])]))];
}

const todayHere = () => {
  const n = new Date();
  return { y: n.getFullYear(), m: n.getMonth() + 1, d: n.getDate() };
};

/** Does any enabled layer want the view-driven raster set right now? */
const VIEW_KINDS = new Set(['raster-months', 'raster-static', 'sky-brightness', 'built-epochs']);
const needsView = () => enabledLayers().some(l => VIEW_KINDS.has(SOURCES[currentSource(l)]?.dataKind));

/**
 * The ground the camera sees, snapped to tile edges and never wider than a
 * few degrees around the pin — looking at the horizon would otherwise ask
 * for half the planet.
 */
function viewRect() {
  const C = window.Cesium;
  const r = viewer?.camera.computeViewRectangle(C.Ellipsoid.WGS84);
  const span = 4;
  let rect = r
    ? [C.Math.toDegrees(r.west), C.Math.toDegrees(r.south), C.Math.toDegrees(r.east), C.Math.toDegrees(r.north)]
    : [state.lon - 0.3, state.lat - 0.3, state.lon + 0.3, state.lat + 0.3];
  rect = [
    Math.max(rect[0], state.lon - span), Math.max(rect[1], state.lat - span),
    Math.min(rect[2], state.lon + span), Math.min(rect[3], state.lat + span),
  ];
  // Always at least the tile under the pin and its neighbours.
  rect = [Math.min(rect[0], state.lon - 0.26), Math.min(rect[1], state.lat - 0.26), Math.max(rect[2], state.lon + 0.26), Math.max(rect[3], state.lat + 0.26)];
  return snapRect(rect);
}

/** Is any switched-on layer drawing from this source? */
const drawnFrom = id => enabledLayers().some(l => currentSource(l) === id);

function wantFor(source) {
  if (source.kind === 'tile' && VIEW_KINDS.has(source.dataKind)) {
    // A layer that is off still reads its number at the pin, and for that
    // the tile under the pin is enough: a mosaic of the whole view for every
    // card, drawn or not, ran a phone out of memory. The view is fetched
    // when the layer is switched on.
    const rect = drawnFrom(source.id) ? viewRect() : snapRect([state.lon, state.lat, state.lon, state.lat]);
    return { source, rect, lat: state.lat, lon: state.lon, key: `${source.id}|${levelFor(rect)}|${rect.join(',')}` };
  }
  if (source.kind === 'tile') {
    const tileId = tileIdFor(state.lat, state.lon);
    // The building mask is cut around the pin, a piece at a time (tiles.js).
    const spot = source.dataKind === 'building-mask' ? `|${maskSpot(state.lat, state.lon).join(',')}` : '';
    return { source, tileId, key: `${source.id}|${tileId}${spot}`, lat: state.lat, lon: state.lon };
  }
  const resolved = resolveDate(source, { y: state.y, m: state.m, d: state.d }, todayHere());
  if (!resolved) return null;
  const grid = gridFor(source, state.lat, state.lon);
  return { source, grid, ...resolved, key: seriesKey(source, resolved.endpoint, resolved.date, grid) };
}

/** One shape for both kinds of source: a promise of a Series, or of null for "nothing here". */
function fetchWant(want) {
  if (want.source.kind === 'tile') {
    // Rasters come as a mosaic of the tile and its neighbours, so a drape
    // does not end at a tile edge; the tables and the mask are per tile.
    if (VIEW_KINDS.has(want.source.dataKind)) return tileProductsInView(want.rect, want.lat, want.lon, want.source.product);
    // Street air reads the tile under the pin and its neighbours, each with
    // the CAMS table its ratios multiply.
    if (want.source.dataKind === 'street-air') return streetSetAround(want.lat, want.lon, want.source.product);
    // The air of earlier years is one file for Europe, read around the pin.
    if (want.source.dataKind === 'grid-past') return gridPastAround(want.lat, want.lon, want.source.product);
    if (want.source.dataKind === 'building-mask') return maskAround(want.lat, want.lon, want.source.product);
    return tileProduct(want.tileId, want.source.product);
  }
  return fetchSeries(want);
}

function scheduleFetch(immediate = false) {
  clearTimeout(fetchTimer);
  // Follow mode moves the pin with every camera nudge; wait for it to settle.
  fetchTimer = setTimeout(fetchNeeded, immediate ? 0 : 600);
}

function setStatus(id, status, error = '') {
  atmo.status[id] = status;
  atmo.error[id] = error;
  emit('atmo', { source: id, status });
}

async function fetchNeeded() {
  await Promise.all(neededSources().map(async id => {
    const source = SOURCES[id];
    const want = wantFor(source);
    if (!want) {
      setStatus(id, 'error', `No ${source.label.toLowerCase()} record for that year.`);
      return;
    }
    const { key } = want;
    if (atmo.series[id]?.key === key || inflight[id] === key) return;
    if (atmo.status[id] === 'none' && atmo.noneKey?.[id] === key) return;

    inflight[id] = key;
    setStatus(id, 'loading');
    try {
      const series = await fetchWant(want);
      // The pin may have moved on while this was in the air.
      if (inflight[id] !== key) return;
      inflight[id] = null;
      if (series) {
        if (series.kind !== undefined) series.key = key;
        atmo.series[id] = series;
        setStatus(id, 'ready');
      } else {
        atmo.series[id] = null;
        (atmo.noneKey ||= {})[id] = key;
        setStatus(id, 'none');
      }
    } catch (err) {
      if (inflight[id] !== key) return;
      inflight[id] = null;
      setStatus(id, 'error', String(err.message || err));
    }
    apply();
  }));
}

/* ── context ───────────────────────────────────────────────────────── */

/** UTC bounds of the selected local day, for legends that span the day. */
function dayBounds() {
  const start = wallToUtc({ y: state.y, m: state.m, d: state.d, minutes: 0 }, state.zone, state.lon);
  return [start.getTime(), start.getTime() + 86400000];
}

/**
 * Everything a layer may want to know about "now": the day's UTC bounds for
 * legends that span it, the chosen option, and the two slider positions as
 * the climatologies read them — a month fraction from the day of the year
 * and an hour fraction from the wall clock at the pin.
 */
/**
 * The ground the camera sees, in degrees, while it is close enough to mean
 * something (a view of the horizon is not "here"); null otherwise. Updated
 * when the camera settles.
 */
let cameraRect = null;
function readCameraRect() {
  const C = window.Cesium;
  const r = viewer?.camera.computeViewRectangle(C.Ellipsoid.WGS84);
  if (!r) return null;
  const rect = [C.Math.toDegrees(r.west), C.Math.toDegrees(r.south), C.Math.toDegrees(r.east), C.Math.toDegrees(r.north)];
  return rect[2] - rect[0] > 3 || rect[3] - rect[1] > 3 ? null : rect;
}

export const contextFor = layer => {
  const mode = modeOf(layer, state.prefs);
  const card = cardOf(mode);
  return {
    viewRect: cameraRect,
    dayBounds: dayBounds(),
    mode,
    /** The kind of data in use, and whether it is coarse enough to be drawn as visible tiles. */
    kind: card?.kind ?? null,
    coarse: !!card?.coarse || (card?.resolution_m ?? 0) >= COARSE_M,
    /**
     * A card that calls itself coarse at a fine grid (an older satellite
     * resampled), or a year it marks as from an older source (the night sky
     * before 2012, from DMSP), is read in blocks.
     */
    blocky: !!card?.coarse || (!!card?.coarse_before && state.y < card.coarse_before),
    option: optionOf(layer),
    monthFrac: monthFraction(dayOfYear(state.y, state.m, state.d), daysInYear(state.y)),
    hourFrac: state.minutes / 60,
    year: state.y,
  };
};

/**
 * Coarse data — this coarse, or a card that says so (the mornings of the
 * 1980s) — is drawn as visible tiles: a fine seam between cells where they
 * are large on screen, and cells read in 2 × 2 blocks where they are not,
 * so that it reads as the estimate it is, not as a sharp picture.
 */
const COARSE_M = 300;

/* ── renderers ─────────────────────────────────────────────────────── */

const lutFor = (layer, ctx) => {
  const key = `${layer.id}|${ctx.mode}|${ctx.option}`;
  if (!luts.has(key)) luts.set(key, rampLut(layer.stops(ctx)));
  return luts.get(key);
};

function drapeRenderer(drape) {
  let current = null;   // { layer, series, ctx, domain }
  return {
    show(layer, series, ctx) {
      const domain = layer.domain(series, ctx);
      if (!domain) { this.hide(); return; }
      current = { layer, series, ctx, domain };
      this.paint(state.utc.getTime());
      drape.show(true);
    },
    paint(t) {
      if (!current) return;
      const { layer, series } = current;
      // The sliders may have moved since show(): re-read them, and for a
      // legend that depends on them (the surface anomaly) the domain too.
      const ctx = contextFor(layer);
      const domain = layer.domain(series, ctx) || current.domain;
      current.ctx = ctx;
      current.domain = domain;
      if (series.kind === 'street-set' || series.kind === 'raster-set') {
        // Repaint only when what the pixels depend on has moved: surface heat
        // follows the month alone, street air the month and the hour.
        const sig = [series.key, ctx.option, Math.round(ctx.monthFrac * 20), ctx.year,
          series.kind === 'street-set' ? Math.round(ctx.hourFrac * 4) : '', domain.join(',')].join('|');
        if (sig === current.sig) return;
        current.sig = sig;
        drape.paintCanvas(series.rect, mosaicCanvas(series, layer, ctx, lutFor(layer, ctx), domain));
      } else {
        drape.paint(series.grid, (lat, lon) => layer.field(series, lat, lon, t, ctx), lutFor(layer, ctx), domain, layer.alpha);
      }
    },
    hide() { current = null; drape.show(false); },
    /** For the pane: the domain the ground is painted on right now. */
    get domain() { return current?.domain ?? null; },
  };
}

/* ── mosaics ────────────────────────────────────────────────────────── */

/**
 * A set of tiles becomes one texture on one ground primitive: a primitive
 * per tile is a draw call, a texture and a classification volume each, and
 * seventy of them stall a laptop and kill a phone. Every quarter-degree cell
 * of the rectangle that has no data is painted a translucent grey — "not
 * computed here yet" — in the same pass.
 */
const CELL = 0.25;
const MISSING = [128, 128, 128, 64];

function mosaicCanvas(series, layer, ctx, lut, domain) {
  const [west, south, east, north] = series.rect;
  const nx = Math.round((east - west) / CELL);
  const ny = Math.round((north - south) / CELL);
  const native = series.kind === 'street-set'
    ? Math.round((series.tiles[0]?.cols ?? 64) * profile.streetScale)
    : (series.tiles[0]?.cols ?? 8);
  const P = Math.max(4, Math.min(native, Math.floor(profile.maxTexture / Math.max(nx, ny))));
  const W = nx * P;
  const H = ny * P;
  const cv = document.createElement('canvas');
  cv.width = W;
  cv.height = H;
  const out = cv.getContext('2d');
  const img = out.createImageData(W, H);
  const px = img.data;
  for (let o = 0; o < px.length; o += 4) {
    px[o] = MISSING[0]; px[o + 1] = MISSING[1]; px[o + 2] = MISSING[2]; px[o + 3] = MISSING[3];
  }
  const alpha = Math.round(layer.alpha * 255);
  for (const tile of series.tiles) {
    const x0 = Math.round((tile.bounds[0] - west) / CELL) * P;
    const y0 = Math.round((north - tile.bounds[3]) / CELL) * P;
    if (x0 < 0 || y0 < 0 || x0 >= W || y0 >= H) continue;
    if (series.kind === 'street-set') fillStreet(tile, ctx, lut, domain, alpha, px, W, x0, y0, P);
    else fillRaster(tile, ctx, lut, domain, alpha, px, W, x0, y0, P);
  }
  out.putImageData(img, 0, 0);
  return cv;
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
    const pair = yearPair(raster, ctx.year);
    a = pair.a.values; b = pair.b.values; w = pair.w;
    if (ctx.kind === 'built-epochs') now = raster.years[pair.last].values;
  } else {
    a = raster.months[m0]?.values;
    b = raster.months[(m0 + 1) % 12]?.values;
  }
  if (!a && !b) return;
  // A 256-entry table instead of a call per pixel.
  const dec = raster.decLut ??= Float32Array.from({ length: 256 }, (_, k) => (k ? raster.decode(k) : NaN));
  const span = hi - lo || 1;
  const seams = ctx.coarse && P >= 4 * raster.cols;
  const block = ctx.blocky && !seams && raster.level === 1 ? 2 : 1;   // an overview is coarse already
  const rows = pick(raster.rows, P).map(r => r - (r % block));
  const cols = pick(raster.cols, P).map(c => c - (c % block));
  // Coarse data: the first pixel row and column of each source cell is a seam.
  const seamAlpha = Math.round(alpha * 0.45);
  for (let y = 0; y < P; y++) {
    const base = rows[y] * raster.cols;
    const rowSeam = seams && y > 0 && rows[y] !== rows[y - 1];
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
      const seam = rowSeam || (seams && x > 0 && cols[x] !== cols[x - 1]);
      paintPixel(px, o, v, lut, lo, span, seam ? seamAlpha : alpha);
    }
  }
}

/**
 * A street tile for the month and hour on the sliders. CAMS changes over
 * kilometres, the ratio over metres: CAMS is evaluated on a 32 × 32 lattice
 * and interpolated a row at a time, the ratio read per cell. The arithmetic
 * is streetValue's (atmo/field.js), written out flat because this loop runs
 * a few million times a repaint; the tests hold streetValue to it.
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
  const camsVar = isOzone ? 'ozone' : option;
  const gMain = lattice(camsVar);
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
        v = Math.max(0, (rowMain[x] / 1.96 + no2 / 1.88 - no2Street / 1.88) * 1.96);
      } else {
        v = Number.isNaN(ratio) ? rowMain[x] : (rowMain[x] + 1) * ratio - 1;
      }
      if (Number.isNaN(v)) { px[o + 3] = 0; continue; }
      paintPixel(px, o, v, lut, lo, span, alpha);
    }
  }
}

function particlesRenderer(wind) {
  return {
    show(layer, series, ctx, extras) {
      wind.setSeries(series);
      wind.setMask(extras['tile-wind'] || null);
      wind.start();
    },
    paint() { /* the particles read the clock themselves, every frame */ },
    hide() { wind.stop(); },
  };
}

let paintQueued = false;
let lastPaintedUtc = NaN;
let lastPaintedAt = 0;

/**
 * Repaints coalesce into one per animation frame, and during playback into
 * one per simulated six minutes: a texture upload sixty times a second is
 * wasted on a field that changes by the hour.
 */
function schedulePaint() {
  if (paintQueued) return;
  paintQueued = true;
  requestAnimationFrame(() => {
    paintQueued = false;
    const t = state.utc.getTime();
    const now = performance.now();
    if (Math.abs(t - lastPaintedUtc) < 6 * 60000 && now - lastPaintedAt < 150) return;
    for (const r of Object.values(RENDERERS)) r.paint(t);
    lastPaintedUtc = t;
    lastPaintedAt = now;
  });
}

/** Push the preferences into the scene: each renderer shows its enabled layer, or nothing. */
function apply() {
  if (!RENDERERS.drape) return;
  for (const [kind, renderer] of Object.entries(RENDERERS)) {
    const layer = enabledLayers().find(l => l.render === kind);
    const series = layer && atmo.series[currentSource(layer)];
    if (layer && series) {
      const extras = Object.fromEntries((layer.also || []).map(id => [id, atmo.series[id]]));
      renderer.show(layer, series, contextFor(layer), extras);
    } else {
      renderer.hide();
    }
  }
  lastPaintedUtc = state.utc.getTime();
  lastPaintedAt = performance.now();
}

/* ── readouts ──────────────────────────────────────────────────────── */

/**
 * The numbers at the pin, at the selected instant, one per layer. Null until
 * the layer's data has arrived; the pane prints an ellipsis in the meantime.
 */
export function readings() {
  const t = state.utc.getTime();
  const out = {};
  for (const layer of LAYERS.filter(available)) {
    const series = atmo.series[currentSource(layer)];
    const ctx = contextFor(layer);
    const reading = series ? layer.reading(series, state.lat, state.lon, t, ctx) : null;
    out[layer.id] = reading && {
      ...reading,
      ctx,
      domain: layer.domain && series ? layer.domain(series, ctx) : null,
    };
  }
  return out;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const longDate = ({ y, m, d }) => `${d} ${MONTHS[m - 1]} ${y}`;

/**
 * One line saying which day the numbers are for, because it is not always the
 * one on the date pill: beyond the forecast horizon the layers show the same
 * date a year earlier, and nobody should mistake that for a prediction.
 */
export function dateNote() {
  const picked = { y: state.y, m: state.m, d: state.d };
  const notes = [];
  for (const id of Object.keys(SOURCES)) {
    const s = atmo.series[id];
    if (!s || SOURCES[id].kind === 'tile') continue;
    const what = SOURCES[id].label;
    if (s.proxy === 'last-year') {
      notes.push(`${what}: no forecast for ${longDate(picked)} yet, so this is ${longDate(s.date)} from the archive — a stand-in for the season, not a prediction.`);
    } else if (s.endpoint === 'archive') {
      notes.push(`${what}: archived ${longDate(s.date)}.`);
    }
  }
  return notes.join(' ');
}

/** The fine print: each source's own caveat, in one paragraph. */
export const sourceNotes = () =>
  `${Object.values(SOURCES).map(s => s.note).join(' ')} The gradient between model cells is interpolation, not measurement.`;

/** Where the precomputed products stand at the pin, for the pane. */
export const tileHere = () => tileIdFor(state.lat, state.lon);
