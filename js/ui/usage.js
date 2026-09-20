/**
 * Cesium ion activity meter.
 *
 * Be clear about what this is: ion exposes no usage or quota endpoint. I
 * checked — `/v1/me` returns only `{id, scopes}`, and `/v1/usage`, `/v1/quota`
 * and `/v1/subscriptions` do not exist. So nothing here is your real balance.
 *
 * What it *is*: a count of the tiles this browser has pulled since the page
 * loaded, shown against a soft reference so a newcomer understands that the
 * free tier is finite and that panning around the planet spends it. The chip
 * links to ion's dashboard, which is the only place with the true figure.
 */

import { state, on } from '../state.js';
import { tileset } from '../scene.js';

/** Tiles in a comfortable month of casual use. A yardstick, not a quota. */
const REFERENCE_TILES = 40000;
const DASHBOARD = 'https://ion.cesium.com/usage';
const STORE = 'solargaze.ionUsage';

/**
 * Kept in localStorage and bucketed by calendar month, because that is how ion
 * resets its free tier. A counter that forgot everything on reload could not
 * tell you anything about a limit you approach over days.
 */
function load() {
  const month = new Date().toISOString().slice(0, 7);
  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || 'null');
    if (saved && saved.month === month) return saved;
  } catch { /* ignore */ }
  return { month, tiles: 0 };
}

function save(record) {
  try { localStorage.setItem(STORE, JSON.stringify(record)); } catch { /* ignore */ }
}

let record = load();
let tilesLoaded = record.tiles;
let lastSaved = 0;
let painted = -1;
let listening = false;

export function initUsage() {
  const chip = document.getElementById('ion-usage');
  if (!chip) return;

  chip.addEventListener('click', () => window.open(DASHBOARD, '_blank', 'noopener'));
  on('basemap', attach);
  attach();
}

function attach() {
  const chip = document.getElementById('ion-usage');
  if (!chip) return;

  if (state.tileSource !== 'ion' || !tileset) {
    chip.hidden = true;
    return;
  }
  chip.hidden = false;
  paint();

  if (listening) return;
  listening = true;

  // `tileLoad` fires once per tile that finishes streaming in.
  tileset.tileLoad.addEventListener(() => {
    tilesLoaded += 1;
    // Writing on every tile would hammer localStorage; every 50 is plenty.
    if (tilesLoaded - lastSaved >= 50) {
      lastSaved = tilesLoaded;
      record = { ...record, tiles: tilesLoaded };
      save(record);
    }
    paint();
  });

  // Never lose the tail end of a session.
  window.addEventListener('pagehide', () => save({ ...record, tiles: tilesLoaded }));
}

let queued = false;
function paint() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    render();
  });
}

function render() {
  const value = document.getElementById('usage-value');
  const fill = document.getElementById('usage-fill');
  const chip = document.getElementById('ion-usage');
  if (!value || !fill || !chip) return;

  const bucket = Math.round(tilesLoaded / 25);
  if (bucket === painted) return;
  painted = bucket;

  const ratio = Math.min(tilesLoaded / REFERENCE_TILES, 1);
  value.textContent = tilesLoaded >= 1000
    ? `${(tilesLoaded / 1000).toFixed(1)}k`
    : String(tilesLoaded);
  fill.style.width = `${(ratio * 100).toFixed(1)}%`;

  const meter = fill.parentElement;
  meter.classList.toggle('is-warm', ratio > 0.6 && ratio <= 0.85);
  meter.classList.toggle('is-hot', ratio > 0.85);

  chip.dataset.tip =
    `${tilesLoaded.toLocaleString()} tiles this month<em>A local gauge of how fast you are ` +
    'spending the free Cesium ion tier — not your real balance, which only ion knows. ' +
    'Resets on the first of the month. Click to open your usage dashboard.</em>';
}
