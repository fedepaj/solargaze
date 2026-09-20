/**
 * Place search.
 *
 * Google's terms require their own geocoder while the photorealistic tiles are
 * displayed, so with the mesh up we go through Cesium ion's geocoder in Google
 * mode — the same brokering that gets us the tiles, already paid for by the
 * token. On the flat basemap there is no such tie and Nominatim keeps the app
 * usable with no account at all.
 */

import { state, setLocation } from '../state.js';
import {
  flyToLocation, tilesetVisible, viewer, resolveGroundAtPin, lookAtPin, distanceTo,
} from '../scene.js';
import { toast } from './toast.js';
import * as ionAuth from '../ion-auth.js';

const C = window.Cesium;
const $ = id => document.getElementById(id);

let ionService = null;
let debounce = null;
let sequence = 0;
let active = -1;
let current = [];

export function initSearch() {
  const input = $('search-input');
  const list = $('search-results');

  input.addEventListener('input', () => {
    clearTimeout(debounce);
    const q = input.value.trim();
    if (q.length < 2) return hide();
    debounce = setTimeout(() => run(q), 420);
  });

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      clearTimeout(debounce);
      if (active >= 0 && current[active]) choose(current[active]);
      else run(input.value.trim(), true);
      e.preventDefault();
    } else if (e.key === 'ArrowDown') {
      move(1); e.preventDefault();
    } else if (e.key === 'ArrowUp') {
      move(-1); e.preventDefault();
    } else if (e.key === 'Escape') {
      hide(); input.blur();
    }
  });

  document.addEventListener('click', e => {
    if (!e.target.closest('.searchrow')) hide();
  });

  list.addEventListener('click', e => {
    const li = e.target.closest('li[data-index]');
    if (li) choose(current[Number(li.dataset.index)]);
  });

  $('btn-locate').addEventListener('click', locateMe);
}

/* ── coordinate shortcut ──────────────────────────────────────── */

const DMS = /^\s*(\d{1,3})[°\s:]+(\d{1,2})['′\s:]*(\d{1,2}(?:\.\d+)?)?["″\s]*([NSEW])\s*[, ]\s*(\d{1,3})[°\s:]+(\d{1,2})['′\s:]*(\d{1,2}(?:\.\d+)?)?["″\s]*([NSEW])\s*$/i;
const DEC = /^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$/;

/** Parse "45.07, 7.68" or "45°04'13\"N 7°41'12\"E" into a lat/lon pair. */
export function parseCoordinates(text) {
  const dec = DEC.exec(text);
  if (dec) {
    const lat = Number(dec[1]);
    const lon = Number(dec[2]);
    if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180) return { lat, lon };
    return null;
  }

  const dms = DMS.exec(text);
  if (dms) {
    const toDeg = (d, m, s, hemi) => {
      const v = Number(d) + Number(m || 0) / 60 + Number(s || 0) / 3600;
      return /[SW]/i.test(hemi) ? -v : v;
    };
    const a = { value: toDeg(dms[1], dms[2], dms[3], dms[4]), hemi: dms[4].toUpperCase() };
    const b = { value: toDeg(dms[5], dms[6], dms[7], dms[8]), hemi: dms[8].toUpperCase() };
    // Accept either order: "7E 45N" is as valid as "45N 7E".
    const latPart = /[NS]/.test(a.hemi) ? a : b;
    const lonPart = latPart === a ? b : a;
    if (Math.abs(latPart.value) <= 90 && Math.abs(lonPart.value) <= 180) {
      return { lat: latPart.value, lon: lonPart.value };
    }
  }
  return null;
}

/* ── query runner ────────────────────────────────────────────────── */

async function run(query, commit = false) {
  if (!query) return hide();

  const coords = parseCoordinates(query);
  if (coords) {
    const hit = { name: `${coords.lat.toFixed(5)}, ${coords.lon.toFixed(5)}`, ...coords };
    if (commit) return choose(hit);
    return show([hit]);
  }

  const token = ++sequence;
  spinner(true);
  try {
    const results = tilesetVisible() ? await ionSearch(query) : await nominatimSearch(query);
    if (token !== sequence) return;
    if (commit && results.length) return choose(results[0]);
    show(results);
  } catch (err) {
    if (token !== sequence) return;
    show([]);
    toast(err.message, { error: true, ms: 6000 });
  } finally {
    if (token === sequence) spinner(false);
  }
}

/**
 * Google's terms tie the 3D tiles to Google's geocoder, so both routes below
 * end up at Google: directly with the user's key, or through Cesium ion, which
 * proxies to Google and is what the ion token is already paying for.
 */
function geocoderService() {
  if (ionService) return ionService;
  if (!state.ionToken) return null;
  ionService = new C.IonGeocoderService({
    scene: viewer.scene,
    accessToken: state.ionToken,
    geocodeProviderType: C.IonGeocodeProviderType.GOOGLE,
  });
  return ionService;
}

/**
 * Ask ion, and say something useful when it says no.
 *
 * The usual cause of a refusal is scope: fetching tiles needs `assets:read`,
 * but the geocoder needs `geocode` as well, and a sign-in granted before we
 * started asking for it will not have one. Rather than leaving the search box
 * dead we fall back to Nominatim and explain, once.
 */
