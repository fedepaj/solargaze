/**
 * The 3D overlay: a compass rose lying on the ground, the sun's arc for the
 * selected day, and the live sun/shadow readout.
 *
 * Geometry is built in the pin's local east-north-up frame, so "azimuth 151°,
 * elevation 42.6°" turns into a world position with no map-projection fudging.
 */

import { state, on, pointHeight } from './state.js';
import { viewer, distanceTo, requestRender } from './scene.js';
import { daySampler } from './solar.js';

const C = window.Cesium;

const ACCENT = C.Color.fromCssColorString('#f6d02f');
const ACCENT_SOFT = ACCENT.withAlpha(0.95);
const SHADOW_COL = C.Color.fromCssColorString('#eef2f8').withAlpha(0.95);

/**
 * Lines get a `depthFailMaterial` so they stay drawn where buildings would
 * hide them, dimmer rather than gone. Without it the arc kept vanishing and
 * reappearing behind rooftops as the camera moved, which read as flicker, and
 * the thin sun ray was invisible almost everywhere.
 */
const occluded = colour => new C.ColorMaterialProperty(colour.withAlpha(0.55));

/**
 * Texture t=1 maps to the top row of the canvas (Cesium uploads with flipY),
 * so canvas-up is north. Flip this if a future Cesium ever changes that.
 */
const ROSE_NORTH_UP = true;


const LABEL_BASE = {
  font: '600 13px Inter, system-ui, sans-serif',
  fillColor: C.Color.WHITE,
  // A background pill rather than an outline: Cesium renders labels from a
  // signed-distance field, and a 3 px outline on 13 px text blobs into a smear.
  style: C.LabelStyle.FILL,
  showBackground: true,
  backgroundColor: C.Color.fromCssColorString('#13161c').withAlpha(0.8),
  backgroundPadding: new C.Cartesian2(9, 6),
  verticalOrigin: C.VerticalOrigin.CENTER,
  horizontalOrigin: C.HorizontalOrigin.CENTER,
  disableDepthTestDistance: Number.POSITIVE_INFINITY,
};

let ents = null;
let rosePrimitive = null;

/**
 * Position buffers handed to Cesium through CallbackProperty.
 *
 * Assigning a fresh array to `polyline.positions` makes Cesium tear the
 * geometry down and re-batch it, which is visible as the line blinking out —
 * the exact complaint about the arc during playback and the beam while
 * scrubbing. A callback returning a buffer we mutate in place keeps the
 * primitive alive and just moves its vertices.
 */
const arcBuffer = [];
const rayBuffer = [];
const shadowBuffer = [];

/** The moving ends of the beam and the shadow line, mutated in place. */
const rayFar = new C.Cartesian3();
const shadowEnd = new C.Cartesian3();

/** Replace a buffer's contents without changing its identity. */
function fill(buffer, positions) {
  buffer.length = 0;
  for (const p of positions) buffer.push(p);
}

const bufferProperty = buffer => new C.CallbackProperty(() => buffer, false);
let queued = false;
let radius = 400;

const d2r = Math.PI / 180;

/* ── frame maths ───────────────────────────────────────────────────── */

/**
 * Everything below runs once per animation frame while the clock is playing.
 * Cesium's maths functions all take a `result`, and handing them a long-lived
 * one is the difference between a few hundred short-lived Cartesians a second
 * and none at all.
 */
const anchorPos = new C.Cartesian3();
const frameMatrix = new C.Matrix4();
const scratchOffset = new C.Cartesian3();
const scratchScale = new C.Cartesian3();

/** Allocating form, for seeding entities once at startup. */
function anchorCartesian() {
  return C.Cartesian3.fromDegrees(state.lon, state.lat, pointHeight());
}

/** The same point, written into the buffer the hot path shares. */
function updateAnchor() {
  return C.Cartesian3.fromDegrees(
    state.lon, state.lat, pointHeight(), C.Ellipsoid.WGS84, anchorPos,
  );
}

