/**
 * Cesium scene: camera, Google Photorealistic 3D Tiles, sun-driven shadows.
 *
 * The sun is not faked. CesiumJS derives the solar direction from
 * `clock.currentTime` using its own ephemeris, so feeding the clock the right
 * UTC instant makes both the lighting and the shadow map physically correct;
 * `solar.js` only computes the numbers the UI prints.
 */

import { state, emit, setGroundHeight } from './state.js';
import { DEFAULTS, ION_GOOGLE_3D_ASSET } from './config.js';

const C = window.Cesium;

export let viewer = null;
export let tileset = null;
let osmLayer = null;

/** Build the viewer. Returns as soon as the canvas is live; tiles stream in after. */
export function createViewer(container) {
  viewer = new C.Viewer(container, {
    baseLayer: false,           // no Cesium ion imagery request
    baseLayerPicker: false,
    geocoder: false,            // we drive search ourselves
    homeButton: false,
    sceneModePicker: false,
    navigationHelpButton: false,
    fullscreenButton: false,
    vrButton: false,
    animation: false,
    timeline: false,
    infoBox: false,
    selectionIndicator: false,
    shouldAnimate: false,
    // The scene is static until someone moves something. Redrawing the shadow
    // map and the photogrammetry sixty times a second to produce an identical
    // frame was this app's largest fixed cost; now Cesium draws when there is a
    // reason to. Anything we change behind its back must ask — see
    // requestRender() below.
    requestRenderMode: true,
    shadows: true,
    terrainShadows: C.ShadowMode.DISABLED,
    msaaSamples: 4,
    contextOptions: { webgl: { powerPreference: 'high-performance' } },
  });

  const { scene, clock } = viewer;

  clock.shouldAnimate = false;
  clock.currentTime = C.JulianDate.fromDate(state.utc);

  scene.light = new C.SunLight();
  scene.highDynamicRange = false;
  if (scene.skyAtmosphere) scene.skyAtmosphere.show = true;
  if (scene.fog) scene.fog.enabled = true;

  // The globe is only the fallback basemap; photorealistic tiles replace it.
  if (scene.globe) {
    scene.globe.show = false;
    scene.globe.depthTestAgainstTerrain = true;
    scene.globe.baseColor = C.Color.fromCssColorString('#1b2028');
    scene.globe.enableLighting = true;
  }

  applyShadowSettings();

  const cam = scene.screenSpaceCameraController;
  cam.enableCollisionDetection = true;
  cam.minimumZoomDistance = 8;
  cam.maximumZoomDistance = 12_000_000;

  // Frame the pin rather than sit on top of it: `setView` with a destination
  // puts the camera *at* that point, which pitches the pin out of shot.
  lookAtPin(DEFAULTS.height);

  // Fraction of the view that must change before `changed` fires; the default
  // 0.5 is far too coarse to keep the compass and the overlay scale in step.
  viewer.camera.percentageChanged = 0.02;
  viewer.camera.changed.addEventListener(onCameraMoved);
  viewer.camera.moveEnd.addEventListener(onCameraMoved);

  return viewer;
}

/**
 * Ask for one frame.
 *
 * Cesium redraws by itself for camera moves, tile streaming, clock changes and
 * entity edits. What it cannot see is a buffer mutated in place behind a
 * CallbackProperty, or a primitive's modelMatrix written directly — the two
 * tricks the sun-path overlay is built on. Those callers say so here.
 */
export function requestRender() {
  viewer?.scene.requestRender();
}

function onCameraMoved() {
  tuneShadowDistance();
  emit('camera', viewer.camera);
}

/**
 * Cesium spreads a fixed shadow-map budget over `maximumDistance`, so a value
 * good for a street is wasteful for a skyline and vice versa. Track the camera.
 */
function tuneShadowDistance() {
  if (!viewer?.shadowMap) return;
  const height = viewer.camera.positionCartographic?.height ?? 1000;
  viewer.shadowMap.maximumDistance = Math.min(Math.max(height * 4, 1500), 30000);
}

/** Push the current shadow preferences into the renderer. */
export function applyShadowSettings() {
  if (!viewer) return;
  const { prefs } = state;
  viewer.shadows = prefs.shadows;

  const sm = viewer.shadowMap;
  sm.enabled = prefs.shadows;
  sm.softShadows = prefs.softShadows;
  sm.size = prefs.shadowQuality;
  sm.darkness = 0.32;
  sm.normalOffset = false;      // keeps contact shadows tight on building faces
  sm.fadingEnabled = false;     // otherwise low-sun shadows wash out — the very
  tuneShadowDistance();         // case a shadow study is about
  if (tileset) {
    tileset.shadows = prefs.shadows ? C.ShadowMode.ENABLED : C.ShadowMode.DISABLED;
  }
  requestRender();
}

