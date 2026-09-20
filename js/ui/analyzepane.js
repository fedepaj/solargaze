/** Wiring for the ANALYZE tab. */

import { state, on } from '../state.js';
import { computeSunHours, canAnalyze } from '../analyze.js';
import { toast } from './toast.js';

const $ = id => document.getElementById(id);
const pad = n => String(n).padStart(2, '0');
const fmt = m => `${pad(Math.floor(m / 60) % 24)}:${pad(Math.round(m) % 60)}`;

export function initAnalyzePane() {
  $('btn-run-analyze').addEventListener('click', run);
  // Any change to the inputs invalidates the last answer.
  on('location', reset);
  on('date', reset);
}

function reset() {
  $('analyze-out').hidden = true;
}

async function run() {
  if (!canAnalyze()) {
    toast('Load the photorealistic 3D tiles first — there is no geometry to test against.', {
      error: true, ms: 5200,
    });
    return;
  }

  const btn = $('btn-run-analyze');
  btn.disabled = true;
  btn.textContent = 'Sampling…';

  try {
    // The run yields between chunks, so this count actually animates.
    const result = await computeSunHours(f => {
      btn.textContent = `Sampling… ${Math.round(f * 100)}%`;
    });
    paint(result);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Compute sun hours';
  }
}

function paint({ hours, samples, step }) {
  const out = $('analyze-out');
  out.hidden = false;

  $('an-hours').textContent = hours.toFixed(1);

  const strip = $('an-strip');
  strip.innerHTML = '';
  // One insertion for the whole strip rather than one per tick.
  const frag = document.createDocumentFragment();
  for (const s of samples) {
    const i = document.createElement('i');
    i.className = s.sun ? 'sun' : 'shade';
    i.title = `${fmt(s.minutes)} — ${s.sun ? 'direct sun' : 'in shade'}`;
    frag.appendChild(i);
  }
  strip.appendChild(frag);

  if (samples.length) {
    $('an-from').textContent = fmt(samples[0].minutes);
    $('an-to').textContent = fmt(samples[samples.length - 1].minutes);
  }

  const daylight = (samples.length * step) / 60;
  $('an-note').textContent =
    `Sampled every ${step} min across ${daylight.toFixed(1)} h of daylight at 1.5 m above the pin, ` +
    'against the tiles loaded right now. Zoom in and re-run for a stricter answer.';
}
