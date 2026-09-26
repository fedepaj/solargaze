/**
 * Wind, as something you can see moving.
 *
 * A few hundred particles drift through a box of air around the pin, each
 * pushed every frame by the wind the model gives for where it is right now
 * and trailing a short fading tail behind it. The box follows the zoom the
 * way the compass card does, so the flow reads at street scale and at city
 * scale alike, and its particles sit at a spread of heights above the ground
 * so that the layer has depth rather than being a sheet.
 *
 * What it is: the model's 10 m wind, interpolated between grid cells, drawn at
 * a speed that makes it legible — a 5 m/s breeze crosses the box in about six
 * seconds regardless of how wide the box is. What it is not: the flow between
 * these particular buildings. That would need a fluid model of the street
 * canyon, and nothing here pretends to one; particles pass through walls.
 *
 * Under `requestRenderMode` every frame of this costs a full redraw, shadows
 * included, so the loop runs only while the layer is on and the page visible,
 * and it is capped well below the display's refresh rate.
 */

import { state, pointHeight } from '../state.js';
import { viewer, distanceTo, requestRender } from '../scene.js';
import { windAt, compassName } from './field.js';
import { createFlow } from './flow.js';

const C = window.Cesium;

/** Tail length in samples; also the fixed vertex count of every trail. */
const TAIL = 9;
const FPS = 24;
/** How much the viewing distance must change before the box is resized. */
const RADIUS_STEP = 1.15;

const scratch = new C.Cartesian3();
const frameMatrix = new C.Matrix4();
const anchorPos = new C.Cartesian3();

