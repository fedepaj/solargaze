/**
 * The atmospheric field: heat, air quality and wind as a small regular grid of
 * hourly values around the pin, sampled anywhere inside it by bilinear
 * interpolation in space and linear interpolation in time.
 *
 * Pure. No Cesium, no DOM, no fetch — the numbers here are what the legend
 * prints and what the drape is painted from, so `node --test` holds them to
 * account the way it does the solar equations.
 *
 * On resolution, because it decides what is honest to draw: Open-Meteo's
 * weather models sit on a grid of roughly 0.0625° (7 km) over Europe and North
 * America and 0.1–0.25° elsewhere; the CAMS air-quality models on 0.1° (11 km)
 * over Europe and 0.4° globally. Nothing this app draws is finer than that —
 * the smooth gradient over a neighbourhood is interpolation between model
 * cells, not measurement. The pane says so.
 */

/** Nodes per side. Seven at model spacing spans a metropolitan area. */
export const GRID_N = 7;

/**
 * The grid a place belongs to.
 *
 * The centre is snapped to the model spacing so that nudging the pin by a
 * street does not produce a new grid — and a new request — every time. Nodes
 * are listed row-major, south to north then west to east, which is also the
 * order the locations go out in and come back in.
 */
export function gridFor(source, lat, lon, n = GRID_N) {
  const { step } = source;
  const half = (n - 1) / 2;
  const cLat = Math.round(lat / step) * step;
  const cLon = Math.round(lon / step) * step;
  const lat0 = cLat - half * step;
  const lon0 = cLon - half * step;
  const lats = [];
  const lons = [];
  for (let i = 0; i < n; i++) {
    for (let j = 0; j < n; j++) {
      lats.push(round6(lat0 + i * step));
      lons.push(round6(lon0 + j * step));
    }
  }
  return {
    source: source.id, step, n, rows: n, cols: n, lats, lons,
    south: round6(lat0), north: round6(lat0 + (n - 1) * step),
    west: round6(lon0), east: round6(lon0 + (n - 1) * step),
    key: `${source.id}|${cLat.toFixed(4)}|${cLon.toFixed(4)}`,
  };
}

const round6 = x => Math.round(x * 1e6) / 1e6;

/**
 * Fold Open-Meteo's per-location responses into typed arrays indexed
 * `[hour * nodes + node]`. Missing values become NaN and stay NaN — a hole in
 * the model is drawn as a hole, not as zero. The source's `derive` then gets
 * a chance to add columns computed from the raw ones.
 */
export function buildSeries(source, grid, locations, meta = {}) {
  const nodes = grid.n * grid.n;
  if (locations.length !== nodes) {
    throw new Error(`expected ${nodes} locations, got ${locations.length}`);
  }
  // Open-Meteo tags every location after the first with its index; the first
  // carries none. Sorting on that makes the fold independent of response order.
  const ordered = [...locations].sort((a, b) => (a.location_id ?? 0) - (b.location_id ?? 0));

  const times = ordered[0].hourly?.time || [];
  const hours = times.length;
  if (!hours) throw new Error('no hourly data');
  const t0 = Date.parse(`${times[0]}Z`);

  const vars = {};
  for (const name of source.vars) vars[name] = new Float32Array(hours * nodes).fill(NaN);

  ordered.forEach((loc, node) => {
    const hourly = loc.hourly || {};
    for (const name of source.vars) {
      const column = hourly[name];
      if (!column) continue;
      for (let h = 0; h < hours; h++) {
        const v = column[h];
        if (typeof v === 'number') vars[name][h * nodes + node] = v;
      }
    }
  });

  source.derive?.(vars, hours * nodes);

  return { source: source.id, grid, t0, hours, vars, ...meta };
}

/* ── sampling ──────────────────────────────────────────────────────── */

/**
 * The value of `name` at a place and a UTC instant (ms). NaN when the model
 * has no value there, or the instant is outside the fetched window.
 */
