/**
 * `node --test` over js/atmo/mosaic.js — the pixels a set of tiles is drawn
 * with. The ground and the pane must say the same thing, so the picture is
 * held to the samplers the readings come from (js/atmo/field.js).
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { mosaicLayout, paintMosaic, mosaicSteps } from '../js/atmo/mosaic.js';
import { sampleRaster, sampleStreet } from '../js/atmo/field.js';

/** A colour table whose red is its own index: a painted pixel then says which entry it took. */
const INDEX_LUT = Uint8ClampedArray.from({ length: 256 * 3 }, (_, k) => Math.floor(k / 3));
const valueOf = (red, [lo, hi]) => lo + (red / 255) * (hi - lo);

function paint(series, ctx, domain, limits = { maxTexture: 4096 }) {
  const layout = mosaicLayout(series, ctx, limits);
  const px = new Uint8ClampedArray(layout.W * layout.H * 4);
  paintMosaic(px, layout, series, ctx, INDEX_LUT, domain, 0.5);
  return { layout, px, at: (x, y) => [...px.subarray((y * layout.W + x) * 4, (y * layout.W + x) * 4 + 4)] };
}

/** A raster tile of `n × n` cells over a quarter-degree square, bytes from `fn(row, col)`. */
function rasterTile(west, south, n, fn) {
  const values = Uint8Array.from({ length: n * n }, (_, k) => fn(Math.floor(k / n), k % n));
  const img = { values, cols: n, rows: n };
  return {
    kind: 'raster', level: 1, bounds: [west, south, west + 0.25, south + 0.25], rows: n, cols: n,
    months: Object.fromEntries([...Array(12).keys()].map(m => [m, img])), decode: b => 10 + (b - 1) * 0.5,
  };
}

test('a mosaic gives each tile its own resolution until the texture limit, and never under four pixels', () => {
  const tile = rasterTile(12, 41, 300, () => 1);
  const set = { kind: 'raster-set', rect: [12, 41, 13, 41.5], tiles: [tile] };
  assert.deepEqual(mosaicLayout(set, {}, { maxTexture: 4096 }), { nx: 4, ny: 2, P: 300, W: 1200, H: 600 });
  assert.equal(mosaicLayout(set, {}, { maxTexture: 400 }).P, 100);
  assert.equal(mosaicLayout(set, {}, { maxTexture: 8 }).P, 4);
  // Coarse and fine-grained at once (an older satellite at 120 m) is not blown up: it is read in blocks.
  assert.equal(mosaicLayout(set, { coarse: true }, { maxTexture: 4096 }).P, 300);
  const street = { kind: 'street-set', rect: [12, 41, 12.25, 41.25], tiles: [{ cols: 556 }] };
  assert.equal(mosaicLayout(street, {}, { maxTexture: 4096, streetScale: 0.5 }).P, 278);
});

test('a raster mosaic paints each tile where it lies, clear where unobserved, grey where no tile is', () => {
  // A rectangle two tiles wide, of which only the eastern one exists.
  const tile = rasterTile(12.25, 41, 8, (r, c) => (r === 0 && c === 0 ? 0 : 1 + r * 8 + c));
  const set = { kind: 'raster-set', rect: [12, 41, 12.5, 41.25], tiles: [tile] };
  const domain = [10, 42];
  const { layout, at } = paint(set, { monthFrac: 3, kind: 'raster-months' }, domain);
  assert.deepEqual([layout.W, layout.H, layout.P], [16, 8, 8]);

  assert.deepEqual(at(3, 3), [128, 128, 128, 64], 'no tile: not computed here yet');
  assert.equal(at(8, 0)[3], 0, 'byte 0 is no observation');
  // Every other pixel is the cell under it, as the sampler reads its centre.
  for (const [r, c] of [[0, 1], [3, 5], [7, 7]]) {
    const [red, , , alpha] = at(8 + c, r);
    assert.equal(alpha, 128);
    const lat = 41.25 - ((r + 0.5) / 8) * 0.25;
    const lon = 12.25 + ((c + 0.5) / 8) * 0.25;
    assert.ok(Math.abs(valueOf(red, domain) - sampleRaster(tile, lat, lon, 3)) < (domain[1] - domain[0]) / 255);
  }
});

