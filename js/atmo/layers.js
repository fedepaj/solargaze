/**
 * The layers, as data.
 *
 * A layer is a *theme* — heat, air, wind — with one switch, one rail button
 * and one row in ANALYZE. A theme drawn from the precomputed tiles has
 * *variants*, one per product the pipeline's catalog shows under it (surface
 * heat in the morning, at night …; atmo/catalog.js): the variants are the
 * layer's `modes`, and what a variant draws and says is decided by its kind
 * of data (KINDS below), worded by its catalog card. So a new product of a
 * kind already here is a new variant with no change to this file; a new kind
 * of data is an entry in KINDS and a loader in tiles.js; a new kind of
 * picture (contours, columns, a volume) is a renderer in RENDERERS in
 * ../atmo.js, named here.
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

import {
  sampleAt, windAt, rangeOf, compassName, sampleClimatology, sampleRaster, sampleRasterSet,
  sampleStreet, streetTileAt, streetRatio, STREET_MODELLED,
} from './field.js';
import {
  HEAT_STOPS, EAQI_BANDS, AIR_METRICS, bandOf, heatStops, airStops, POLLEN, pollenBand, pollenStops,
  noiseBand, noiseStops, WHO_ROAD_LDEN, skyBand, skyStops, skyRatio,
} from './scales.js';
import { FALLBACK, cardOf, tileSourceId, variantsOf, catalog } from './catalog.js';

const ICONS = {
  heat: '<path d="M10 4.5a2 2 0 0 1 4 0v9.2a3.6 3.6 0 1 1-4 0Z"/><path d="M12 9v6.2"/><circle cx="12" cy="16.9" r="1.3" class="fill"/>',
  air: '<path d="M7.5 17h9.2a3.8 3.8 0 0 0 .3-7.6 5.5 5.5 0 0 0-10.5-1A3.8 3.8 0 0 0 7.5 17Z"/><circle cx="8" cy="20.6" r=".9" class="fill"/><circle cx="12" cy="20.6" r=".9" class="fill"/><circle cx="16" cy="20.6" r=".9" class="fill"/>',
  noise: '<path d="M4 9.5h3.2L12 5.5v13l-4.8-4H4Z"/><path d="M15.5 9a4.2 4.2 0 0 1 0 6M18.2 6.5a8 8 0 0 1 0 11"/>',
  pollen: '<circle cx="12" cy="9.2" r="2"/><path d="M12 7.2c-.3-2.6.6-4 1.9-4s1.9 1.7.3 3.7M14 9.2c2.6-.3 4 .6 4 1.9s-1.7 1.9-3.7.3M12 11.2c.3 2.6-.6 4-1.9 4s-1.9-1.7-.3-3.7M10 9.2c-2.6.3-4-.6-4-1.9s1.7-1.9 3.7-.3"/><path d="M12 11.2V21M12 17.5c1.5-1.7 3.3-2.2 4.6-1.7"/>',
  light: '<path d="M14.5 4.5a7.5 7.5 0 1 0 5 13 6 6 0 0 1-5-13Z"/><path d="M6 4.5v3M4.5 6h3M18.5 10v2M17.5 11h2"/>',
  wind: '<path d="M3 8.5h10.5a2.5 2.5 0 1 0-2.5-2.5"/><path d="M3 12.5h14.5a2.5 2.5 0 1 1-2.5 2.5"/><path d="M3 16.5h7a2 2 0 1 1-2 2"/>',
};

/* ── helpers shared by the entries ───────────────────────────────── */

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const monthName = mf => MONTHS[((Math.round(mf) % 12) + 12) % 12];

/** Anomaly span of a surface-heat legend, °C either side of the tile median, for a card with no fixed scale. */
const HEAT_SPAN = 6;

/** A raster or the centre tile of a raster-set, for medians and meta. */
const centreOf = s => (s.kind === 'raster-set' ? s.centre : s);
const readSurface = (s, lat, lon, mf) =>
  (s.kind === 'raster-set' ? sampleRasterSet(s, lat, lon, mf) : sampleRaster(s, lat, lon, mf));

