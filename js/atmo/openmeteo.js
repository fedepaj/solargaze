/**
 * Open-Meteo, the one provider behind every source so far: free, keyless,
 * CC-BY 4.0, and happy to answer for a whole grid of points in one request.
 *
 * Each answer is folded into a `Series` and cached, so scrubbing the time
 * slider costs nothing and only a new place or a new day fetches. Sources
 * describe *what* to ask for (sources.js); this file knows only *how*.
 */

import { buildSeries } from './field.js';
import { isoDate, shiftDate } from './sources.js';
import { cachedFetch } from './cache.js';

/** Recent answers by request. Small: each is 49 nodes × 72 hours × a few variables. */
const cache = new Map();
const CACHE_MAX = 24;

export function seriesKey(source, endpoint, date, grid) {
  return `${source.id}|${endpoint}|${isoDate(date)}|${grid.key}`;
}

/**
 * @param {{source:object, grid:object, endpoint:string, date:{y,m,d}, proxy:string|null}} want
 * @returns {Promise<object>} a Series (see field.js), with `date`, `endpoint`
 *   and `proxy` carried along so the pane can say what it is looking at.
 */
export function fetchSeries(want) {
  const { source, grid, endpoint, date, proxy } = want;
  const key = seriesKey(source, endpoint, date, grid);
  const hit = cache.get(key);
  if (hit) return hit;

  const promise = request(source, grid, endpoint, date)
    .then(locations => buildSeries(source, grid, locations, { date, endpoint, proxy, key }))
    .catch(err => {
      // A failed answer must not be remembered as the answer.
      cache.delete(key);
      throw err;
    });

  cache.set(key, promise);
  if (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value);
  return promise;
}

async function request(source, grid, endpoint, date) {
  const url = new URL(source.endpoints[endpoint]);
  const p = url.searchParams;
  p.set('latitude', grid.lats.join(','));
  p.set('longitude', grid.lons.join(','));
  p.set('hourly', source.vars.join(','));
  // The day before and after as well: a local day is two UTC days, and the
  // playback should not run off the end of the data at midnight.
  p.set('start_date', isoDate(shiftDate(date, -1)));
  p.set('end_date', isoDate(shiftDate(date, 1)));
  p.set('timezone', 'UTC');
  for (const [k, v] of Object.entries(source.params || {})) p.set(k, v);

  // The archive does not change: a day that has happened is worth keeping.
  // Forecasts are not, and the air-quality endpoint mixes both, so only the
  // archive endpoint goes through the cache.
  const res = endpoint === 'archive'
    ? await cachedFetch(url.toString())
    : await fetch(url, { headers: { Accept: 'application/json' } });
  let body = null;
  try { body = await res.json(); } catch { /* not JSON: handled below */ }

  if (!res.ok || body?.error) {
    const reason = body?.reason || `HTTP ${res.status}`;
    throw new Error(`Open-Meteo: ${reason}`);
  }
  return Array.isArray(body) ? body : [body];
}
