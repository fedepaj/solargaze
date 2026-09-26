/**
 * The AIR tab and the rail's Layers group, both generated from the layer
 * registry: a switch per layer, the number at the point, an option strip
 * where the layer has one, and a legend where it paints the ground. Nothing
 * in here knows what a layer *is*; add one to atmo/layers.js and it appears.
 */

import { state, on, setPref } from '../state.js';
import { atmo, readings, dateNote, sourceNotes, toggleLayer, isOn, optionOf, layerPrefs, currentSource } from '../atmo.js';
import { LAYERS, modeOf, optionsOf } from '../atmo/layers.js';
import { cssGradient } from '../atmo/scales.js';

const $ = id => document.getElementById(id);

const svg = paths => `<svg viewBox="0 0 24 24">${paths}</svg>`;

/** Per-layer DOM, looked up once. */
const rows = new Map();
let renderTimer = null;

export function initAirPane() {
  buildRail();
  buildRows();

  on('atmo', schedule);
  on('pref', ({ key }) => {
    if (key === 'layers' || layerPrefs().has(key)) { paintToggles(); schedule(); }
  });
  on('time', schedule);
  on('location', schedule);
  on('date', schedule);
  on('tab', schedule);

  $('atmo-fine').textContent = sourceNotes();
  paintToggles();
  render();
}

function buildRail() {
  const body = $('layers-group');
  if (!body) return;
  for (const layer of LAYERS) {
    const btn = document.createElement('button');
    btn.className = 'icon-btn';
    btn.id = `btn-${layer.id}`;
    btn.setAttribute('aria-pressed', 'false');
    btn.dataset.tip = layer.tip;
    btn.innerHTML = svg(layer.icon);
    btn.addEventListener('click', () => toggleLayer(layer.id));
    body.appendChild(btn);
  }
}

function buildRows() {
  const host = $('layer-rows');
  if (!host) return;
  for (const layer of LAYERS) {
    const el = document.createElement('div');
    el.className = 'atmo-item';
    el.innerHTML = `
      <div class="atmo-row">
        <button class="atmo-toggle" id="chip-${layer.id}" aria-pressed="false">${svg(layer.icon)}<span>${layer.label}</span></button>
        <div class="atmo-read">
          <svg class="wind-arrow" hidden viewBox="0 0 24 24"><path d="M12 3v18M6 9l6-6 6 6"/></svg>
          <b>…</b>
          <em class="band" hidden></em>
          <span></span>
        </div>
      </div>
      ${layer.modes ? `<div class="seg seg-mode">${layer.modes.choices.map(c =>
        `<button data-mode="${c.key}" title="${c.title}">${c.label}</button>`).join('')}</div>` : ''}
      ${optionsOf(layer, layer.modes?.fallback ?? null) ? '<div class="seg seg-metric"></div>' : ''}
      ${layer.render === 'drape' ? `<div class="legend"><i class="legend-mark"></i></div>
        <div class="legend-lbl"><span>–</span><span>–</span></div>` : ''}`;
    host.appendChild(el);

    const q = sel => el.querySelector(sel);
    rows.set(layer.id, {
      chip: q('.atmo-toggle'), value: q('.atmo-read b'), band: q('.band'), sub: q('.atmo-read span'),
      arrow: q('.wind-arrow'), seg: q('.seg-metric'), modes: q('.seg-mode'), bar: q('.legend'), mark: q('.legend-mark'),
      lo: q('.legend-lbl span:first-child'), hi: q('.legend-lbl span:last-child'),
    });

    q('.atmo-toggle').addEventListener('click', () => toggleLayer(layer.id));
    q('.seg-mode')?.addEventListener('click', e => {
      const btn = e.target.closest('button[data-mode]');
      if (btn) setPref(layer.modes.pref, btn.dataset.mode);
    });
    q('.seg-metric')?.addEventListener('click', e => {
      const btn = e.target.closest('button[data-option]');
      if (btn) setPref(optionsOf(layer, modeOf(layer, state.prefs)).pref, btn.dataset.option);
    });
  }
}

function paintToggles() {
  for (const layer of LAYERS) {
    const onNow = isOn(layer.id);
    for (const el of [$(`btn-${layer.id}`), rows.get(layer.id)?.chip]) {
      if (!el) continue;
      el.classList.toggle('is-on', onNow);
      el.setAttribute('aria-pressed', String(onNow));
    }
  }
}

/** Playback fires `time` every frame; the pane reads fine at four a second. */
function schedule() {
  if (renderTimer) return;
  renderTimer = setTimeout(() => { renderTimer = null; render(); }, 250);
}

const pct = (v, lo, hi) => `${(Math.min(Math.max((v - lo) / (hi - lo || 1), 0), 1) * 100).toFixed(1)}%`;

function render() {
  if (state.tab !== 'air') return;
  const all = readings();

  for (const layer of LAYERS) {
    const el = rows.get(layer.id);
    const r = all[layer.id];
    const mode = modeOf(layer, state.prefs);
    const ctx = r?.ctx ?? { mode, option: optionOf(layer) };

    if (el.modes) {
      for (const btn of el.modes.querySelectorAll('button')) btn.classList.toggle('is-on', btn.dataset.mode === mode);
    }
    if (el.seg) {
      // The choices can change with the mode, so the strip is rebuilt when they do.
      const group = optionsOf(layer, mode);
      const want = group ? group.choices.map(c => c.key).join(',') : '';
      if (el.seg.dataset.choices !== want) {
        el.seg.dataset.choices = want;
        el.seg.innerHTML = (group?.choices || []).map(c =>
          `<button data-option="${c.key}" title="${c.title}">${c.label}</button>`).join('');
      }
      for (const btn of el.seg.querySelectorAll('button')) btn.classList.toggle('is-on', btn.dataset.option === ctx.option);
    }
    if (el.bar) {
      el.bar.style.setProperty('--ramp', cssGradient(layer.stops(ctx)));
      const domain = r?.domain;
      if (domain) {
        const { lo, hi } = layer.legend(domain, ctx);
        el.lo.textContent = lo;
        el.hi.textContent = hi;
        el.mark.style.left = pct(r.value, domain[0], domain[1]);
      }
    }

    if (r) {
      el.value.textContent = r.text;
      el.sub.textContent = r.sub || '';
      el.band.hidden = !r.band;
      if (r.band) {
        el.band.textContent = r.band.name;
        el.band.style.setProperty('--c', r.band.colour);
      }
      el.arrow.hidden = r.heading === undefined;
      if (r.heading !== undefined) el.arrow.style.transform = `rotate(${r.heading}deg)`;
    } else {
      const status = atmo.status[currentSource(layer)];
      el.value.textContent = status === 'error' || status === 'none' ? '—' : '…';
      el.sub.textContent = status === 'none' ? 'no precomputed tile here yet' : '';
      el.band.hidden = true;
      el.arrow.hidden = true;
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
