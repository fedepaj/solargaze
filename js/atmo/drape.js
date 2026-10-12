/**
 * A field painted onto the world.
 *
 * The heat and air layers are one texture each, stretched over the grid's
 * rectangle and *classified* onto whatever is there: the photorealistic mesh
 * when it is up, the flat globe when it is not. Classification projects the
 * colour straight down, so a warm cell tints the streets and the walls of the
 * buildings standing in it alike — which is what a map of the air over a city
 * should look like, rather than a coloured sheet hovering over the rooftops.
 *
 * A raw GroundPrimitive rather than an entity: the entity rectangle takes the
 * same options but never appeared on the tileset, and the primitive has the
 * material uniform we need to swap the texture in place as the clock moves.
 */

import { requestRender } from '../scene.js';
import { CELL, pendingGrid, pendingFrame } from './pending.js';

const C = window.Cesium;

/** Texture side. 128 pixels over a 7-node grid is ~20 per cell: smooth, cheap. */
const SIZE = 128;

/**
 * Widen the volume the drape is projected through.
 *
 * Cesium bounds a classification volume by the *terrain* heights of the tile
 * it lands in, from a coarse global table. Buildings are not terrain: a tower
 * taller than the highest hill in the tile would poke out of the top of the
 * volume with its upper floors untinted. There is no option for this, so the
 * lookup is wrapped once to add headroom. The only ground primitives in this
 * app are the ones in this file, so nothing else feels it.
 */
function widenClassificationVolume() {
  const ATH = C.ApproximateTerrainHeights;
  if (!ATH || ATH.__solargazeWidened || typeof ATH.getMinimumMaximumHeights !== 'function') return;
  const original = ATH.getMinimumMaximumHeights;
  ATH.getMinimumMaximumHeights = function widened(...args) {
    const r = original.apply(this, args);
    r.minimumTerrainHeight -= 200;
    r.maximumTerrainHeight += 1500;
    return r;
  };
  ATH.__solargazeWidened = true;
}

function makeCanvas() {
  const cv = document.createElement('canvas');
  cv.width = cv.height = SIZE;
  return cv;
}

/** A rectangle of ground (west, south, east, north in degrees) classified with an image. */
function groundRect([west, south, east, north], image) {
  const material = C.Material.fromType('Image', { image });
  const primitive = new C.GroundPrimitive({
    geometryInstances: new C.GeometryInstance({
      geometry: new C.RectangleGeometry({
        rectangle: C.Rectangle.fromDegrees(west, south, east, north),
        vertexFormat: C.EllipsoidSurfaceAppearance.VERTEX_FORMAT,
      }),
    }),
    appearance: new C.EllipsoidSurfaceAppearance({ material, translucent: true, flat: true }),
    classificationType: C.ClassificationType.BOTH,
    asynchronous: false,
    allowPicking: false,
  });
  return { primitive, material };
}

export function createDrape(scene) {
  widenClassificationVolume();

  let primitive = null;
  let material = null;
  let gridKey = null;
  let shown = true;
  // Two canvases, used in turn. Cesium re-uploads a texture when the uniform
  // is set to a *different* object; repainting the same canvas in place would
  // leave the old pixels on the GPU.
  const canvases = [makeCanvas(), makeCanvas()];
  const contexts = canvases.map(cv => cv.getContext('2d', { willReadFrequently: true }));
  let flip = 0;

  function ensurePrimitive(grid) {
    if (primitive && gridKey === grid.key) return;
    if (primitive) {
      scene.primitives.remove(primitive);
      primitive = null;
    }
    gridKey = grid.key;
    ({ primitive, material } = groundRect([grid.west, grid.south, grid.east, grid.north], canvases[flip]));
    primitive.show = shown;
    scene.primitives.add(primitive);
  }

  /**
   * Repaint from a sampler.
   *
   * @param grid   the field's grid, which is also the rectangle to cover
   * @param value  (lat, lon) → number, NaN for no data
   * @param lut    256×3 colour table
   * @param domain [min, max] mapped onto the table
   * @param alpha  0..1 opacity of the tint
   */
  function paint(grid, value, lut, [lo, hi], alpha = 0.5) {
    clearSet();
    ensurePrimitive(grid);
    primitive.show = shown;
    flip ^= 1;
    const ctx = contexts[flip];
    const img = ctx.createImageData(SIZE, SIZE);
    const px = img.data;
    const span = hi - lo || 1;
    const a = Math.round(alpha * 255);
    // Canvas row 0 is the top of the texture, which Cesium lays along the
    // rectangle's northern edge.
    for (let y = 0; y < SIZE; y++) {
      const lat = grid.north - ((y + 0.5) / SIZE) * (grid.north - grid.south);
      for (let x = 0; x < SIZE; x++) {
        const lon = grid.west + ((x + 0.5) / SIZE) * (grid.east - grid.west);
        const v = value(lat, lon);
        const o = (y * SIZE + x) * 4;
        if (Number.isNaN(v)) { px[o + 3] = 0; continue; }
        const i = Math.min(255, Math.max(0, Math.round(((v - lo) / span) * 255))) * 3;
        px[o] = lut[i];
        px[o + 1] = lut[i + 1];
        px[o + 2] = lut[i + 2];
        px[o + 3] = a;
      }
    }
    ctx.putImageData(img, 0, 0);
    material.uniforms.image = canvases[flip];
    requestRender();
  }

  /**
   * Upload a finished canvas over a bounds rectangle: the raster products
   * arrive already at full resolution, and resampling them through a
   * 128-pixel sampler would throw away exactly the detail they exist for.
   */
  function paintCanvas(bounds, canvas) {
    paintSet([{ bounds, canvas }]);
  }

  /**
   * Several canvases over several rectangles — a mosaic of tiles. Each
   * keeps its own primitive, keyed by bounds; ones no longer in the list go.
   */
  const extras = new Map();
  function paintSet(items) {
    const keep = new Set();
    for (const { bounds, canvas } of items) {
      const key = bounds.join(',');
      keep.add(key);
      let entry = extras.get(key);
      if (!entry) {
        const { primitive: prim, material: mat } = groundRect(bounds, canvas);
        scene.primitives.add(prim);
        entry = { prim, mat };
        extras.set(key, entry);
      }
      entry.mat.uniforms.image = canvas;
      entry.prim.show = shown;
    }
    for (const [key, entry] of extras) {
      if (!keep.has(key)) { scene.primitives.remove(entry.prim); extras.delete(key); }
    }
    // The single-rectangle primitive and the mosaic never show together.
    if (primitive) primitive.show = false;
    requestRender();
  }

  function clearSet() {
    for (const [key, entry] of extras) { scene.primitives.remove(entry.prim); extras.delete(key); }
  }

  function show(on) {
    shown = on;
    if (primitive) primitive.show = on;
    for (const entry of extras.values()) entry.prim.show = on;
    requestRender();
  }

  return { paint, paintCanvas, show };
}

