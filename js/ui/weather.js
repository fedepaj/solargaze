/**
 * The weather chip: the selected day at the pin, from Open-Meteo (free,
 * keyless, CC-BY) — the temperature at the hour on the clock, the day's
 * sky as a glyph, its high and low. Nothing in the sun model depends on it.
 */

import { state, on } from '../state.js';
import { SOURCES, isoDate } from '../atmo/sources.js';

const ICONS = {
  clear: '<circle cx="12" cy="12" r="4.5"/><path d="M12 2.6v2.4M12 19v2.4M2.6 12H5M19 12h2.4M5.3 5.3 7 7M17 17l1.7 1.7M18.7 5.3 17 7M7 17l-1.7 1.7"/>',
  cloud: '<path d="M7.5 18h9.2a3.8 3.8 0 0 0 .3-7.6 5.5 5.5 0 0 0-10.5-1A3.8 3.8 0 0 0 7.5 18Z"/>',
  rain: '<path d="M7.5 15h9.2a3.8 3.8 0 0 0 .3-7.6 5.5 5.5 0 0 0-10.5-1A3.8 3.8 0 0 0 7.5 15Z"/><path d="M9 18.2 8.2 20M13 18.2 12.2 20M17 18.2 16.2 20"/>',
  snow: '<path d="M7.5 15h9.2a3.8 3.8 0 0 0 .3-7.6 5.5 5.5 0 0 0-10.5-1A3.8 3.8 0 0 0 7.5 15Z"/><path d="M9 19h.01M13 19h.01M17 19h.01M11 21h.01M15 21h.01"/>',
  storm: '<path d="M7.5 14h9.2a3.8 3.8 0 0 0 .3-7.6 5.5 5.5 0 0 0-10.5-1A3.8 3.8 0 0 0 7.5 14Z"/><path d="m12.5 15-2.5 4h3l-2 3.5"/>',
  fog: '<path d="M4 9h16M6 13h12M8 17h8"/>',
};

/** WMO weather interpretation codes → the handful of glyphs we draw. */
function glyphFor(code) {
  if (code === 0 || code === 1) return 'clear';
  if (code === 2 || code === 3) return 'cloud';
  if (code >= 45 && code <= 48) return 'fog';
  if (code >= 71 && code <= 77) return 'snow';
  if (code >= 85 && code <= 86) return 'snow';
  if (code >= 95) return 'storm';
  if (code >= 51) return 'rain';
  return 'cloud';
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad = n => String(n).padStart(2, '0');

/** One day at one place: { key, hourly temps, day code, max, min, uv, resolved date, proxy }. */
let day = null;
let pending = '';
let timer = null;

export function initWeather() {
  on('location', () => schedule());
  on('date', () => schedule());
  on('time', paint);
  on('pref', ({ key }) => { if (key === 'weather') schedule(true); });
  schedule(true);
}

function schedule(immediate = false) {
  clearTimeout(timer);
  timer = setTimeout(fetchDay, immediate ? 0 : 600);
}

/**
 * The selected day's weather at the pin. Inside the forecast horizon and in
 * the past it is that day; beyond the horizon, the same date a year earlier,
 * said so — the same policy the layers use (SOURCES.weather.resolve).
 */
async function fetchDay() {
  const el = document.getElementById('weather');
  if (!el) return;
  if (!state.prefs.weather) { el.hidden = true; return; }

  const now = new Date();
  const picked = { y: state.y, m: state.m, d: state.d };
  const resolved = SOURCES.weather.resolve(picked, { y: now.getFullYear(), m: now.getMonth() + 1, d: now.getDate() });
  const iso = isoDate(resolved.date);
  const key = `${state.lat.toFixed(2)},${state.lon.toFixed(2)},${iso}`;
  if (day?.key === key) { el.hidden = false; paint(); return; }
  if (pending === key) return;
  pending = key;

  const archive = resolved.endpoint === 'archive';
  const url = (archive ? 'https://archive-api.open-meteo.com/v1/archive' : 'https://api.open-meteo.com/v1/forecast') +
    `?latitude=${state.lat.toFixed(4)}&longitude=${state.lon.toFixed(4)}` +
    `&hourly=temperature_2m&daily=weather_code,temperature_2m_max,temperature_2m_min${archive ? '' : ',uv_index_max'}` +
    `&timezone=auto&start_date=${iso}&end_date=${iso}`;
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    if (pending !== key) return;   // the pin or the date moved on meanwhile
    const temps = data.hourly?.temperature_2m || [];
    if (!temps.some(Number.isFinite)) throw new Error('no data');
    day = {
      key, temps, resolved, picked,
      code: data.daily?.weather_code?.[0],
      max: data.daily?.temperature_2m_max?.[0],
      min: data.daily?.temperature_2m_min?.[0],
      uv: data.daily?.uv_index_max?.[0],
    };
    el.hidden = false;
    paint();
  } catch {
    if (pending === key) el.hidden = true;   // never let the decoration shout about itself
  } finally {
    if (pending === key) pending = '';
  }
}

/** The hour moves more often than the day: repaint from what is in hand. */
function paint() {
  const el = document.getElementById('weather');
  if (!el || !day) return;
  const h = Math.min(day.temps.length - 1, Math.max(0, Math.floor(state.minutes / 60)));
  const temp = day.temps[h];
  document.getElementById('wx-temp').textContent = Number.isFinite(temp) ? `${Math.round(temp)}°` : '–°';
  const range = Number.isFinite(day.max) && Number.isFinite(day.min) ? `↑${Math.round(day.max)}° ↓${Math.round(day.min)}°` : '';
  document.getElementById('wx-uv').textContent = day.resolved.proxy ? `as in ${day.resolved.date.y}` : range;
  document.getElementById('wx-ico').innerHTML = ICONS[glyphFor(day.code ?? 3)];
  const { d, m, y } = day.picked;
  const when = `${d} ${MONTHS[m - 1]} ${y}, ${pad(h)}:00`;
  const source = day.resolved.proxy
    ? `no forecast that far ahead: ${day.resolved.date.d} ${MONTHS[m - 1]} ${day.resolved.date.y} from the archive, a stand-in for the season`
    : day.resolved.endpoint === 'archive' ? 'archive' : 'forecast';
  el.dataset.tip = `Weather on ${when}<em>${source}${Number.isFinite(day.uv) ? ` · UV ${Math.round(day.uv)}` : ''} · ${range} · Open-Meteo</em>`;
}
