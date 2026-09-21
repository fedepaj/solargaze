/**
 * The waiting state, as a sun.
 *
 * Every spinner in here is the same mark: a disc that breathes and eight rays
 * that retract into it and stretch back out while the whole card turns. It is
 * the favicon and the wordmark in motion, which is the point — a generic ring
 * would say "something is loading", and this says "SolarGaze is loading".
 *
 * The SVG carries no size of its own. It fills its host, and the host is sized
 * with `font-size`, so the same markup serves the 16 px slot in the search bar
 * and the 34 px one in the middle of a pane. See `.sunload` in app.css.
 */

/** Eight rays on the 45° marks, each 3.4 units long, from r=6.3 out to r=9.7. */
const RAYS = [
  'M12 5.7V2.3',
  'M16.46 7.55 18.86 5.14',
  'M18.3 12h3.4',
  'M16.46 16.46 18.86 18.86',
  'M12 18.3v3.4',
  'M7.55 16.46 5.14 18.86',
  'M5.7 12H2.3',
  'M7.55 7.55 5.14 5.14',
];

const SVG =
  '<svg viewBox="0 0 24 24" aria-hidden="true">' +
  '<circle class="sl-core" cx="12" cy="12" r="4.4"/>' +
  `<g class="sl-rays">${RAYS.map(d => `<path d="${d}"/>`).join('')}</g>` +
  '</svg>';

/** Loader markup, for templates that build their HTML as a string. */
export const sunLoader = () => `<span class="sunload">${SVG}</span>`;

/** Turn an existing element into a loader in place. */
export function mountLoader(el) {
  if (!el) return;
  el.classList.add('sunload');
  el.innerHTML = SVG;
}

/**
 * Put a button to work: loader in, label swapped, presses ignored.
 *
 * Returns the undo, because the caller usually cannot tell in advance whether
 * the work will end by replacing this button's whole panel or by handing it
 * back — a failed token does the first, a finished analyze run the second.
 *
 * The undo carries a `setLabel` for work that reports progress. It exists
 * because the obvious `btn.querySelector('span')` finds the loader's own span
 * first and writing text into that replaces the sun with the word.
 */
export function working(btn, label) {
  if (!btn) return Object.assign(() => {}, { setLabel: () => {} });
  const was = { html: btn.innerHTML, disabled: btn.disabled };
  btn.disabled = true;
  btn.classList.add('is-working');
  btn.innerHTML = `${sunLoader()}<span class="sl-label">${label}</span>`;

  const restore = () => {
    btn.innerHTML = was.html;
    btn.disabled = was.disabled;
    btn.classList.remove('is-working');
  };
  restore.setLabel = text => {
    const span = btn.querySelector('.sl-label');
    if (span) span.textContent = text;
  };
  return restore;
}
