/**
 * `node --test` over js/atmo/field.js — the maths behind the heat, air and
 * wind layers. Everything here runs without a browser.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  gridFor, buildSeries, sampleAt, windAt, rangeOf, compassName, sampleClimatology, monthFraction, sampleRaster,
} from '../js/atmo/field.js';
import { bandOf, rampLut, airStops, heatStops } from '../js/atmo/scales.js';
import { SOURCES, resolveDate, lastYear, shiftDate } from '../js/atmo/sources.js';
import { LAYERS, rivalsOf, sourceOf, modeOf, optionsOf } from '../js/atmo/layers.js';

const W = SOURCES.weather;
const A = SOURCES.air;

/** A fake Open-Meteo response: 3×3 nodes, `hours` hours, values from `fn`. */
function fakeLocations(grid, hours, fn) {
  const times = [];
  for (let h = 0; h < hours; h++) times.push(`2026-09-25T${String(h).padStart(2, '0')}:00`);
  return grid.lats.map((lat, node) => ({
    location_id: node || undefined,
    hourly: {
      time: times,
      temperature_2m: times.map((_, h) => fn(lat, grid.lons[node], h)),
      apparent_temperature: times.map((_, h) => fn(lat, grid.lons[node], h) + 1),
      wind_speed_10m: times.map(() => 4),
      wind_direction_10m: times.map(() => 270),   // from the west
    },
  }));
}

test('the grid snaps to the model spacing and lists nodes south-to-north, west-to-east', () => {
  const g = gridFor(W, 41.8905, 12.4924, 3);
  assert.equal(g.step, 0.0625);
  assert.equal(g.n, 3);
  assert.equal(g.south, 41.8125);
  assert.equal(g.north, 41.9375);
  assert.equal(g.west, 12.4375);
  assert.equal(g.east, 12.5625);
  assert.deepEqual(g.lats.slice(0, 3), [41.8125, 41.8125, 41.8125]);
  assert.deepEqual(g.lons.slice(0, 3), [12.4375, 12.5, 12.5625]);
  // A nudge well inside one cell keeps the same grid — and the same cache key.
  assert.equal(gridFor(W, 41.895, 12.50, 3).key, g.key);
});

test('bilinear sampling reproduces a linear field exactly and interpolates in time', () => {
  const g = gridFor(W, 45, 7, 3);
  // Temperature rises 10 °C per degree of latitude and 1 °C per hour.
  const s = buildSeries(W, g, fakeLocations(g, 4, (lat, lon, h) => (lat - g.south) * 10 + h));
  const t = h => s.t0 + h * 3600000;

  assert.ok(Math.abs(sampleAt(s, 'temperature_2m', g.south, g.west, t(0)) - 0) < 1e-4);
  assert.ok(Math.abs(sampleAt(s, 'temperature_2m', g.north, g.east, t(0)) - (g.north - g.south) * 10) < 1e-4);
  const mid = (g.south + g.north) / 2;
  assert.ok(Math.abs(sampleAt(s, 'temperature_2m', mid, g.west, t(0)) - (mid - g.south) * 10) < 1e-4);
  // Half past the hour sits halfway between the two hourly values.
  assert.ok(Math.abs(sampleAt(s, 'temperature_2m', g.south, g.west, t(1.5)) - 1.5) < 1e-4);
  // Outside the window there is no answer, not a stale one.
  assert.ok(Number.isNaN(sampleAt(s, 'temperature_2m', g.south, g.west, t(-3))));
  assert.ok(Number.isNaN(sampleAt(s, 'temperature_2m', g.south, g.west, t(9))));
});

test('missing model values stay NaN rather than turning into zero', () => {
  const g = gridFor(A, 45, 7, 3);
  const locs = g.lats.map((lat, node) => ({
    location_id: node || undefined,
    hourly: { time: ['2026-09-25T00:00', '2026-09-25T01:00'], pm2_5: [node === 4 ? null : 8, 9] },
  }));
  const s = buildSeries(A, g, locs);
  const centre = { lat: g.lats[4], lon: g.lons[4] };
  assert.ok(Number.isNaN(sampleAt(s, 'pm2_5', centre.lat, centre.lon, s.t0)));
  assert.equal(sampleAt(s, 'pm2_5', centre.lat, centre.lon, s.t0 + 3600000), 9);
  assert.equal(sampleAt(s, 'pm2_5', g.south, g.west, s.t0), 8);
});