/** Local ENU direction for a bearing/altitude pair, scaled to `r` metres. */
function localOffset(azimuthDeg, elevationDeg, r, result) {
  const az = azimuthDeg * d2r;
  const el = elevationDeg * d2r;
  const h = Math.cos(el) * r;
  return C.Cartesian3.fromElements(Math.sin(az) * h, Math.cos(az) * h, Math.sin(el) * r, result);
}

/**
 * `result` is optional: omit it for a value handed to an entity, which must own
 * its Cartesian, and pass one for a value we keep mutating ourselves.
 */
function toWorld(frame, azimuthDeg, elevationDeg, r, result) {
  return C.Matrix4.multiplyByPoint(
    frame,
    localOffset(azimuthDeg, elevationDeg, r, scratchOffset),
    result || new C.Cartesian3(),
  );
}

/* ── the compass rose texture ──────────────────────────────────────── */

/**
 * Draw the graduated ring once into a canvas; it is then stretched across an
 * ellipse on the ground, which is what makes the numbers sit flat in
 * perspective instead of floating as billboards.
 */
function buildRoseCanvas(size = 1024) {
  const cv = document.createElement('canvas');
  cv.width = cv.height = size;
  const g = cv.getContext('2d');
  const c = size / 2;
  const R = size * 0.47;

  g.clearRect(0, 0, size, size);
  g.translate(c, c);
  if (!ROSE_NORTH_UP) g.scale(1, -1);

  const at = (bearing, r) => [Math.sin(bearing * d2r) * r, -Math.cos(bearing * d2r) * r];

  // Everything is stroked twice: a dark casing first, the bright line on top.
  // Over sunlit photogrammetry a plain white hairline simply disappears.
  const ring = (r, width, colour, casing) => {
    g.beginPath();
    g.arc(0, 0, r, 0, Math.PI * 2);
    g.lineWidth = width + casing;
    g.strokeStyle = 'rgba(0,0,0,0.45)';
    g.stroke();
    g.lineWidth = width;
    g.strokeStyle = colour;
    g.stroke();
  };

  ring(R, size * 0.0032, 'rgba(255,255,255,0.72)', size * 0.0034);
  ring(R * 0.965, size * 0.0018, 'rgba(255,255,255,0.3)', size * 0.0022);

  // graduations: long every 30°, short every 10°
  for (let b = 0; b < 360; b += 10) {
    const major = b % 30 === 0;
    const inner = R * (major ? 0.925 : 0.952);
    const [x1, y1] = at(b, inner);
    const [x2, y2] = at(b, R);
    g.beginPath();
    g.moveTo(x1, y1);
    g.lineTo(x2, y2);
    g.lineWidth = size * (major ? 0.0056 : 0.0040);
    g.strokeStyle = 'rgba(0,0,0,0.42)';
    g.stroke();
    g.lineWidth = size * (major ? 0.0032 : 0.0020);
    g.strokeStyle = major ? 'rgba(255,255,255,0.8)' : 'rgba(255,255,255,0.45)';
    g.stroke();
  }

  // bearing numbers, rotated to sit along the ring like a real compass card
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.font = `600 ${Math.round(size * 0.030)}px Inter, system-ui, sans-serif`;
  g.fillStyle = 'rgba(255,255,255,0.92)';
  g.shadowColor = 'rgba(0,0,0,0.75)';
  g.shadowBlur = size * 0.008;
  for (let b = 0; b < 360; b += 30) {
    if (b % 90 === 0) continue; // cardinals get letters instead
    const [x, y] = at(b, R * 0.875);
    // Read radially outward, flipped on the far side so nothing is upside down.
    const rotation = b > 90 && b < 270 ? b + 180 : b;
    g.save();
    g.translate(x, y);
    g.rotate(rotation * d2r);
    g.fillText(String(b), 0, 0);
    g.restore();
  }

  // cardinal letters, kept upright and heavier
  g.font = `700 ${Math.round(size * 0.066)}px Inter, system-ui, sans-serif`;
  g.fillStyle = '#ffffff';
  g.shadowColor = 'rgba(0,0,0,0.8)';
  g.shadowBlur = size * 0.016;
  for (const [b, letter] of [[0, 'N'], [90, 'E'], [180, 'S'], [270, 'W']]) {
    const [x, y] = at(b, R * 0.87);
    g.fillText(letter, x, y);
  }

  return cv;
}

