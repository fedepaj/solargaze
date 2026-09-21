/**
 * The mouse cheat sheet in the bottom-right corner.
 *
 * It exists because CesiumJS's camera bindings are undiscoverable: a left-drag
 * orbits, the same drag with shift held looks around and with ctrl held tilts,
 * and nothing on screen says so. The card is open on a first visit for exactly
 * that reason — a reminder nobody opens is not a reminder — and once it has
 * been dismissed the preference sticks, because by then it has done its job.
 */

import { state, setPref } from '../state.js';

const $ = id => document.getElementById(id);

export function initMouseCard() {
  const card = $('mousecard');
  const toggle = $('mousecard-toggle');
  if (!card || !toggle) return;

  const apply = open => {
    card.dataset.open = String(open);
    toggle.setAttribute('aria-expanded', String(open));
    // The tooltip has to follow the state or it offers to open an open card.
    toggle.dataset.tip = open ? 'Hide the mouse controls' : 'Show the mouse controls';
  };

  apply(state.prefs.mouseCard);

  toggle.addEventListener('click', () => {
    const next = card.dataset.open !== 'true';
    apply(next);
    setPref('mouseCard', next);
  });
}
