/**
 * The main control panel: clock, date, and the two sliders that are the whole
 * point of the app — time of day and time of year.
 */

import {
  state, on, emit, setTime, setDate, setDayOfYear, currentDayOfYear,
  jumpToNow, sliderRange, setPref,
} from '../state.js';
import { daysInYear, dayOfYear, fromDayOfYear } from '../solar.js';
import { offsetLabel } from '../timezone.js';
import { toast } from './toast.js';

const $ = id => document.getElementById(id);

const pad = n => String(n).padStart(2, '0');
const fmtClock = m => `${pad(Math.floor(m / 60))}:${pad(Math.round(m) % 60)}`;
const fmtDate = ({ y, m, d }) => `${pad(d)}/${pad(m)}/${y}`;
const iso = ({ y, m, d }) => `${y}-${pad(m)}-${pad(d)}`;

let play = null;

/** Looked up once: render() used to re-query eight nodes on every frame. */
let els = null;

/** Slider bounds, settled by render() and reused by the per-frame renderClock. */
let range = { from: 0, to: 1439 };
let lastClock = '';
let lastTz = '';
let lastNight = null;
let ticksYear = 0;

export function initTimePanel() {
  els = {
    timeSlider: $('time-slider'),
    dateSlider: $('date-slider'),
    dateNative: $('date-native'),
    timeNative: $('time-native'),
    clockTime: $('clock-time'),
    clockTz: $('clock-tz'),
    clockTzVal: $('clock-tz-val'),
    datePill: $('date-pill'),
    labelSunrise: $('label-sunrise'),
    labelSunset: $('label-sunset'),
  };

  const { timeSlider, dateSlider, dateNative, timeNative } = els;

  buildSeasonTicks();

  /* ── time of day ──────────────────────────────────────────────── */
  timeSlider.addEventListener('input', () => {
    stopPlayback();
    setTime(Number(timeSlider.value));
  });

  /* ── time of year ─────────────────────────────────────────────── */
  dateSlider.addEventListener('input', () => setDayOfYear(Number(dateSlider.value)));

  /* ── exact date ───────────────────────────────────────────────── */
  const openDate = () => {
    dateNative.value = iso(state);
    if (typeof dateNative.showPicker === 'function') {
      try { dateNative.showPicker(); return; } catch { /* fall through */ }
    }
    dateNative.classList.remove('visually-hidden');
    dateNative.focus();
  };
  $('date-pill').addEventListener('click', openDate);
  dateNative.addEventListener('change', () => {
    const [y, m, d] = dateNative.value.split('-').map(Number);
    if (y) setDate({ y, m, d });
    dateNative.classList.add('visually-hidden');
  });
  dateNative.addEventListener('blur', () => dateNative.classList.add('visually-hidden'));

  /* ── exact time ───────────────────────────────────────────────── */
  $('clock-btn').addEventListener('click', () => {
    timeNative.value = fmtClock(state.minutes);
    if (typeof timeNative.showPicker === 'function') {
      try { timeNative.showPicker(); return; } catch { /* fall through */ }
    }
    timeNative.classList.remove('visually-hidden');
    timeNative.focus();
  });
  timeNative.addEventListener('change', () => {
    const [h, mi] = timeNative.value.split(':').map(Number);
    if (Number.isFinite(h)) {
      const mins = h * 60 + (mi || 0);
      const { from, to, full } = sliderRange();
      if (!full && (mins < from || mins > to)) setPref('fullDayRange', true);
      stopPlayback();
      setTime(mins);
    }
    timeNative.classList.add('visually-hidden');
  });
  timeNative.addEventListener('blur', () => timeNative.classList.add('visually-hidden'));

  /* ── now / play ───────────────────────────────────────────────── */
  $('btn-now').addEventListener('click', () => {
    stopPlayback();
    jumpToNow();
    toast('Jumped to the current local time at the pin');
  });
  $('btn-play').addEventListener('click', togglePlayback);

  /* ── tabs ─────────────────────────────────────────────────────── */
  $('tab-visualize').addEventListener('click', () => setTab('visualize'));
  $('tab-analyze').addEventListener('click', () => setTab('analyze'));

  /* ── keyboard ─────────────────────────────────────────────────── */
  window.addEventListener('keydown', onKey);

  on('time', renderClock);
  on('date', () => { buildSeasonTicks(); render(); });
  on('location', render);
  on('pref', ({ key }) => { if (key === 'fullDayRange') render(); });

  render();
}

function setTab(tab) {
  state.tab = tab;
  $('tab-visualize').classList.toggle('is-active', tab === 'visualize');
  $('tab-analyze').classList.toggle('is-active', tab === 'analyze');
  $('tab-visualize').setAttribute('aria-selected', String(tab === 'visualize'));
  $('tab-analyze').setAttribute('aria-selected', String(tab === 'analyze'));
  $('pane-visualize').hidden = tab !== 'visualize';
  $('pane-analyze').hidden = tab !== 'analyze';
  emit('tab', tab);
}

/** Equinox and solstice marks under the year slider. Only the year moves them. */
function buildSeasonTicks() {
  const list = document.getElementById('season-ticks');
  if (!list || state.y === ticksYear) return;
  ticksYear = state.y;
  const y = state.y;
  list.innerHTML = '';
  for (const [m, d] of [[3, 20], [6, 21], [9, 22], [12, 21]]) {
    const opt = document.createElement('option');
    opt.value = String(dayOfYear(y, m, d));
    list.appendChild(opt);
  }
}