export function sampleAt(series, name, lat, lon, t) {
  const arr = series.vars[name];
  if (!arr) return NaN;
  const { grid } = series;
  const nodes = grid.rows * grid.cols;

  const h = (t - series.t0) / 3600000;
  if (h < -0.5 || h > series.hours - 0.5) return NaN;
  const hc = Math.min(Math.max(h, 0), series.hours - 1);
  const h0 = Math.min(Math.floor(hc), series.hours - 1);
  const h1 = Math.min(h0 + 1, series.hours - 1);
  const wh = hc - h0;

  const a = bilinear(arr, grid, h0 * nodes, lat, lon);
  if (wh === 0 || h1 === h0) return a;
  const b = bilinear(arr, grid, h1 * nodes, lat, lon);
  return a + (b - a) * wh;
}

/** Bilinear interpolation over one hour-slice of a node array. */
function bilinear(arr, grid, base, lat, lon) {
  const { rows, cols } = grid;
  const fi = clamp((lat - grid.south) / grid.step, 0, rows - 1);
  const fj = clamp((lon - grid.west) / grid.step, 0, cols - 1);
  const i0 = Math.min(Math.floor(fi), rows - 2);
  const j0 = Math.min(Math.floor(fj), cols - 2);
  const wi = fi - i0;
  const wj = fj - j0;
  const k = base + i0 * cols + j0;

  // Weighted by hand rather than as the usual two lerps, because a node the
  // model left empty must only poison the answer where it actually counts —
  // NaN × 0 is NaN in IEEE arithmetic, so the plain formula would blank a
  // whole cell for a hole in its far corner. The four weights add up to one,
  // and a NaN that does count carries through the sum by itself. Written
  // out flat: the wind asks this for every particle, every frame.
  const w00 = (1 - wi) * (1 - wj);
  const w01 = (1 - wi) * wj;
  const w10 = wi * (1 - wj);
  const w11 = wi * wj;
  let sum = 0;
  if (w00 > 0) sum += arr[k] * w00;
  if (w01 > 0) sum += arr[k + 1] * w01;
  if (w10 > 0) sum += arr[k + cols] * w10;
  if (w11 > 0) sum += arr[k + cols + 1] * w11;
  return sum;
}

const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

/** Wind at a place and instant: components in m/s, speed, and the bearing it blows from. */
export function windAt(series, lat, lon, t) {
  const u = sampleAt(series, 'wind_u', lat, lon, t);
  const v = sampleAt(series, 'wind_v', lat, lon, t);
  if (Number.isNaN(u) || Number.isNaN(v)) return null;
  const speed = Math.hypot(u, v);
  const from = (Math.atan2(-u, -v) * 180 / Math.PI + 360) % 360;
  return { u, v, speed, from };
}

/** Sixteen-point compass name for a bearing. */
export function compassName(bearing) {
  const names = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
    'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  return names[Math.round(((bearing % 360) + 360) % 360 / 22.5) % 16];
}

/* ── climatologies and rasters: the precomputed products ──────────── */

/**
 * A climatology is the same shape as a Series, except that its "hours" are
 * the 288 cells of a month × hour table and time is read from the sliders
 * rather than the clock: a month fraction (0 → mid-January … 12 → mid-January
 * again) and an hour fraction. Both wrap: December blends into January and
 * 23:30 into 00:00.
 *
 * `vars[name]` is indexed `[(month * 24 + hour) * nodes + node]`.
 */
export function sampleClimatology(series, name, lat, lon, monthFrac, hourFrac) {
  const arr = series.vars[name];
  if (!arr) return NaN;
  const { grid } = series;
  const nodes = grid.rows * grid.cols;
  const mf = ((monthFrac % 12) + 12) % 12;
  const hf = ((hourFrac % 24) + 24) % 24;
  const m0 = Math.floor(mf);
  const m1 = (m0 + 1) % 12;
  const wm = mf - m0;
  const h0 = Math.floor(hf);
  const h1 = (h0 + 1) % 24;
  const wh = hf - h0;
  const at = (m, h) => bilinear(arr, grid, (m * 24 + h) * nodes, lat, lon);
  const a = at(m0, h0) * (1 - wh) + at(m0, h1) * wh;
  const b = at(m1, h0) * (1 - wh) + at(m1, h1) * wh;
  return a * (1 - wm) + b * wm;
}