/**
 * The median of the tiles in view for a month fraction: what the colours
 * are centred on, so that looking at a city the contrast is within the city
 * rather than between it and the fields of the tile's edges. A single
 * raster is its own view.
 */
function viewMedian(s, monthFrac, viewRect = null) {
  // What the camera actually sees, sampled on a 40 × 40 lattice: a view of a
  // city's centre is then read against that centre, not against the fields
  // its tiles also hold.
  if (viewRect && s.kind === 'raster-set') {
    const [w, south, e, n] = viewRect;
    const vals = [];
    for (let i = 0; i < 40; i++) {
      for (let j = 0; j < 40; j++) {
        const v = sampleRasterSet(s, south + ((i + 0.5) / 40) * (n - south), w + ((j + 0.5) / 40) * (e - w), monthFrac);
        if (!Number.isNaN(v)) vals.push(v);
      }
    }
    if (vals.length >= 80) {
      vals.sort((a, b) => a - b);
      return vals[Math.floor(vals.length / 2)];
    }
  }
  const tiles = s.kind === 'raster-set' && s.tiles?.length ? s.tiles : [centreOf(s)];
  const meds = tiles.map(t => blendedMedian(t, monthFrac)).filter(v => v !== null).sort((a, b) => a - b);
  return meds.length ? meds[Math.floor(meds.length / 2)] : null;
}

