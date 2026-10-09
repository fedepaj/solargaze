/**
 * Colour, and the bands behind it.
 *
 * The heat ramp and the European Air Quality Index live here so that the
 * legend in the pane and the texture on the ground are drawn from the same
 * stops — they can disagree only by editing one file.
 */

const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

/**
 * The heat ramp: cold blue through a pale neutral to a deep red. Luminance
 * rises with temperature so it survives greyscale and most colour blindness.
 */
export const HEAT_STOPS = ['#3b4fc2', '#5c9fdd', '#a5d6cb', '#f7ec9c', '#f5a850', '#df4b3b', '#8e1130'];

/**
 * The European Air Quality Index, as the EEA draws it. The same six bands and
 * the same colours apply to each pollutant, with per-pollutant edges in µg/m³
 * — so a PM2.5 map and an NO₂ map read on the same legend.
 */
export const EAQI_BANDS = [
  { name: 'Good', colour: '#50f0e6' },
  { name: 'Fair', colour: '#50ccaa' },
  { name: 'Moderate', colour: '#f0e641' },
  { name: 'Poor', colour: '#ff5050' },
  { name: 'Very poor', colour: '#960032' },
  { name: 'Extremely poor', colour: '#7d2181' },
];

export const AIR_METRICS = {
  european_aqi: { label: 'EAQI', long: 'European Air Quality Index', unit: '', edges: [20, 40, 60, 80, 100], top: 125 },
  pm2_5: { label: 'PM2.5', long: 'Fine particles, PM2.5', unit: 'µg/m³', edges: [10, 20, 25, 50, 75], top: 100 },
  pm10: { label: 'PM10', long: 'Coarse particles, PM10', unit: 'µg/m³', edges: [20, 40, 50, 100, 150], top: 200 },
  nitrogen_dioxide: { label: 'NO₂', long: 'Nitrogen dioxide', unit: 'µg/m³', edges: [40, 90, 120, 230, 340], top: 400 },
  ozone: { label: 'O₃', long: 'Ozone', unit: 'µg/m³', edges: [50, 100, 130, 240, 380], top: 450 },
};

/** Which EAQI band a value of `metric` falls in. */
export function bandOf(metric, value) {
  const { edges } = AIR_METRICS[metric];
  let i = 0;
  while (i < edges.length && value >= edges[i]) i++;
  return { index: i, ...EAQI_BANDS[i] };
}

/**
 * Pollen, in grains per cubic metre of air, with the bands most European
 * pollen services use. Each taxon has its own edges, because the dose that
 * sets off an allergy differs by an order of magnitude: forty grains of olive
 * is a quiet day, forty of ragweed a bad one. Below one grain there is none
 * in the air, and the ground is left uncoloured.
 */
export const POLLEN_BANDS = [
  { name: 'Low', colour: '#8fd694' },
  { name: 'Moderate', colour: '#f0e641' },
  { name: 'High', colour: '#ff8a3c' },
  { name: 'Very high', colour: '#c8253a' },
];

export const POLLEN = {
  grass_pollen: { label: 'Grass', long: 'Grasses (Poaceae)', edges: [1, 10, 50, 150], top: 200 },
  birch_pollen: { label: 'Birch', long: 'Birch (Betula)', edges: [1, 10, 100, 1000], top: 1200 },
  alder_pollen: { label: 'Alder', long: 'Alder (Alnus)', edges: [1, 10, 100, 1000], top: 1200 },
  olive_pollen: { label: 'Olive', long: 'Olive (Olea)', edges: [1, 50, 200, 400], top: 500 },
  mugwort_pollen: { label: 'Mugwort', long: 'Mugwort (Artemisia)', edges: [1, 10, 50, 100], top: 120 },
  ragweed_pollen: { label: 'Ragweed', long: 'Ragweed (Ambrosia)', edges: [1, 5, 20, 50], top: 80 },
};

/** Which pollen band a count falls in, or null below one grain. */
export function pollenBand(taxon, value) {
  const { edges } = POLLEN[taxon];
  if (!(value >= edges[0])) return null;
  let i = 0;
  while (i < edges.length - 1 && value >= edges[i + 1]) i++;
  return { index: i, ...POLLEN_BANDS[i] };
}

/** Pollen: the band colours pinned at the taxon's edges. */
export function pollenStops(taxon) {
  const { edges, top } = POLLEN[taxon];
  return POLLEN_BANDS.map((band, i) => [Math.min(edges[i] / top, 1), band.colour]);
}

/**
 * Noise, Lden in dB, in the 5 dB bands the END noise maps are drawn in, on
 * a fixed scale: a decibel is a decibel anywhere. The WHO's guideline for
 * road traffic is 53 dB Lden.
 */
export const NOISE_BANDS = [
  { name: 'Quiet', colour: '#5aa469', from: 0 },
  { name: 'Moderate', colour: '#c8d65a', from: 45 },
  { name: 'Noisy', colour: '#f2b13a', from: 55 },
  { name: 'Loud', colour: '#e5603b', from: 60 },
  { name: 'Very loud', colour: '#b01f45', from: 65 },
  { name: 'Extreme', colour: '#5b1a6e', from: 70 },
];
export const WHO_ROAD_LDEN = 53;

