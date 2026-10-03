/**
 * What kind of machine this is, decided once at load.
 *
 * Two questions matter. Is there a mouse — which decides hover hints, the
 * cheat sheet and how big a target the pin needs to be — and is this a phone,
 * where the same shadow map and the same tile budget that are fine on a
 * laptop turn into a hot, stuttering, memory-starved page. The answers here
 * set the *defaults*; every one of them stays adjustable in Settings.
 */

const coarse = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;

/** Fingers rather than a pointer. Includes tablets and touch laptops. */
export const isTouch = coarse || (navigator.maxTouchPoints ?? 0) > 0;

/**
 * A phone: touch, and a short side under 700 CSS pixels. Tablets are left
 * with the desktop budget — an iPad renders this app comfortably.
 */
export const isPhone = isTouch && Math.min(screen.width, screen.height) < 700;

/** The narrow layout, which follows the viewport rather than the device. */
export const compactLayout = () =>
  typeof matchMedia === 'function' && matchMedia('(max-width: 640px)').matches;

export const profile = {
  /** MSAA samples for the WebGL context; four is free on a desktop GPU and not on a phone. */
  msaa: isPhone ? 1 : 4,
  shadowQuality: isPhone ? 1024 : 2048,
  softShadows: !isPhone,
  /** Coarser tiles on a phone: the screen is small and the ion quota is not. */
  meshDetail: isPhone ? 24 : 16,
  /** Tile cache. Mobile Safari kills a tab well before Cesium's 512 MB default. */
  cacheBytes: (isPhone ? 192 : 512) * 1024 * 1024,
  /** Wind particles. Each one is a polyline updated every frame. */
  windParticles: isPhone ? 140 : 320,
  /** Street air is drawn at this share of its 50 m cells: a quarter of the pixels on a phone. */
  streetScale: isPhone ? 0.5 : 1,
  /** Surface heat: up to this many tiles in view at 90 m, then at 270 m; beyond, 810 m. */
  heatLevels: isPhone ? [4, 30] : [12, 110],
  /** The widest a drape texture may be, in pixels, on either side. */
  maxTexture: isPhone ? 2048 : 4096,
};
