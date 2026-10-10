# SolarGaze

A CesiumJS web app on Google Photorealistic 3D Tiles (GitHub Pages, `fedepaj/solargaze`) and an offline
Python pipeline that builds data tiles into Cloudflare R2. The app has **no build step**: plain ES modules,
deployed as they are. The owner writes in Italian, wants everything free and automatic, and likes concrete
numbers. Code, comments and commit messages are in English, in the voice of the surrounding code.

## Layout

| Path | What |
|---|---|
| `index.html`, `css/`, `js/` | The app. `js/atmo.js` is the data engine; `js/atmo/` its parts; `js/ui/` the panels. |
| `test/*.test.mjs` | App tests (pure modules only: layers, scales, field samplers, mosaic pixels, solar). |
| `pipeline/sg/` | The pipeline framework: products, runner, state, border pieces, catalog, gaps. |
| `pipeline/*.py` | One module per data source (`heat_landsat.py`, `noise_osm.py`, `light_viirs.py`, …). |
| `pipeline/tests/` | Pipeline tests (`unittest`, no network). |
| `pipeline/ARCHITECTURE.md` | The pipeline's principles. Read before changing the runner. |
| `.github/workflows/` | `pipeline.yml` (nightly build), `pages.yml` (deploy), `claude.yml` (agent). |
| `.github/claude/` | Briefs for the agents that triage runs and work on source gaps. |
| `tools/record/` | Playwright recorder for the README clips and GIFs. |
| `data/tiles/` | Local pipeline output (gitignored). The app reads it on `localhost`. |

## Running and testing

```sh
npm run dev                                   # serve the app on http://localhost:5173
npm test                                      # app tests
cd pipeline && .venv/bin/python -m unittest discover -s tests   # pipeline tests
cd pipeline && .venv/bin/python -m sg doctor  # is this machine ready to build?
cd pipeline && .venv/bin/python -m sg run --bbox 11.5,41.25,13.25,42.5 --products light --no-publish --no-pull
```

- The venv is managed with `uv`: `uv pip install --python pipeline/.venv/bin/python <pkg>`, then pin it in
  `pipeline/requirements.txt`.
- `pipeline/cache` is a symlink to an external SSD (`/Volumes/PIE/solargaze/cache`). If it is unplugged,
  local builds fail; nothing is lost.
- After changing code run `graphify update .` (see the last section).

## The app in one page

- **Catalog** (`js/atmo/catalog.js`): `catalog.json` on R2, written by the pipeline from each product's
  `card`. A card names a `theme`, a `variant` and a `kind`. The built-in `FALLBACK` stands in until the real
  one arrives; a pipeline test fails if `FALLBACK` does not list every product.
- **Kinds** (`KINDS` in `js/atmo/layers.js`): what a kind of data draws and says — `raster-months` (heat),
  `raster-static` (noise), `sky-brightness`, `built-epochs` (growth), `street-air`, `grid-past` (old air).
  A new product of an existing kind needs no app change. A new kind needs: an entry in `KINDS`, a loader
  case in `js/atmo/tiles.js`, `KNOWN_KINDS`, and `APP_KINDS` in `pipeline/tests/test_catalog.py`.
- **Layers** (`LAYERS`): one theme, one card in the right panel, one switch. Layers in the `ground` slot
  are rivals: one on, the others off. Every card reads its number at the pin whether drawn or not.
- **The date is the one selector.** Sun up or down picks morning or night heat (`byDaypart`); the year
  slider (from 1975) picks a variant whose card has `years`, and the year of a stacked raster.
- **Memory rules, learned the hard way:** a layer that is off fetches only the tile under the pin
  (`wantFor`); decoded tiles live in an LRU with a byte budget (`remember` in `tiles.js`); images are
  decoded in bands of rows (`channels`); the wind's building mask is cut around the pin (`maskAround`).
  Do not reintroduce whole-view fetches for readings or whole-tile `getImageData`.
- **One picture per set of tiles**: `js/atmo/mosaic.js` (pure, tested) writes the pixels, the engine makes
  the canvas. A mosaic is repainted only when its signature moves (`paint` in `drapeRenderer`); `show()`
  keeps the signature, so a source landing or another switch does not repaint what is already up. It is
  painted a tile at a time with a turn for the page in between (`mosaicCanvas`): do not make it one loop
  again, a view of 10 m noise held the page for 360 ms.
- **Could not ask is not "does not exist"**: a meta or an index that cannot be reached throws and is not
  remembered (`fetchJson` in `tiles.js`); a set with tiles that failed is `incomplete`; the engine asks
  again after 4, 15 and 60 s and when the connection comes back (`fetchNeeded`). Only a 404 is "not
  computed here yet".
- **Coarse data is drawn coarse**: `card.resolution_m >= 300` is drawn eight pixels a cell with a seam
  between cells (while a tile stays under 512 px); `card.coarse` or a year before `card.coarse_before`
  is read in 2 × 2 blocks.

## The pipeline in one page

`python -m sg run` plans jobs (tile × product), builds each into a staging folder, validates, installs
with one rename, records the outcome in the state, and publishes. State is a base plus deltas on the
**ops bucket** (`solargaze-ops`, private); tiles, `index.json` and `catalog.json` go to the **tiles
bucket** (public).

