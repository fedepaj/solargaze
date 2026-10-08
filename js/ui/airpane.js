/**
 * The layer panel: one card per theme, each a switch — tap it and the map
 * shows it — reading its number at the point whether drawn or not. The sun's
 * card is the sun path and the hours of direct sun; the others come from the
 * layer registry. A drawn layer's card carries its legend, the point's value
 * marked on it, and its option strip where it has one (the pollutant). Heat
 * has no switch between morning and night: the date and time decide, by the
 * sun at the point, and the card says which it is showing. Reading-only
 * entries (the air on the day) sit inside the card of the theme they belong
 * to. Nothing in here knows what a layer *is*; add one to atmo/layers.js, or
 * a product to the pipeline's catalog, and it appears. All of it is rebuilt
 * when the catalog lands.
 */

import { state, on, setPref } from '../state.js';
import { atmo, readings, dateNote, sourceNotes, toggleLayer, isOn, optionOf, layerPrefs, currentSource } from '../atmo.js';
import { LAYERS, modeOf, optionsOf, available } from '../atmo/layers.js';
import { cssGradient } from '../atmo/scales.js';

const $ = id => document.getElementById(id);
const svg = paths => `<svg viewBox="0 0 24 24">${paths}</svg>`;
const pad = n => String(n).padStart(2, '0');
const hhmm = minutes => (minutes === null || minutes === undefined ? '–:–'
  : `${pad(Math.floor(minutes / 60) % 24)}:${pad(Math.round(minutes) % 60)}`);

/** Per-layer DOM, looked up once. */
const cards = new Map();
let renderTimer = null;

export function initAirPane() {
  buildCards();
  wireSun();
  on('catalog', () => {
    buildCards();
    $('atmo-fine').textContent = sourceNotes();
    render();
  });

  on('atmo', schedule);
  on('pref', ({ key }) => {
    if (key === 'layers' || key === 'sunPath' || layerPrefs().has(key)) schedule();
  });
  on('time', schedule);
  on('location', schedule);
  on('date', schedule);
  on('camera', schedule);   // the heat's scale follows what is in view

  $('atmo-fine').textContent = sourceNotes();
  render();
}

/* ── the sun's card ─────────────────────────────────────────────────── */

function wireSun() {
  $('chip-sun').addEventListener('click', () => setPref('sunPath', !state.prefs.sunPath));
}

function renderSun() {
  const on = !!state.prefs.sunPath;
  $('card-sun').classList.toggle('is-on', on);
  $('chip-sun').setAttribute('aria-pressed', String(on));
  const { elevation, azimuth } = state.sun;
  const { sunrise, sunset } = state.events || {};
  const up = elevation > -0.833;
  $('sun-elev').textContent = up ? `${elevation.toFixed(1)}°` : 'Below the horizon';
  $('sun-sub').textContent = up
    ? `high · from ${azimuth.toFixed(0)}° · sunrise ${hhmm(sunrise)} · sunset ${hhmm(sunset)}`
    : `sunrise ${hhmm(sunrise)} · sunset ${hhmm(sunset)}`;
}

/* ── the layers' cards ──────────────────────────────────────────────── */

function buildCards() {
  const host = $('layer-rows');
  if (!host) return;
  host.innerHTML = '';
  cards.clear();
  const drawn = LAYERS.filter(l => available(l) && l.render);
  for (const layer of drawn) {
    const el = document.createElement('div');
    el.className = 'lcard';
    el.id = `card-${layer.id}`;
    el.innerHTML = `
      <button class="lcard-head" id="chip-${layer.id}" aria-pressed="false" data-tip="${layer.tip.replace(/"/g, '&quot;')}">
        ${svg(layer.icon)}<span class="lcard-name">${layer.label}</span><em class="lcard-tag" hidden></em>
        <i class="lcard-switch" aria-hidden="true"></i>
      </button>
      <div class="lcard-read">
        <svg class="wind-arrow" hidden viewBox="0 0 24 24"><path d="M12 3v18M6 9l6-6 6 6"/></svg>
        <b>…</b><em class="band" hidden></em><span></span>
      </div>
      ${optionsOf(layer, layer.modes?.fallback ?? null) ? '<div class="seg seg-metric"></div>' : ''}
      <div class="lcard-legend" hidden>
        <div class="legend"><i class="legend-mark"></i></div>
        <div class="legend-lbl"><span>–</span><span>–</span></div>
      </div>
      <div class="lcard-subs"></div>`;
    host.appendChild(el);
    const q = sel => el.querySelector(sel);
    cards.set(layer.id, {
      el, head: q('.lcard-head'), tag: q('.lcard-tag'), value: q('.lcard-read b'), band: q('.lcard-read .band'),
      sub: q('.lcard-read span'), arrow: q('.wind-arrow'), seg: q('.seg-metric'),
      legend: q('.lcard-legend'), bar: q('.legend'), mark: q('.legend-mark'),
      lo: q('.legend-lbl span:first-child'), hi: q('.legend-lbl span:last-child'), subs: q('.lcard-subs'),
    });
    q('.lcard-head').addEventListener('click', () => toggleLayer(layer.id));
    q('.seg-metric')?.addEventListener('click', e => {
      const btn = e.target.closest('button[data-option]');
      if (btn) setPref(optionsOf(layer, modeOf(layer, state.prefs)).pref, btn.dataset.option);
    });
  }
  // Reading-only entries live in the card of the theme they belong to.
  for (const layer of LAYERS.filter(l => available(l) && !l.render)) {
    const home = cards.get(layer.attachTo) || null;
    const row = document.createElement('div');
    row.className = 'lcard-sub';
    row.innerHTML = `<span class="lcard-sub-name">${layer.label}</span><b>…</b><em class="band" hidden></em><span></span>`;
    (home?.subs || host).appendChild(row);
    cards.set(layer.id, {
      el: row, value: row.querySelector('b'), band: row.querySelector('.band'), sub: row.querySelector('span:last-child'),
    });
  }
}