/**
 * Push the mesh-detail preference into the live tileset.
 *
 * `maximumScreenSpaceError` is how many pixels of error Cesium will tolerate
 * before fetching a finer tile, so it is the direct lever on how fast the ion
 * quota in the usage meter is spent. 16 is Cesium's default; 24 is noticeably
 * cheaper and, on an oblique shadow study, hard to tell apart.
 */
export function applyMeshDetail() {
  if (tileset) tileset.maximumScreenSpaceError = state.prefs.meshDetail;
  requestRender();
}

/** Keep the render clock in step with the app clock. */
export function syncClock() {
  if (!viewer) return;
  viewer.clock.currentTime = C.JulianDate.fromDate(state.utc);
  viewer.scene.requestRender?.();
}

/* ── basemaps ──────────────────────────────────────────────────────── */

/**
 * Google Photorealistic 3D Tiles — the Google Earth mesh — served through
 * Cesium ion.
 *
 * ion brokers Google's credential, so nobody using this needs a Google Cloud
 * project or a card on file. The tile traffic is billed to whoever owns the
 * token, which after an OAuth sign-in is the visitor themselves.
 */
export async function loadIonTiles(token) {
  if (!token) throw new Error('no-token');
  if (tileset) return tileset;

  C.Ion.defaultAccessToken = token;
  // Built into a local first, and only published to the module binding once it
  // is actually in the scene. `hasTileset()` is then never true for a tileset
  // that failed on the way up, so a rejected credential falls cleanly through
  // to the next rung of startTiles().
  const loaded = await C.Cesium3DTileset.fromIonAssetId(ION_GOOGLE_3D_ASSET, {
    maximumScreenSpaceError: state.prefs.meshDetail,
    shadows: state.prefs.shadows ? C.ShadowMode.ENABLED : C.ShadowMode.DISABLED,
    showCreditsOnScreen: true,
  });

  viewer.scene.primitives.add(loaded);
  if (viewer.scene.globe) viewer.scene.globe.show = false;
  tileset = loaded;

  // The tileset resolving and there being geometry under the cursor are two
  // different moments; this is the second one.
  tileset.initialTilesLoaded.addEventListener(() => emit('mesh-ready', tileset));

  emit('basemap', { kind: 'ion' });
  return tileset;
}

/** Keyless fallback: OSM raster on the ellipsoid. No 3D geometry, no shadows. */
export function useFlatBasemap() {
  if (!viewer) return;
  if (tileset) {
    tileset.show = false;
  }
  if (viewer.scene.globe) {
    viewer.scene.globe.show = true;
    if (!osmLayer) {
      osmLayer = viewer.imageryLayers.addImageryProvider(
        new C.OpenStreetMapImageryProvider({ url: 'https://tile.openstreetmap.org/' }),
      );
    }
    osmLayer.show = true;
  }
  requestRender();
  emit('basemap', { kind: 'flat' });
}

export function usePhotorealisticBasemap() {
  if (!viewer || !tileset) return false;
  tileset.show = true;
  if (osmLayer) osmLayer.show = false;
  if (viewer.scene.globe) viewer.scene.globe.show = false;
  requestRender();
  emit('basemap', { kind: 'google' });
  return true;
}

export const hasTileset = () => !!tileset;
export const tilesetVisible = () => !!tileset && tileset.show;

/* ── camera helpers ────────────────────────────────────────────────── */

/** Centre the camera on the sun pin at `range` metres, keeping the bearing. */
export function lookAtPin(range, { heading = null, pitchRadians = null } = {}) {
  if (!viewer) return;
  const cam = viewer.camera;
  cam.lookAt(
    C.Cartesian3.fromDegrees(state.lon, state.lat, state.groundHeight),
    new C.HeadingPitchRange(
      heading === null ? C.Math.toRadians(DEFAULTS.heading) : heading,
      pitchRadians === null ? C.Math.toRadians(DEFAULTS.pitch) : pitchRadians,
      range,
    ),
  );
  // Release the reference frame, or every later camera move orbits the pin.
  cam.lookAtTransform(C.Matrix4.IDENTITY);
}

/** Distance in metres from the camera to a cartesian point. */
export function distanceTo(cartesian) {
  if (!viewer || !cartesian) return 1000;
  return C.Cartesian3.distance(viewer.camera.positionWC, cartesian);
}

export function flyToLocation(lat, lon, { height, duration = 1.6 } = {}) {
  if (!viewer) return Promise.resolve();
  const cam = viewer.camera;
  const current = cartographicOf(cam.positionWC);
  const h = height ?? Math.min(Math.max(current?.height ?? DEFAULTS.height, 250), 2500);

  // Approach at an angle rather than straight down: an oblique view is what
  // makes shadow length readable.
  const pitch = C.Math.toRadians(-40);
  return new Promise(resolve => {
    cam.flyToBoundingSphere(
      new C.BoundingSphere(C.Cartesian3.fromDegrees(lon, lat, 0), 1),
      {
        duration,
        offset: new C.HeadingPitchRange(cam.heading, pitch, h),
        complete: resolve,
        cancel: resolve,
      },
    );
  });
}