/** Tile median for a month fraction, blended like the pixels are. */
function blendedMedian(s, monthFrac) {
  const r = centreOf(s);
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
 * What each kind of data draws and says. Every function takes the variant's
 * catalog card last, for the words: `card.when` ("mornings", "nights") says
 * which part of the day a surface reading stands for.
 */
export const KINDS = {
  /** Twelve months of a raster, blended between months like the pixels are (Landsat, ECOSTRESS). */
  'raster-months': {
    field: (s, lat, lon, t, ctx) => readSurface(s, lat, lon, ctx.monthFrac),
    /**
     * Around the median of the tiles in view for the month, the card's `span_c` either side
     * (narrower at night, when the ground spreads less): within a city the
     * surfaces differ by ten degrees, across a continent and a year by
     * seventy, and a fixed scale for the second flattens the first. The
     * centre is a whole degree, so the legend does not twitch as the date
     * moves; the reading says the temperature itself. A card can still ask
     * for a fixed scale (`scale_c`).
     */
    domain(s, ctx, card) {
      if (card?.scale_c) return card.scale_c;
      const med = viewMedian(s, ctx.monthFrac, ctx.viewRect);
      const span = card?.span_c ?? HEAT_SPAN;
      return med === null ? null : [Math.round(med) - span, Math.round(med) + span];
    },
    stops: () => heatStops(),
    legend: ([lo, hi]) => ({ lo: `${lo.toFixed(0)} °C`, hi: `${hi.toFixed(0)} °C` }),
    reading(s, lat, lon, t, ctx, card) {
      const v = readSurface(s, lat, lon, ctx.monthFrac);
      if (Number.isNaN(v)) return null;
      const med = blendedMedian(s, ctx.monthFrac);
      const delta = med === null ? null : v - med;
      return {
        value: v,
        text: `${v.toFixed(1)} °C`,
        sub: `${delta === null ? '' : `${delta >= 0 ? '+' : ''}${delta.toFixed(1)} °C vs the area · `}${monthName(ctx.monthFrac)} ${card?.when || ''}`.trim(),
      };
    },
  },
  /** One raster, no months (noise): a fixed scale, read in its own unit. */
  'raster-static': {
    field: (s, lat, lon, t, ctx) => readSurface(s, lat, lon, 0),
    domain: (s, ctx, card) => card?.scale_c ?? [40, 80],
    stops: (ctx, card) => noiseStops(card?.scale_c ?? [40, 80]),
    legend: ([lo, hi], ctx, card) => ({ lo: `${lo} ${card?.unit || ''}`.trim(), hi: `${hi} ${card?.unit || ''}`.trim() }),
    reading(s, lat, lon, t, ctx) {
      const v = readSurface(s, lat, lon, 0);
      if (Number.isNaN(v)) return null;
      const over = v - WHO_ROAD_LDEN;
      return {
        value: v,
        text: `${v.toFixed(0)} dB`,
        sub: `day–evening–night level · ${Math.abs(over).toFixed(0)} dB ${over >= 0 ? 'above' : 'below'} the WHO guideline for road traffic (${WHO_ROAD_LDEN})`,
        band: noiseBand(v),
      };
    },
  },
  /**
   * The night sky's zenith brightness, one raster, no months (VIIRS by year):
   * a fixed scale in mag/arcsec², read with what is left of the Milky Way.
   */
  'sky-brightness': {
    field: (s, lat, lon, t, ctx) => readSurface(s, lat, lon, 0),
    domain: (s, ctx, card) => card?.scale_c ?? [16.5, 22],
    stops: (ctx, card) => skyStops(card?.scale_c ?? [16.5, 22]),
    legend: ([lo, hi]) => ({ lo: `${lo} city`, hi: `${hi} mag/arcsec²` }),
    reading(s, lat, lon) {
      const v = readSurface(s, lat, lon, 0);
      if (Number.isNaN(v)) return null;
      const band = skyBand(v);
      const ratio = skyRatio(v);
      const year = centreOf(s).meta?.year;
      return {
        value: v,
        text: `${v.toFixed(2)} mag/arcsec²`,
        sub: `Bortle ${band.bortle} · Milky Way ${band.milkyWay} · sky ${ratio < 1.1 ? 'as nature made it' : `${ratio < 10 ? ratio.toFixed(1) : ratio.toFixed(0)}× natural`}${year ? ` · ${year} lights` : ''}`,
        band,
      };
    },
  },
  /** CAMS by month and hour, times a 50 m ratio per pollutant. */
  'street-air': {
    field: (s, lat, lon, t, ctx) => sampleStreet(s, ctx.option, lat, lon, ctx.monthFrac, ctx.hourFrac),
    domain: (s, { option }) => [0, AIR_METRICS[option].top],
    stops: ({ option }) => airStops(option),
    legend: airLegend,
    reading(s, lat, lon, t, ctx) {
      const { option } = ctx;
      const m = AIR_METRICS[option];
      const value = sampleStreet(s, option, lat, lon, ctx.monthFrac, ctx.hourFrac);
      if (Number.isNaN(value)) return null;
      const tile = streetTileAt(s, lat, lon);
      const cams = sampleClimatology(tile.clim, option, lat, lon, ctx.monthFrac, ctx.hourFrac);
      const how = option === 'ozone' ? 'from NO₂ by titration'
        : STREET_MODELLED.includes(option) ? `×${streetRatio(tile, option, lat, lon).toFixed(2)} on CAMS for the roads and buildings here`
          : 'CAMS: no street-scale gain for this one';
      return {
        value,
        text: `${value.toFixed(0)} ${m.unit}`,
        sub: `${monthName(ctx.monthFrac)}, ${String(Math.floor(ctx.hourFrac)).padStart(2, '0')}:00 typical · ${how}` +
          (Number.isNaN(cams) ? '' : ` · CAMS alone ${cams.toFixed(0)}`),
        band: bandOf(option, value),
      };
    },
  },
};

/**
 * A theme drawn from the tiles. Its variants come from the catalog
 * (applyCatalog); each call goes to the kind of the variant in use, which
 * the engine passes as `ctx.mode` — the product's name.
 */
function theme(spec) {
  const kindOf = ctx => KINDS[cardOf(ctx.mode)?.kind] || null;
  return {
    ...spec,
    field: (s, lat, lon, t, ctx) => kindOf(ctx)?.field(s, lat, lon, t, ctx) ?? NaN,
    domain: (s, ctx) => kindOf(ctx)?.domain(s, ctx, cardOf(ctx.mode)) ?? null,
    stops: ctx => (kindOf(ctx) || KINDS[spec.defaultKind]).stops(ctx, cardOf(ctx.mode)),
    legend: (domain, ctx) => (kindOf(ctx) || KINDS[spec.defaultKind]).legend(domain, ctx, cardOf(ctx.mode)),
    reading: (s, lat, lon, t, ctx) => kindOf(ctx)?.reading(s, lat, lon, t, ctx, cardOf(ctx.mode)) ?? null,
  };
}

/**
 * The registry.
 *
 * The map shows habits, which are what a place is like: the surface heat
 * Landsat sees by month, the air by month and hour at 50 m, the wind
 * threaded between the buildings. What a particular day was like — its
 * weather, its air — is read at the pin instead (the weather chip, and the
 * reading-only `dayair` entry, which has no renderer and no rail button).
 *
 * A layer may still have `modes` — one switch, several sources, `ctx.mode`
 * saying which is live — and the engine and the pane support them; none
 * needs them now.
 */
export const LAYERS = [
  theme({
    id: 'temperature',
    theme: 'heat',
    label: 'Heat',
    tip: 'Surface heat<em>What the ground reads by month: a clear morning while the sun is up, a clear night once it is down</em>',
    icon: ICONS.heat,
    defaultKind: 'raster-months',
    /** Morning or night by the sun at the point, not by a switch. */
    byDaypart: true,
    render: 'drape',
    slot: 'ground',
    alpha: 0.5,
  }),
  theme({
    id: 'air',
    theme: 'air',
    label: 'Air',
    tip: 'Air quality<em>The five-year habit for this month and hour, street by street at 50 m</em>',
    icon: ICONS.air,
    defaultKind: 'street-air',
    /** Which pollutant the ground is tinted by. */
    options: () => ({
      pref: 'airMetric',
      fallback: 'nitrogen_dioxide',
      choices: metricChoices(['nitrogen_dioxide', 'pm10', 'pm2_5', 'ozone']),
    }),
    render: 'drape',
    slot: 'ground',
    alpha: 0.55,
  }),
  {
    /**
     * Not a layer of the scene: the air on the selected day and hour, from
     * the CAMS forecast or archive, read at the pin for the pane. The map
     * shows the habit; this says what that day was (or is forecast) like.
     */
    id: 'dayair',
    label: 'On the day',
    /** Read inside the Air card. */
    attachTo: 'air',
    tip: 'Air quality on the selected day and hour, CAMS forecast or archive',
    icon: ICONS.air,
    source: 'air',
    rail: false,
    reading(s, lat, lon, t) {
      const aqi = sampleAt(s, 'european_aqi', lat, lon, t);
      if (Number.isNaN(aqi)) return null;
      const parts = ['nitrogen_dioxide', 'pm10', 'pm2_5', 'ozone']
        .map(k => [AIR_METRICS[k].label, sampleAt(s, k, lat, lon, t)])
        .filter(([, v]) => !Number.isNaN(v))
        .map(([label, v]) => `${label} ${v.toFixed(0)}`);
      return {
        value: aqi,
        text: `${aqi.toFixed(0)}`,
        sub: `European AQI · ${parts.join(' · ')} µg/m³`,
        band: bandOf('european_aqi', aqi),
      };
    },
  },
  theme({
    id: 'noise',
    theme: 'noise',
    label: 'Noise',
    tip: 'Road noise<em>The day–evening–night level of road traffic, at 10 m, screened by the buildings</em>',
    icon: ICONS.noise,
    defaultKind: 'raster-static',
    render: 'drape',
    slot: 'ground',
    alpha: 0.55,
  }),
  {
    /**
     * Pollen on the selected day and hour, CAMS forecast or archive: a tint
     * on the ground for one taxon, the reading at the point with the others
     * that are in the air. Out of season it is none, and says so.
     */
    id: 'pollen',
    label: 'Pollen',
    tip: 'Pollen<em>Grains in the air on the selected day and hour, by taxon — CAMS forecast or archive</em>',
    icon: ICONS.pollen,
    source: 'air',
    options: () => ({
      pref: 'pollenTaxon',
      fallback: 'grass_pollen',
      choices: Object.entries(POLLEN).map(([key, p]) => ({ key, label: p.label, title: p.long })),
    }),
    render: 'drape',
    slot: 'ground',
    alpha: 0.5,
    field(s, lat, lon, t, ctx) {
      const v = sampleAt(s, ctx.option, lat, lon, t);
      return v >= 1 ? v : NaN;
    },
    domain: (s, { option }) => [0, POLLEN[option].top],
    stops: ({ option }) => pollenStops(option),
    legend: ([, hi]) => ({ lo: '0', hi: `${hi} grains/m³` }),
    reading(s, lat, lon, t, ctx) {
      const v = sampleAt(s, ctx.option, lat, lon, t);
      if (Number.isNaN(v)) return { value: 0, text: '—', sub: 'no pollen record for this day (from 2022 on)' };
      const others = Object.keys(POLLEN)
        .filter(k => k !== ctx.option)
        .map(k => [POLLEN[k].label, sampleAt(s, k, lat, lon, t)])
        .filter(([, x]) => x >= 1)
        .sort((a, b) => b[1] - a[1])
        .map(([label, x]) => `${label} ${x.toFixed(0)}`);
      const band = pollenBand(ctx.option, v);
      return {
        value: v,
        text: `${v < 1 ? 0 : v.toFixed(0)} grains/m³`,
        sub: v < 1 && !others.length ? 'none in the air'
          : others.length ? `also ${others.join(' · ')}` : 'no other pollen in the air',
        band,
      };
    },
  },
  theme({
    id: 'sky',
    theme: 'light',
    label: 'Light',
    tip: 'Light pollution<em>How bright the sky overhead is on a clear, moonless night, from the year\'s night lights</em>',
    icon: ICONS.light,
    defaultKind: 'sky-brightness',
    render: 'drape',
    slot: 'ground',
    alpha: 0.55,
  }),
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

/**
 * Give each theme its variants from a catalog: the products shown under it,
 * best first, as modes `{ key: product, label, title, source }` with the
 * preference `<theme>Variant`. A theme with no variant this app can read is
 * left without modes and without a source, and the rail and the pane skip it.
 */
export function applyCatalog(cat = catalog()) {
  for (const layer of LAYERS.filter(l => l.theme)) {
    const variants = variantsOf(layer.theme, cat);
    layer.modes = variants.length ? {
      pref: `${layer.theme}Variant`,
      fallback: variants[0].name,
      choices: variants.map(({ name, card }) => ({
        key: name, label: card.label, title: card.title, source: tileSourceId(name),
      })),
    } : null;
    layer.source = variants.length ? tileSourceId(variants[0].name) : null;
  }
}
applyCatalog(FALLBACK);

/** Is there anything for this layer to show — a source, or a variant with one? */
export const available = layer => !!(layer.source || layer.modes);

/**
 * Is the sun up at the point? Set by the engine as the clock moves; a theme
 * whose variants are parts of the day (heat: morning, night) follows it
 * rather than a preference — the date and time are the one selector.
 */
let daylight = true;
export const setDaylight = up => { const changed = up !== daylight; daylight = up; return changed; };

/** The mode a layer is in, from the hour or its preference, or null for a single-source layer. */
export function modeOf(layer, prefs) {
  if (!layer.modes) return null;
  if (layer.byDaypart) {
    const want = daylight ? 'day' : 'night';
    // A catalog older than `daypart` says it in words (`when`: mornings, nights).
    const part = key => { const c = cardOf(key); return c?.daypart ?? (c?.when === 'nights' ? 'night' : 'day'); };
    return layer.modes.choices.find(c => part(c.key) === want)?.key ?? layer.modes.fallback;
  }
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