/* ── entity construction ───────────────────────────────────────────── */

/**
 * Build the compass card as a raw primitive rather than an entity.
 *
 * Two reasons, both reported as bugs against the earlier ellipse version:
 *
 * 1. A flat disc sitting on photogrammetry z-fights wherever the ground is not
 *    level, which reads as flicker. Turning the depth test off removes the
 *    contest entirely.
 * 2. Buildings used to swallow the ring. A compass is an instrument, not a
 *    decal — it should stay legible over whatever it crosses.
 *
 * A unit PlaneGeometry lets the whole thing be rescaled by touching
 * `modelMatrix`, so following the zoom costs no geometry rebuild.
 */
function buildRosePrimitive() {
  const appearance = new C.MaterialAppearance({
    material: C.Material.fromType('Image', {
      image: buildRoseCanvas(),
      color: C.Color.WHITE,
    }),
    materialSupport: C.MaterialAppearance.MaterialSupport.TEXTURED,
    flat: true,
    translucent: true,
    // `translucent` makes Cesium supply the blending and depth-mask state; we
    // only override the depth test itself.
    renderState: {
      depthTest: { enabled: false },
      cull: { enabled: false },
    },
  });

  return new C.Primitive({
    geometryInstances: new C.GeometryInstance({
      geometry: new C.PlaneGeometry({
        vertexFormat: C.MaterialAppearance.MaterialSupport.TEXTURED.vertexFormat,
      }),
    }),
    appearance,
    asynchronous: false,
    allowPicking: false,
    modelMatrix: C.Matrix4.IDENTITY.clone(),
  });
}

