/**
 * SolarGaze — bootstrap.
 *
 * Order matters here: the viewer has to exist before the overlay can be built,
 * and the timezone table has to land before the clock means anything, but
 * neither should block first paint. So the scene comes up immediately with a
 * longitude-estimated offset and everything refines as the pieces arrive.
 */

import {
  REPO_URL, DEMO_ION_TOKEN, ION_CLIENT_ID, DEFAULTS, consumeResetRequest,
} from './config.js';
import {
  state, on, emit, setLocation, setPref, setIonToken, setAltitude, pointHeight,
  recompute, refreshZone,
} from './state.js';
import {
  createViewer, viewer, loadIonTiles, useFlatBasemap, usePhotorealisticBasemap, lookAtPin,
  applyShadowSettings, applyMeshDetail, syncClock, zoomBy, pickAt, pickCentre, cartographicOf,
  resolveGroundAtPin,
  hasTileset, tilesetVisible,
} from './scene.js';
import { loadTzDatabase } from './timezone.js';
import { escapeHtml } from './util.js';
import * as ionAuth from './ion-auth.js';
import { initSunPath, refreshSunPath } from './sunpath.js';
import { initTimePanel, stopPlayback } from './ui/timepanel.js';
import { initCompass } from './ui/compass.js';
import { initSearch } from './ui/search.js';
import { initWeather } from './ui/weather.js';
import { initModals, openGate, closeGate, isGated } from './ui/modals.js';
import { initAnalyzePane } from './ui/analyzepane.js';
import { initTooltips, hide as hideTooltip } from './ui/tooltip.js';
import { initUsage } from './ui/usage.js';
import { initMouseCard } from './ui/mousecard.js';
import { toast } from './ui/toast.js';
import { mountLoader } from './ui/sunloader.js';
import { startOfflineDemo, stopOfflineDemo, seedDemoTime } from './ui/offlinedemo.js';

const C = window.Cesium;
const $ = id => document.getElementById(id);