/** Playback fires `time` every frame; the panel reads fine at four a second. */
function schedule() {
  if (renderTimer) return;
  renderTimer = setTimeout(() => { renderTimer = null; render(); }, 250);
}

const pct = (v, lo, hi) => `${(Math.min(Math.max((v - lo) / (hi - lo || 1), 0), 1) * 100).toFixed(1)}%`;

function render() {
  renderSun();
  const all = readings();

  for (const layer of LAYERS.filter(available)) {
    const c = cards.get(layer.id);
    if (!c) continue;
    const r = all[layer.id];
    const mode = modeOf(layer, state.prefs);
    const ctx = r?.ctx ?? { mode, option: optionOf(layer) };
    const drawn = !!layer.render && isOn(layer.id);

    if (c.head) {
      c.el.classList.toggle('is-on', drawn);
      c.head.setAttribute('aria-pressed', String(drawn));
      // Which part of the day the heat is showing: the clock decides.
      const variant = layer.modes?.choices.length > 1 ? layer.modes.choices.find(v => v.key === mode)?.label : '';
      c.tag.hidden = !variant;
      c.tag.textContent = variant || '';
    }
    if (c.seg) {
      const group = optionsOf(layer, mode);
      const want = group ? group.choices.map(o => o.key).join(',') : '';
      if (c.seg.dataset.choices !== want) {
        c.seg.dataset.choices = want;
        c.seg.innerHTML = (group?.choices || []).map(o =>
          `<button data-option="${o.key}" title="${o.title}">${o.label}</button>`).join('');
      }
      for (const btn of c.seg.querySelectorAll('button')) btn.classList.toggle('is-on', btn.dataset.option === ctx.option);
    }
    if (c.legend) {
      const domain = r?.domain;
      c.legend.hidden = !(drawn && domain);
      if (drawn && domain) {
        c.bar.style.setProperty('--ramp', cssGradient(layer.stops(ctx)));
        const { lo, hi } = layer.legend(domain, ctx);
        c.lo.textContent = lo;
        c.hi.textContent = hi;
        c.mark.style.left = pct(r.value, domain[0], domain[1]);
      }
    }

    if (r) {
      c.value.textContent = r.text;
      c.sub.textContent = r.sub || '';
      c.band.hidden = !r.band;
      if (r.band) {
        c.band.textContent = r.band.name;
        c.band.style.setProperty('--c', r.band.colour);
      }
      if (c.arrow) {
        c.arrow.hidden = r.heading === undefined;
        if (r.heading !== undefined) c.arrow.style.transform = `rotate(${r.heading}deg)`;
      }
    } else {
      const status = atmo.status[currentSource(layer)];
      c.value.textContent = status === 'error' || status === 'none' ? '—' : '…';
      c.sub.textContent = status === 'none' ? 'not computed here yet' : '';
      c.band.hidden = true;
      if (c.arrow) c.arrow.hidden = true;
    }
  }

  const errors = Object.keys(atmo.status)
    .filter(id => atmo.status[id] === 'error')
    .map(id => `${id}: ${atmo.error[id]}`);
  const status = $('atmo-status');
  status.textContent = errors.join(' ');
  status.hidden = !errors.length;
  const note = $('atmo-note');
  note.textContent = dateNote();
  note.hidden = !note.textContent;
}
