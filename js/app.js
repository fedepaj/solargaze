/**
 * SolarGaze — bootstrap.
 *
 * Order matters here: the viewer has to exist before the overlay can be built,
 * and the timezone table has to land before the clock means anything, but
 * neither should block first paint. So the scene comes up immediately with a
 * longitude-estimated offset and everything refines as the pieces arrive.
 */

import {
  REPO_URL, DEMO_ION_TOKEN, ION_CLIENT_ID, consumeResetRequest,
} from './config.js';
import {
  state, on, emit, setLocation, setPref, setIonToken, setAltitude, pointHeight,
  recompute, refreshZone,
} from './state.js';
import {
  createViewer, viewer, loadIonTiles, useFlatBasemap, usePhotorealisticBasemap,
  applyShadowSettings, applyMeshDetail, syncClock, zoomBy, pickAt, pickCentre, cartographicOf,
  resolveGroundAtPin,
  hasTileset, tilesetVisible, markTilesetFailed,
} from './scene.js';
import { loadTzDatabase } from './timezone.js';
import * as ionAuth from './ion-auth.js';
import { initSunPath, refreshSunPath } from './sunpath.js';
import { initTimePanel, stopPlayback } from './ui/timepanel.js';
import { initCompass } from './ui/compass.js';
import { initSearch } from './ui/search.js';
import { initWeather } from './ui/weather.js';
import { initModals, openKeyPrompt } from './ui/modals.js';
import { initAnalyzePane } from './ui/analyzepane.js';
import { initTooltips, hide as hideTooltip } from './ui/tooltip.js';
import { initUsage } from './ui/usage.js';
import { toast } from './ui/toast.js';

const C = window.Cesium;
const $ = id => document.getElementById(id);

/** Camera pose recovered from the URL, applied once the viewer exists. */
let pendingView = null;
let followQueued = false;
let followRetries = 0;
let followRetryTimer = null;

boot();

async function boot() {
  if (!C) {
    document.body.innerHTML =
      '<p style="padding:40px;font:15px system-ui;color:#fff">CesiumJS failed to load. Check your network connection and reload.</p>';
    return;
  }

  // A `?reset` navigates away, so nothing below should run.
  if (consumeResetRequest()) return;

  restoreFromUrl();
  createViewer('scene');
  applyPendingView();

  initSunPath();
  initTimePanel();
  initCompass();
  initSearch();
  initWeather();
  initAnalyzePane();
  initModals({ onCredentialSaved: () => startTiles(), onSkip: () => goFlat() });
  initTooltips();
  initUsage();

  wireDock();
  wireAltitude();
  wireTools();
  wireMapClick();
  wireLinks();

  on('time', syncClock);
  on('location', () => { pushUrl(); });
  on('date', pushUrl);
  on('camera', () => { followRetries = 0; scheduleFollow(); });
  on('camera', schedulePushUrl);
  // Belt and braces: whatever the polling did, settle again the moment the
  // mesh reports it has finished its first pass.
  on('mesh-ready', () => seatOverlayOnMesh({ tries: 6, gap: 300 }));
  on('pref', ({ key }) => {
    if (key === 'shadows' || key === 'softShadows' || key === 'shadowQuality') applyShadowSettings();
    if (key === 'meshDetail') applyMeshDetail();
    if (key === 'sunPath') refreshSunPath();
    paintDock();
  });

  syncClock();
  paintDock();
  exposeDebugHandle();

  // Timezones refine the clock; do it off the critical path.
  loadTzDatabase().then(fn => {
    if (!fn) return;
    if (refreshZone()) {
      recompute();
      emit('time', state);
    }
  });

  await resumeIonSession();
  startTiles();
}

/**
 * Pick up a Cesium ion sign-in: either we have just been redirected back with
 * an authorization code, or a previous session is still valid, or one is close
 * enough to expiry to be worth refreshing silently.
 */