test('a fine grid marked coarse is read in 2 × 2 blocks', () => {
  const fine = rasterTile(12, 41, 8, (r, c) => 1 + r * 8 + c);
  const set = { kind: 'raster-set', rect: [12, 41, 12.25, 41.25], tiles: [fine] };
  const sharp = paint(set, { monthFrac: 0 }, [10, 42]);
  assert.notDeepEqual(sharp.at(1, 1), sharp.at(0, 0));
  // Every pixel takes the top-left cell of its block.
  const blocky = paint(set, { monthFrac: 0, blocky: true }, [10, 42]);
  assert.deepEqual(blocky.at(1, 1), blocky.at(0, 0));
  assert.deepEqual(blocky.at(3, 2), blocky.at(2, 2));
  assert.notDeepEqual(blocky.at(2, 2), blocky.at(0, 0));
  // An overview is coarse already and is left as it is.
  const overview = paint({ ...set, tiles: [{ ...fine, level: 3 }] }, { monthFrac: 0, blocky: true }, [10, 42]);
  assert.deepEqual(overview.at(1, 1), sharp.at(1, 1));
});

test('coarse cells are drawn eight pixels a side with a seam on their first row and column, across tiles too', () => {
  const west = rasterTile(12, 41, 3, () => 5);
  const east = rasterTile(12.25, 41, 3, () => 5);
  const set = { kind: 'raster-set', rect: [12, 41, 12.5, 41.25], tiles: [west, east] };
  const { layout, at } = paint(set, { monthFrac: 0, coarse: true }, [10, 14]);
  assert.deepEqual([layout.P, layout.W, layout.H], [24, 48, 24]);
  const alphas = y => [...Array(layout.W).keys()].map(x => at(x, y)[3]);
  const seam = Math.round(128 * 0.45);
  // Inside a row of cells: a seam every eight pixels, the tile edge at 24 included.
  assert.deepEqual(alphas(1), [...Array(48).keys()].map(x => (x % 8 === 0 ? seam : 128)));
  // The first pixel row of each cell is seam all along.
  for (const y of [0, 8, 16]) assert.ok(alphas(y).every(a => a === seam));
  // Where the texture has no room for eight pixels a cell, there are none.
  const tight = paint(set, { monthFrac: 0, coarse: true }, [10, 14], { maxTexture: 24 });
  assert.equal(tight.layout.P, 12);
  assert.ok([...Array(24).keys()].every(x => tight.at(x, 1)[3] === 128));
});

test('a mosaic painted a tile at a time is the mosaic painted at once', () => {
  const tiles = [rasterTile(12, 41, 8, (r, c) => 1 + r + c), rasterTile(12.25, 41, 8, (r, c) => 200 - r * c)];
  const set = { kind: 'raster-set', rect: [12, 41, 12.75, 41.25], tiles };
  const whole = paint(set, { monthFrac: 2.5 }, [10, 110]);
  const px = new Uint8ClampedArray(whole.px.length).fill(7);
  const steps = mosaicSteps(px, whole.layout, set, { monthFrac: 2.5 }, INDEX_LUT, [10, 110], 0.5);
  assert.equal(steps.length, 3);   // the grey, then a tile each
  for (const step of steps) step();
  assert.deepEqual(px, whole.px);
});

test('a street mosaic is streetValue, cell by cell: modelled pollutants, ozone by titration, PM2.5 as CAMS', () => {
  const n = 8;
  const bounds = [12, 41, 12.25, 41.25];
  // CAMS on a 2 × 2 grid around the tile, linear in space and steady in time, so a lattice loses nothing.
  const grid = { rows: 2, cols: 2, step: 0.5, south: 40.9, north: 41.4, west: 11.9, east: 12.4 };
  const steady = corners => Float32Array.from({ length: 288 * 4 }, (_, k) => corners[k % 4]);
  const clim = { grid, vars: { nitrogen_dioxide: steady([20, 30, 40, 50]), ozone: steady([80, 70, 60, 50]), pm2_5: steady([8, 9, 10, 11]) } };
  const ratioOf = Float32Array.from({ length: 256 }, (_, b) => (b ? 0.5 + b / 100 : NaN));
  const bytes = { nitrogen_dioxide: Uint8Array.from({ length: n * n }, (_, k) => (k === 5 ? 0 : 20 + k)) };
  const tile = { kind: 'street', bounds, rows: n, cols: n, bytes, ratioOf, clim };
  const set = { kind: 'street-set', rect: bounds, tiles: [tile], centre: tile };

  for (const option of ['nitrogen_dioxide', 'ozone', 'pm2_5']) {
    const domain = [0, 120];
    const { layout, at } = paint(set, { option, monthFrac: 6.3, hourFrac: 14.5 }, domain);
    assert.equal(layout.P, n);
    for (const [r, c] of [[0, 0], [0, 5], [3, 4], [7, 7]]) {
      const lat = 41.25 - ((r + 0.5) / n) * 0.25;
      const lon = 12 + ((c + 0.5) / n) * 0.25;
      const want = sampleStreet(set, option, lat, lon, 6.3, 14.5);
      assert.ok(Math.abs(valueOf(at(c, r)[0], domain) - want) < 0.5, `${option} at ${r},${c}: ${valueOf(at(c, r)[0], domain)} vs ${want}`);
    }
  }
});
