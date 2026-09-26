/**
 * The layers, as data.
 *
 * A layer is a way of looking at one source: which number to pull out of it,
 * how to colour that number, how to say it in the pane, and which renderer
 * puts it in the scene. Everything the engine and the pane do is driven from
 * this list — the rail button, the row in the AIR tab, the legend, the fetch
 * that has to happen first. Adding a layer is adding an entry; adding a kind
 * of picture (contours, columns, a volume) is adding a renderer to
 * RENDERERS in ../atmo.js and naming it here.
 *
 * Two renderers exist today. `drape` paints a scalar field onto the ground
 * and the buildings, and only one drape can be on at a time — they share the
 * ground, so layers that use it share the `ground` slot and switching one on
 * switches the other off. `particles` draws a vector field as drifting
 * trails in the air above, in its own slot, and combines with any drape.
 *
 * The functions all take `ctx`: `{ dayBounds: [fromMs, toMs], option }`, where
 * `option` is the layer's chosen option value (a pollutant, say) or null.
 */

import { sampleAt, windAt, rangeOf, compassName, sampleClimatology, sampleRaster } from './field.js';
import {
  HEAT_STOPS, EAQI_BANDS, AIR_METRICS, bandOf, heatStops, airStops,
} from './scales.js';

const ICONS = {
  heat: '<path d="M10 4.5a2 2 0 0 1 4 0v9.2a3.6 3.6 0 1 1-4 0Z"/><path d="M12 9v6.2"/><circle cx="12" cy="16.9" r="1.3" class="fill"/>',
  air: '<path d="M7.5 17h9.2a3.8 3.8 0 0 0 .3-7.6 5.5 5.5 0 0 0-10.5-1A3.8 3.8 0 0 0 7.5 17Z"/><circle cx="8" cy="20.6" r=".9" class="fill"/><circle cx="12" cy="20.6" r=".9" class="fill"/><circle cx="16" cy="20.6" r=".9" class="fill"/>',
  wind: '<path d="M3 8.5h10.5a2.5 2.5 0 1 0-2.5-2.5"/><path d="M3 12.5h14.5a2.5 2.5 0 1 1-2.5 2.5"/><path d="M3 16.5h7a2 2 0 1 1-2 2"/>',
};

/* ── helpers shared by the entries ───────────────────────────────── */

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const monthName = mf => MONTHS[((Math.round(mf) % 12) + 12) % 12];

/** Anomaly span of the surface-heat legend, °C either side of the tile median. */
const HEAT_SPAN = 6;

/** Tile median for a month fraction, blended like the pixels are. */
function blendedMedian(r, monthFrac) {
  const mf = ((monthFrac % 12) + 12) % 12;
  const m0 = Math.floor(mf);
  const m1 = (m0 + 1) % 12;
  const a = r.tileMedian(m0);
  const b = r.tileMedian(m1);
  if (a === null && b === null) return null;
  if (a === null) return b;
  if (b === null) return a;
  return a * (1 - (mf - m0)) + b * (mf - m0);
}

function nearestNode(s, lat, lon) {
  let best = null;
  let d = Infinity;
  for (const n of s.nodes || []) {
    const dd = (n.requested[0] - lat) ** 2 + (n.requested[1] - lon) ** 2;
    if (dd < d) { d = dd; best = n; }
  }
  return best;
}

const airLegend = ([lo, hi], { option }) => {
  const { unit } = AIR_METRICS[option];
  return { lo: String(lo), hi: unit ? `${hi} ${unit}` : String(hi) };
};

const metricChoices = keys => keys.map(key => ({ key, label: AIR_METRICS[key].label, title: AIR_METRICS[key].long }));

/**
 * The registry.
 *
 * A layer with `modes` is one switch with several ways of answering: each
 * mode names the source it reads, and `ctx.mode` tells the functions which
 * one is live. Temperature is the air for the selected day and hour, or
 * the surface the satellite saw that month; air quality is the model for
 * the selected day, or its five-year habit. The pane shows the mode strip
 * and the rail button just switches the layer on.
 */