async function resumeIonSession() {
  if (!ionAuth.isConfigured(ION_CLIENT_ID)) return;
  try {
    const fresh = await ionAuth.completeSignIn(ION_CLIENT_ID);
    if (fresh) {
      setIonToken(fresh);
      state.ionSignedIn = true;
      return;
    }
    const existing = ionAuth.currentSession();
    if (existing) {
      setIonToken(existing.accessToken);
      state.ionSignedIn = true;
      return;
    }
    const renewed = await ionAuth.refreshSession(ION_CLIENT_ID);
    if (renewed) {
      setIonToken(renewed);
      state.ionSignedIn = true;
    }
  } catch (err) {
    toast(String(err.message || err), { error: true, ms: 6000 });
  }
}

/**
 * A small handle for embedding, console tinkering and automated checks.
 * Deliberately read-mostly: everything here already has a UI control.
 */
function exposeDebugHandle() {
  window.solargaze = {
    get state() { return state; },
    get viewer() { return viewer; },
    setLocation,
    setPref,
    version: '1.0.0',
  };
}

/* ── basemap lifecycle ────────────────────────────────────────────── */

/**
 * Bring up the 3D mesh with whatever credential we have, ordered by whose
 * quota it spends: the visitor's own ion token or sign-in first, then the token
 * shipped with the build, which is ours. Each rung falls through to the next,
 * and running out of them means the flat basemap and a prompt.
 */
async function startTiles() {
  const attempts = [];
  if (state.ionToken) {
    attempts.push({ run: () => loadIonTiles(state.ionToken) });
  }
  if (DEMO_ION_TOKEN) {
    attempts.push({ run: () => loadIonTiles(DEMO_ION_TOKEN), shared: true });
  }

  if (!attempts.length) {
    goFlat();
    openKeyPrompt();
    return;
  }

  toast('Loading the 3D mesh…', { ms: 2200 });
  let lastError = null;

  for (const attempt of attempts) {
    try {
      await attempt.run();
      // Set the source before announcing the basemap: the usage meter listens
      // for that event and used to read a source that was still null.
      state.tileSource = 'ion';
      usePhotorealisticBasemap();
      paintDock();

      await seatOverlayOnMesh();
      if (attempt.shared) {
        toast('Using the shared demo quota — sign in for your own.', { ms: 5200 });
      }
      return;
    } catch (err) {
      lastError = err;
    }
  }

  markTilesetFailed();
  goFlat();
  openKeyPrompt({ reason: explainTileFailure(lastError) });
}

/**
 * Put the overlay on the real surface once the mesh is up.
 *
 * The tileset resolving is not the same moment as there being geometry under
 * the cursor to pick, and the first pick usually comes back empty. Left at
 * that, the ground height stays zero — which in Turin is ~290 m below the
 * street. The ring and the labels survive that, because they are drawn with the
 * depth test off, but the arc and the sun beam are ordinary polylines and the
 * terrain simply covers them. That is the "no arc at startup" report. So keep
 * asking until something answers.
 */
async function seatOverlayOnMesh({ tries = 30, gap = 500 } = {}) {
  for (let i = 0; i < tries; i++) {
    if (state.prefs.pinLocked) {
      await resolveGroundAtPin();
      if (state.groundHeight !== 0) break;
    } else if (followViewCentre()) {
      break;
    }
    await new Promise(r => setTimeout(r, gap));
  }
  refreshSunPath();
}

function explainTileFailure(err) {
  const message = String(err?.message || err);
  // Quota exhaustion on ion surfaces as 429; a rejected credential as 400/401/403.
  if (/\b429\b|quota|rate limit/i.test(message)) {
    return 'The 3D tile quota for that token is spent for now. Add your own Cesium ion token or Google Maps key to carry on.';
  }
  if (/\b4(00|01|03)\b|denied|unauthor|forbidden|invalid/i.test(message)) {
    return 'Cesium ion turned that token down. Sign in again, or check a pasted token carries the <code>assets:read</code> scope.';
  }
  return `The 3D tiles could not be loaded (<code>${escapeHtml(message.slice(0, 160))}</code>). The flat basemap is active in the meantime.`;
}

