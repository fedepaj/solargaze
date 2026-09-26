# pipeline — the offline products

SolarGaze has no server. What it cannot compute in the browser it reads from
`data/tiles/`, which these scripts fill in, one quarter-degree tile at a time,
on a laptop, whenever someone runs them. Outputs are committed; inputs are
fetched again on demand and cached under `pipeline/cache/` (ignored).

```bash
uv venv pipeline/.venv
uv pip install --python pipeline/.venv/bin/python numpy pillow requests \
    pystac-client planetary-computer rasterio pyproj scipy

cd pipeline
.venv/bin/python tiles.py 41.89 12.49          # which tile holds a place → N41.75E12.25
.venv/bin/python heat_landsat.py N41.75E12.25  # ~5 min: 12 monthly PNGs, 30 m
.venv/bin/python air_cams.py N41.75E12.25      # ~3 min: month × hour tables
.venv/bin/python wind/run.py N41.75E12.25      # see wind/README.md
```

## The tiles

`tiles.py` is the authority: quarter-degree squares keyed by their south-west
corner, `N41.75E12.25` for Rome. Each tile is a folder with a `meta.json`
listing its products, and `data/tiles/index.json` lists every tile with
anything in it. The app (`js/atmo/tiles.js`) mirrors the scheme: one integer
division from the pin gives the tile id, one fetch says what is there.

Going north to south through Italy is a matter of running the scripts with
the next id. Nothing is per-city; a tile over farmland works as well as one
over Rome, it is just less interesting.

## Products

| product | script | what it is | size per tile |
| --- | --- | --- | --- |
| `heat` | `heat_landsat.py` | Landsat 8/9 surface temperature, per-pixel median of clear scenes per calendar month, read at 30 m and written at 90 m, 2018 on | ~0.5 MB (12 PNGs) |
| `air` | `air_cams.py` or `air_cams_bulk.py` | CAMS air quality (PM2.5, PM10, NO₂, O₃) as means by month × hour, local time, 2020–2024, 0.1° nodes | ~200 KB |

`air_cams_bulk.py` produces the same product for a whole region from the
Copernicus Atmosphere Data Store (needs `~/.cdsapirc` with your key, never
in the repo) and is the one to use beyond a handful of tiles.
| `wind` | `wind/` | the OpenStreetMap building mask the browser solves the street-level flow on | ~0.4 MB |

Many tiles at once, resumable, with a pause between them for Overpass's sake:

```bash
.venv/bin/python run_region.py lazio                      # a named bbox
.venv/bin/python run_region.py 41.89,12.49 45.07,7.69      # the tiles holding these places
.venv/bin/python run_region.py --bbox 6.5,36.5,18.6,47.2 --products wind,heat
```

Tiles with fewer than 2000 OSM buildings get the mask only, which is what
keeps a country-sized box tractable: Italy is 2107 quarter-degree squares
and most are fields or sea. Even so, Landsat is ~5 minutes a built tile and
CAMS through Open-Meteo a handful of tiles a day, so a night covers a region,
not a country; see the sizing notes in `run_region.py`.

Every product writes its provenance, encoding and caveats into `meta.json`;
the app prints the caveats under the numbers.

## Honesty notes

- Surface temperature is written at 90 m although the scenes are read at
  30 m: the thermal band is acquired at 100 m and only resampled by the
  USGS, and against a 90 m mean just 2 % of pixels differ by more than half
  a degree (`reencode_heat.py` converts older 30 m tiles). Vectorising was
  measured and rejected — 1 °C isotherms of one month weigh 1.9 MB gzipped,
  a greedy TIN at 1 °C tolerance 309 KB — against 40 KB for the raster.
- Surface temperature is what the roofs and streets radiate at ~10:30 on a
  clear day. It is not air temperature and there is no hourly cycle in it;
  the app colours it as an anomaly against the tile's median for the month,
  because "hotter than the rest of the city" is the question it answers.
- CAMS is an ~11 km model. The month × hour tables say how the regional
  background behaves; a street-level correction from monitoring stations is
  the natural next product and would add to these tables, not replace them.
- Open-Meteo's free tier weights long requests. Five years for 36 nodes is
  fine; do not loop over all of Italy in one afternoon.
