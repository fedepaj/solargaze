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
import { profile } from './device.js';
import { MONTHS } from './util.js';
import { gridFor, monthFraction } from './atmo/field.js';
import { rampLut } from './atmo/scales.js';
import { dayOfYear, daysInYear } from './solar.js';
import { tileIdFor, tileProduct, tileProductsInView, snapRect, levelFor, streetSetAround, gridPastAround, maskAround, maskSpot, getCatalog } from './atmo/tiles.js';
import { SOURCES, resolveDate, registerTileSources } from './atmo/sources.js';
import { LAYERS, layerById, rivalsOf, modeOf, sourceOf, optionsOf, applyCatalog, available, setDaylight, setViewYear } from './atmo/layers.js';
import { setCatalog, cardOf } from './atmo/catalog.js';
import { fetchSeries, seriesKey } from './atmo/openmeteo.js';
import { createDrape } from './atmo/drape.js';
import { mosaicLayout, mosaicSteps } from './atmo/mosaic.js';
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
/** The request a source last answered "nothing here" to, so it is not asked again. */
const noneKeys = {};
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
  // A connection that comes back is a reason to ask again for what failed.
  window.addEventListener('online', () => { retries = 0; scheduleFetch(true); });

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
    // A card may have new words and a new scale for a picture already up.
    luts.clear();
    RENDERERS.drape.hide();
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

/**
 * What failed is asked again by itself, a few times and further apart: the
 * network drops for minutes and comes back, and a tile that could not be
 * read is not a tile that does not exist. After the last try it waits for
 * the next reason to fetch — the pin, the view, the connection coming back.
 */
const RETRY_MS = [4000, 15000, 60000];
let retries = 0;
let retryTimer = null;
const canRetry = () => retries < RETRY_MS.length;
/** The sources whose last answer failed, or came back with tiles missing. */
const failing = new Set();

/** A series with tiles that could not be read is asked again while there are tries left. */
const settled = (series, key) => series?.key === key && !(series.incomplete && canRetry());

async function fetchNeeded() {
  await Promise.all(neededSources().map(async id => {
    const source = SOURCES[id];
    const want = wantFor(source);
    if (!want) {
      setStatus(id, 'error', `No ${source.label.toLowerCase()} record for that year.`);
      return;
    }
    const { key } = want;
    if (settled(atmo.series[id], key) || inflight[id] === key) return;
    if (atmo.status[id] === 'none' && noneKeys[id] === key) return;

    inflight[id] = key;
    // Asked again with its picture still up, it is not "loading" to anyone.
    if (atmo.series[id]?.key !== key) setStatus(id, 'loading');
    try {
      const series = await fetchWant(want);
      // The pin may have moved on while this was in the air.
      if (inflight[id] !== key) return;
      inflight[id] = null;
      if (series) {
        if (series.kind !== undefined) series.key = key;
        if (series.incomplete) failing.add(id); else failing.delete(id);
        atmo.series[id] = series;
        setStatus(id, 'ready');
      } else {
        failing.delete(id);
        atmo.series[id] = null;
        noneKeys[id] = key;
        setStatus(id, 'none');
      }
    } catch (err) {
      if (inflight[id] !== key) return;
      inflight[id] = null;
      failing.add(id);
      // A picture with tiles missing, asked again in vain, stays up as it is.
      if (atmo.series[id]?.key !== key) setStatus(id, 'error', String(err.message || err));
    }
    apply();
  }));
  if (!failing.size) {
    clearTimeout(retryTimer);
    retryTimer = null;
    retries = 0;
  } else if (!retryTimer && canRetry()) {
    retryTimer = setTimeout(() => { retryTimer = null; fetchNeeded(); }, RETRY_MS[retries++]);
  }
}

/* ── context ───────────────────────────────────────────────────────── */

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

/**
 * Coarse data — this coarse, or a card that says so (the mornings of the
 * 1980s) — is drawn as visible tiles: a fine seam between cells where they
 * are large on screen, and cells read in 2 × 2 blocks where they are not,
 * so that it reads as the estimate it is, not as a sharp picture.
 */
const COARSE_M = 300;

/**
 * Everything a layer may want to know about "now": the mode and the kind of
 * its data, the chosen option, what the camera sees, the year, and the two
 * slider positions as the climatologies read them — a month fraction from
 * the day of the year and an hour fraction from the wall clock at the pin.
 */
