# The pipeline, built to run unattended

The pipeline will run for months on machines nobody watches — GitHub Actions
runners that are killed after six hours, a laptop on a network that loses its
DNS — over thousands of tiles and dozens of national data sources, each with
its own way of failing. This file is the contract every part of it keeps.

## Principles

1. **Nothing half-done is ever visible.** A product is built in a staging
   directory, validated, then moved into place in one rename, and only then
   recorded and published. A crash at any point leaves the previous good
   version, or nothing — never an empty month marked as done.
2. **Every run can be killed and re-run.** Work is keyed by (tile, product,
   version); what is done is skipped, what was interrupted is redone. Inputs
   are cached on disk, so a re-run pays only for what it had not finished.
3. **One failure is one line, not a dead run.** A tile that fails is recorded
   with its error and the run moves on. The run itself fails only if
   something systemic breaks (no disk, no credentials, every tile failing).
4. **Every failure is classified.** See *Errors*: the class decides whether
   to retry now, retry next run, or wait for a code change.
5. **Every run explains itself.** Structured logs, a summary, and a digest of
   failures grouped by cause — readable by a human in a minute, by a program
   without parsing prose. See *Logging*.
6. **The laptop and the runner run the same code.** One command line,
   `python -m sg`, used locally and by the workflow; no CI-only logic.

## Pieces

```
pipeline/sg/
  log.py        structured events, run context, timing, the run summary
  errors.py     the error taxonomy and the classifier
  state.py      what is done, failed or empty, per tile and product (local + R2)
  product.py    the Product interface: plan → build in staging → validate → install
  runner.py     planning, retries, isolation, time and disk budgets, shutdown
  importers/    one module per national / network data source, one schema out
  products/     heat, air, air_street, wind, … each a Product
  __main__.py   the CLI: plan, run, status, publish, doctor
```

The existing scripts (`heat_landsat.py`, `air_lur.py`, …) keep their science;
the products wrap them, so the maths has one home.

## Products

A product declares:

- `name`, `version` — bump the version when the output would change; every
  tile built with an older version becomes stale.
- `needs(tile, state)` — missing, stale (version), or its inputs changed.
- `build(tile, stage)` — write files into `stage/`, return the meta entry.
- `validate(stage, meta)` — reject what must never be published: a heat tile
  with no month, a ratio raster all one value, a mask with no building where
  OSM has thousands. Validation failures are bugs, not bad luck.

The runner installs a validated product atomically and records it.

## Errors

| class | examples | what happens |
|---|---|---|
| `Transient` | DNS, timeouts, 5xx, 429, a dropped connection, Overpass busy | retried in-run with backoff; if still failing, recorded and retried next run |
| `NoData` | open sea, no clear Landsat scene, no station in a country | recorded as *empty* with the reason; not retried until the product version or its inputs change |
| `Upstream` | a source changed its format, a 4xx that is not ours | recorded as *failed*; retried next run, flagged in the digest |
| `Bug` | any other exception, a validation failure | recorded as *failed* with its traceback signature; not retried until the code changes (the version of the product, or the git commit) |

`errors.classify(exc)` maps exceptions to classes; importers raise the
specific ones themselves where they know better.

## State

One record per (tile, product): status (`done` · `empty` · `failed`),
version, when, attempts, the error class and signature, the duration, and
the input stamps it was built from. It lives in `cache/state/` locally and
is mirrored to the R2 bucket under `state/`, so a fresh runner knows what the
last one did. A run takes a short lease on what it works on, so two runners
do not build the same tile.

## Logging

- **Events**: one JSON object per line — time, run id, level, event name,
  tile, product, stage, duration, and fields — to `cache/logs/<run>.jsonl`,
  and a readable line on the console.
- **Run summary**: counts by outcome and product, time spent per stage,
  slowest tiles, bytes published; written as `summary.json` and, on GitHub
  Actions, as the job summary.
- **Failure digest**: failures grouped by signature (exception type and the
  message with numbers and ids stripped), with the tiles each group hit — so
  "Planetary Computer 403 on 212 tiles" is one line, not 212.
- Logs and summaries go to R2 under `logs/`, where they outlive the runner.

## Importers

Each national or network source is a module with one job: return stations
in the common schema (`station`, `lat`, `lon`, `type`, `area`, `var`,
`annual_mean`, `by_month`, `by_month_hour`, `capture`, `resolution`,
`source`). EEA (Europe), OpenAQ (the Americas and elsewhere), NIES/AEROS
(Japan) are the first three. A registry maps countries to importers; adding a
country is adding a module and a line, and its failures stay its own.

## Where it runs

GitHub Actions, on a schedule: a planner job reads the state from R2 and
writes the work list; a matrix of workers each takes a shard, processes it
within its time budget, publishes, and writes state and logs back; a final
job merges the summaries and opens or updates an issue when something
systemic broke. The same commands run on a laptop.

## Border tiles

A worker holds one Geofabrik region's extract, so a tile across a border has
only part of its buildings and roads there and fails as `NotCovered`
(per-tile: it never stops the run, and it is retried every run, never given
up on). After indexing, each region leaves its *piece* of every border tile
on the private bucket (`border/`, refreshed monthly) and picks up its
neighbours' pieces; a tile whose land they cover together is merged and
built like any other. Regions outside the catalogue whose land reaches into
it — Ukraine, Belarus, Turkey, Russia's north-west, Kaliningrad, Kosovo,
Morocco and a few microstates — run with `--pieces-only`: they leave pieces
and build nothing. With them every border tile of Europe can be closed
(`sg/border.py`, `regions.NEIGHBOURS`).
