/** Helpers small enough to have no home of their own. Pure: the tests import some. */

export const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** Two digits, as clocks and dates are written. */
export const pad2 = n => String(n).padStart(2, '0');

/**
 * Escape a string for interpolation into HTML.
 *
 * Needed in two places that build markup as strings: the failure text on the
 * connect screen, which embeds a message from Cesium ion, and the token fields,
 * whose `value` is whatever was pasted last.
 */
export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}
