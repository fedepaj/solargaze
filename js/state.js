/**
 * Single source of truth plus a tiny pub/sub bus.
 *
 * Everything the app draws is derived from: where you are looking (lat/lon),
 * which calendar day you picked, and what time of day. Derived solar values are
 * recomputed once per change here so no module has to duplicate the maths.
 */

import { sunPosition, dayEvents, dayOfYear, fromDayOfYear, daysInYear } from './solar.js';
import { wallToUtc, offsetAt, zoneFor, solarMeanOffset } from './timezone.js';
import {
  DEFAULTS, loadPrefs, savePrefs, getIonToken, saveIonToken,
} from './config.js';

const listeners = new Map();

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event).delete(fn);
}

export function emit(event, detail) {
  const set = listeners.get(event);
  // Iterating the Set directly rather than a copy: `time` is emitted on every
  // animation frame during playback, and nothing in this app registers a
  // listener from inside a handler, which is the only case the copy guarded.
  if (set) for (const fn of set) fn(detail);
}

const now = new Date();

export const state = {
  /** Sun pin: the point the overlay is anchored to. */
  lat: DEFAULTS.lat,
  lon: DEFAULTS.lon,
  /** Ground height of the pin in metres, filled in once the 3D tiles resolve. */
  groundHeight: 0,
  /**
   * Metres above that ground. An offset rather than an absolute altitude, so it
   * survives moving the pin onto higher terrain: +15 m stays a fifth floor.
   */
  altitude: 0,
  placeName: '',

  zone: null,
  offsetMinutes: solarMeanOffset(DEFAULTS.lon),

  /** Wall-clock date/time at the pin. */
  y: now.getFullYear(),
  m: now.getMonth() + 1,
  d: now.getDate(),
  minutes: now.getHours() * 60 + now.getMinutes(),

  /* derived, refreshed by recompute() */
  utc: new Date(),
  sun: { elevation: 0, azimuth: 0 },
  events: { sunrise: null, sunset: null, noon: 720, polar: null },

  playing: false,
  tab: 'visualize',

  prefs: loadPrefs(),
  ionToken: getIonToken(),
  /** 'ion' once the mesh is up, null while it is not. */
  tileSource: null,
  /** True when the ion token came from an OAuth sign-in rather than a paste. */
  ionSignedIn: false,
};

/** Local-midnight-relative minutes of an event returned by dayEvents (UTC min). */
function toWallMinutes(utcMinutes) {
  if (utcMinutes === null) return null;
  const t = new Date(Date.UTC(state.y, state.m - 1, state.d, 0, 0, 0) + utcMinutes * 60000);
  return utcMinutes + offsetAt(t, state.zone, state.lon);
}

/**
 * Sunrise, sunset and noon depend on the calendar day and the place — never on
 * the time of day. Playback calls setTime() once per animation frame, so
 * without this the most expensive half of recompute() ran sixty times a second
 * to produce the same three numbers. Four decimal places of latitude is about
 * eleven metres, which moves sunrise by a quarter of a second.
 */
let eventsKey = '';

/** Recompute everything derived from the primary fields. */
export function recompute() {
  state.utc = wallToUtc(
    { y: state.y, m: state.m, d: state.d, minutes: state.minutes },
    state.zone,
    state.lon,
  );
  state.offsetMinutes = offsetAt(state.utc, state.zone, state.lon);
  state.sun = sunPosition(state.utc, state.lat, state.lon);

  const key = `${state.y},${state.m},${state.d},${state.lat.toFixed(4)},${state.lon.toFixed(4)},${state.zone}`;
  if (key === eventsKey) return;
  eventsKey = key;

  const ev = dayEvents(state.y, state.m, state.d, state.lat, state.lon);
  state.events = {
    sunrise: toWallMinutes(ev.sunrise),
    sunset: toWallMinutes(ev.sunset),
    noon: toWallMinutes(ev.noon),
    polar: ev.polar,
  };
}

/** Refresh the IANA zone after the pin moves (or after tz-lookup finishes). */
export function refreshZone() {
  const zone = zoneFor(state.lat, state.lon);
  if (zone !== state.zone) {
    state.zone = zone;
    return true;
  }
  return false;
}

/* ── mutations: each one recomputes and announces what changed ─────── */

export function setTime(minutes, { silent = false } = {}) {
  const wrapped = ((Math.round(minutes) % 1440) + 1440) % 1440;
  if (wrapped === state.minutes) return;
  state.minutes = wrapped;
  recompute();
  if (!silent) emit('time', state);
}

export function setDate({ y, m, d }) {
  if (y === state.y && m === state.m && d === state.d) return;
  Object.assign(state, { y, m, d });
  recompute();
  emit('date', state);
  emit('time', state);
}

export function setDayOfYear(doy) {
  const max = daysInYear(state.y);
  setDate(fromDayOfYear(state.y, Math.min(Math.max(1, Math.round(doy)), max)));
}

export function currentDayOfYear() {
  return dayOfYear(state.y, state.m, state.d);
}

/** Move the sun pin. `name` is an optional label from search/geocoding. */
export function setLocation(lat, lon, { name = '', groundHeight = null } = {}) {
  // Follow mode calls this on every camera nudge, so skip the polygon lookup
  // unless we have actually travelled far enough to plausibly change zone.
  const travelled = Math.abs(lat - state.lat) > 0.005 || Math.abs(lon - state.lon) > 0.005;

  state.lat = lat;
  state.lon = lon;
  state.placeName = name;
  if (groundHeight !== null) state.groundHeight = groundHeight;
  if (travelled || state.zone === null) refreshZone();
  recompute();
  emit('location', state);
  emit('time', state);
}

export function setGroundHeight(h) {
  if (Math.abs(h - state.groundHeight) < 0.5) return;
  state.groundHeight = h;
  emit('ground', state);
}

/** Height of the studied point: sampled ground plus the user's offset. */
export function pointHeight() {
  return state.groundHeight + state.altitude;
}

export function setAltitude(metres) {
  const next = Math.min(Math.max(Math.round(metres), -50), 500);
  if (next === state.altitude) return;
  state.altitude = next;
  emit('altitude', state);
  emit('ground', state);
}

/** Snap the clock to the real current time at the pin. */
export function jumpToNow() {
  const n = new Date();
  const offset = offsetAt(n, state.zone, state.lon);
  const local = new Date(n.getTime() + offset * 60000);
  Object.assign(state, {
    y: local.getUTCFullYear(),
    m: local.getUTCMonth() + 1,
    d: local.getUTCDate(),
    minutes: local.getUTCHours() * 60 + local.getUTCMinutes(),
  });
  recompute();
  emit('date', state);
  emit('time', state);
}

export function setPref(key, value) {
  state.prefs[key] = value;
  savePrefs(state.prefs);
  emit('pref', { key, value });
}

export function setIonToken(token) {
  state.ionToken = token;
  saveIonToken(token);
}

/**
 * The window the time slider spans: sunrise→sunset like a sun study, unless
 * the user asked for the full 24 h or the day has no sunrise at all.
 */
export function sliderRange() {
  const { sunrise, sunset, polar } = state.events;
  if (state.prefs.fullDayRange || polar || sunrise === null || sunset === null) {
    return { from: 0, to: 1439, full: true };
  }
  const from = Math.max(0, Math.floor(sunrise));
  const to = Math.min(1439, Math.ceil(sunset));
  if (to - from < 30) return { from: 0, to: 1439, full: true };
  return { from, to, full: false };
}

recompute();
