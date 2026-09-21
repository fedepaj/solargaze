/** Wiring for the ANALYZE tab. */

import { state, on } from '../state.js';
import { computeSunHours, canAnalyze } from '../analyze.js';
import { toast } from './toast.js';
import { working } from './sunloader.js';

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
  const done = working(btn, 'Sampling…');

  try {
    // The run yields between chunks, so this count actually animates.
    const result = await computeSunHours(f => {
      done.setLabel(`Sampling… ${Math.round(f * 100)}%`);
    });
    paint(result);
  } catch (err) {
    // Every other failure in this app says so; a silent one here just looks
    // like a button that does nothing.
    toast(`The sun-hours run failed: ${err.message || err}`, { error: true, ms: 6000 });
  } finally {
    done();
  }
}

function paint({ hours, daylightHours, samples, step }) {
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
    i.dataset.tip = `${fmt(s.minutes)} — ${s.sun ? 'direct sun' : 'in shade'}`;
    frag.appendChild(i);
  }
  strip.appendChild(frag);

  if (samples.length) {
    $('an-from').textContent = fmt(samples[0].minutes);
    $('an-to').textContent = fmt(samples[samples.length - 1].minutes);
  }

  $('an-note').textContent =
    `Sampled every ${step} min across ${daylightHours.toFixed(1)} h of daylight at 1.5 m above the pin, ` +
    'against the tiles loaded right now. Zoom in and re-run for a stricter answer.';
}