/** Day of year (1-based) → month fraction with 0 at mid-January, matching the tables. */
export function monthFraction(doy, daysInYear = 365) {
  // Mid-month is the table's anchor, so the 15th of a month reads that
  // month's column exactly and the 1st is halfway to the previous one.
  return ((doy - 15.5) / daysInYear) * 12;
}

/**
 * A raster product: byte images on the tile's grid, one per calendar month,
 * byte 0 where there is no observation. Read bilinearly between pixel
 * centres — the way a terrain viewer reads a DEM, which is what a 90 m
 * thermal field effectively is — and blended between the two months around
 * the date. A missing pixel drops out of the weights rather than poisoning
 * its neighbours; a missing month falls back to the other one.
 *
 * `months[m]` (m = 0..11, missing when the month has no data) is
 * `{ values: Uint8Array, cols, rows }`; `decode(byte)` maps a byte to °C.
 */
/**
 * The image of a raster stacked by year for a year: that year's, or the
 * nearest one it holds (the first before it began, the latest after).
 */
export function yearImage(raster, year) {
  const { a, year: y, at } = yearPair(raster, year);
  return { year: at, image: a, requested: y };
}

/**
 * The two images of a raster stacked by year (or by epoch, five years
 * apart) around a year, and the weight of the second: a year between two
 * epochs is read between them. Outside the stack, its first or last.
 */
export function yearPair(raster, year) {
  const ys = raster.yearList ??= Object.keys(raster.years).map(Number).sort((p, q) => p - q);
  const y = Math.min(Math.max(year ?? ys[ys.length - 1], ys[0]), ys[ys.length - 1]);
  let i = 0;
  while (i < ys.length - 1 && ys[i + 1] <= y) i++;
  const j = Math.min(i + 1, ys.length - 1);
  const w = j === i ? 0 : (y - ys[i]) / (ys[j] - ys[i]);
  return { a: raster.years[ys[i]], b: raster.years[ys[j]], w, year: y, at: ys[i], first: ys[0], last: ys[ys.length - 1] };
}

/**
 * Where the built ground is compared from: the year on the slider — and, with
 * the slider on the present (at or after the last epoch), the first epoch,
 * so that the layer switched on today shows everything built since 1975
 * rather than nothing.
 */
export function growthFrom(raster, year) {
  if (!raster?.years) return year;
  const { first, last } = yearPair(raster, year);
  return year >= last ? first : Math.max(year, first);
}

/** One image of a raster between four pixel centres; a missing pixel drops out of the weights. */
function readImage(img, cols, decode, x0, y0, wx, wy) {
  if (!img) return NaN;
  const { values } = img;
  const k = y0 * cols + x0;
  let sum = 0;
  let weight = 0;
  let w = (1 - wx) * (1 - wy);
  if (w > 0 && values[k]) { sum += decode(values[k]) * w; weight += w; }
  w = wx * (1 - wy);
  if (w > 0 && values[k + 1]) { sum += decode(values[k + 1]) * w; weight += w; }
  w = (1 - wx) * wy;
  if (w > 0 && values[k + cols]) { sum += decode(values[k + cols]) * w; weight += w; }
  w = wx * wy;
  if (w > 0 && values[k + cols + 1]) { sum += decode(values[k + cols + 1]) * w; weight += w; }
  return weight > 0.25 ? sum / weight : NaN;
}

export function sampleRaster(raster, lat, lon, monthFrac, year) {
  const { bounds, rows, cols, decode } = raster;
  const [west, south, east, north] = bounds;
  const fx = ((lon - west) / (east - west)) * cols - 0.5;
  const fy = ((north - lat) / (north - south)) * rows - 0.5;
  if (fx < -0.5 || fy < -0.5 || fx > cols - 0.5 || fy > rows - 0.5) return NaN;
  const x0 = Math.min(Math.max(Math.floor(fx), 0), cols - 2);
  const y0 = Math.min(Math.max(Math.floor(fy), 0), rows - 2);
  const wx = Math.min(Math.max(fx - x0, 0), 1);
  const wy = Math.min(Math.max(fy - y0, 0), 1);

  const read = img => readImage(img, cols, decode, x0, y0, wx, wy);

  if (raster.years) {
    const { a, b, w } = yearPair(raster, year);
    const va = read(a);
    return w ? va * (1 - w) + read(b) * w : va;
  }
  const mf = ((monthFrac % 12) + 12) % 12;
  const m0 = Math.floor(mf);
  const m1 = (m0 + 1) % 12;
  const wm = mf - m0;
  const a = read(raster.months[m0]);
  const b = read(raster.months[m1]);
  if (Number.isNaN(a)) return Number.isNaN(b) ? NaN : b;
  if (Number.isNaN(b)) return a;
  return a * (1 - wm) + b * wm;
}

