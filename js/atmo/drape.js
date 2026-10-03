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
    material = C.Material.fromType('Image', { image: canvases[flip] });
    primitive = new C.GroundPrimitive({
      geometryInstances: new C.GeometryInstance({
        geometry: new C.RectangleGeometry({
          rectangle: C.Rectangle.fromDegrees(grid.west, grid.south, grid.east, grid.north),
          vertexFormat: C.EllipsoidSurfaceAppearance.VERTEX_FORMAT,
        }),
      }),
      appearance: new C.EllipsoidSurfaceAppearance({ material, translucent: true, flat: true }),
      classificationType: C.ClassificationType.BOTH,
      asynchronous: false,
      allowPicking: false,
    });
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
        const [west, south, east, north] = bounds;
        const mat = C.Material.fromType('Image', { image: canvas });
        const prim = new C.GroundPrimitive({
          geometryInstances: new C.GeometryInstance({
            geometry: new C.RectangleGeometry({
              rectangle: C.Rectangle.fromDegrees(west, south, east, north),
              vertexFormat: C.EllipsoidSurfaceAppearance.VERTEX_FORMAT,
            }),
          }),
          appearance: new C.EllipsoidSurfaceAppearance({ material: mat, translucent: true, flat: true }),
          classificationType: C.ClassificationType.BOTH,
          asynchronous: false,
          allowPicking: false,
        });
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

  function destroy() {
    clearSet();
    if (primitive) scene.primitives.remove(primitive);
    primitive = null;
    gridKey = null;
  }

  return { paint, paintCanvas, paintSet, show, destroy, get visible() { return shown && (!!primitive || extras.size > 0); } };
}