export const LAYERS = [
  {
    id: 'temperature',
    label: 'Temperature',
    tip: 'Temperature<em>Air for the date, or the surface Landsat sees</em>',
    icon: ICONS.heat,
    modes: {
      pref: 'tempMode',
      fallback: 'date',
      choices: [
        { key: 'date', label: 'This date', title: 'Air temperature for the selected day and hour', source: 'weather' },
        { key: 'surface', label: 'Surface', title: 'Landsat surface temperature, median by month at 30 m', source: 'tile-heat' },
      ],
    },
    render: 'drape',
    slot: 'ground',
    alpha: 0.5,
    field(s, lat, lon, t, ctx) {
      return ctx.mode === 'surface'
        ? sampleRaster(s, lat, lon, ctx.monthFrac)
        : sampleAt(s, 'temperature_2m', lat, lon, t);
    },
    /**
     * Air: the legend spans the selected day, whole degrees, at least four
     * of them. Surface: an anomaly around the tile's median for the month,
     * because "hotter than the rest of the city" is the question.
     */
    domain(s, ctx) {
      if (ctx.mode === 'surface') {
        const med = blendedMedian(s, ctx.monthFrac);
        return med === null ? null : [med - HEAT_SPAN, med + HEAT_SPAN];
      }
      const [from, to] = ctx.dayBounds;
      const range = rangeOf(s, 'temperature_2m', from, to);
      if (!range) return null;
      let { min, max } = range;
      if (max - min < 4) { const mid = (min + max) / 2; min = mid - 2; max = mid + 2; }
      return [Math.floor(min), Math.ceil(max)];
    },
    stops: () => heatStops(),
    legend: ([lo, hi]) => ({ lo: `${lo.toFixed(0)} °C`, hi: `${hi.toFixed(0)} °C` }),
    reading(s, lat, lon, t, ctx) {
      if (ctx.mode === 'surface') {
        const v = sampleRaster(s, lat, lon, ctx.monthFrac);
        if (Number.isNaN(v)) return null;
        const med = blendedMedian(s, ctx.monthFrac);
        const delta = med === null ? null : v - med;
        return {
          value: v,
          text: `${v.toFixed(1)} °C`,
          sub: `${delta === null ? '' : `${delta >= 0 ? '+' : ''}${delta.toFixed(1)} °C vs the area · `}surface, ${monthName(ctx.monthFrac)} mornings`,
        };
      }
      const temp = sampleAt(s, 'temperature_2m', lat, lon, t);
      if (Number.isNaN(temp)) return null;
      const feels = sampleAt(s, 'apparent_temperature', lat, lon, t);
      return {
        value: temp,
        text: `${temp.toFixed(1)} °C`,
        sub: Number.isNaN(feels) ? 'air' : `air · feels like ${feels.toFixed(0)} °C`,
      };
    },
  },
  {
    id: 'air',
    label: 'Air',
    tip: 'Air quality<em>The model for the date, or its five-year habit</em>',
    icon: ICONS.air,
    modes: {
      pref: 'airMode',
      fallback: 'date',
      choices: [
        { key: 'date', label: 'This date', title: 'CAMS air quality for the selected day and hour', source: 'air' },
        { key: 'typical', label: 'Typical', title: 'Five-year mean for this month and hour of day', source: 'tile-air' },
      ],
    },
    /** Which pollutant. The index is not in the climatology, so the choices depend on the mode. */
    options: mode => ({
      pref: 'airMetric',
      fallback: mode === 'typical' ? 'pm2_5' : 'european_aqi',
      choices: metricChoices(mode === 'typical'
        ? ['pm2_5', 'pm10', 'nitrogen_dioxide', 'ozone']
        : Object.keys(AIR_METRICS)),
    }),
    render: 'drape',
    slot: 'ground',
    alpha: 0.55,
    field(s, lat, lon, t, ctx) {
      return ctx.mode === 'typical'
        ? sampleClimatology(s, ctx.option, lat, lon, ctx.monthFrac, ctx.hourFrac)
        : sampleAt(s, ctx.option, lat, lon, t);
    },
    domain: (s, { option }) => [0, AIR_METRICS[option].top],
    stops: ({ option }) => airStops(option),
    legend: airLegend,
    reading(s, lat, lon, t, ctx) {
      const { option } = ctx;
      const m = AIR_METRICS[option];
      if (ctx.mode === 'typical') {
        const value = sampleClimatology(s, option, lat, lon, ctx.monthFrac, ctx.hourFrac);
        if (Number.isNaN(value)) return null;
        const annual = nearestNode(s, lat, lon)?.[option];
        const who = annual?.days_over_who;
        return {
          value,
          text: `${value.toFixed(0)} ${m.unit}`,
          sub: `${monthName(ctx.monthFrac)}, ${String(Math.floor(ctx.hourFrac)).padStart(2, '0')}:00 typical` +
            (annual ? ` · year mean ${annual.annual_mean} · ${Math.round((who ?? 0) * 100)}% of days over WHO` : ''),
          band: bandOf(option, value),
        };
      }
      const value = sampleAt(s, option, lat, lon, t);
      if (Number.isNaN(value)) return null;
      return {
        value,
        text: m.unit ? `${value.toFixed(0)} ${m.unit}` : value.toFixed(0),
        sub: m.long,
        band: bandOf(option, value),
      };
    },
  },
  {
    id: 'wind',
    label: 'Wind',
    tip: 'Wind<em>Particles drifting with the 10 m wind, between the buildings where a tile exists</em>',
    icon: ICONS.wind,
    source: 'weather',
    /** Optional extras: fetched when the layer is on, absent without complaint. */
    also: ['tile-wind'],
    render: 'particles',
    slot: 'sky',
    /** The particles renderer reads the vector field itself; this is the pane's number. */
    reading(s, lat, lon, t) {
      const w = windAt(s, lat, lon, t);
      if (!w) return null;
      return {
        value: w.speed,
        text: `${w.speed.toFixed(1)} m/s`,
        sub: `from ${compassName(w.from)} · ${w.from.toFixed(0)}°`,
        /** Where the air is going, for a glyph that points like the particles. */
        heading: (w.from + 180) % 360,
      };
    },
  },
];

/** The mode a layer is in, from its preference, or null for a single-source layer. */
export function modeOf(layer, prefs) {
  if (!layer.modes) return null;
  const value = prefs[layer.modes.pref];
  return layer.modes.choices.some(c => c.key === value) ? value : layer.modes.fallback;
}

/** The source a layer reads in its current mode. */
export function sourceOf(layer, prefs) {
  if (!layer.modes) return layer.source;
  const mode = modeOf(layer, prefs);
  return layer.modes.choices.find(c => c.key === mode).source;
}

/** The option group, which may depend on the mode. */
export const optionsOf = (layer, mode) =>
  (typeof layer.options === 'function' ? layer.options(mode) : layer.options) || null;

export const layerById = id => LAYERS.find(l => l.id === id);

/** The layers that share a slot with this one, and so cannot be on with it. */
export const rivalsOf = layer => LAYERS.filter(l => l !== layer && l.slot === layer.slot);

export { HEAT_STOPS, EAQI_BANDS, AIR_METRICS };