test('wind is carried as components and comes back as a speed and a "from" bearing', () => {
  const g = gridFor(W, 45, 7, 3);
  const s = buildSeries(W, g, fakeLocations(g, 2, () => 20));
  const w = windAt(s, 45, 7, s.t0);
  assert.ok(Math.abs(w.speed - 4) < 1e-4);
  assert.ok(Math.abs(w.from - 270) < 1e-4);
  assert.ok(w.u > 3.99, 'a west wind blows eastward');
  assert.ok(Math.abs(w.v) < 1e-4);
  assert.equal(compassName(w.from), 'W');
  assert.equal(compassName(22), 'NNE');
  assert.equal(compassName(359), 'N');
});

test('the legend domain spans every node across the requested hours', () => {
  const g = gridFor(W, 45, 7, 3);
  const s = buildSeries(W, g, fakeLocations(g, 6, (lat, lon, h) => 10 + h));
  assert.deepEqual(rangeOf(s, 'temperature_2m', s.t0, s.t0 + 2 * 3600000), { min: 10, max: 12 });
  assert.deepEqual(rangeOf(s, 'temperature_2m', s.t0, s.t0 + 40 * 3600000), { min: 10, max: 15 });
});

test('EAQI bands follow the EEA edges per pollutant', () => {
  assert.equal(bandOf('european_aqi', 0).name, 'Good');
  assert.equal(bandOf('european_aqi', 20).name, 'Fair');
  assert.equal(bandOf('european_aqi', 99.9).name, 'Very poor');
  assert.equal(bandOf('european_aqi', 140).name, 'Extremely poor');
  assert.equal(bandOf('pm2_5', 12).name, 'Fair');
  assert.equal(bandOf('ozone', 130).name, 'Poor');
});

test('colour ramps are monotone tables with the stop colours at the stops', () => {
  const lut = rampLut(heatStops());
  assert.equal(lut.length, 256 * 3);
  assert.deepEqual([...lut.slice(0, 3)], [0x3b, 0x4f, 0xc2]);
  assert.deepEqual([...lut.slice(255 * 3)], [0x8e, 0x11, 0x30]);
  const air = airStops('pm2_5');
  assert.equal(air.length, 6);
  assert.equal(air[0][0], 0);
  assert.equal(air[1][0], 0.1);
  for (let i = 1; i < air.length; i++) assert.ok(air[i][0] >= air[i - 1][0]);
});

test('the day to fetch follows the forecast horizons and falls back a year', () => {
  const today = { y: 2026, m: 9, d: 25 };
  assert.deepEqual(resolveDate(W, today, today), { date: today, endpoint: 'forecast', proxy: null });
  assert.equal(resolveDate(W, shiftDate(today, 14), today).endpoint, 'forecast');
  const far = resolveDate(W, shiftDate(today, 15), today);
  assert.equal(far.endpoint, 'archive');
  assert.equal(far.proxy, 'last-year');
  assert.deepEqual(far.date, { y: 2025, m: 10, d: 10 });
  assert.equal(resolveDate(W, shiftDate(today, -85), today).endpoint, 'forecast');
  assert.equal(resolveDate(W, shiftDate(today, -86), today).endpoint, 'archive');
  assert.equal(resolveDate(A, shiftDate(today, 4), today).proxy, null);
  assert.equal(resolveDate(A, shiftDate(today, 5), today).proxy, 'last-year');
  assert.equal(resolveDate(A, { y: 2012, m: 6, d: 1 }, today), null);
});