/** Camera pose recovered from the URL, applied once the viewer exists. */
let pendingView = null;
/** True when the URL asked for a specific place or camera — then leave it alone. */
let urlPlacedView = false;
let initialViewSettled = false;
let meshReady = false;
/** Held while the opening view is being composed, so the pin cannot drift. */
let suppressFollow = false;
let settleSkipped = null;
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
  initModals({ onCredentialSaved: () => startTiles(), onRetry: () => startTiles() });
  initTooltips();
  initUsage();
  initMouseCard();
  mountLoader($('meshload-sun'));

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
  on('mesh-ready', async () => {
    meshReady = true;
    // Strictly in order: seatOverlayOnMesh polls followViewCentre, which writes
    // the very groundHeight the framing is computed from. Run them together and
    // they race, which is how the opening shot ended up 78 m low.
    settleInitialView();
    hideMeshLoader();
    seatOverlayOnMesh({ tries: 6, gap: 300 });
  });
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
    /** Boot milestones, for the automated checks this handle exists for. */
    get diag() {
      return { meshReady, initialViewSettled, urlPlacedView, settleSkipped };
    },
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
    openGate();
    return;
  }

  showMeshLoader();
  let lastError = null;

  for (const attempt of attempts) {
    try {
      await attempt.run();
      // Set the source before announcing the basemap: the usage meter listens
      // for that event and used to read a source that was still null.
      state.tileSource = 'ion';
      stopOfflineDemo();
      usePhotorealisticBasemap();
      // Before seatOverlayOnMesh starts polling the centre pick: the opening
      // composition is a stated one and nothing should have moved yet.
      settleInitialView();
      // Only now is there an app behind the gate. Lifting it any earlier —
      // when a token is pasted, say — would hand over a cockpit with no world
      // under it if ion then turned that token down.
      closeGate();
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

  hideMeshLoader();

  const failure = explainTileFailure(lastError);
  // A credential ion rejected is the visitor's to fix, so it gets the gate and
  // the sign-in. Everything else is ion's end being unreachable, and there the
  // recorded day is better company than a wall — if it can be fetched at all.
  if (failure.kind !== 'credential') {
    seedDemoTime();
    const shown = await startOfflineDemo({
      reason: failure.reason,
      onRetry: () => startTiles(),
    });
    if (shown) return;
  }
  openGate(failure);
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

/**
 * Turn a tile failure into something the gate can act on.
 *
 * `kind` decides which button the gate leads with, and the distinction is the
 * whole point: retrying a credential ion has just rejected will fail again in
 * exactly the same way, so offering it first wastes the one move the visitor
 * has. A spent quota or a dropped connection is the opposite — retrying is the
 * right thing and fixing the account is not.
 */
/* ── mesh loading overlay ─────────────────────────────────────────── */

let meshLoadTimer = null;

/**
 * Say that something is happening, for as long as it is happening.
 *
 * Photogrammetry over a slow line can take twenty seconds to look like
 * anything, and an empty dark canvas is indistinguishable from a broken page.
 * The old toast vanished after two seconds and left exactly that impression,
 * so this stays until the tileset reports its first full pass.
 */
function showMeshLoader() {
  const el = $('meshload');
  if (!el) return;
  el.hidden = false;
  $('meshload-note').textContent = 'Streaming Google\u2019s photorealistic tiles…';
  clearTimeout(meshLoadTimer);
  // Past a certain wait, silence reads as failure. Name the cause instead.
  meshLoadTimer = setTimeout(() => {
    $('meshload-note').textContent =
      'Still going — the mesh arrives over your connection, so this is slower on a poor one.';
    // `initialTilesLoaded` never fires if the camera ends up over nothing that
    // needs tiles, and a loader that cannot end is worse than none at all.
    meshLoadTimer = setTimeout(hideMeshLoader, 30_000);
  }, 9000);
}

function hideMeshLoader() {
  clearTimeout(meshLoadTimer);
  const el = $('meshload');
  if (el) el.hidden = true;
}

/**
 * Re-frame the opening shot once the mesh has a real surface to measure.
 *
 * createViewer has to seat the camera before a single tile exists, so it aims
 * at the pin at ellipsoid height — about 79 m below the floor of the Colosseum.
 * The camera therefore ends up 79 m too low, and the follow-the-centre pick
 * then drags the pin some 90 m south onto whatever that mis-aimed ray really
 * hit. Both drift away from the composition DEFAULTS describes, which is why
 * the opening view came out smaller and higher than the shared link. There is
 * nothing to fix until a surface exists, so fix it the moment one does.
 *
 * A URL carrying `ll` or `cam` is someone else's composition; leave it alone.
 */
function settleInitialView() {
  if (initialViewSettled || urlPlacedView || !tilesetVisible()) {
    settleSkipped = initialViewSettled ? 'already' : urlPlacedView ? 'url' : 'no-tileset';
    return;
  }
  initialViewSettled = true;

  // The pin must hold still while this runs. It normally tracks the middle of
  // the screen, which over an oblique view of a 48 m wall means the near rim
  // rather than the arena — so left free it slides ~100 m south mid-measurement
  // and the camera gets composed around the wrong point.
  suppressFollow = true;
  try {
    state.lat = DEFAULTS.lat;
    state.lon = DEFAULTS.lon;
    // Stated, not sampled: sampleHeightMostDetailed does not answer reliably
    // over this mesh, and the camera has to be composed before anything can be
    // measured anyway. A known viewpoint has a known floor — see DEFAULTS.
    state.groundHeight = DEFAULTS.groundHeight;
    lookAtPin(DEFAULTS.height);
  } finally {
    suppressFollow = false;
  }
  refreshSunPath();
}

function explainTileFailure(err) {
  const message = String(err?.message || err);

  // A dead connection is worth separating from anything ion did: there is
  // nothing to fix on the account, and nothing the visitor can usefully do but
  // wait — so say so plainly rather than making them doubt their token.
  if (!navigator.onLine) {
    return {
      kind: 'offline',
      reason: 'This browser is offline, so Cesium ion cannot be reached and there is no mesh to draw. The Colosseum behind this panel is a still image, not the live map — nothing can be panned, searched or measured until the connection is back. SolarGaze will try again by itself the moment it is.',
    };
  }
  // Quota exhaustion on ion surfaces as 429; a rejected credential as 400/401/403.
  if (/\b429\b|quota|rate limit/i.test(message)) {
    return {
      kind: 'quota',
      reason: 'The 3D tile quota for this Cesium ion account is spent for now. It resets at the start of the month — or sign in with another account to carry on.',
    };
  }
  if (/\b4(00|01|03)\b|denied|unauthor|forbidden|invalid/i.test(message)) {
    return {
      kind: 'credential',
      reason: 'Cesium ion turned that credential down. Sign in again, or check that a pasted token carries the <code>assets:read</code> scope.',
    };
  }
  return {
    kind: 'network',
    reason: `The 3D tiles could not be loaded (<code>${escapeHtml(message.slice(0, 160))}</code>). This is usually Cesium ion being unreachable rather than anything wrong with your account. The Colosseum behind this panel is a still image, not the live map.`,
  };
}

/**
 * A gate raised by a dropped connection should lift itself.
 *
 * Nothing about the visitor's account changed while the wifi was out, so making
 * them find the retry button is asking them to do the browser's job.
 */
window.addEventListener('online', () => {
  if (isGated()) startTiles();
});

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
      openGate();
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
  if (suppressFollow || state.prefs.pinLocked || !viewer) return false;
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

/*
 * A function declaration, not a `const` arrow, and that is load-bearing.
 *
 * boot() is called from the top of this module, long before the lines down
 * here have been evaluated, and it calls restoreFromUrl() synchronously. As a
 * `const` this sat in the temporal dead zone at that moment, so every link
 * carrying an `ll=` or `cam=` — which is every link the share button has ever
 * produced — threw before the viewer was built and left a blank page. A
 * declaration hoists, so where it sits in the file stops mattering.
 */
function isLatLon(lat, lon) {
  return Number.isFinite(lat) && Number.isFinite(lon)
    && Math.abs(lat) <= 90 && Math.abs(lon) <= 180;
}

function restoreFromUrl() {
  const hash = location.hash.replace(/^#/, '');
  if (!hash) return;
  const q = new URLSearchParams(hash);

  // Links get truncated in chat windows and hand-edited in address bars, so
  // every field is checked on its own. A NaN reaching camera.setView costs the
  // whole canvas, with nothing on screen to say why.
  const ll = q.get('ll');
  if (ll) {
    urlPlacedView = true;
    const [lat, lon] = ll.split(',').map(Number);
    if (isLatLon(lat, lon)) {
      state.lat = lat;
      state.lon = lon;
    }
  }

  const t = q.get('t');
  if (t) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(t);
    if (m) {
      const [, y, mo, d, hh, mi] = m.map(Number);
      if (mo >= 1 && mo <= 12 && d >= 1 && d <= 31 && hh <= 23 && mi <= 59) {
        state.y = y;
        state.m = mo;
        state.d = d;
        state.minutes = hh * 60 + mi;
      }
    }
  }

  const cam = q.get('cam');
  const hp = q.get('hp');
  if (cam) {
    urlPlacedView = true;
    const [lat, lon, height] = cam.split(',').map(Number);
    const [heading, pitch] = (hp || '').split(',').map(Number);
    if (isLatLon(lat, lon)) {
      pendingView = {
        lat,
        lon,
        height: Number.isFinite(height) ? height : DEFAULTS.height,
        heading: Number.isFinite(heading) ? heading : 0,
        pitch: Number.isFinite(pitch) ? pitch : DEFAULTS.pitch,
      };
    }
  }

  refreshZone();
  recompute();
}