export function initSunPath() {
  if (ents) return;

  // Seed with a valid, if tiny, segment. A polyline created from an empty
  // buffer can settle into a "nothing to draw" state that later fills of the
  // same array do not lift it out of.
  const seedA = anchorCartesian();
  const seedB = C.Cartesian3.add(seedA, new C.Cartesian3(1, 0, 0), new C.Cartesian3());
  for (const buffer of [arcBuffer, rayBuffer, shadowBuffer]) fill(buffer, [seedA, seedB]);

  rosePrimitive = buildRosePrimitive();
  viewer.scene.primitives.add(rosePrimitive);

  const e = viewer.entities;

  ents = {
    arc: e.add({
      polyline: {
        positions: bufferProperty(arcBuffer),
        width: 10,
        material: new C.PolylineGlowMaterialProperty({ glowPower: 0.2, color: ACCENT }),
        depthFailMaterial: occluded(ACCENT),
        arcType: C.ArcType.NONE,
      },
    }),

    // The sun is, for every practical purpose, infinitely far away. Drawing a
    // short segment to a marker on a small dome says the opposite; a long beam
    // arriving from off-scene says the true thing and reads far better.
    ray: e.add({
      polyline: {
        positions: bufferProperty(rayBuffer),
        width: 6,
        material: new C.PolylineGlowMaterialProperty({ glowPower: 0.28, color: ACCENT }),
        depthFailMaterial: occluded(ACCENT),
        arcType: C.ArcType.NONE,
      },
    }),

    shadowRay: e.add({
      polyline: {
        positions: bufferProperty(shadowBuffer),
        width: 5,
        material: new C.PolylineGlowMaterialProperty({ glowPower: 0.25, color: SHADOW_COL }),
        depthFailMaterial: occluded(SHADOW_COL),
        arcType: C.ArcType.NONE,
      },
    }),

    pin: e.add({
      position: anchorCartesian(),
      point: {
        pixelSize: 9,
        color: C.Color.WHITE,
        outlineColor: C.Color.BLACK.withAlpha(0.6),
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    }),

    sun: e.add({
      position: anchorCartesian(),
      point: {
        pixelSize: 16,
        color: ACCENT,
        outlineColor: C.Color.WHITE.withAlpha(0.85),
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: {
        ...LABEL_BASE,
        text: '',
        font: '700 15px Inter, system-ui, sans-serif',
        pixelOffset: new C.Cartesian2(0, -26),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    }),

    elevation: e.add({
      position: anchorCartesian(),
      label: {
        ...LABEL_BASE,
        text: '',
        fillColor: C.Color.WHITE.withAlpha(0.92),
        pixelOffset: new C.Cartesian2(0, 18),
      },
    }),

    azimuth: e.add({
      position: anchorCartesian(),
      label: {
        ...LABEL_BASE,
        text: '',
        fillColor: C.Color.WHITE.withAlpha(0.85),
        pixelOffset: new C.Cartesian2(0, 22),
      },
    }),

    sunrise: e.add({
      position: anchorCartesian(),
      point: { pixelSize: 7, color: ACCENT.withAlpha(0.9), disableDepthTestDistance: Number.POSITIVE_INFINITY },
      label: { ...LABEL_BASE, text: '', font: '600 12px Inter, system-ui, sans-serif', pixelOffset: new C.Cartesian2(0, -20) },
    }),

    sunset: e.add({
      position: anchorCartesian(),
      point: { pixelSize: 7, color: ACCENT.withAlpha(0.9), disableDepthTestDistance: Number.POSITIVE_INFINITY },
      label: { ...LABEL_BASE, text: '', font: '600 12px Inter, system-ui, sans-serif', pixelOffset: new C.Cartesian2(0, -20) },
    }),
  };

  entKeys = Object.keys(ents);

  // Each source touches only what it actually changes. Time moves two entities;
  // the day's arc is left alone entirely, which is what stops it flickering
  // during playback — it used to be rebuilt on every animation frame.
  on('time', () => schedule({ sun: true }));
  on('date', () => schedule({ sun: true, arc: true }));
  on('location', () => schedule({ sun: true, arc: true, frame: true }));
  on('ground', () => schedule({ sun: true, arc: true, frame: true }));
  on('altitude', () => schedule({ sun: true, arc: true, frame: true }));
  on('camera', () => schedule({ frame: true }));
  on('pref', ({ key }) => {
    if (key === 'sunPath') schedule({ sun: true, arc: true, frame: true });
  });

  // Cesium rasterises label text immediately; redo it once Inter is available.
  document.fonts?.ready.then(() => schedule());

  update();
}

/**
 * Coalesce change notifications into one pass per animation frame, remembering
 * which parts actually need redoing.
 */
const pending = { sun: false, arc: false, frame: false };

function schedule(parts = { sun: true, arc: true, frame: true }) {
  if (!ents) return;
  for (const key of Object.keys(pending)) if (parts[key]) pending[key] = true;
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    const work = { ...pending };
    pending.sun = pending.arc = pending.frame = false;
    update(work);
  });
}

const fmtTime = mins => {
  const m = ((Math.round(mins) % 1440) + 1440) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
};

/**
 * How much the viewing distance must change before the ring is resized.
 * Rebuilding the arc costs 160 sun positions, so doing it for every pixel of
 * zoom was pure waste; a 12% step is invisible and cuts rebuilds to a handful
 * per gesture.
 */
const RADIUS_STEP = 1.12;

/** How far up the sun beam reaches, in ring radii. Far enough to read as "sky". */
const BEAM_REACH = 9;



/* ── the day's arc ─────────────────────────────────────────────────── */

/**
 * The arc is two jobs that change at very different rates.
 *
 * *What* the sun does that day — a list of azimuth/elevation pairs — depends on
 * the date and the place, and costs 160 solar evaluations. *Where* those land
 * in the world depends on the anchor's frame and the ring radius, and is one
 * matrix multiply per point.
 *
 * In follow mode the anchor moves with every camera nudge, so the second job
 * genuinely has to rerun; the first almost never does. Keeping them apart is
 * what stops a pan from re-deriving the ephemeris a hundred and sixty times,
 * and it is why the RADIUS_STEP guard above is no longer the only brake.
 */
let arcSamples = [];      // flat [az0, el0, az1, el1, …] in degrees
let arcSamplesKey = '';
let riseAz = null;
let setAz = null;

function computeArcSamples() {
  // Three decimals of latitude is about 110 m, which moves the sun by well
  // under a thousandth of a degree — invisible at any ring radius we draw.
  const key = `${state.y},${state.m},${state.d},`
    + `${state.lat.toFixed(3)},${state.lon.toFixed(3)},${state.offsetMinutes}`;
  if (key === arcSamplesKey) return;
  arcSamplesKey = key;

  const { sunrise, sunset, polar } = state.events;
  const at = daySampler(state.y, state.m, state.d, state.lat, state.lon);
  const dayOffset = state.offsetMinutes;

  arcSamples.length = 0;
  riseAz = null;
  setAz = null;

  if (sunrise !== null && sunset !== null && sunset > sunrise) {
    const step = Math.max(2, (sunset - sunrise) / 160);
    for (let t = sunrise; t <= sunset; t += step) {
      const p = at(t - dayOffset);
      if (p.elevation < 0) continue;
      arcSamples.push(p.azimuth, p.elevation);
    }
    riseAz = at(sunrise - dayOffset).azimuth;
    setAz = at(sunset - dayOffset).azimuth;
  } else if (polar === 'day') {
    for (let t = 0; t < 1440; t += 10) {
      const p = at(t - dayOffset);
      arcSamples.push(p.azimuth, Math.max(p.elevation, 0));
    }
  }
}

/** Re-project the cached samples into the current frame. Allocates nothing. */
function projectArc(frame, visible) {
  const n = arcSamples.length / 2;

  // Below two points there is no line to draw. Leave the buffer holding its
  // last valid segment rather than emptying it — see the note in initSunPath.
  if (n > 1) {
    arcBuffer.length = n;
    for (let i = 0; i < n; i++) {
      const point = arcBuffer[i] || (arcBuffer[i] = new C.Cartesian3());
      toWorld(frame, arcSamples[i * 2], arcSamples[i * 2 + 1], radius, point);
    }
  }
  ents.arc.show = visible && n > 1;

  const { sunrise, sunset } = state.events;
  for (const [key, minutes, az] of [['sunrise', sunrise, riseAz], ['sunset', sunset, setAz]]) {
    const ent = ents[key];
    if (minutes === null || az === null) {
      ent.show = false;
      continue;
    }
    ent.position.setValue(toWorld(frame, az, 0, radius));
    ent.label.text.setValue(fmtTime(minutes));
    ent.show = visible;
  }
}

/* ── the pass ──────────────────────────────────────────────────────── */

let lastVisible = null;
let entKeys = [];
let lastAnchorX = NaN;
let lastAnchorY = NaN;
let lastAnchorZ = NaN;
let lastRadius = NaN;

export function update(parts = { sun: true, arc: true, frame: true }) {
  if (!ents || !viewer) return;

  const visible = state.prefs.sunPath;
  if (visible !== lastVisible) {
    lastVisible = visible;
    // Writing `show` raises a definitionChanged on every entity it touches, so
    // do it when the answer changes — not on all eight, sixty times a second.
    for (const key of entKeys) ents[key].show = visible;
    if (rosePrimitive) rosePrimitive.show = visible;
    requestRender();
  }
  if (!visible) return;

  const anchor = updateAnchor();
  const frame = C.Transforms.eastNorthUpToFixedFrame(anchor, C.Ellipsoid.WGS84, frameMatrix);

  // Resize only in steps: rebuilding for every pixel of zoom was pure waste.
  if (parts.frame) {
    const wanted = Math.min(Math.max(distanceTo(anchor) * 0.32, 35), 4000);
    if (wanted > radius * RADIUS_STEP || wanted < radius / RADIUS_STEP) radius = wanted;
  }

  // Everything built in world coordinates hangs off these two. A camera move in
  // locked mode changes neither, which is most camera moves.
  const moved = anchor.x !== lastAnchorX || anchor.y !== lastAnchorY || anchor.z !== lastAnchorZ;
  const resized = radius !== lastRadius;
  lastAnchorX = anchor.x;
  lastAnchorY = anchor.y;
  lastAnchorZ = anchor.z;
  lastRadius = radius;

  if (moved || resized) {
    C.Cartesian3.fromElements(2 * radius, 2 * radius, 1, scratchScale);
    C.Matrix4.multiplyByScale(frame, scratchScale, rosePrimitive.modelMatrix);
  }
  if (moved) ents.pin.position.setValue(C.Cartesian3.clone(anchor, new C.Cartesian3()));

  if (parts.arc) computeArcSamples();
  if (parts.arc || moved || resized) projectArc(frame, visible);
  if (parts.sun || moved || resized) updateSun(frame, anchor, visible);

  requestRender();
}

/** Everything that moves with the clock: marker, beam, shadow line, readouts. */
function updateSun(frame, anchor, visible) {
  const { elevation, azimuth } = state.sun;

  if (elevation <= -0.833) {
    ents.sun.show = false;
    ents.ray.show = false;
    ents.shadowRay.show = false;
    ents.elevation.position.setValue(toWorld(frame, 0, 0, radius * 0.25));
    ents.elevation.label.text.setValue('below horizon');
    ents.azimuth.label.text.setValue('');
    return;
  }

  const up = Math.max(elevation, 0);

  // setValue rather than assignment: assigning a Cartesian to `position` builds
  // a fresh ConstantPositionProperty each time, and setValue's own equality
  // check means an unchanged readout raises no event at all.
  ents.sun.position.setValue(toWorld(frame, azimuth, up, radius));
  ents.sun.label.text.setValue(fmtTime(state.minutes));
  ents.sun.show = visible;

  // A beam arriving from far along the sun vector, straight into the point.
  // These two keep the same Cartesians for the life of the app and are mutated
  // in place — the CallbackProperty re-reads them, so the line never blinks.
  toWorld(frame, azimuth, up, radius * BEAM_REACH, rayFar);
  if (rayBuffer[0] !== rayFar) fill(rayBuffer, [rayFar, anchor]);
  ents.ray.show = visible;

  ents.elevation.position.setValue(toWorld(frame, azimuth, up / 2, radius * 0.55));
  ents.elevation.label.text.setValue(`△ ${elevation.toFixed(1)}°`);

  // Sit outside the ring and below: at low sun the marker is almost on the
  // ring too, and the two labels would print on top of each other.
  ents.azimuth.position.setValue(toWorld(frame, azimuth, 0, radius * 1.12));
  ents.azimuth.label.text.setValue(`${azimuth.toFixed(1)}°`);

  const shadowLen = Math.min(radius * 0.9, radius / Math.max(Math.tan(elevation * d2r), 0.08) * 0.25);
  toWorld(frame, (azimuth + 180) % 360, 0, shadowLen, shadowEnd);
  if (shadowBuffer[0] !== anchor) fill(shadowBuffer, [anchor, shadowEnd]);
  ents.shadowRay.show = visible;
}

export { schedule as refreshSunPath };