function goFlat() {
  useFlatBasemap();
  paintDock();
  refreshSunPath();
}

/* ── left dock ────────────────────────────────────────────────────── */

function wireDock() {
  $('btn-pin').addEventListener('click', () => {
    const locked = !state.prefs.pinLocked;
    setPref('pinLocked', locked);
    // Settle the height now: from here on nothing else will refresh it, and a
    // stale height slides the whole overlay sideways in an oblique view.
    if (locked) resolveGroundAtPin().then(refreshSunPath);
    else followViewCentre();
    toast(locked
      ? 'Point locked — drag it on the map to move it'
      : 'Point follows the centre of the view');
  });

  $('btn-shadows').addEventListener('click', () => setPref('shadows', !state.prefs.shadows));
  $('btn-sunpath').addEventListener('click', () => setPref('sunPath', !state.prefs.sunPath));

  $('btn-basemap').addEventListener('click', () => {
    if (!hasTileset()) {
      openKeyPrompt();
      return;
    }
    if (tilesetVisible()) useFlatBasemap();
    else usePhotorealisticBasemap();
    paintDock();
    refreshSunPath();
  });
}

function paintDock() {
  const locked = state.prefs.pinLocked;
  $('btn-pin').classList.toggle('is-on', locked);
  $('btn-pin').setAttribute('aria-pressed', String(locked));
  $('btn-pin').dataset.tip = locked
    ? 'Point locked<em>Drag it on the map to move it</em>'
    : 'Point follows the view<em>Click to lock it in place</em>';

  $('btn-shadows').classList.toggle('is-on', state.prefs.shadows);
  $('btn-shadows').setAttribute('aria-pressed', String(state.prefs.shadows));

  $('btn-sunpath').classList.toggle('is-on', state.prefs.sunPath);
  $('btn-sunpath').setAttribute('aria-pressed', String(state.prefs.sunPath));

  $('btn-basemap').classList.toggle('is-on', tilesetVisible());
  $('btn-basemap').setAttribute('aria-pressed', String(tilesetVisible()));

  // Altitude only means something once the point has stopped chasing the view.
  const altitude = $('btn-altitude');
  altitude.disabled = !locked;
  altitude.classList.toggle('is-on', locked && state.altitude !== 0);
  altitude.dataset.tip = locked
    ? `Height above the ground — now ${state.altitude} m<em>Study a balcony or a roof, not just the pavement</em>`
    : 'Height above the ground<em>Lock the point first</em>';
}

/* ── altitude above the ground ────────────────────────────────────── */

