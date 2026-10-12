import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CELL, pendingGrid, pendingFrame, glow } from '../js/atmo/pending.js';

const RECT = [9, 48, 10, 48.5];      // four tiles wide, two tall
const grid = pendingGrid(RECT, 0.25);

test('a rectangle of tiles is one cell a tile, north on top', () => {
  assert.equal(grid.cols, 4);
  assert.equal(grid.rows, 2);
  assert.equal(grid.cellOf([9, 48.25, 9.25, 48.5]), 0);        // north-west
  assert.equal(grid.cellOf([9.75, 48, 10, 48.25]), 7);         // south-east
  assert.equal(grid.cellOf([10, 48, 10.25, 48.25]), -1);       // outside
  assert.deepEqual(grid.at(48.375, 9.125), [0, 0]);            // the middle of the first cell
});

const alphaAt = (px, cell, x = 0, y = 0) => {
  const col = cell % grid.cols;
  const row = Math.floor(cell / grid.cols);
  return px[((row * CELL + y) * grid.cols * CELL + col * CELL + x) * 4 + 3];
};

test('only the tiles still waiting are lit, and a gap is left between them', () => {
  const px = new Uint8ClampedArray(grid.cols * CELL * grid.rows * CELL * 4);
  const waiting = new Uint8Array(8);
  waiting[1] = waiting[6] = 1;
  pendingFrame(px, grid, waiting, [0, 0], 0);
  assert.ok(alphaAt(px, 1) > 0);
  assert.ok(alphaAt(px, 6) > 0);
  assert.equal(alphaAt(px, 0), 0);
  assert.equal(alphaAt(px, 7), 0);
  assert.equal(alphaAt(px, 1, CELL - 1, 0), 0);                // the gap
  assert.equal(alphaAt(px, 1, 0, CELL - 1), 0);
  // A tile that lands goes dark on the next frame.
  waiting[1] = 0;
  pendingFrame(px, grid, waiting, [0, 0], 80);
  assert.equal(alphaAt(px, 1), 0);
});

test('the light is a wave that moves outward from where it starts', () => {
  // The crest is at the origin at t = 0 and reaches a tile five cells out one period later.
  assert.ok(glow(0, 0) > glow(2.5, 0));
  assert.ok(Math.abs(glow(0, 0) - glow(5, 1600)) < 1e-9);
  assert.ok(glow(5, 1600) > glow(5, 800));
  // Never dark between crests, never opaque on one.
  for (let t = 0; t < 3200; t += 50) {
    const g = glow(3, t);
    assert.ok(g >= 0.07 - 1e-9 && g <= 0.3 + 1e-9);
  }
});

test('with less motion asked for, the tiles are lit and still', () => {
  assert.equal(glow(0, 0, true), glow(7, 900, true));
});