async function ionSearch(query) {
  const service = geocoderService();
  if (!service) throw new Error('Sign in to Cesium ion to search places.');

  let raw;
  try {
    raw = await service.geocode(query);
  } catch (err) {
    const status = err?.statusCode ?? err?.status;
    console.warn('[solargaze] ion geocoding failed', status ?? '', err);

    const missingScope =
      status === 401 || status === 403 ||
      (ionAuth.currentSession() && !ionAuth.sessionCanGeocode());

    if (missingScope) {
      noteFallback(
        'This Cesium ion sign-in has no geocoding permission, so search is using ' +
        'OpenStreetMap. Sign out and back in from Settings to grant it.',
      );
    } else if (status === 429) {
      noteFallback('Cesium ion geocoding is out of quota; search is using OpenStreetMap.');
    } else {
      noteFallback(`Cesium ion geocoding failed${status ? ` (${status})` : ''}; search is using OpenStreetMap.`);
    }
    return nominatimSearch(query);
  }

  return raw.slice(0, 6).map(r => {
    const carto = destinationToCartographic(r.destination);
    return { name: r.displayName, detail: '', lat: carto.lat, lon: carto.lon };
  });
}

/** Say it once per session, not on every keystroke. */
let fallbackNoted = false;
function noteFallback(message) {
  if (fallbackNoted) return;
  fallbackNoted = true;
  toast(message, { error: true, ms: 8000 });
}

function destinationToCartographic(destination) {
  if (destination instanceof C.Rectangle) {
    const centre = C.Rectangle.center(destination);
    return { lat: C.Math.toDegrees(centre.latitude), lon: C.Math.toDegrees(centre.longitude) };
  }
  const c = C.Cartographic.fromCartesian(destination);
  return { lat: C.Math.toDegrees(c.latitude), lon: C.Math.toDegrees(c.longitude) };
}

async function nominatimSearch(query) {
  const url =
    'https://nominatim.openstreetmap.org/search?format=jsonv2&limit=6&q=' +
    encodeURIComponent(query);
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error('Search service unavailable.');
  const data = await res.json();
  return data.map(r => {
    const parts = String(r.display_name).split(', ');
    return {
      name: parts[0],
      detail: parts.slice(1).join(', '),
      lat: Number(r.lat),
      lon: Number(r.lon),
    };
  });
}

/* ── dropdown ────────────────────────────────────────────────────── */

function show(results) {
  current = results;
  active = -1;
  const list = $('search-results');
  list.innerHTML = '';

  if (!results.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'No matches';
    list.appendChild(li);
  } else {
    results.forEach((r, i) => {
      const li = document.createElement('li');
      li.dataset.index = String(i);
      li.textContent = r.name;
      if (r.detail) {
        const small = document.createElement('small');
        small.textContent = r.detail;
        li.appendChild(small);
      }
      list.appendChild(li);
    });
  }
  list.hidden = false;
}

function hide() {
  const list = $('search-results');
  list.hidden = true;
  list.innerHTML = '';
  current = [];
  active = -1;
}

function move(delta) {
  if (!current.length) return;
  active = (active + delta + current.length) % current.length;
  [...$('search-results').children].forEach((li, i) =>
    li.setAttribute('aria-selected', String(i === active)));
}

async function choose(hit) {
  if (!hit) return;
  hide();
  $('search-input').value = hit.name;
  $('search-input').blur();
  setLocation(hit.lat, hit.lon, { name: hit.name });

  // The first flight aims at ellipsoid height, so the ray through the middle of
  // the screen meets the mesh short of the target — tens of metres out at an
  // oblique pitch. Once we know the real surface height, aim again properly.
  await flyToLocation(hit.lat, hit.lon);
  if (!viewer) return;

  // While the point is following the view, the centre pick owns its height;
  // resolving it separately here would make the two disagree and nudge the
  // overlay off centre. Flying is enough — the follow puts it back on the spot.
  if (!state.prefs.pinLocked) return;

  await resolveGroundAtPin();
  const C = window.Cesium;
  const target = C.Cartesian3.fromDegrees(hit.lon, hit.lat, state.groundHeight);
  lookAtPin(distanceTo(target), {
    heading: viewer.camera.heading,
    pitchRadians: viewer.camera.pitch,
  });
}

function spinner(on) {
  const el = $('search-spin');
  if (el) el.hidden = !on;
}

/* ── geolocation ─────────────────────────────────────────────────── */

function locateMe() {
  if (!navigator.geolocation) {
    toast('This browser has no geolocation support.', { error: true });
    return;
  }
  toast('Locating…', { ms: 2000 });
  navigator.geolocation.getCurrentPosition(
    pos => {
      const { latitude, longitude } = pos.coords;
      setLocation(latitude, longitude, { name: 'My location' });
      flyToLocation(latitude, longitude, { height: 600 });
    },
    err => toast(`Location unavailable: ${err.message}`, { error: true }),
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 },
  );
}
