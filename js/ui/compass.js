/** Bottom-right compass: mirrors the camera bearing, click to face north. */

import { on } from '../state.js';
import { viewer, resetBearing } from '../scene.js';

export function initCompass() {
  const rose = document.getElementById('compass-rose');
  const button = document.getElementById('compass');
  if (!rose || !button) return;

  button.addEventListener('click', resetBearing);

  const paint = () => {
    if (!viewer) return;
    const headingDeg = window.Cesium.Math.toDegrees(viewer.camera.heading);
    // Turning the camera clockwise swings the card the other way, as on a real
    // compass rose held in the hand.
    rose.setAttribute('transform', `rotate(${(-headingDeg).toFixed(2)} 22 22)`);
    // `data-tip`, not `title`: the button already carries one and the browser
    // would draw its own box alongside ours.
    button.dataset.tip = `Bearing ${headingDeg.toFixed(0)}°<em>Click to face north</em>`;
  };

  on('camera', paint);
  paint();
}