export function cartographicOf(cartesian) {
  if (!cartesian) return null;
  const c = C.Cartographic.fromCartesian(cartesian);
  return c ? { lat: C.Math.toDegrees(c.latitude), lon: C.Math.toDegrees(c.longitude), height: c.height } : null;
}

export function zoomBy(factor) {
  if (!viewer) return;
  const cam = viewer.camera;
  const centre = pickCentre();
  const dist = centre ? distanceTo(centre) : cam.positionCartographic.height;
  const delta = dist * (factor > 0 ? 0.45 : -0.8);
  if (factor > 0) cam.zoomIn(delta);
  else cam.zoomOut(-delta);
}

/** Whatever is under the middle of the screen — tiles first, ellipsoid second. */
export function pickCentre(options) {
  if (!viewer) return null;
  const canvas = viewer.scene.canvas;
  const centre = new C.Cartesian2(canvas.clientWidth / 2, canvas.clientHeight / 2);
  return pickAt(centre, options);
}

/**
 * What is under a screen point.
 *
 * The ellipsoid fallback is a trap while the 3D mesh is up: it never fails, and
 * it always answers "height 0" — which in Turin is 290 m below the street. Code
 * that needs a real surface must say so, and be told nothing rather than be
 * told zero.
 */
export function pickAt(windowPosition, { requireGeometry = false } = {}) {
  if (!viewer) return null;
  const scene = viewer.scene;
  if (scene.pickPositionSupported) {
    const p = scene.pickPosition(windowPosition);
    if (C.defined(p) && isPlausibleSurface(p)) return p;
  }
  if (requireGeometry) return null;
  return viewer.camera.pickEllipsoid(windowPosition, C.Ellipsoid.WGS84) || null;
}

/**
 * While tiles are still streaming, the depth buffer is a patchwork and
 * `pickPosition` will happily return a point tens of kilometres away and
 * kilometres underground. Nothing downstream can tell that from a real answer,
 * so reject it here: no surface on Earth sits below the Dead Sea shore or above
 * Everest, with room to spare.
 */
function isPlausibleSurface(cartesian) {
  const carto = C.Cartographic.fromCartesian(cartesian);
  if (!carto || !Number.isFinite(carto.height)) return false;
  return carto.height > -1000 && carto.height < 12000;
}

/** Point the camera north while keeping the current target and pitch. */
export function resetBearing() {
  if (!viewer) return;
  const cam = viewer.camera;
  const target = pickCentre();
  if (!target) {
    cam.flyTo({
      destination: cam.positionWC.clone(),
      orientation: { heading: 0, pitch: cam.pitch, roll: 0 },
      duration: 0.6,
    });
    return;
  }
  cam.flyToBoundingSphere(new C.BoundingSphere(target, 1), {
    duration: 0.6,
    offset: new C.HeadingPitchRange(0, cam.pitch, distanceTo(target)),
  });
}

/**
 * Authoritative height of the mesh under the pin.
 *
 * Unlike `sampleGroundAtPin` below, this streams the tiles it needs before
 * answering, so it is right even where the mesh has not arrived yet. That
 * costs frames, so it is called deliberately rather than on every camera move:
 * when the pin is locked, when a drag ends, and after a search flight.
 *
 * Getting it wrong is not cosmetic. The compass card is drawn on a plane at
 * this height with the depth test off, so a height out by h metres slides the
 * whole card sideways by about h/tan(pitch) in an oblique view — which looks
 * exactly like the cardinal points pointing the wrong way.
 */
export async function resolveGroundAtPin() {
  if (!viewer || !viewer.scene.sampleHeightSupported || !tilesetVisible()) return;
  const carto = C.Cartographic.fromDegrees(state.lon, state.lat);
  // This streams the tiles it needs across several frames. Under
  // requestRenderMode nothing else would ask for those frames, so it would sit
  // waiting for a camera move that may never come: drive them until it answers.
  const pump = setInterval(() => viewer.scene.requestRender(), 32);
  try {
    const [result] = await viewer.scene.sampleHeightMostDetailed([carto]);
    if (result && Number.isFinite(result.height)) setGroundHeight(result.height);
  } catch { /* nothing sampleable there */ } finally {
    clearInterval(pump);
  }
}

/**
 * The cheap probe: whatever is already resident under the pin, this frame.
 * Answers nothing rather than something wrong when the tiles are not there.
 */
export function sampleGroundAtPin() {
  if (!viewer || !viewer.scene.sampleHeightSupported || !tilesetVisible()) return;
  const carto = C.Cartographic.fromDegrees(state.lon, state.lat);
  let h;
  try {
    h = viewer.scene.sampleHeight(carto, []);
  } catch {
    return;
  }
  if (typeof h === 'number' && Number.isFinite(h)) setGroundHeight(h);
}
