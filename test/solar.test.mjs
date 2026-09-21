/**
 * `node --test` over js/solar.js.
 *
 * solar.js is the one module with no browser in it, and the one whose output
 * the README makes numerical claims about. These lock those claims down: change
 * the equations and the spot-check in the README fails here first.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  sunPosition, dayEvents, daySampler, dayOfYear, fromDayOfYear, daysInYear, isLeap,
} from '../js/solar.js';

/* The figures quoted in README § Accuracy. Turin, 19 September 2026. */
const TURIN = { lat: 45.0546, lon: 7.6858 };
const CEST = 120;   // minutes east of UTC on that date

const wall = (utcMinutes, offset) => {
  const m = ((Math.round(utcMinutes + offset) % 1440) + 1440) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
};

test('README spot-check: sun position over Turin at 12:00 CEST', () => {
  const at = new Date(Date.UTC(2026, 8, 19, 10, 0, 0));
  const { elevation, azimuth } = sunPosition(at, TURIN.lat, TURIN.lon);
  assert.equal(elevation.toFixed(2), '42.65');
  assert.equal(azimuth.toFixed(2), '151.18');
});

test('README spot-check: sunrise and sunset over Turin', () => {
  const { sunrise, sunset } = dayEvents(2026, 9, 19, TURIN.lat, TURIN.lon);
  assert.equal(wall(sunrise, CEST), '07:12');
  assert.equal(wall(sunset, CEST), '19:33');
});

test('solar noon sits between sunrise and sunset, and the sun is highest there', () => {
  const { noon, sunrise, sunset } = dayEvents(2026, 6, 21, TURIN.lat, TURIN.lon);
  assert.ok(sunrise < noon && noon < sunset);

  const elevationAt = m =>
    sunPosition(new Date(Date.UTC(2026, 5, 21) + m * 60000), TURIN.lat, TURIN.lon).elevation;
  assert.ok(elevationAt(noon) > elevationAt(noon - 30));
  assert.ok(elevationAt(noon) > elevationAt(noon + 30));
});

test('polar day and polar night are reported, not faked', () => {
  const summer = dayEvents(2026, 6, 21, 78.22, 15.65);   // Svalbard
  assert.equal(summer.polar, 'day');
  assert.equal(summer.sunrise, null);

  const winter = dayEvents(2026, 12, 21, 78.22, 15.65);
  assert.equal(winter.polar, 'night');
  assert.equal(winter.sunset, null);

  assert.equal(dayEvents(2026, 6, 21, TURIN.lat, TURIN.lon).polar, null);
});

test('daySampler tracks sunPosition across a whole day', () => {
  // The interpolation budget claimed in solar.js is 0.0035°; hold it to that.
  const at = daySampler(2026, 3, 20, TURIN.lat, TURIN.lon);
  let worst = 0;
  for (let m = 0; m <= 1440; m += 7) {
    const exact = sunPosition(new Date(Date.UTC(2026, 2, 20) + m * 60000), TURIN.lat, TURIN.lon);
    const approx = at(m);
    worst = Math.max(worst, Math.abs(exact.elevation - approx.elevation));
    if (exact.elevation > 1) {
      worst = Math.max(worst, Math.abs(exact.azimuth - approx.azimuth));
    }
  }
  assert.ok(worst < 0.0035, `worst interpolation error ${worst.toFixed(5)}° exceeds the budget`);
});

test('day-of-year round-trips, leap years included', () => {
  for (const y of [2025, 2026, 2028, 2100, 2400]) {
    assert.equal(dayOfYear(y, 12, 31), daysInYear(y));
    for (const doy of [1, 59, 60, 200, daysInYear(y)]) {
      const { y: ry, m, d } = fromDayOfYear(y, doy);
      assert.equal(ry, y);
      assert.equal(dayOfYear(ry, m, d), doy);
    }
  }
  assert.equal(isLeap(2028), true);
  assert.equal(isLeap(2100), false);
  assert.equal(isLeap(2400), true);
});

test('azimuth stays in [0,360) and elevation in [-90,90]', () => {
  for (const lat of [-89, -45, 0, 45, 78, 89]) {
    for (const m of [0, 370, 725, 1100, 1439]) {
      const { elevation, azimuth } = sunPosition(new Date(Date.UTC(2026, 6, 4) + m * 60000), lat, 12);
      assert.ok(azimuth >= 0 && azimuth < 360, `azimuth ${azimuth} out of range`);
      assert.ok(elevation >= -90 && elevation <= 90, `elevation ${elevation} out of range`);
    }
  }
});
