/**
 * The canned day, for when Cesium ion cannot be reached.
 *
 * Twenty renders of the Colosseum from one fixed camera, forty minutes apart
 * across a whole day, encoded as a video and scrubbed rather than played —
 * because the camera never moves, inter-frame compression takes twenty
 * 1024×640 plates down to well under a megabyte, where the same frames as
 * separate images came to seven.
 *
 * What it is for: a visitor who arrives while ion is down or a quota is spent
 * sees the thing the app does, and can drag the time slider through it, instead
 * of a dead page and an apology. What it is NOT: a working app. Every other
 * control is dead, because every other control needs geometry that is not
 * there — you cannot search, move, or measure a picture.
 *
 * The honest limit: this file has to be fetched like anything else, so a
 * browser that is genuinely offline on a first visit cannot have it either. It
 * covers ion being unreachable while the network is fine, which is the common
 * case. Surviving a real offline start would need a service worker holding
 * these bytes from a previous visit.
 */

import { state, on, setTime } from '../state.js';

const $ = id => document.getElementById(id);

/** Wall-clock minute of each captured frame, in file order. */
const FRAME_MINUTES = [
  400, 440, 480, 520, 560, 600, 640, 680, 720, 760,
  800, 840, 880, 920, 960, 1000, 1040, 1080, 1120, 1160,
];
/** The encode ran at 2 fps, so frame i sits at i/2 seconds. */
const FPS = 2;

let active = false;
let video = null;
let seeking = false;
let pendingIndex = null;
let blobUrl = null;

export const isDemoActive = () => active;

/**
 * Show the canned day. Resolves false when the video cannot be fetched — an
 * offline first visit — so the caller can fall back to the plain gate.
 */
export function startOfflineDemo({ reason = '', onRetry = null } = {}) {
  if (active) return Promise.resolve(true);
  video = $('demo-video');
  if (!video) return Promise.resolve(false);

  return load(reason, onRetry).catch(() => false);
}

/**
 * Fetch the whole file and hand the element a blob URL.
 *
 * Streaming it straight from the server would be the obvious thing, and it does
 * not work: seeking needs HTTP range requests, and a server without them hands
 * back a video whose `seekable` range is empty — `currentTime` simply refuses
 * to move, so every frame of the day shows the same picture. A blob is seekable
 * unconditionally. It also makes the success test honest: the promise resolves
 * only once all 820 KB are actually here, which is exactly the question being
 * asked when the network is the thing that failed.
 */
async function load(reason, onRetry) {
  const res = await fetch('./docs/offline-colosseum.webm', { cache: 'force-cache' });
  if (!res.ok) return false;
  const url = URL.createObjectURL(await res.blob());

  let guard = null;
  const ready = new Promise((ok, fail) => {
    video.addEventListener('loadeddata', ok, { once: true });
    video.addEventListener('error', fail, { once: true });
    guard = setTimeout(fail, 12_000);
  });
  video.src = url;
  video.load();
  try {
    await ready;
  } catch (err) {
    // A decoder that never answers must not leave 820 KB pinned in the tab.
    URL.revokeObjectURL(url);
    video.removeAttribute('src');
    throw err;
  } finally {
    clearTimeout(guard);
  }

  active = true;
  blobUrl = url;
  $('demo').hidden = false;
  document.body.classList.add('is-demo');
  $('demo-note').innerHTML = reason;
  $('demo-retry').onclick = () => onRetry?.();
  video.addEventListener('seeked', onSeeked);
  on('time', paint);
  paint();
  return true;
}

/** Snap the slider's minute to the nearest captured frame and seek to it. */
function paint() {
  if (!active || !video) return;

  let best = 0;
  let gap = Infinity;
  for (let i = 0; i < FRAME_MINUTES.length; i++) {
    const d = Math.abs(FRAME_MINUTES[i] - state.minutes);
    if (d < gap) { gap = d; best = i; }
  }
  $('demo-time').textContent = label(FRAME_MINUTES[best]);

  // One seek in flight at a time; dragging the slider fires far faster than a
  // decoder can answer, and queuing them all makes the picture lag the handle.
  if (seeking) { pendingIndex = best; return; }
  seek(best);
}

function seek(index) {
  seeking = true;
  // Half a frame in, so rounding never lands on the boundary of the next one.
  video.currentTime = (index + 0.5) / FPS;
}

function onSeeked() {
  seeking = false;
  if (pendingIndex === null) return;
  const next = pendingIndex;
  pendingIndex = null;
  seek(next);
}

const label = m =>
  `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;

/** Tear the demo down — the mesh is up, the real thing takes over. */
export function stopOfflineDemo() {
  if (!active) return;
  active = false;
  document.body.classList.remove('is-demo');
  const el = $('demo');
  if (el) el.hidden = true;
  if (video) {
    video.removeEventListener('seeked', onSeeked);
    video.removeAttribute('src');
    video.load();          // drop the decoded frames rather than hide them
  }
  if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = null; }
  seeking = false;
  pendingIndex = null;
}

/**
 * Put the slider somewhere the demo actually has a frame for.
 *
 * The app opens at 10:03 and the plates start at 06:40; landing between two of
 * them is fine, but starting mid-morning shows the shape of the day best.
 */
export function seedDemoTime() {
  if (state.minutes < FRAME_MINUTES[0] || state.minutes > FRAME_MINUTES.at(-1)) {
    setTime(FRAME_MINUTES[Math.floor(FRAME_MINUTES.length / 2)]);
  }
}