export const contextFor = layer => {
  const mode = modeOf(layer, state.prefs);
  const card = cardOf(mode);
  return {
    viewRect: cameraRect,
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

/* ── renderers ─────────────────────────────────────────────────────── */

const lutFor = (layer, ctx) => {
  const key = `${layer.id}|${ctx.mode}|${ctx.option}`;
  if (!luts.has(key)) luts.set(key, rampLut(layer.stops(ctx)));
  return luts.get(key);
};

function drapeRenderer(drape) {
  let current = null;   // { layer, series, ctx, domain, sig, ready }
  return {
    show(layer, series, ctx) {
      const domain = layer.domain(series, ctx);
      if (!domain) { this.hide(); return; }
      // Shown again as it is — another source landed, another layer's switch
      // moved — the mosaic keeps its signature, and paint() leaves it alone.
      const same = current?.layer === layer;
      if (same && current.series === series) {
        current.ctx = ctx;
        current.domain = domain;
      } else {
        // A mosaic arrives a moment after it is asked for. Until it does, the
        // layer's last picture stays up (the pin's tile while the view loads,
        // the morning while the night is painted) — but never another layer's.
        current = { layer, series, ctx, domain, sig: null, ready: same && current.ready };
      }
      this.paint(state.utc.getTime());
      drape.show(current.ready);
    },
    paint(t) {
      if (!current) return;
      const now = current;
      const { layer, series } = now;
      // The sliders may have moved since show(): re-read them, and for a
      // legend that depends on them (the surface anomaly) the domain too.
      const ctx = contextFor(layer);
      const domain = layer.domain(series, ctx) || now.domain;
      now.ctx = ctx;
      now.domain = domain;
      if (series.kind === 'street-set' || series.kind === 'raster-set') {
        // Repaint only when what the pixels depend on has moved: surface heat
        // follows the month alone, street air the month and the hour.
        const sig = [series.key, ctx.option, Math.round(ctx.monthFrac * 20), ctx.year,
          series.kind === 'street-set' ? Math.round(ctx.hourFrac * 4) : '', domain.join(',')].join('|');
        if (sig === now.sig) return;
        now.sig = sig;
        // Still wanted once painted? Not if the layer went, or the sliders moved on.
        const wanted = () => current === now && now.sig === sig;
        mosaicCanvas(series, layer, ctx, lutFor(layer, ctx), domain, wanted).then(canvas => {
          if (!canvas || !wanted()) return;
          drape.paintCanvas(series.rect, canvas);
          now.ready = true;
          drape.show(true);
        }).catch(err => {
          // Not painted, so not to be taken for painted.
          if (wanted()) now.sig = null;
          console.error(err);
        });
      } else {
        drape.paint(series.grid, (lat, lon) => layer.field(series, lat, lon, t, ctx), lutFor(layer, ctx), domain, layer.alpha);
        now.ready = true;
      }
    },
    hide() { current = null; drape.show(false); },
  };
}

/** Give the page a turn: input, a frame, whatever was waiting. */
const breathe = () => (globalThis.scheduler?.yield ? globalThis.scheduler.yield() : new Promise(r => setTimeout(r, 0)));

/** The longest the page is held between two turns while a mosaic is painted, in ms. */
const PAINT_SLICE_MS = 10;

/**
 * A set of tiles as one canvas for the drape; what goes into its pixels is
 * atmo/mosaic.js. Painted a tile at a time with a turn for the page in
 * between: a view of 10 m noise in one go froze it for a third of a second.
 * Resolves to null if, at one of those turns, the picture is `wanted` no more.
 */
async function mosaicCanvas(series, layer, ctx, lut, domain, wanted) {
  const layout = mosaicLayout(series, ctx, profile);
  const cv = document.createElement('canvas');
  cv.width = layout.W;
  cv.height = layout.H;
  const out = cv.getContext('2d');
  const img = out.createImageData(layout.W, layout.H);
  let since = performance.now();
  for (const paintNext of mosaicSteps(img.data, layout, series, ctx, lut, domain, layer.alpha)) {
    paintNext();
    if (performance.now() - since < PAINT_SLICE_MS) continue;
    await breathe();
    if (!wanted()) return null;
    since = performance.now();
  }
  out.putImageData(img, 0, 0);
  return cv;
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
      // For the legend, which only a drawn layer shows.
      domain: layer.domain && isOn(layer.id) ? layer.domain(series, ctx) : null,
    };
  }
  return out;
}

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