export function noiseBand(db) {
  let i = 0;
  while (i < NOISE_BANDS.length - 1 && db >= NOISE_BANDS[i + 1].from) i++;
  return { index: i, ...NOISE_BANDS[i] };
}

/** Noise: the band colours pinned at their edges on a fixed [lo, hi] dB scale. */
export const noiseStops = ([lo, hi]) => NOISE_BANDS.map(b => [Math.min(Math.max((b.from - lo) / (hi - lo), 0), 1), b.colour]);

/**
 * The night sky, zenith brightness in mag/arcsec² (what a Sky Quality Meter
 * reads; higher is darker), in the Bortle classes by their usual SQM edges,
 * on a fixed scale: a dark sky is dark anywhere. Each class says what is
 * left of the Milky Way.
 */
export const SKY_BANDS = [
  { name: 'City', bortle: '8–9', milkyWay: 'hidden; only the brightest stars', colour: '#fbe7ef', from: 0 },
  { name: 'Urban', bortle: '7', milkyWay: 'hidden', colour: '#ef7b5f', from: 18.38 },
  { name: 'Bright suburb', bortle: '6', milkyWay: 'hidden, or a trace overhead', colour: '#f2b13a', from: 18.94 },
  { name: 'Suburb', bortle: '5', milkyWay: 'faint, washed out towards the horizon', colour: '#c8d65a', from: 19.5 },
  { name: 'Rural edge', bortle: '4', milkyWay: 'visible, without detail near the horizon', colour: '#45c4b0', from: 20.49 },
  { name: 'Rural', bortle: '4', milkyWay: 'clear, with some structure', colour: '#3f7fc4', from: 21.2 },
  { name: 'Dark', bortle: '3', milkyWay: 'bright and structured', colour: '#2c3f8f', from: 21.69 },
  { name: 'Pristine', bortle: '1–2', milkyWay: 'casts shadows; the zodiacal light shows', colour: '#151a45', from: 21.89 },
];
/** The natural sky, mag/arcsec²: what is left with no light anywhere. */
export const NATURAL_SKY = 22.0;

export function skyBand(mag) {
  let i = 0;
  while (i < SKY_BANDS.length - 1 && mag >= SKY_BANDS[i + 1].from) i++;
  return { index: i, ...SKY_BANDS[i] };
}

/** How many times brighter than natural a sky of this brightness is. */
export const skyRatio = mag => 10 ** ((NATURAL_SKY - mag) / 2.5);

/** The night sky: the class colours pinned at their edges on a fixed [lo, hi] mag scale. */
export const skyStops = ([lo, hi]) => SKY_BANDS.map(b => [Math.min(Math.max((b.from - lo) / (hi - lo), 0), 1), b.colour]);

/**
 * What was built after the year on the slider, as a share of each cell: a
 * ghost tint over today's city, from a trace to a whole block. Under
 * GROWTH_MIN the cell is left clear — what was there then is there now.
 */
export const GROWTH_MIN = 0.03;
export const GROWTH_STOPS = [[0, '#f6d8a8'], [0.35, '#f0a35e'], [0.7, '#e2673f'], [1, '#a8283a']];

const hexToRgb = hex => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));

/**
 * A 256-entry lookup table from a list of colour stops at positions in [0,1].
 * Painting 16k pixels through a table beats parsing a hex string per pixel.
 */
export function rampLut(stops) {
  const lut = new Uint8ClampedArray(256 * 3);
  const rgb = stops.map(([, hex]) => hexToRgb(hex));
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let k = 0;
    while (k < stops.length - 2 && t > stops[k + 1][0]) k++;
    const [p0] = stops[k];
    const [p1] = stops[k + 1];
    const f = p1 === p0 ? 0 : clamp((t - p0) / (p1 - p0), 0, 1);
    for (let c = 0; c < 3; c++) lut[i * 3 + c] = rgb[k][c] + (rgb[k + 1][c] - rgb[k][c]) * f;
  }
  return lut;
}

/** Heat: evenly spaced stops across the domain. */
export const heatStops = () => HEAT_STOPS.map((hex, i) => [i / (HEAT_STOPS.length - 1), hex]);

/**
 * Air: the band colours pinned at the band edges, so that a smooth field still
 * turns "Fair" green exactly where the index says it does.
 */
export function airStops(metric) {
  const { edges, top } = AIR_METRICS[metric];
  const positions = [0, ...edges].map(e => e / top);
  return EAQI_BANDS.map((band, i) => [Math.min(positions[i], 1), band.colour]);
}

/** CSS for a legend bar drawn from the same stops the drape uses. */
export const cssGradient = stops =>
  `linear-gradient(90deg, ${stops.map(([p, hex]) => `${hex} ${(p * 100).toFixed(1)}%`).join(', ')})`;
