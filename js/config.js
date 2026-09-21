/** Static configuration and persisted-settings plumbing. */

/**
 * The CesiumJS build this app is written against, mirrored here for anything
 * that wants to report it. The version that actually loads is pinned in three
 * places in index.html — the widgets stylesheet, `CESIUM_BASE_URL` and the
 * script tag — which all move together. Editing this line alone loads nothing
 * new; edit those three and keep this in step.
 */
export const CESIUM_VERSION = '1.145.0';

/** Cesium ion's catalogue entry for Google Photorealistic 3D Tiles. */
export const ION_GOOGLE_3D_ASSET = 2275207;

/**
 * Optional built-in Cesium ion token, so visitors get the 3D mesh with no
 * setup at all. Leave empty to force everyone to bring their own.
 *
 * Read this before you fill it in:
 *
 * - A token in a static site is PUBLIC. There is no way around that: whatever
 *   the browser can read, so can a reader of the page source. The question is
 *   never "how do I hide it" but "how small is the blast radius".
 * - Use a token created for this app alone, scoped to `assets:read` only, and
 *   never your account's default token.
 * - Every visitor's tile loading is billed to YOUR ion quota. The free tier is
 *   small. When it runs out the app falls back to the flat basemap and says so.
 */
export const DEMO_ION_TOKEN = '';

/**
 * Numeric client id of your registered Cesium ion application. Setting it turns
 * on the "Sign in with Cesium ion" button, which is the nicest route of all:
 * visitors approve once on ion's own page and then draw on their own quota, with
 * no token to copy and nothing of yours to spend.
 *
 * Register at ion → your username → Developer Settings → Add Application, and
 * list every URL you serve this from as a redirect URI, exactly — e.g.
 * `https://fedepaj.github.io/solargaze/` and `http://localhost:5173/`.
 *
 * FORKING: the id below belongs to the deployment at fedepaj.github.io, whose
 * redirect URIs do not include yours — the button will fail on ion's own page
 * until you swap in your own application. Setting it to '' hides the button and
 * leaves the paste-a-token route, which needs no registration at all.
 */
export const ION_CLIENT_ID = '2354';

const ION_KEY_STORE = 'solargaze.ionToken';
const PREF_STORE = 'solargaze.prefs';

/** Everything this app ever writes, in one place, so a reset can be complete. */
const ALL_STORES = [
  ION_KEY_STORE,
  PREF_STORE,
  'solargaze.ionSession',
  'solargaze.ionUsage',
  'solargaze.googleKey',   // retired; cleared so old installs do not keep it
];

/**
 * Where the app opens: `lat`/`lon` place the sun pin, and the other three seat
 * the camera on it — `height` is the slant distance from the pin, not an
 * altitude, with `heading`/`pitch` in degrees. These frame the Colosseum from
 * the south, which is the view in the README's animation.
 *
 * `minutes` is a time of day, not a date. Opening at mid-morning guarantees a
 * long, legible shadow whatever day someone arrives, where "now" would show a
 * flat overhead sun at noon in June and pitch darkness at 23:00 — a poor first
 * impression for an app that is entirely about shadows. NOW is one click away.
 */
export const DEFAULTS = {
  lat: 41.890498,
  lon: 12.492392,
  height: 400,
  heading: 0,
  pitch: -40,
  minutes: 10 * 60 + 3,
  /**
   * Metres of ground under the pin, stated rather than measured.
   *
   * The camera has to be composed before anything can be sampled, and
   * sampleHeightMostDetailed does not answer reliably over photogrammetry this
   * fresh. A known viewpoint has a known floor, so this is simply part of the
   * description: 335 m of camera minus 257 m of slant rise, the numbers in the
   * shared link. Move lat/lon and this moves with them.
   */
  groundHeight: 78,
};

export const PREFS = {
  shadows: true,
  sunPath: true,
  softShadows: true,
  shadowQuality: 2048,
  /**
   * `maximumScreenSpaceError` for the 3D tileset. Cesium's default is 16;
   * raising it asks for coarser tiles, which is the single biggest lever on how
   * fast the ion quota in the usage meter is spent.
   */
  meshDetail: 16,
  fullDayRange: false,
  weather: true,
  pinLocked: false,
};

/* localStorage can throw in private windows or with site data blocked, so every
   access is guarded and simply degrades to in-memory defaults. */
const safeGet = k => { try { return localStorage.getItem(k); } catch { return null; } };
const safeSet = (k, v) => { try { localStorage.setItem(k, v); } catch { /* ignore */ } };
const safeDel = k => { try { localStorage.removeItem(k); } catch { /* ignore */ } };

/** Pull a credential out of the query string once, then scrub it from history. */
function consumeFromUrl(param, store) {
  const found = new URLSearchParams(location.search).get(param);
  if (!found) return null;
  const value = found.trim();
  safeSet(store, value);
  const url = new URL(location.href);
  url.searchParams.delete(param);
  history.replaceState(null, '', url);
  return value;
}

/**
 * Wipe every trace of this app from the browser and reload — signed-in state,
 * pasted token, preferences, the half-finished PKCE handshake, the lot.
 * Triggered by `?reset` on the URL or from Settings.
 */
export function resetAll({ reload = true } = {}) {
  for (const key of ALL_STORES) safeDel(key);
  try { sessionStorage.removeItem('solargaze.pkce'); } catch { /* ignore */ }
  if (reload) {
    location.replace(`${location.origin}${location.pathname}`);
  }
}

/** Honour `?reset` before anything else reads stored state. */
export function consumeResetRequest() {
  if (!new URLSearchParams(location.search).has('reset')) return false;
  resetAll();
  return true;
}

export function getIonToken() {
  return consumeFromUrl('ion', ION_KEY_STORE) || safeGet(ION_KEY_STORE) || '';
}

export function saveIonToken(token) {
  if (token) safeSet(ION_KEY_STORE, token);
  else safeDel(ION_KEY_STORE);
}

export function loadPrefs() {
  try {
    return { ...PREFS, ...JSON.parse(safeGet(PREF_STORE) || '{}') };
  } catch {
    return { ...PREFS };
  }
}

export function savePrefs(prefs) {
  safeSet(PREF_STORE, JSON.stringify(prefs));
}

/** Shown in the footer and the "Source" button. Point it at your own fork. */
export const REPO_URL = 'https://github.com/fedepaj/solargaze';
