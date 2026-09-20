/**
 * Current conditions badge, from Open-Meteo: free, keyless, CC-BY.
 * Purely decorative — nothing in the sun model depends on it.
 */

import { state, on } from '../state.js';

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

let lastKey = '';
let timer = null;

export function initWeather() {
  on('location', () => schedule());
  on('pref', ({ key }) => { if (key === 'weather') schedule(true); });
  schedule(true);
}

function schedule(immediate = false) {
  clearTimeout(timer);
  timer = setTimeout(fetchWeather, immediate ? 0 : 600);
}

async function fetchWeather() {
  const el = document.getElementById('weather');
  if (!el) return;

  if (!state.prefs.weather) {
    el.hidden = true;
    return;
  }

  const key = `${state.lat.toFixed(2)},${state.lon.toFixed(2)}`;
  if (key === lastKey) return;

  const url =
    'https://api.open-meteo.com/v1/forecast' +
    `?latitude=${state.lat.toFixed(4)}&longitude=${state.lon.toFixed(4)}` +
    '&current=temperature_2m,weather_code&daily=uv_index_max&forecast_days=1&timezone=auto';

  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();

    const temp = Math.round(data.current?.temperature_2m ?? NaN);
    const uv = data.daily?.uv_index_max?.[0];
    if (!Number.isFinite(temp)) throw new Error('no data');

    document.getElementById('wx-temp').textContent = `${temp}°`;
    document.getElementById('wx-uv').textContent =
      Number.isFinite(uv) ? `UV ${Math.round(uv)}` : 'UV –';
    document.getElementById('wx-ico').innerHTML = ICONS[glyphFor(data.current?.weather_code ?? 3)];
    el.hidden = false;
    lastKey = key;
  } catch {
    el.hidden = true;   // never let the decoration shout about itself
  }
}
