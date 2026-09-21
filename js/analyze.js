/**
 * Direct-sunlight hours at the pin.
 *
 * For each sampled minute of daylight we shoot a ray from just above the pin
 * towards the sun and ask the renderer whether anything is in the way. That is
 * the same question a shadow map answers per pixel, asked once per time step
 * for one point — cheap enough to run synchronously.
 *
 * The honest caveat, surfaced in the UI: the ray can only hit tiles that are
 * currently loaded at the resolution they are currently loaded at. Zoomed out,
 * distant towers simply are not there to block anything.
 */

import { state, pointHeight } from './state.js';
import { viewer, tilesetVisible } from './scene.js';
import { daySampler } from './solar.js';

const C = window.Cesium;
const d2r = Math.PI / 180;

/** Metres above the studied point the virtual sensor sits at. */
const SENSOR_HEIGHT = 1.5;
const STEP_MINUTES = 10;
/** How long a run of ray casts may hold the main thread before yielding. */
const FRAME_BUDGET_MS = 8;

/* One of each, reused across the whole run rather than per sample. */
const NOTHING_EXCLUDED = [];
const scratchLocal = new C.Cartesian3();
const scratchWorld = new C.Cartesian3();
const scratchDir = new C.Cartesian3();
const scratchRay = new C.Ray();

export function canAnalyze() {
  return !!viewer && typeof viewer.scene.pickFromRay === 'function' && tilesetVisible();
}

/**
 * @param {(fraction:number) => void} [onProgress] called between chunks.
 * @returns {Promise<{hours:number, samples:Array<{minutes:number, sun:boolean}>, step:number}>}
 */
export async function computeSunHours(onProgress) {
  const { sunrise, sunset, polar } = state.events;
  const samples = [];

  const from = polar === 'day' ? 0 : sunrise;
  const to = polar === 'day' ? 1440 : sunset;
  if (polar === 'night' || from === null || to === null || to <= from) {
    return { hours: 0, daylightHours: 0, samples, step: STEP_MINUTES };
  }

  const origin = C.Cartesian3.fromDegrees(state.lon, state.lat, pointHeight() + SENSOR_HEIGHT);
  const frame = C.Transforms.eastNorthUpToFixedFrame(origin);
  const scene = viewer.scene;

  // One zone resolution for the whole day, as in the overlay.
  const dayOffset = state.offsetMinutes;
  const at = daySampler(state.y, state.m, state.d, state.lat, state.lon);

  scratchRay.origin = origin;
  const total = Math.floor((to - from) / STEP_MINUTES) + 1;
  let deadline = performance.now() + FRAME_BUDGET_MS;

  for (let t = from; t <= to; t += STEP_MINUTES) {
    const { elevation, azimuth } = at(t - dayOffset);

    if (elevation <= 0) {
      samples.push({ minutes: t, sun: false });
    } else {
      const az = azimuth * d2r;
      const el = elevation * d2r;
      C.Cartesian3.fromElements(
        Math.sin(az) * Math.cos(el),
        Math.cos(az) * Math.cos(el),
        Math.sin(el),
        scratchLocal,
      );
      C.Matrix4.multiplyByPoint(frame, scratchLocal, scratchWorld);
      C.Cartesian3.subtract(scratchWorld, origin, scratchDir);
      C.Cartesian3.normalize(scratchDir, scratchRay.direction);

      let blocked = false;
      try {
        const hit = scene.pickFromRay(scratchRay, NOTHING_EXCLUDED);
        blocked = !!(hit && C.defined(hit.position));
      } catch {
        blocked = false;   // unsupported or nothing loaded: report open sky
      }

      samples.push({ minutes: t, sun: !blocked });
    }

    // pickFromRay is synchronous and there are dozens of them. Left in one
    // block the whole run lands as a single long frame: the button never
    // repaints and the page stops answering the mouse.
    if (performance.now() >= deadline) {
      onProgress?.(samples.length / total);
      await new Promise(r => requestAnimationFrame(r));
      deadline = performance.now() + FRAME_BUDGET_MS;
    }
  }

  // Weight by the window, not by the sample count. n samples ten minutes apart
  // span n-1 intervals, so charging each one a full step bills ten minutes that
  // are not in the day: a clear day in Turin came out 12.5 h against 12.34 h of
  // actual daylight. The sunny *fraction* is what the samples measure honestly;
  // the window length is known exactly.
  const sunny = samples.filter(s => s.sun).length;
  const daylightHours = (to - from) / 60;
  return {
    hours: samples.length ? daylightHours * (sunny / samples.length) : 0,
    daylightHours,
    samples,
    step: STEP_MINUTES,
  };
}