test('the layer registry is well-formed and drapes exclude each other', () => {
  const ids = new Set();
  for (const layer of LAYERS) {
    assert.ok(!ids.has(layer.id), `duplicate layer id ${layer.id}`);
    ids.add(layer.id);
    const modes = layer.modes ? layer.modes.choices.map(c => c.key) : [null];
    for (const mode of modes) {
      const prefs = layer.modes ? { [layer.modes.pref]: mode } : {};
      assert.ok(SOURCES[sourceOf(layer, prefs)], `${layer.id}/${mode} names an unknown source`);
      const group = optionsOf(layer, mode);
      if (group) assert.ok(group.choices.some(c => c.key === group.fallback), `${layer.id}/${mode} fallback`);
    }
    for (const extra of layer.also || []) assert.ok(SOURCES[extra], `${layer.id} extra ${extra}`);
    assert.ok(['drape', 'particles'].includes(layer.render));
    assert.equal(typeof layer.reading, 'function');
    if (layer.render === 'drape') {
      for (const fn of ['field', 'domain', 'stops', 'legend']) assert.equal(typeof layer[fn], 'function', `${layer.id}.${fn}`);
    }
  }
  const temperature = LAYERS.find(l => l.id === 'temperature');
  assert.equal(modeOf(temperature, {}), 'date');
  assert.equal(modeOf(temperature, { tempMode: 'nonsense' }), 'date');
  assert.equal(sourceOf(temperature, { tempMode: 'surface' }), 'tile-heat');
  assert.deepEqual(rivalsOf(temperature).map(l => l.id), ['air']);
  assert.deepEqual(rivalsOf(LAYERS.find(l => l.id === 'wind')), []);
});

test('a climatology wraps December into January and 23:30 into midnight', () => {
  const grid = { rows: 2, cols: 2, step: 0.1, south: 41.8, west: 12.4, north: 41.9, east: 12.5 };
  const nodes = 4;
  const arr = new Float32Array(288 * nodes);
  // value = month * 10 + hour, the same at every node
  for (let m = 0; m < 12; m++) for (let h = 0; h < 24; h++) for (let k = 0; k < nodes; k++) arr[(m * 24 + h) * nodes + k] = m * 10 + h;
  const s = { grid, vars: { pm2_5: arr } };
  assert.ok(Math.abs(sampleClimatology(s, 'pm2_5', 41.85, 12.45, 3, 8) - 38) < 1e-4);
  // half way from March to April at 08:30
  assert.ok(Math.abs(sampleClimatology(s, 'pm2_5', 41.85, 12.45, 3.5, 8.5) - 43.5) < 1e-4);
  // mid December blended half way into January: (110 + 0) / 2 + hour
  assert.ok(Math.abs(sampleClimatology(s, 'pm2_5', 41.85, 12.45, 11.5, 0) - 55) < 1e-4);
  // 23:30 blends hour 23 with hour 0 of the same month
  assert.ok(Math.abs(sampleClimatology(s, 'pm2_5', 41.85, 12.45, 0, 23.5) - 11.5) < 1e-4);
  assert.ok(Math.abs(monthFraction(15.5) - 0) < 1e-9);
  assert.ok(Math.abs(monthFraction(15.5 + 365 / 12) - 1) < 1e-9);
});

test('a raster stack is read by nearest pixel, blended between months, transparent where unobserved', () => {
  const cols = 4; const rows = 2;
  const month = fill => { const d = new Uint8ClampedArray(cols * rows * 4); for (let i = 0; i < cols * rows; i++) { d[i * 4] = fill(i); d[i * 4 + 3] = fill(i) === 0 ? 0 : 255; } return { data: d, cols, rows }; };
  const raster = {
    bounds: [12, 41, 13, 41.5], rows, cols, decode: b => b / 10,
    months: { 5: month(i => 100 + i), 6: month(i => i === 0 ? 0 : 200 + i) },
  };
  // pixel (x=0, y=0) is the north-west corner
  assert.equal(sampleRaster(raster, 41.4, 12.1, 5), 10.0);
  assert.equal(sampleRaster(raster, 41.4, 12.9, 5), 10.3);
  assert.equal(sampleRaster(raster, 41.1, 12.9, 5), 10.7);
  // half way between June and July at pixel 1: (101 + 201) / 2 / 10
  assert.ok(Math.abs(sampleRaster(raster, 41.4, 12.3, 5.5) - 15.1) < 1e-9);
  // July has no observation at pixel 0: fall back to June alone
  assert.equal(sampleRaster(raster, 41.4, 12.1, 5.5), 10.0);
  // a month with no raster at all
  assert.ok(Number.isNaN(sampleRaster(raster, 41.4, 12.1, 2)));
  assert.ok(Number.isNaN(sampleRaster(raster, 40, 12.1, 5)));
});
