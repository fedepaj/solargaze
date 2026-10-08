/**
 * Where the numbers come from.
 *
 * A source is a hourly model on a regular grid, reachable through Open-Meteo:
 * which spacing to ask on, which variables, which endpoint serves which day,
 * and what to derive once the answer lands. Layers (layers.js) name a source
 * and never care how it was fetched; the engine (../atmo.js) fetches the union
 * of what the enabled layers need.
 *
 * Adding a source is adding an entry here. Adding a whole other provider —
 * a satellite raster, a sensor network — means a different `fetch` shape and
 * is the point where openmeteo.js gets a sibling, not a bigger switch.
 *
 * Pure: no fetch, no DOM. The date policy is the part worth testing.
 */

import { catalog, tileSourceId, KNOWN_KINDS } from './catalog.js';

export const DAY_MS = 86400000;
const dayNumber = ({ y, m, d }) => Math.round(Date.UTC(y, m - 1, d) / DAY_MS);

export function shiftDate({ y, m, d }, days) {
  const t = new Date(Date.UTC(y, m - 1, d) + days * DAY_MS);
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate() };
}

/** The same calendar date a year earlier; a leap day becomes the 28th. */
export function lastYear({ y, m, d }) {
  const yy = y - 1;
  const leap = (yy % 4 === 0 && yy % 100 !== 0) || yy % 400 === 0;
  return { y: yy, m, d: m === 2 && d === 29 && !leap ? 28 : d };
}

export const isoDate = ({ y, m, d }) =>
  `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;

/**
 * Wind arrives as a speed and a meteorological "from" bearing. Interpolating
 * those directly is wrong across the 0°/360° seam and averages two opposing
 * winds into a strong one; components do not have that problem, so they are
 * derived once and the raw pair is dropped.
 */
function deriveWindComponents(vars, length) {
  const u = new Float32Array(length).fill(NaN);
  const v = new Float32Array(length).fill(NaN);
  const speed = vars.wind_speed_10m;
  const dir = vars.wind_direction_10m;
  for (let k = 0; k < length; k++) {
    if (Number.isNaN(speed[k]) || Number.isNaN(dir[k])) continue;
    const rad = dir[k] * Math.PI / 180;
    // A wind *from* the north blows southward: u = -sin, v = -cos.
    u[k] = -speed[k] * Math.sin(rad);
    v[k] = -speed[k] * Math.cos(rad);
  }
  vars.wind_u = u;
  vars.wind_v = v;
  delete vars.wind_speed_10m;
  delete vars.wind_direction_10m;
}

/**
 * Which day to ask for, and where — see the essay in resolveDate below.
 * Every request spans the day before and after too (a local day is two UTC
 * days), so the horizons leave room for that.
 */
export const SOURCES = {
  weather: {
    id: 'weather',
    label: 'Heat and wind',
    /** Open-Meteo's best-match models sit on ~0.0625° over Europe and North America. */
    step: 0.0625,
    vars: ['temperature_2m', 'apparent_temperature', 'wind_speed_10m', 'wind_direction_10m'],
    params: { wind_speed_unit: 'ms' },
    endpoints: {
      forecast: 'https://api.open-meteo.com/v1/forecast',
      archive: 'https://archive-api.open-meteo.com/v1/archive',
    },
    resolve(date, today) {
      const delta = dayNumber(date) - dayNumber(today);
      if (delta > 14) return { date: lastYear(date), endpoint: 'archive', proxy: 'last-year' };
      // The forecast endpoint answers for past days with nulls once they are
      // more than about two months old (measured: data at -60, none at -75),
      // while the archive has a day from about five days on: so anything
      // older than a week goes to the archive, the better record anyway.
      if (delta >= -7) return { date, endpoint: 'forecast', proxy: null };
      return { date, endpoint: 'archive', proxy: null };
    },
    derive: deriveWindComponents,
    note: 'Weather on a ~7 km model grid; wind is the 10 m regional wind, not the flow between these buildings.',
  },
  air: {
    id: 'air',
    label: 'Air quality',
    /** CAMS European ensemble, 0.1°; the global model is coarser still. */
    step: 0.1,
    // Pollen comes with the air, from the same CAMS model and the same call:
    // forecast about four days out, archived from 2022 (mugwort and ragweed
    // from 2024).
    vars: ['pm2_5', 'pm10', 'nitrogen_dioxide', 'ozone', 'european_aqi',
      'grass_pollen', 'birch_pollen', 'alder_pollen', 'olive_pollen', 'mugwort_pollen', 'ragweed_pollen'],
    params: {},
    endpoints: {
      air: 'https://air-quality-api.open-meteo.com/v1/air-quality',
    },
    resolve(date, today) {
      const delta = dayNumber(date) - dayNumber(today);
      if (delta > 4) return { date: lastYear(date), endpoint: 'air', proxy: 'last-year' };
      if (date.y < 2013) return null;
      return { date, endpoint: 'air', proxy: null };
    },
    derive: null,
    note: 'Air quality and pollen on a ~11 km grid (CAMS); the pollen bands are those most European pollen services use, a guide rather than a diagnosis.',
  },
};

/**
 * Precomputed tiles (pipeline/, data/tiles/), one source per product of the
 * catalog (atmo/catalog.js). No endpoint and no date policy: the product is
 * a climatology, and the sliders index it directly. `kind: 'tile'` is what
 * tells the engine to go through atmo/tiles.js; `dataKind` how to read it.
 * Called again when the real catalog arrives; returns the ids it added.
 */
export function registerTileSources(cat = catalog()) {
  const added = [];
  for (const [name, card] of Object.entries(cat.products)) {
    if (!KNOWN_KINDS.has(card.kind)) continue;
    const id = tileSourceId(name);
    if (!SOURCES[id]) added.push(id);
    SOURCES[id] = { id, kind: 'tile', product: name, dataKind: card.kind, label: card.title, note: card.note };
  }
  return added;
}
registerTileSources();

/**
 * Decide which day to ask a source for.
 *
 * The app lets you pick any day of the year; the atmosphere does not oblige.
 * Weather is forecast about two weeks out, air quality four or five days, and
 * both are archived back for years. So a date inside the forecast horizon gets
 * that forecast; a date in the past gets the archive; and a date beyond the
 * horizon — next June, picked in January — gets the same calendar date a year
 * earlier, flagged as a stand-in. That is not a prediction, and the pane says
 * so; it is the best single day the data can offer for "what is this place
 * like in June".
 */
export const resolveDate = (source, date, today) => source.resolve(date, today);
