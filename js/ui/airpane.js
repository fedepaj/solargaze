/**
 * The rail's Layers group, the readings in the ANALYZE tab and the legend on
 * the map, all generated from the layer registry. A layer is switched on in
 * one place only, the rail; ANALYZE reads every theme at the point whether
 * it is drawn or not, with a strip of variants where the catalog gives the
 * theme more than one (morning, night …) and an option strip where it has
 * one; the legend sits on the map, for the layer colouring the ground, with
 * the point's value marked on it. Nothing in here knows what a layer *is*;
 * add one to atmo/layers.js, or a product to the pipeline's catalog, and it
 * appears. All of it is rebuilt when the catalog lands.
 */

import { state, on, setPref } from '../state.js';
import { atmo, readings, dateNote, sourceNotes, toggleLayer, isOn, optionOf, layerPrefs, currentSource, contextFor } from '../atmo.js';
import { LAYERS, modeOf, optionsOf, available } from '../atmo/layers.js';
import { cssGradient } from '../atmo/scales.js';

const $ = id => document.getElementById(id);

const svg = paths => `<svg viewBox="0 0 24 24">${paths}</svg>`;

/** Per-layer DOM, looked up once. */
const rows = new Map();
let renderTimer = null;

export function initAirPane() {
  buildRail();
  buildRows();
  on('catalog', () => {
    buildRail();
    buildRows();
    $('atmo-fine').textContent = sourceNotes();
    paintToggles();
    render();
  });

  on('atmo', schedule);
  on('pref', ({ key }) => {
    if (key === 'layers' || layerPrefs().has(key)) { paintToggles(); schedule(); }
  });
  on('time', schedule);
  on('location', schedule);
  on('date', schedule);
  on('tab', schedule);
  on('camera', schedule);

  $('atmo-fine').textContent = sourceNotes();
  paintToggles();
  render();
  renderLegend();
}

function buildRail() {
  const body = $('layers-group');
  if (!body) return;
  body.innerHTML = '';
  for (const layer of LAYERS.filter(l => l.rail !== false && available(l))) {
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
  host.innerHTML = '';
  rows.clear();
  for (const layer of LAYERS.filter(available)) {
    const el = document.createElement('div');
    el.className = 'atmo-item';
    el.innerHTML = `
      <div class="atmo-row">
        <span class="atmo-label" id="chip-${layer.id}">${svg(layer.icon)}<span>${layer.label}</span></span>
        <div class="atmo-read">
          <svg class="wind-arrow" hidden viewBox="0 0 24 24"><path d="M12 3v18M6 9l6-6 6 6"/></svg>
          <b>…</b>
          <em class="band" hidden></em>
          <span></span>
        </div>
      </div>
      ${layer.modes?.choices.length > 1 ? `<div class="seg seg-mode">${layer.modes.choices.map(c =>
        `<button data-mode="${c.key}" title="${c.title}">${c.label}</button>`).join('')}</div>` : ''}
      ${optionsOf(layer, layer.modes?.fallback ?? null) ? '<div class="seg seg-metric"></div>' : ''}`;
    host.appendChild(el);

    const q = sel => el.querySelector(sel);
    rows.set(layer.id, {
      chip: q('.atmo-label'), value: q('.atmo-read b'), band: q('.band'), sub: q('.atmo-read span'),
      arrow: q('.wind-arrow'), seg: q('.seg-metric'), modes: q('.seg-mode'),
    });

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
  for (const layer of LAYERS.filter(available)) {
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
  renderTimer = setTimeout(() => { renderTimer = null; render(); renderLegend(); }, 250);
}

/**
 * The legend of the layer colouring the ground, on the map: what it is, the
 * ramp it is painted with, its ends, and the value at the point marked on
 * it. Hidden when nothing colours the ground.
 */
function renderLegend() {
  const box = $('maplegend');
  if (!box) return;
  const layer = LAYERS.find(l => available(l) && l.render === 'drape' && isOn(l.id));
  const series = layer && atmo.series[currentSource(layer)];
  const ctx = layer && contextFor(layer);
  const domain = series ? layer.domain(series, ctx) : null;
  box.hidden = !domain;
  if (!domain) return;
  const variant = layer.modes?.choices.length > 1 ? layer.modes.choices.find(c => c.key === ctx.mode)?.label : '';
  const option = optionsOf(layer, ctx.mode)?.choices.find(c => c.key === ctx.option)?.label || '';
  box.querySelector('.ml-title').innerHTML = `${svg(layer.icon)}<span>${[layer.label, variant, option].filter(Boolean).join(' · ')}</span>`;
  box.querySelector('.legend').style.setProperty('--ramp', cssGradient(layer.stops(ctx)));
  const { lo, hi } = layer.legend(domain, ctx);
  box.querySelector('.ml-lo').textContent = lo;
  box.querySelector('.ml-hi').textContent = hi;
  const r = layer.reading(series, state.lat, state.lon, state.utc.getTime(), ctx);
  const mark = box.querySelector('.legend-mark');
  mark.hidden = !r;
  if (r) mark.style.left = pct(r.value, domain[0], domain[1]);
}

const pct = (v, lo, hi) => `${(Math.min(Math.max((v - lo) / (hi - lo || 1), 0), 1) * 100).toFixed(1)}%`;

function render() {
  if (state.tab !== 'analyze') return;
  const all = readings();

  for (const layer of LAYERS.filter(available)) {
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
      el.sub.textContent = status === 'none' ? 'not computed here yet' : '';
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
