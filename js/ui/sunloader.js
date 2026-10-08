/**
 * The waiting state, as the mark in motion.
 *
 * The logo is a sun over two contour lines; here the sun rises and sets
 * behind the nearer one while the lines flow past, like hills seen from a
 * moving train or water in a light wind. The same mark as the wordmark and
 * the favicon, which is the point: a generic ring would say "something is
 * loading", and this says "SolarGaze is loading".
 *
 * The SVG carries no size of its own. It fills its host, and the host is sized
 * with `font-size`, so the same markup serves the 16 px slot in the search bar
 * and the 34 px one in the middle of a pane. See `.sunload` in app.css.
 */

/**
 * Two contour lines long enough to slide by one period without showing an
 * end: half-waves of 13 units (the near line) and 10 (the far one), as in
 * the logo, repeated.
 */
const wave = (x0, y, half, amp, n) => {
  let d = `M${x0} ${y}c${half / 3} ${-amp} ${(2 * half) / 3} ${-amp} ${half} 0`;
  for (let i = 1; i < n; i++) d += `s${(2 * half) / 3} ${i % 2 ? amp : -amp} ${half} 0`;
  return d;
};

const SVG =
  '<svg viewBox="0 0 32 32" aria-hidden="true">' +
  // The sun sinks below the near line, not through it.
  '<clipPath id="sl-sky"><rect x="-4" y="-12" width="40" height="33.5"/></clipPath>' +
  '<g clip-path="url(#sl-sky)"><circle class="sl-sun" cx="16" cy="12.5" r="7"/></g>' +
  `<g class="sl-waves"><path class="sl-w1" d="${wave(-49, 21.5, 13, 3.2, 8)}"/>` +
  `<path class="sl-w2" d="${wave(-14, 27.5, 10, 2.4, 7)}"/></g>` +
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
