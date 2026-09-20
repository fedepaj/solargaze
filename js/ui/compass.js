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
    button.title = `Bearing ${headingDeg.toFixed(0)}° — click to face north`;
  };

  on('camera', paint);
  paint();
}