export function createWind(scene, { count = 320 } = {}) {
  const lines = scene.primitives.add(new C.PolylineCollection());
  const labels = scene.primitives.add(new C.LabelCollection());

  // One material shared by every trail: Cesium batches polylines by material,
  // so this is one draw call rather than three hundred.
  const trailMaterial = C.Material.fromType('Fade', {
    fadeInColor: C.Color.fromCssColorString('#dff5ff').withAlpha(0.95),
    fadeOutColor: C.Color.fromCssColorString('#dff5ff').withAlpha(0),
    time: new C.Cartesian2(1, 0.5),
    fadeDirection: { x: true, y: false },
    maximumDistance: 1,
    repeat: false,
  });

  const vane = lines.add({
    positions: [new C.Cartesian3(1, 0, 0), new C.Cartesian3(2, 0, 0)],
    width: 12,
    material: C.Material.fromType('PolylineArrow', { color: C.Color.fromCssColorString('#8fe3ff') }),
    show: false,
  });
  const vaneLabel = labels.add({
    text: '',
    font: '600 13px Inter, system-ui, sans-serif',
    fillColor: C.Color.WHITE,
    showBackground: true,
    backgroundColor: C.Color.fromCssColorString('#13161c').withAlpha(0.8),
    backgroundPadding: new C.Cartesian2(9, 6),
    verticalOrigin: C.VerticalOrigin.BOTTOM,
    horizontalOrigin: C.HorizontalOrigin.CENTER,
    pixelOffset: new C.Cartesian2(0, -10),
    disableDepthTestDistance: Number.POSITIVE_INFINITY,
    show: false,
  });

  /* Particle state, in metres east/north/up of the anchor. */
  const px = new Float32Array(count);
  const py = new Float32Array(count);
  const pz = new Float32Array(count);      // as a fraction of the base height
  const age = new Float32Array(count);
  const life = new Float32Array(count);
  const trails = [];                        // TAIL Cartesian3s per particle
  const polylines = [];

  for (let i = 0; i < count; i++) {
    const tail = [];
    for (let k = 0; k < TAIL; k++) tail.push(new C.Cartesian3());
    trails.push(tail);
    polylines.push(lines.add({ positions: tail, width: 2.2, material: trailMaterial, show: false }));
  }

  let series = null;
  /** The street-level solver; it holds a field only while a tile's mask is set. */
  const flow = createFlow();
  let radius = 400;
  let anchor = { lat: state.lat, lon: state.lon, ground: 0 };
  let running = false;
  let rafId = 0;
  let last = 0;
  let acc = 0;

  const seed = i => {
    px[i] = (Math.random() * 2 - 1) * radius;
    py[i] = (Math.random() * 2 - 1) * radius;
    pz[i] = 0.5 + Math.random();
    age[i] = 0;
    life[i] = 5 + Math.random() * 9;
  };
  for (let i = 0; i < count; i++) seed(i);

  /**
   * Lay a whole tail out behind a particle, one frame's travel per vertex.
   *
   * Every vertex must be written: a Cartesian left at its default is the centre
   * of the Earth, and a trail with one of those in it draws as a line from
   * underground up to the particle. The vertices must also differ, because
   * Cesium drops repeated positions and a trail that collapses to a point has
   * to be rebuilt as a different size — hence the tiny spread for a particle
   * that has not moved yet.
   */
  function layTail(i, dx, dy) {
    if (Math.abs(dx) + Math.abs(dy) < 1e-3) { dx = 0.01; dy = 0.01; }
    const tail = trails[i];
    for (let k = 0; k < TAIL; k++) {
      const back = TAIL - 1 - k;
      toWorld(px[i] - dx * back, py[i] - dy * back, pz[i], tail[k]);
    }
  }

  /** World position of a local (east, north) offset at this particle's height. */
  function toWorld(x, y, zFrac, out) {
    C.Cartesian3.fromElements(x, y, baseHeight() * zFrac, scratch);
    return C.Matrix4.multiplyByPoint(frameMatrix, scratch, out);
  }

  /**
   * Metres above the anchor's ground the layer floats at, mid-spread. With
   * the buildings in play the particles drop to street level, where the
   * solved flow is; without them they ride above the roofs as before.
   */
  const baseHeight = () => (flow.hasMask
    ? Math.min(Math.max(radius * 0.03, 6), 40)
    : Math.min(Math.max(radius * 0.1, 15), 400));

  /** Metres of box per m/s per second: a 5 m/s wind crosses the radius in ~6 s. */
  const flowScale = () => radius / 30;

  function reanchor() {
    anchor = { lat: state.lat, lon: state.lon, ground: pointHeight() };
    C.Cartesian3.fromDegrees(anchor.lon, anchor.lat, anchor.ground, C.Ellipsoid.WGS84, anchorPos);
    C.Transforms.eastNorthUpToFixedFrame(anchorPos, C.Ellipsoid.WGS84, frameMatrix);
  }

  /** The box tracks the zoom in steps, and the particles are scaled with it. */
  function fitRadius() {
    const wanted = Math.min(Math.max(distanceTo(anchorPos) * 0.55, 60), 25000);
    if (wanted > radius * RADIUS_STEP || wanted < radius / RADIUS_STEP) {
      const k = wanted / radius;
      for (let i = 0; i < count; i++) { px[i] *= k; py[i] *= k; }
      radius = wanted;
      return true;
    }
    return false;
  }

  const metresPerDegLat = 111320;

  function windAtLocal(x, y, t) {
    const lat = anchor.lat + y / metresPerDegLat;
    const lon = anchor.lon + x / (metresPerDegLat * Math.cos(anchor.lat * Math.PI / 180));
    return windAt(series, lat, lon, t);
  }

  /**
   * The wind a particle feels. Regional wind from the model; where the
   * buildings' flow field covers the spot, its direction and relative
   * speed instead, scaled by the regional speed at the anchor — so a
   * street canyon aligned with the wind runs faster than its neighbours,
   * and a courtyard barely moves.
   */
  function windFor(x, y, t, ref) {
    const coarse = windAtLocal(x, y, t);
    if (!coarse || !ref) return coarse;
    const lat = anchor.lat + y / metresPerDegLat;
    const lon = anchor.lon + x / (metresPerDegLat * Math.cos(anchor.lat * Math.PI / 180));
    const fine = flow.sample(lat, lon);
    if (!fine) return coarse;
    if (fine.solid) return { u: 0, v: 0, speed: 0, solid: true };
    return { u: fine.u * ref.speed, v: fine.v * ref.speed, speed: Math.hypot(fine.u, fine.v) * ref.speed };
  }

  function step(dt) {
    const t = state.utc.getTime();
    const scale = flowScale();
    const ref = windAt(series, anchor.lat, anchor.lon, t);
    if (ref && flow.hasMask) flow.ensure(anchor.lat, anchor.lon, ref.from);
    for (let i = 0; i < count; i++) {
      const w = windFor(px[i], py[i], t, ref);
      if (!w) { polylines[i].show = false; continue; }
      px[i] += w.u * scale * dt;
      py[i] += w.v * scale * dt;
      age[i] += dt;
      const tail = trails[i];
      // A particle that has drifted into a wall, or aged out, or left the
      // box, starts again somewhere in the open air.
      if (w.solid || age[i] > life[i] || Math.abs(px[i]) > radius || Math.abs(py[i]) > radius) {
        seed(i);
        for (let tries = 0; tries < 8 && windFor(px[i], py[i], t, ref)?.solid; tries++) seed(i);
        const w2 = windFor(px[i], py[i], t, ref) || w;
        layTail(i, w2.u * scale * dt, w2.v * scale * dt);
      } else {
        // Shift the tail down and put the new head at the end (head is s=1).
        const first = tail[0];
        for (let k = 0; k < TAIL - 1; k++) tail[k] = tail[k + 1];
        tail[TAIL - 1] = toWorld(px[i], py[i], pz[i], first);
      }
      // Reassigned rather than mutated: the setter is what marks it dirty.
      polylines[i].positions = tail;
      polylines[i].show = true;
    }
    paintVane(t);
  }

  function paintVane(t) {
    const w = windAt(series, anchor.lat, anchor.lon, t);
    if (!w || w.speed < 0.05) {
      vane.show = false;
      vaneLabel.show = false;
      return;
    }
    const len = Math.min(Math.max(w.speed * flowScale() * 1.2, radius * 0.12), radius * 0.85);
    const ux = w.u / w.speed;
    const uy = w.v / w.speed;
    const h = 0.8;
    const from = toWorld(-ux * len * 0.5, -uy * len * 0.5, h, new C.Cartesian3());
    const to = toWorld(ux * len * 0.5, uy * len * 0.5, h, new C.Cartesian3());
    vane.positions = [from, to];
    vane.show = true;
    vaneLabel.position = to;
    vaneLabel.text = `${w.speed.toFixed(1)} m/s · from ${compassName(w.from)}${flow.ready ? ' · streets' : ''}`;
    vaneLabel.show = true;
  }

  function tick(now) {
    if (!running) return;
    rafId = requestAnimationFrame(tick);
    const dt = Math.min((now - last) / 1000, 0.1);
    last = now;
    acc += dt;
    if (acc < 1 / FPS) return;
    const frameDt = acc;
    acc = 0;
    if (!series) return;
    // Re-anchor once the pin has wandered a good way out of the box.
    const drift = Math.hypot(
      (state.lat - anchor.lat) * metresPerDegLat,
      (state.lon - anchor.lon) * metresPerDegLat * Math.cos(anchor.lat * Math.PI / 180),
    );
    if (drift > radius * 0.5 || Math.abs(pointHeight() - anchor.ground) > 5) reanchor();
    fitRadius();
    step(frameDt);
    requestRender();
  }

  function start() {
    if (running) return;
    running = true;
    reanchor();
    fitRadius();
    for (let i = 0; i < count; i++) layTail(i, 0, 0);
    last = performance.now();
    acc = 1;               // draw on the very first frame
    rafId = requestAnimationFrame(tick);
  }

  function stop() {
    running = false;
    cancelAnimationFrame(rafId);
    for (const line of polylines) line.show = false;
    vane.show = false;
    vaneLabel.show = false;
    requestRender();
  }

  // A tab in the background gets no frames; when it comes back, do not try to
  // catch up on a minute of missed simulation in one jump.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { last = performance.now(); acc = 0; }
  });

  return {
    setSeries(s) { series = s; },
    /** The tile's building mask, or null: with it the particles go down to the streets. */
    setMask(mask) {
      flow.setMask(mask);
      if (running) reanchor();
    },
    start,
    stop,
    get running() { return running; },
    destroy() {
      stop();
      flow.destroy();
      scene.primitives.remove(lines);
      scene.primitives.remove(labels);
    },
  };
}
