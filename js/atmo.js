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
import { gridFor, monthFraction } from './atmo/field.js';
import { rampLut } from './atmo/scales.js';
import { dayOfYear, daysInYear } from './solar.js';
import { tileIdFor, tileProduct } from './atmo/tiles.js';
import { SOURCES, resolveDate } from './atmo/sources.js';
import { LAYERS, layerById, rivalsOf, modeOf, sourceOf, optionsOf } from './atmo/layers.js';
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

  on('location', () => { scheduleFetch(); schedulePaint(); });
  on('date', () => { scheduleFetch(); schedulePaint(); });
  on('time', schedulePaint);
  on('tab', () => scheduleFetch(true));
  on('pref', ({ key }) => {
    if (key === 'layers' || layerPrefs().has(key)) {
      apply();
      scheduleFetch(true);
    }
  });

  apply();
  scheduleFetch(true);
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

const enabledLayers = () => LAYERS.filter(l => isOn(l.id));

/** Sources to have in hand: every enabled layer's, and all of them while the pane is open. */
function neededSources() {
  if (state.tab === 'air') return Object.keys(SOURCES);
  return [...new Set(enabledLayers().flatMap(l => [currentSource(l), ...(l.also || [])]))];
}

const todayHere = () => {
  const n = new Date();
  return { y: n.getFullYear(), m: n.getMonth() + 1, d: n.getDate() };
};

function wantFor(source) {
  if (source.kind === 'tile') {
    const tileId = tileIdFor(state.lat, state.lon);
    return { source, tileId, key: `${source.id}|${tileId}` };
  }
  const resolved = resolveDate(source, { y: state.y, m: state.m, d: state.d }, todayHere());
  if (!resolved) return null;
  const grid = gridFor(source, state.lat, state.lon);
  return { source, grid, ...resolved, key: seriesKey(source, resolved.endpoint, resolved.date, grid) };
}

/** One shape for both kinds of source: a promise of a Series, or of null for "nothing here". */
function fetchWant(want) {
  if (want.source.kind === 'tile') return tileProduct(want.tileId, want.source.product);
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
        if (series.kind === 'raster' || series.kind === 'climatology') series.key = key;
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
export const contextFor = layer => ({
  dayBounds: dayBounds(),
  mode: modeOf(layer, state.prefs),
  option: optionOf(layer),
  monthFrac: monthFraction(dayOfYear(state.y, state.m, state.d), daysInYear(state.y)),
  hourFrac: state.minutes / 60,
});

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
      if (series.kind === 'raster') {
        drape.paintCanvas(series.bounds, rasterCanvas(series, layer, ctx, lutFor(layer, ctx), domain));
      } else {
        drape.paint(series.grid, (lat, lon) => layer.field(series, lat, lon, t, ctx), lutFor(layer, ctx), domain, layer.alpha);
      }
    },
    hide() { current = null; drape.show(false); },
    /** For the pane: the domain the ground is painted on right now. */
    get domain() { return current?.domain ?? null; },
  };
}

/**
 * Colour a raster product at its own resolution. The two months around the
 * date are blended per pixel, exactly as the layer's sampler does for the
 * readout, so what the ground shows and what the pane prints agree.
 */
function rasterCanvas(raster, layer, ctx, lut, [lo, hi]) {
  const { cols, rows } = raster;
  // A fresh canvas each time: Cesium re-uploads only when the object changes.
  const cv = document.createElement('canvas');
  cv.width = cols;
  cv.height = rows;
  const out = cv.getContext('2d');
  const img = out.createImageData(cols, rows);
  const px = img.data;
  const mf = ((ctx.monthFrac % 12) + 12) % 12;
  const m0 = Math.floor(mf);
  const m1 = (m0 + 1) % 12;
  const wm = mf - m0;
  const a = raster.months[m0]?.values;
  const b = raster.months[m1]?.values;
  const span = hi - lo || 1;
  const alpha = Math.round(layer.alpha * 255);
  for (let i = 0, o = 0; o < px.length; i++, o += 4) {
    const va = a && a[i] ? raster.decode(a[i]) : NaN;
    const vb = b && b[i] ? raster.decode(b[i]) : NaN;
    let v;
    if (Number.isNaN(va)) v = vb;
    else if (Number.isNaN(vb)) v = va;
    else v = va * (1 - wm) + vb * wm;
    if (Number.isNaN(v)) { px[o + 3] = 0; continue; }
    const li = Math.min(255, Math.max(0, Math.round(((v - lo) / span) * 255))) * 3;
    px[o] = lut[li];
    px[o + 1] = lut[li + 1];
    px[o + 2] = lut[li + 2];
    px[o + 3] = alpha;
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
  for (const layer of LAYERS) {
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