function wireAltitude() {
  const input = $('alt-input');
  const toggle = $('btn-altitude');

  const row = $('alt-row');
  const setOpen = open => {
    row.classList.toggle('is-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    if (open) setTimeout(() => input.focus({ preventScroll: true }), 160);
  };

  toggle.addEventListener('click', () => {
    if (!state.prefs.pinLocked) {
      toast('Lock the point first — an altitude means nothing while it follows the view.');
      return;
    }
    setOpen(!row.classList.contains('is-open'));
  });

  const step = delta => {
    setAltitude(state.altitude + delta);
    input.value = String(state.altitude);
  };
  $('alt-up').addEventListener('click', () => step(+1));
  $('alt-down').addEventListener('click', () => step(-1));

  input.addEventListener('change', () => {
    setAltitude(Number(input.value) || 0);
    input.value = String(state.altitude);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { setOpen(false); return; }
    if (e.key === 'Escape') { setOpen(false); return; }
    // Let the arrows drive the value rather than the map underneath.
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') e.stopPropagation();
  });

  // Unlocking the point takes the altitude away with it.
  on('pref', ({ key }) => {
    if (key === 'pinLocked' && !state.prefs.pinLocked) setOpen(false);
  });
  on('altitude', () => { input.value = String(state.altitude); });
}

/* ── bottom-right tools ───────────────────────────────────────────── */

function wireTools() {
  $('btn-zoom-in').addEventListener('click', () => zoomBy(1));
  $('btn-zoom-out').addEventListener('click', () => zoomBy(-1));

  $('btn-share').addEventListener('click', async e => {
    e.preventDefault();
    const url = buildUrl();
    try {
      await navigator.clipboard.writeText(url);
      toast('Link copied — it restores this exact view, date and time');
    } catch {
      history.replaceState(null, '', url);
      toast('Link is in the address bar');
    }
  });
}

function wireLinks() {
  const mark = $('wordmark');
  if (mark) mark.href = REPO_URL;
}

/* ── clicking the map moves the pin ───────────────────────────────── */

/**
 * Direct manipulation of the point.
 *
 * With the padlock open the point chases the centre of the view, so grabbing
 * it would fight the camera — it is only draggable once locked. A press within
 * a few pixels of the marker starts a drag and takes the camera controls out of
 * the way until the button comes back up.
 */
function wireMapClick() {
  const handler = new C.ScreenSpaceEventHandler(viewer.scene.canvas);
  const sceneEl = $('scene');
  let pressedAt = null;
  let dragging = false;

  const pinOnScreen = () => C.SceneTransforms.worldToWindowCoordinates(
    viewer.scene,
    C.Cartesian3.fromDegrees(state.lon, state.lat, pointHeight()),
  );

  const overPin = position => {
    if (!state.prefs.pinLocked) return false;
    const screen = pinOnScreen();
    return !!screen && C.Cartesian2.distance(screen, position) < 22;
  };

  const placeAt = position => {
    const cartesian = pickAt(position, { requireGeometry: tilesetVisible() });
    if (!cartesian) return false;
    const carto = cartographicOf(cartesian);
    if (!carto || !Number.isFinite(carto.lat)) return false;
    setLocation(carto.lat, carto.lon, { name: '', groundHeight: carto.height - state.altitude });
    return true;
  };

  handler.setInputAction(({ position }) => {
    pressedAt = position.clone();
    if (!overPin(position)) return;
    dragging = true;
    hideTooltip();
    stopPlayback();
    viewer.scene.screenSpaceCameraController.enableInputs = false;
    sceneEl.classList.add('is-grabbing');
  }, C.ScreenSpaceEventType.LEFT_DOWN);

  handler.setInputAction(({ endPosition }) => {
    if (dragging) {
      placeAt(endPosition);
      return;
    }
    sceneEl.classList.toggle('can-grab', overPin(endPosition));
  }, C.ScreenSpaceEventType.MOUSE_MOVE);

  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    viewer.scene.screenSpaceCameraController.enableInputs = true;
    sceneEl.classList.remove('is-grabbing');
    // The drag used the coarse depth pick; settle on the authoritative height.
    resolveGroundAtPin().then(refreshSunPath);
  };
  handler.setInputAction(endDrag, C.ScreenSpaceEventType.LEFT_UP);

  handler.setInputAction(({ position }) => {
    if (dragging || !state.prefs.pinLocked) return;

    // Cesium's click tolerance is generous enough that finishing an orbit can
    // register as a click, which nudged a point the user had deliberately locked.
    if (pressedAt && C.Cartesian2.distance(pressedAt, position) > 3) return;
    if (placeAt(position)) resolveGroundAtPin().then(refreshSunPath);
  }, C.ScreenSpaceEventType.LEFT_CLICK);
}

/**
 * Keep the point under the middle of the screen.
 *
 * The pick carries its own height, and that is the whole reason this stays
 * centred: the anchor *is* the surface point under the middle of the canvas, so
 * it projects straight back there. Overwrite that height from any other source
 * — sampleHeightMostDetailed, say — and the two disagree by a metre or two,
 * which in an oblique view slides the overlay visibly off centre. So while the
 * point is following, the pick is the only authority on its height.
 */