function onKey(e) {
  const tag = e.target?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || e.metaKey || e.ctrlKey || e.altKey) return;

  const step = e.shiftKey ? 60 : 10;
  switch (e.key) {
    case 'ArrowLeft':  stopPlayback(); setTime(state.minutes - step); e.preventDefault(); break;
    case 'ArrowRight': stopPlayback(); setTime(state.minutes + step); e.preventDefault(); break;
    case 'ArrowUp':    shiftDay(e.shiftKey ? 30 : 1); e.preventDefault(); break;
    case 'ArrowDown':  shiftDay(e.shiftKey ? -30 : -1); e.preventDefault(); break;
    case ' ':          togglePlayback(); e.preventDefault(); break;
    // As the NOW button does: jumping to now while the sun is sweeping would
    // be overwritten on the very next frame.
    case 'n': case 'N': stopPlayback(); jumpToNow(); break;
    default: break;
  }
}

function shiftDay(delta) {
  const total = daysInYear(state.y);
  let doy = currentDayOfYear() + delta;
  let y = state.y;
  while (doy > daysInYear(y)) { doy -= daysInYear(y); y += 1; }
  while (doy < 1) { y -= 1; doy += daysInYear(y); }
  setDate(fromDayOfYear(y, doy));
}

/* ── playback ─────────────────────────────────────────────────────── */

const SWEEP_MS = 26_000;

function togglePlayback() {
  if (play) stopPlayback();
  else startPlayback();
}

function startPlayback() {
  let last = performance.now();
  let cursor = Math.min(Math.max(state.minutes, range.from), range.to);

  const tick = now => {
    const dt = now - last;
    last = now;
    // Read the span each frame rather than capturing it at the start: the
    // arrow keys and the year slider move the date without stopping playback,
    // and with it sunrise and sunset. A captured span would then run the sun
    // off the end of a shorter day, or stop it short of a longer one.
    const { from, to } = range;
    cursor += (dt / SWEEP_MS) * Math.max(to - from, 1);
    if (cursor > to || cursor < from) cursor = from;
    setTime(cursor);
    play.id = requestAnimationFrame(tick);
  };

  play = { id: requestAnimationFrame(tick) };
  state.playing = true;
  paintPlayButton();
}

export function stopPlayback() {
  if (!play) return;
  cancelAnimationFrame(play.id);
  play = null;
  state.playing = false;
  paintPlayButton();
}

function paintPlayButton() {
  const btn = document.getElementById('btn-play');
  const ico = document.getElementById('play-ico');
  const label = document.getElementById('play-label');
  btn.classList.toggle('is-on', state.playing);
  label.textContent = state.playing ? 'STOP' : '24H';
  const shape = ico.querySelector('.fill');
  shape.setAttribute('d', state.playing ? 'M9.6 8.4h1.9v7.2H9.6zM12.5 8.4h1.9v7.2h-1.9z' : 'M10 8.6 16 12l-6 3.4Z');
}

/* ── rendering ────────────────────────────────────────────────────── */

/** Everything that only moves when the date, the place or a preference does. */
export function render() {
  els.clockTz.dataset.tip = state.zone
    ? `Wall clock at the pin<em>${state.zone}</em>`
    : 'Wall clock at the pin<em>Offset estimated from longitude</em>';
  els.datePill.textContent = fmtDate(state);

  const { sunrise, sunset, polar } = state.events;
  els.labelSunrise.textContent = polar === 'day' ? '00:00' : polar === 'night' ? '—' : fmtClock(sunrise);
  els.labelSunset.textContent = polar === 'day' ? '24:00' : polar === 'night' ? '—' : fmtClock(sunset);

  range = sliderRange();
  els.timeSlider.min = String(range.from);
  els.timeSlider.max = String(range.to);

  const total = daysInYear(state.y);
  const doy = currentDayOfYear();
  els.dateSlider.max = String(total);
  els.dateSlider.value = String(doy);
  setPct(els.dateSlider, (doy - 1) / (total - 1));

  renderClock();
}

/**
 * The half that playback drives, once per animation frame. The clock reads the
 * same for a whole simulated minute and the offset for months at a time, so
 * both are guarded; only the slider genuinely moves every frame.
 *
 * This used to reassign `clock-tz` through innerHTML, which re-parsed a
 * fragment of HTML sixty times a second to print the same string.
 */
function renderClock() {
  const text = fmtClock(state.minutes);
  if (text !== lastClock) {
    lastClock = text;
    els.clockTime.textContent = text;
  }

  // Tracked here rather than in render() so a DST boundary crossed mid-playback
  // still shows up the moment it happens.
  const tz = offsetLabel(state.offsetMinutes);
  if (tz !== lastTz) {
    lastTz = tz;
    els.clockTzVal.textContent = tz;
  }

  const clamped = Math.min(Math.max(state.minutes, range.from), range.to);
  els.timeSlider.value = String(clamped);
  setPct(els.timeSlider, (clamped - range.from) / Math.max(range.to - range.from, 1));

  const night = state.sun.elevation <= -0.833;
  if (night !== lastNight) {
    lastNight = night;
    els.timeSlider.classList.toggle('is-night', night);
  }
}

function setPct(el, fraction) {
  el.style.setProperty('--pct', `${(fraction * 100).toFixed(2)}%`);
}
