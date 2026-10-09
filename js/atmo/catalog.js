/**
 * What the pipeline says it builds: catalog.json, beside index.json at the
 * root of the tiles (pipeline/sg/catalog.py writes it from each product's
 * card). One entry per product — the theme and variant it is shown under,
 * the kind of data, which decides how it is read and drawn, and the words
 * and credits that go with it.
 *
 * The themes (layers.js) take their variants from here, so a product of a
 * kind this app already reads appears as a new variant without a change
 * here. Until the real catalog arrives — and if it never does — the copy
 * below stands in: the products as they were when this was written.
 *
 * Pure: no fetch, no DOM. tiles.js fetches; layers.js and sources.js read.
 */

/** The kinds of data this app knows how to read (tiles.js) and draw (layers.js). */
export const KNOWN_KINDS = new Set(['raster-months', 'raster-static', 'sky-brightness', 'built-epochs', 'grid-past', 'street-air', 'climatology', 'building-mask']);

export const FALLBACK = {
  version: 1,
  products: {
    wind: {
      kind: 'building-mask', title: 'Buildings for the street-level wind', resolution_m: 10,
      source: 'OpenStreetMap buildings via Geofabrik extracts', licence: 'ODbL',
      note: 'Where a tile has been built, the wind is threaded between the OpenStreetMap buildings by a potential-flow model solved in the browser: channelling, shelter and corner gusts, but no wakes — a picture, not a measurement.',
    },
    noise: {
      theme: 'noise', variant: 'roads', order: 10, kind: 'raster-static',
      label: 'Roads', title: 'Road traffic noise, Lden', unit: 'dB', scale_c: [40, 80], resolution_m: 10,
      source: 'OpenStreetMap roads and buildings; calibrated on EEA END strategic noise maps (Berlin, checked on Hamburg)',
      licence: 'ODbL',
      note: "Road traffic noise as a day–evening–night level (Lden), modelled from the class of every road and the buildings that screen it, calibrated on the official END noise maps of Berlin and checked on Hamburg's: within one 5 dB band of the official map on nine cells in ten. No traffic counts, speeds, barriers, railways or aircraft; where a city has an official noise map, that is the better source.",
    },
    light: {
      theme: 'light',
      variant: 'sky',
      order: 10,
      kind: 'sky-brightness',
      label: 'Night sky',
      title: 'Night-sky brightness',
      unit: 'mag/arcsec²',
      scale_c: [16.5, 22.0],
      resolution_m: 460,
      source: 'NASA Black Marble VNP46A4 (VIIRS night lights, yearly) from 2012; DMSP-OLS harmonised by Li et al. 2020 for 1992–2011; glow kernel fitted on Falchi et al. 2016',
      licence: 'public domain (NASA); CC BY 4.0 (harmonised DMSP)',
      coarse_before: 2012,
      note: 'How bright the sky overhead is on a clear, moonless night, as a Sky Quality Meter reads it: 22 is a pristine sky, 17 a city centre, year by year since 1992. Each year\'s VIIRS night lights spread by a kernel of distance fitted on the World Atlas of Artificial Night Sky Brightness, within a factor of 1.5 of it on 94 cells in 100 where it was never fitted. VIIRS is blind to blue light, so white LEDs are undercounted; altitude and terrain are not modelled. Before 2012 the older DMSP satellites stand in: coarser, saturated in city centres, within a factor of 1.5 of the VIIRS sky on about 8 cells in 10.',
    },
    built: {
      theme: 'growth', variant: 'built', order: 10, kind: 'built-epochs',
      label: 'Built', title: 'Built since the year on the slider', unit: 'share of the ground', scale_c: [0, 0.6], resolution_m: 90,
      source: 'GHS-BUILT-S R2023A, European Commission Joint Research Centre',
      licence: 'CC BY 4.0 (© European Union)',
      note: "What was built after the year on the slider, over the city of today: the share of each 90 m cell that buildings cover, epoch by epoch from 1975 to 2020, from the JRC's Global Human Settlement Layer (Landsat and Sentinel-2). Comparable between epochs, but a single cell is an estimate, and the 1970s and 1980s the least certain. Nothing after 2020.",
    },
    heat: {
      theme: 'heat', variant: 'morning', order: 10, kind: 'raster-months',
      label: 'Morning', title: 'Surface heat on clear mornings', when: 'mornings', daypart: 'day', span_c: 6, resolution_m: 90,
      source: 'Landsat 8/9 Collection 2 Level-2 surface temperature (USGS), via Microsoft Planetary Computer',
      licence: 'public domain',
      note: 'A per-pixel median of clear mid-morning Landsat passes (about 10:30 local) since 2020, by month: what the roofs and streets typically read, not the air. From afar it is shown coarser.',
    },
    heat_night: {
      theme: 'heat', variant: 'night', order: 20, kind: 'raster-months',
      label: 'Night', title: 'Surface heat on clear nights', when: 'nights', daypart: 'night', span_c: 2.5, resolution_m: 70,
      source: 'ECOSTRESS ECO_L2T_LSTE v002 land surface temperature (NASA LP DAAC)',
      licence: 'public domain',
      note: 'A per-pixel median of clear-night ECOSTRESS passes (21:00–05:00 local) since 2018, by month: the heat the city gives back at night, when the gap between dense blocks and parks is widest. Only between about 52° south and north, the reach of the Space Station it flies on.',
    },
    heat_2004: {
      theme: 'heat',
      variant: 'morning-2004',
      order: 11,
      kind: 'raster-months',
      label: 'Mornings 2004–13',
      title: 'Surface heat on clear mornings, 2004–2013',
      when: 'mornings 2004–2013',
      daypart: 'day',
      years: [2004, 2013],
      span_c: 6,
      resolution_m: 120,
      coarse: true,
      source: 'Landsat 5 TM and Landsat 7 ETM+ Collection 2 Level-2 surface temperature (USGS), via Microsoft Planetary Computer',
      licence: 'public domain',
      note: 'The same morning surface heat for 2004–2013: a per-pixel median of the clearest Landsat 5 TM and Landsat 7 ETM+ passes of each month in the decade, read at 120 m. Earlier satellites passed a little earlier in the morning (about 9:30–10:00), so a decade-to-decade difference is partly the clock and the weather of those years, not only the city.',
    },
    heat_1994: {
      theme: 'heat',
      variant: 'morning-1994',
      order: 12,
      kind: 'raster-months',
      label: 'Mornings 1994–03',
      title: 'Surface heat on clear mornings, 1994–2003',
      when: 'mornings 1994–2003',
      daypart: 'day',
      years: [1994, 2003],
      span_c: 6,
      resolution_m: 120,
      coarse: true,
      source: 'Landsat 5 TM and Landsat 7 ETM+ Collection 2 Level-2 surface temperature (USGS), via Microsoft Planetary Computer',
      licence: 'public domain',
      note: 'The same morning surface heat for 1994–2003: a per-pixel median of the clearest Landsat 5 TM and Landsat 7 ETM+ passes of each month in the decade, read at 120 m. Earlier satellites passed a little earlier in the morning (about 9:30–10:00), so a decade-to-decade difference is partly the clock and the weather of those years, not only the city.',
    },
    heat_1984: {
      theme: 'heat',
      variant: 'morning-1984',
      order: 13,
      kind: 'raster-months',
      label: 'Mornings 1984–93',
      title: 'Surface heat on clear mornings, 1984–1993',
      when: 'mornings 1984–1993',
      daypart: 'day',
      years: [1984, 1993],
      span_c: 6,
      resolution_m: 120,
      coarse: true,
      source: 'Landsat 5 TM Collection 2 Level-2 surface temperature (USGS), via Microsoft Planetary Computer',
      licence: 'public domain',
      note: 'The same morning surface heat for 1984–1993: a per-pixel median of the clearest Landsat 5 TM passes of each month in the decade, read at 120 m. Earlier satellites passed a little earlier in the morning (about 9:30–10:00), so a decade-to-decade difference is partly the clock and the weather of those years, not only the city.',
    },
    air: {
      kind: 'climatology', title: 'CAMS air quality by month and hour', resolution_m: 10000,
      source: 'Copernicus Atmosphere Monitoring Service (CAMS) European reanalysis 2020–2024',
      licence: 'Copernicus licence',
      note: "The five-year habit of the air for each month and hour, on CAMS's ~10 km grid; the street layer is built on it.",
    },
    air_street: {
      theme: 'air', variant: 'street', order: 10, kind: 'street-air', base: 'air',
      label: 'Street', title: 'Air quality street by street', resolution_m: 50,
      source: 'CAMS (Copernicus) corrected by a land-use regression fitted to monitoring stations (EEA in Europe); OpenStreetMap, ESA WorldCover, Copernicus DEM',
      licence: 'Copernicus licence; EEA re-use policy; ODbL; CC-BY 4.0',
      note: 'The CAMS climatology at 50 m: NO₂ and PM10 corrected by a land-use regression fitted to the monitoring stations (roads, buildings, green, terrain), ozone from NO₂ by titration, PM2.5 left as CAMS — a statistical model of where the stations are, not a measurement where you are.',
    },
    air_past: {
      theme: 'air',
      variant: 'past',
      order: 20,
      kind: 'grid-past',
      label: 'Year',
      title: 'The air of earlier years',
      years: [2003, 2019],
      coarse: true,
      resolution_m: 80000,
      meta: 'air_past/meta.json',
      source: 'CAMS global reanalysis (EAC4) monthly means, Copernicus Atmosphere Monitoring Service',
      licence: 'Copernicus licence',
      note: 'Before 2020, the month of the year on the slider from the CAMS global reanalysis: a model on an 80 km grid, a region\'s background rather than a street — NO₂ especially reads far below what a road saw. There to show how a region\'s air changed since 2003, drawn in its own coarse blocks.',
    },
  },
};

let current = FALLBACK;

export const catalog = () => current;

/** A product's card, or null. */
export const cardOf = name => current.products[name] || null;

/** The source id the engine knows a tile product by: `air_street` → `tile-air-street`. */
export const tileSourceId = name => `tile-${name.replace(/_/g, '-')}`;

/**
 * The variants of a theme, best first: every product shown under it whose
 * kind this app can read. A kind it cannot read is a newer app's business
 * and is left out rather than drawn wrong.
 */
export function variantsOf(theme, cat = current) {
  return Object.entries(cat.products)
    .filter(([, c]) => c.theme === theme && KNOWN_KINDS.has(c.kind))
    .sort(([, a], [, b]) => (a.order ?? 99) - (b.order ?? 99))
    .map(([name, card]) => ({ name, card }));
}

/**
 * Take a catalog fetched from the tiles. A shape that is not one — an older
 * format, a half-written file — is ignored and the current one kept.
 * Returns true when it changed anything.
 */
export function setCatalog(cat) {
  if (!cat || cat.version !== 1 || typeof cat.products !== 'object') return false;
  const same = JSON.stringify(cat.products) === JSON.stringify(current.products);
  current = cat;
  return !same;
}