function followViewCentre() {
  if (state.prefs.pinLocked || !viewer) return false;
  // With the mesh up, only a real surface hit will do — see pickAt.
  const cartesian = pickCentre({ requireGeometry: tilesetVisible() });
  if (!cartesian) return false;
  const carto = cartographicOf(cartesian);
  if (!carto || !Number.isFinite(carto.lat)) return false;
  setLocation(carto.lat, carto.lon, { groundHeight: carto.height });
  return true;
}

/**
 * Follow the view centre, and keep asking if the answer is "not yet".
 *
 * Panning into ground whose tiles have not arrived means there is genuinely no
 * surface to pick. Falling back to the ellipsoid would answer zero — the bug
 * that put the overlay underground — and giving up would strand the point
 * behind the camera, drifting further with every pan. So retry briefly: the
 * tiles are on their way.
 */
function scheduleFollow() {
  if (followQueued || state.prefs.pinLocked) return;
  followQueued = true;
  requestAnimationFrame(() => {
    followQueued = false;
    if (followViewCentre()) {
      followRetries = 0;
      clearTimeout(followRetryTimer);
      return;
    }
    if (followRetries < 20) {
      followRetries += 1;
      clearTimeout(followRetryTimer);
      followRetryTimer = setTimeout(scheduleFollow, 250);
    }
  });
}

/** Apply a camera pose recovered from the URL, once the viewer exists. */
function applyPendingView() {
  if (!pendingView || !viewer) return;
  const v = pendingView;
  pendingView = null;
  viewer.camera.setView({
    destination: C.Cartesian3.fromDegrees(v.lon, v.lat, v.height),
    orientation: {
      heading: C.Math.toRadians(v.heading),
      pitch: C.Math.toRadians(v.pitch),
      roll: 0,
    },
  });
}

/* ── shareable URL state ──────────────────────────────────────────── */

let urlTimer = null;
function schedulePushUrl() {
  clearTimeout(urlTimer);
  urlTimer = setTimeout(pushUrl, 500);
}

function buildUrl() {
  const cam = viewer?.camera;
  const pos = cam ? cartographicOf(cam.positionWC) : null;
  const pad = n => String(n).padStart(2, '0');

  const parts = [
    `ll=${state.lat.toFixed(6)},${state.lon.toFixed(6)}`,
    `t=${state.y}-${pad(state.m)}-${pad(state.d)}T${pad(Math.floor(state.minutes / 60))}:${pad(state.minutes % 60)}`,
  ];
  if (pos && cam) {
    parts.push(`cam=${pos.lat.toFixed(6)},${pos.lon.toFixed(6)},${Math.round(pos.height)}`);
    parts.push(`hp=${C.Math.toDegrees(cam.heading).toFixed(1)},${C.Math.toDegrees(cam.pitch).toFixed(1)}`);
  }
  return `${location.origin}${location.pathname}#${parts.join('&')}`;
}

function pushUrl() {
  try {
    history.replaceState(null, '', buildUrl());
  } catch { /* some embedding contexts forbid this */ }
}

function restoreFromUrl() {
  const hash = location.hash.replace(/^#/, '');
  if (!hash) return;
  const q = new URLSearchParams(hash);

  const ll = q.get('ll');
  if (ll) {
    const [lat, lon] = ll.split(',').map(Number);
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      state.lat = lat;
      state.lon = lon;
    }
  }

  const t = q.get('t');
  if (t) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(t);
    if (m) {
      state.y = Number(m[1]);
      state.m = Number(m[2]);
      state.d = Number(m[3]);
      state.minutes = Number(m[4]) * 60 + Number(m[5]);
    }
  }

  const cam = q.get('cam');
  const hp = q.get('hp');
  if (cam) {
    const [lat, lon, height] = cam.split(',').map(Number);
    const [heading = 0, pitch = -35] = (hp || '').split(',').map(Number);
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      pendingView = { lat, lon, height, heading, pitch };
    }
  }

  refreshZone();
  recompute();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}