/**
 * The tiles of a view still on their way, pulsing in a wave from the pin
 * until they land (atmo/pending.js writes the pixels). A view of a province
 * is a few hundred small files, and for the seconds they take there was
 * nothing to say they were coming.
 *
 * `watch(rect, lat, lon, step)` starts a new picture and returns what the
 * loader calls: `plan(bounds[])` with the tiles it is fetching, `landed(bounds)`
 * as each arrives, `done()` at the end. A set that is in memory lands before
 * the picture's delay is up, and is never shown.
 */
const PENDING_DELAY_MS = 250;
const PENDING_FRAME_MS = 80;

export function createPending(scene) {
  let now = null;   // { grid, waiting, left, origin, prim, mat, canvases, flip, t0, timer }
  const still = globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

  function stop() {
    if (!now) return;
    clearTimeout(now.timer);
    clearInterval(now.timer);
    if (now.prim) scene.primitives.remove(now.prim);
    now = null;
    requestRender();
  }

  function frame(p) {
    p.flip ^= 1;
    const cv = p.canvases[p.flip];
    const ctx = cv.getContext('2d');
    const img = ctx.createImageData(cv.width, cv.height);
    pendingFrame(img.data, p.grid, p.waiting, p.origin, performance.now() - p.t0, still);
    ctx.putImageData(img, 0, 0);
    p.mat.uniforms.image = cv;
    requestRender();
  }

  function begin(p, rect) {
    if (now !== p || !p.left) return;
    p.canvases = [0, 1].map(() => {
      const cv = document.createElement('canvas');
      cv.width = p.grid.cols * CELL;
      cv.height = p.grid.rows * CELL;
      return cv;
    });
    p.flip = 0;
    ({ primitive: p.prim, material: p.mat } = groundRect(rect, p.canvases[0]));
    scene.primitives.add(p.prim);
    p.t0 = performance.now();
    frame(p);
    if (!still) p.timer = setInterval(() => frame(p), PENDING_FRAME_MS);
  }

  function watch(rect, lat, lon, step) {
    stop();
    const grid = pendingGrid(rect, step);
    const p = { grid, waiting: new Uint8Array(grid.cols * grid.rows), left: 0, origin: grid.at(lat, lon), prim: null };
    now = p;
    const mark = (bounds, on) => {
      const i = grid.cellOf(bounds);
      if (i < 0 || !!p.waiting[i] === on) return;
      p.waiting[i] = on ? 1 : 0;
      p.left += on ? 1 : -1;
    };
    return {
      plan(list) {
        if (now !== p) return;
        for (const bounds of list) mark(bounds, true);
        p.timer = setTimeout(() => begin(p, rect), PENDING_DELAY_MS);
      },
      landed(bounds) {
        if (now !== p) return;
        mark(bounds, false);
        // Without a wave there is no next frame to take the tile off the picture.
        if (still && p.prim) frame(p);
      },
      done() { if (now === p) stop(); },
    };
  }

  return { watch, stop };
}
