/**
 * Timezone handling.
 *
 * The app's clock is a *wall clock at the place you are looking at*, exactly
 * like a sundial: pick Tokyo and 12:00 means local Tokyo noon. That needs an
 * IANA zone for an arbitrary lat/lon, which `tz-lookup` provides offline from a
 * compressed polygon table (loaded lazily, it is the single biggest asset).
 * Until it resolves — or if it fails — we fall back to the solar-mean offset
 * from longitude, which is never more than about an hour off.
 */

const TZ_CDN = 'https://cdn.jsdelivr.net/npm/tz-lookup@6.1.25/tz.js';

let lookupFn = null;
let loading = null;

/** Kick off (or reuse) the tz-lookup download. Resolves to a fn or null. */
export function loadTzDatabase() {
  if (lookupFn) return Promise.resolve(lookupFn);
  if (loading) return loading;
  loading = new Promise(resolve => {
    const s = document.createElement('script');
    s.src = TZ_CDN;
    s.async = true;
    s.onload = () => {
      lookupFn = typeof window.tzlookup === 'function' ? window.tzlookup : null;
      resolve(lookupFn);
    };
    s.onerror = () => resolve(null);
    document.head.appendChild(s);
  });
  return loading;
}

/** IANA zone name for a location, or null if the table isn't available yet. */
export function zoneFor(lat, lon) {
  if (!lookupFn) return null;
  try {
    return lookupFn(lat, lon);
  } catch {
    return null;
  }
}

const formatters = new Map();
function formatter(zone) {
  let f = formatters.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: zone,
      hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
    formatters.set(zone, f);
  }
  return f;
}

/** Longitude-based fallback: 15° of longitude per hour, rounded to the hour. */
export const solarMeanOffset = lon => Math.round(lon / 15) * 60;

function zoneOffset(date, zone) {
  const parts = Object.create(null);
  for (const p of formatter(zone).formatToParts(date)) parts[p.type] = p.value;
  const hour = Number(parts.hour) % 24; // some engines emit "24" at midnight
  const asUtc = Date.UTC(
    Number(parts.year), Number(parts.month) - 1, Number(parts.day),
    hour, Number(parts.minute), Number(parts.second),
  );
  return Math.round((asUtc - date.getTime()) / 60000);
}

/**
 * `formatToParts` costs ~3.7 µs, and recompute() asks for six of them. During
 * playback that is the single most expensive thing the app does per frame, to
 * re-derive an offset that changes twice a year.
 *
 * A zone's offset is constant across any UTC hour it does not transition in, so
 * one probe per zone per hour is exact for every zone that shifts on the hour
 * and, for the few that shift at :30 or :45, still an hour finer than the
 * whole-day offset the overlay and the analyser already work with.
 */
const offsets = new Map();

export function offsetAt(date, zone, lon = 0) {
  if (!zone) return solarMeanOffset(lon);
  const key = `${zone}|${Math.floor(date.getTime() / 3600000)}`;
  const hit = offsets.get(key);
  if (hit !== undefined) return hit;
  let value;
  try {
    value = zoneOffset(date, zone);
  } catch {
    return solarMeanOffset(lon);   // never cached: the fallback depends on lon
  }
  if (offsets.size >= 4096) offsets.clear();
  offsets.set(key, value);
  return value;
}

/**
 * Convert a wall-clock reading at `zone` into a real UTC instant.
 * Two passes settle the chicken-and-egg between offset and instant, which is
 * all that is needed outside the one ambiguous hour of a DST fall-back.
 */
export function wallToUtc({ y, m, d, minutes }, zone, lon = 0) {
  const base = Date.UTC(y, m - 1, d, 0, 0, 0) + minutes * 60000;
  let t = base;
  for (let i = 0; i < 2; i++) {
    t = base - offsetAt(new Date(t), zone, lon) * 60000;
  }
  return new Date(t);
}

/** The wall-clock reading at `zone` for a real UTC instant. */
export function utcToWall(date, zone, lon = 0) {
  const shifted = new Date(date.getTime() + offsetAt(date, zone, lon) * 60000);
  return {
    y: shifted.getUTCFullYear(),
    m: shifted.getUTCMonth() + 1,
    d: shifted.getUTCDate(),
    minutes: shifted.getUTCHours() * 60 + shifted.getUTCMinutes(),
  };
}

/** "+02:00" / "−05:30" style label. */
export function offsetLabel(minutes) {
  const sign = minutes < 0 ? '−' : '+';
  const abs = Math.abs(minutes);
  return `${sign}${String(Math.floor(abs / 60)).padStart(2, '0')}:${String(abs % 60).padStart(2, '0')}`;
}
