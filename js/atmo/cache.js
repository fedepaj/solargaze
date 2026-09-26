/**
 * A cache for what does not change: the precomputed tiles, and the
 * archived days from Open-Meteo.
 *
 * Someone who studies the same neighbourhood every evening should not pull
 * the same four megabytes every evening. The Cache API keeps the bytes;
 * this file keeps a small ledger of what is in it — url, size, last use —
 * so that the total can be shown in Settings, wiped from there, and kept
 * under a budget by evicting the least recently used entries. Entries
 * also expire, because a tile is occasionally rebuilt.
 *
 * Only immutable answers go in. A forecast for tomorrow does not; a tile
 * product does, and so does an archive request for a day already past.
 */

const CACHE_NAME = 'solargaze-data-v1';
const LEDGER_KEY = 'solargaze.cacheLedger';
/** Bytes we allow ourselves before evicting. Phones have less to spare. */
export const BUDGET_BYTES = (navigator.maxTouchPoints > 0 ? 120 : 400) * 1024 * 1024;
const MAX_AGE_MS = 45 * 86400000;

const supported = typeof caches !== 'undefined' && typeof CacheStorage !== 'undefined';

/**
 * The ledger lives in memory and is written through: twelve images arrive
 * at once, and twelve reads-then-writes of localStorage would keep only the
 * last one's entry.
 */
let ledger = null;
function readLedger() {
  if (ledger) return ledger;
  try { ledger = JSON.parse(localStorage.getItem(LEDGER_KEY) || '{}'); } catch { ledger = {}; }
  return ledger;
}
function writeLedger() {
  try { localStorage.setItem(LEDGER_KEY, JSON.stringify(ledger)); } catch { /* full or blocked */ }
}

/**
 * Fetch through the cache. `key` defaults to the url; pass one when the
 * url carries something transient (a signed token, say).
 */
export async function cachedFetch(url, { key = url, reload = false } = {}) {
  if (!supported) return fetch(url, reload ? { cache: 'reload' } : undefined);
  const cache = await caches.open(CACHE_NAME);
  readLedger();
  const entry = ledger[key];
  if (!reload && entry && Date.now() - entry.at < MAX_AGE_MS) {
    const hit = await cache.match(key);
    if (hit) {
      entry.used = Date.now();
      writeLedger();
      return hit;
    }
  }
  const res = await fetch(url, reload ? { cache: 'reload' } : undefined);
  if (!res.ok) return res;
  // Clone before the body is read; the ledger wants the size, so read it here.
  const blob = await res.clone().blob();
  await cache.put(key, new Response(blob, { headers: { 'Content-Type': res.headers.get('Content-Type') || 'application/octet-stream' } }));
  ledger[key] = { size: blob.size, at: Date.now(), used: Date.now() };
  writeLedger();
  await evict(cache);
  return res;
}

/** Drop expired entries, then the least recently used until under budget. */
async function evict(cache) {
  const now = Date.now();
  let total = 0;
  for (const [key, e] of Object.entries(ledger)) {
    if (now - e.at > MAX_AGE_MS) { await cache.delete(key); delete ledger[key]; continue; }
    total += e.size;
  }
  if (total > BUDGET_BYTES) {
    const byUse = Object.entries(ledger).sort((a, b) => a[1].used - b[1].used);
    for (const [key, e] of byUse) {
      if (total <= BUDGET_BYTES * 0.8) break;
      await cache.delete(key);
      delete ledger[key];
      total -= e.size;
    }
  }
  writeLedger();
}

/** Bytes and entries currently held, for Settings. */
export function cacheStats() {
  const entries = Object.values(readLedger());
  return { bytes: entries.reduce((s, e) => s + e.size, 0), entries: entries.length, supported };
}

export async function clearCache() {
  if (supported) await caches.delete(CACHE_NAME);
  ledger = {};
  try { localStorage.removeItem(LEDGER_KEY); } catch { /* ignore */ }
}

export const formatBytes = n => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`);