const holds = (b, lat, lon) => lat >= b[1] && lat < b[3] && lon >= b[0] && lon < b[2];

/**
 * The tile of a set holding a point, or null. The one that answered last is
 * asked first: readings come from the pin and a legend's lattice walks the
 * view cell by cell, so the next point is nearly always in the same tile —
 * and a view can hold hundreds.
 */
function tileAt(set, lat, lon) {
  if (set.hit && holds(set.hit.bounds, lat, lon)) return set.hit;
  for (const t of set.tiles) {
    if (holds(t.bounds, lat, lon)) { set.hit = t; return t; }
  }
  return null;
}

/** A raster-set: the value from whichever member tile holds the point. */
export function sampleRasterSet(set, lat, lon, monthFrac, year) {
  const r = tileAt(set, lat, lon);
  return r ? sampleRaster(r, lat, lon, monthFrac, year) : NaN;
}

/**
 * Street-scale air, from a CAMS value and the 50 m ratios (see
 * pipeline/air_lur.py). NO₂ and PM10 are CAMS times the cell's ratio, on
 * the same +1 µg/m³ footing the model was fitted on. Ozone is not modelled:
 * at street scale Ox = O₃ + NO₂ (in ppb) is conserved, so where traffic
 * adds NO₂ it takes about as much O₃ away — validated at 241 stations
 * measuring both. PM2.5 is regional; the model found nothing to add, so it
 * stays CAMS.
 *
 * `cams(name)` is the CAMS value at the point; `ratio(name)` the cell's
 * ratio, NaN where there is none.
 */
export const STREET_MODELLED = ['nitrogen_dioxide', 'pm10'];
/** µg/m³ per ppb, for the ozone titration (also the mosaic's, atmo/mosaic.js). */
export const PPB_NO2 = 1.88;
export const PPB_O3 = 1.96;

export function streetValue(option, cams, ratio) {
  const scaled = name => {
    const c = cams(name);
    const r = ratio(name);
    return Number.isNaN(r) ? c : (c + 1) * r - 1;
  };
  if (option === 'ozone') {
    const ox = cams('ozone') / PPB_O3 + cams('nitrogen_dioxide') / PPB_NO2;
    return Math.max(0, (ox - scaled('nitrogen_dioxide') / PPB_NO2) * PPB_O3);
  }
  return STREET_MODELLED.includes(option) ? scaled(option) : cams(option);
}

/** The street tile of a street-set holding the point, or null. */
export const streetTileAt = tileAt;

/** The 50 m ratio of `name` at a point of a street tile (nearest cell). */
export function streetRatio(tile, name, lat, lon) {
  const bytes = tile.bytes[name];
  if (!bytes) return NaN;
  const [west, south, east, north] = tile.bounds;
  const c = Math.min(tile.cols - 1, Math.max(0, Math.floor(((lon - west) / (east - west)) * tile.cols)));
  const r = Math.min(tile.rows - 1, Math.max(0, Math.floor(((north - lat) / (north - south)) * tile.rows)));
  return tile.ratioOf[bytes[r * tile.cols + c]];
}

/** Street-scale value at a point, for the reading and the tooltip. */
export function sampleStreet(set, option, lat, lon, monthFrac, hourFrac) {
  const tile = streetTileAt(set, lat, lon);
  if (!tile) return NaN;
  return streetValue(option,
    name => sampleClimatology(tile.clim, name, lat, lon, monthFrac, hourFrac),
    name => streetRatio(tile, name, lat, lon));
}
