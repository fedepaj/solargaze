/** Transient status line at the bottom of the screen. */
let timer = null;

export function toast(message, { error = false, ms = 3600 } = {}) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = message;
  el.classList.toggle('err', error);
  el.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => { el.hidden = true; }, ms);
}
