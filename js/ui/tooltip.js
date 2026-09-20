/**
 * Hover hints, driven by `data-tip` on any element.
 *
 * Native `title` attributes were doing this job badly: a second of delay, no
 * styling, and on a dark map they arrive as a white system box. This keeps the
 * same one-attribute authoring but renders in our own chrome.
 *
 * Write a second line by putting it in an `<em>`:
 *   data-tip="Cast shadows<em>Turn off for a faster view</em>"
 */

const OPEN_DELAY = 260;
const GAP = 10;

let el = null;
let timer = null;
let anchor = null;

export function initTooltips() {
  el = document.getElementById('tooltip');
  if (!el) return;

  // Delegated, so anything added later (modals, generated rows) works for free.
  document.addEventListener('pointerover', onOver, true);
  document.addEventListener('pointerout', onOut, true);
  document.addEventListener('pointerdown', hide, true);
  window.addEventListener('scroll', hide, true);
  window.addEventListener('blur', hide);
}

function tipTarget(node) {
  return node instanceof Element ? node.closest('[data-tip]') : null;
}

function onOver(event) {
  // Touch and pen open things by tapping; a hover hint would just be in the way.
  if (event.pointerType && event.pointerType !== 'mouse') return;

  const target = tipTarget(event.target);
  if (!target || target === anchor) return;

  anchor = target;
  clearTimeout(timer);
  timer = setTimeout(() => show(target), OPEN_DELAY);
}

function onOut(event) {
  const target = tipTarget(event.target);
  if (!target) return;
  if (event.relatedTarget && tipTarget(event.relatedTarget) === target) return;
  hide();
}

function show(target) {
  if (!el || !target.isConnected) return;
  const tip = target.dataset.tip;
  if (!tip) return;

  // `data-tip` is authored by us, never by user input, so the markup is ours.
  el.innerHTML = tip;
  el.hidden = false;
  position(target);
  requestAnimationFrame(() => el.classList.add('is-in'));
}

/**
 * Sit below the element by default, above when that would run off the bottom,
 * and always clamped inside the viewport.
 */
function position(target) {
  const rect = target.getBoundingClientRect();
  const tip = el.getBoundingClientRect();

  let top = rect.bottom + GAP;
  if (top + tip.height > window.innerHeight - 8) top = rect.top - tip.height - GAP;

  let left = rect.left + rect.width / 2 - tip.width / 2;
  left = Math.min(Math.max(left, 8), window.innerWidth - tip.width - 8);

  el.style.top = `${Math.round(top)}px`;
  el.style.left = `${Math.round(left)}px`;
}

export function hide() {
  clearTimeout(timer);
  anchor = null;
  if (!el) return;
  el.classList.remove('is-in');
  el.hidden = true;
}