| Product | Source | Notes |
|---|---|---|
| `wind` | OSM buildings (Geofabrik) | Runs first; its building count gates the expensive products. |
| `noise` | OSM roads + buildings | Line-source model, calibrated on Berlin's END map. |
| `light` | NASA Black Marble (VIIRS) 2012+, harmonised DMSP 1992–2011 | Glow kernel fitted on the Falchi atlas. |
| `built` | GHSL GHS-BUILT-S, epochs 1975–2020 | Stored as deltas between epochs. |
| `heat`, `heat_night` | Landsat 8/9, ECOSTRESS | Twelve months per tile. |
| `heat_1984/1994/2004` | Landsat 5 and 7 | Last in `ORDER`: built with what time is left. |
| `air`, `air_street` | CAMS + stations | Built locally, not nightly. |
| `air_past` | CAMS EAC4 2003–2019 | **Not a tile product and not built by the runner**: `pipeline/air_eac4.py --publish` on the owner's Mac (needs `~/.cdsapirc`). |

- **A new product**: a module with `build(tile, out_dir, …) -> meta`, a class in `pipeline/sg/products/`
  with a `card`, `validate`, and where needed `prepare` (region-wide downloads), `unavailable`
  (a missing credential) and `reason` (when a done tile is due again). Register it in
  `products/__init__.py` (`PRODUCTS`, `ORDER`) and in the workflow's default `products`.
- **Border tiles** need buildings and roads from the neighbouring region. Each region leaves *pieces* on
  the ops bucket (`sg/border.py`); a tile waits (`NotCovered`, never given up on) until all are there.
  Pieces carry a `FORMAT`: bump it when what a piece holds changes, or old pieces poison new code.
- **Shared inputs** that are expensive to fetch are kept reduced on the ops bucket (`light/r30/`,
  `light/dmsp/`), so one runner's download serves the rest.
- **The breaker**: eight first-attempt failures in a row with the same signature stop the job
  (`Systemic`). A per-tile cause must raise an error with `per_tile` set, or it takes the region down.
- **The nightly workflow**: `plan` (matrix of the regions with most work, six at a time) → `build` (one
  job per region, 4.5 h budget) → `report` → `gaps` → `triage` (an agent writes the digest, in Italian,
  on issue #1 "Pipeline runs", and opens an issue when a run needs a look).

## Debugging

**The app**
- State of a user's session, from their console:
  `copy(JSON.stringify({prefs: solargaze.state.prefs, y: solargaze.state.y, status: solargaze.atmo.status, error: solargaze.atmo.error}))`
- Local code against **production tiles**: open `http://[::1]:5173` instead of `localhost` (only
  `localhost`/`127.0.0.1` read `./data/tiles`).
- Headless reproduction: `tools/record/lib.mjs` (`open`, `settle`) with `SG_URL` for the address and
  `CHROME` for another browser binary. `playwright-core` is not a dependency of the repo: symlink a
  `node_modules` that has it into `tools/record/` and remove the link afterwards. Never print the page
  URL from a script: it carries the Cesium ion token.
- "It broke right after a deploy": Pages caches each module for ten minutes and module paths are not
  versioned, so a reload can mix old and new files. A forced reload settles it.
- "Not computed here yet" means the tile lacks that product: check `index.json` on R2, then the run.
- Memory: `tileMemory()` in `tiles.js` says what the decoded products hold; `usedJSHeapSize` does not
  count typed arrays, so measure the renderer process.

**A pipeline run**
```sh
gh run list --workflow pipeline.yml -L 5
gh run view <run> --json jobs --jq '.jobs[] | .name + ": " + (.conclusion // .status)'
gh run view <run> --job <job> --log | grep -E "systemic|counts|run.end|##\[error\]"
gh issue view 1 --json comments --jq '.comments[-1].body'      # the latest digest
```
- `NotCovered … needs a neighbouring region's extract` on `wind` is a border tile waiting: normal.
- `systemic: the last 8 tiles all failed with …` is the real cause; fix that signature first.
- What production holds: `index.json` and `catalog.json` at the tiles bucket's public URL
  (`TILES_REMOTE` in `js/config.js`).
- The home network drops DNS for minutes: Python has `pipeline/netdns.py`
  (`socket.getaddrinfo = netdns.getaddrinfo`); `curl` and `gh` simply fail and are retried.

## Rules

- Secrets live in `pipeline/.env` (gitignored) and in GitHub secrets. Never print their values, never
  commit them, never paste them in an issue.
- Agents never get the R2 keys, never push to `main` (pull requests only), never weaken a validation,
  never touch `data/tiles` or credentials.
- Reference data with a restrictive licence is used for fitting only and never published: the Falchi
  sky-brightness atlas (no redistribution, no commercial use) and the EEA END noise maps.
- Every source shown in the app is credited in the guide's DATA SOURCES (`js/ui/modals.js`).
- A validation number in a card or a note must be one that was measured; say where it was not fitted.

## Open work

- Noise in Italy: the model reads louder than Messina's own END map (77 % within one band, against 93 %
  in Hamburg). More Italian maps are needed before recalibrating; the owner is looking for them.
- Backfill: `light`, `built` and the past decades of heat reach six regions a night.
- Versioned module paths (an import map stamped in `pages.yml`), to end mixed deploys.
- The Azores heat one-off (`pipeline/oneoff_azores.py`) waits on USGS M2M access.
- Later: railways and aircraft in the noise model; the Americas and Japan.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
