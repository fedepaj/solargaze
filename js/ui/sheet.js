/**
 * On a phone the control panel is a sheet docked to the bottom edge, and
 * everything else that lives at the bottom — the rail of buttons, the view
 * tools — stacks on top of it. Its height is not a constant: the cards grow
 * as layers are switched on, and a collapsed state leaves only the grab bar.
 * So the height is measured and published as a CSS variable, and the
 * stylesheet does the stacking arithmetic from there.
 */

import { on } from '../state.js';

const $ = id => document.getElementById(id);

export function initSheet() {
  const panel = $('panel');
  const handle = $('sheet-handle');
  if (!panel || !handle) return;

  const measure = () => {
    document.documentElement.style.setProperty('--panel-h', `${panel.offsetHeight}px`);
  };

  const setCollapsed = collapsed => {
    panel.classList.toggle('is-collapsed', collapsed);
    handle.setAttribute('aria-expanded', String(!collapsed));
    handle.setAttribute('aria-label', collapsed ? 'Expand the panel' : 'Collapse the panel');
    measure();
  };

  handle.addEventListener('click', () => setCollapsed(!panel.classList.contains('is-collapsed')));

  if ('ResizeObserver' in window) new ResizeObserver(measure).observe(panel);
  window.addEventListener('resize', measure);
  measure();
}
