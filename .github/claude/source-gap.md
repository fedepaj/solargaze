You are filling a gap in the SolarGaze tile pipeline: issue #GAP. Read the issue, then
pipeline/ARCHITECTURE.md and pipeline/importers/__init__.py (the importer contract and the
common schema), and pipeline/importers/eea.py as the example to follow.

This is one unattended session with no later turn: when you stop, the job ends and
whatever is not pushed is lost. So:
- never run a command in the background, and never end your turn to wait for one —
  everything runs in the foreground and finishes before you go on;
- you have about 45 minutes in all: run the importer on a sample (one state or region,
  a few hundred stations, one year) rather than the whole country, and say in the pull
  request how large the sample was — the pipeline runs the full import later;
- if time or turns run short, push what you have as a draft pull request
  (`gh pr create --draft`) saying what is done and what is left, and comment on the
  issue with its link. A draft with honest notes is worth more than lost work.

0. OpenAQ (api.openaq.org/v3, header X-API-Key from $OPENAQ_API_KEY) aggregates many
   national networks and records, per provider, whether redistribution is allowed:
   check it first, and say what it has for this country and under which licence.

1. Find where the data actually are. Search the web for the national or regional
   monitoring network (environment ministry, national institute, open-data portal) or an
   aggregator that has it; read its documentation and terms. Prefer a source with an
   API or stable bulk files, hourly data, station coordinates and station types, and a
   licence that allows publishing derived data (open licences, government open data).
   A source with no clear licence, behind a login we cannot automate, or forbidding
   redistribution of derived products is not acceptable: say so and look further.

2. If you found a usable source, write `pipeline/importers/<name>.py`: an Importer that
   returns the common schema for NO₂, PM10, PM2.5 and O₃ over the years asked, folding
   hourly values to month × local hour like air_stations.py does (local time of the
   country), converting units to µg/m³ (ppb → µg/m³ at 20 °C: NO₂ × 1.88, O₃ × 1.96),
   mapping station types and areas to the schema's values, and stating `source` and
   `licence`. Register it in REGISTRY. Keep network access inside the importer.
   Add `pipeline/tests/fixtures/<name>/` with a few kilobytes of real data fetched with
   curl (a station or two, a few days), and a test in `pipeline/tests/` that runs the
   importer's parsing on the fixture without the network and passes `importers.validate`.
   Run `cd pipeline && python -m unittest discover -s tests`; everything must pass.

3. Check the numbers. Where another source already covers some of the same stations or
   area (OpenAQ, the EEA near a border), compare annual means on a handful of stations
   and report the differences. If you could only try a sample, say how large.

4. Open a pull request titled "Importer: <network> for <country> (#GAP)" whose body
   starts with "Fixes #GAP" and gives, in this order: the source and its licence (with
   links), what the importer covers (pollutants, years, how many stations), the
   comparison, and anything a reviewer should doubt. Then comment on issue #GAP with a
   three-line summary and the link.

If you found no usable source, do not open a pull request: comment on issue #GAP with
what you searched, what you found and why each candidate was not usable, and add the
label gap-blocked (`gh issue edit #GAP --add-label gap-blocked`). Never commit
credentials or large data, never touch data/tiles, never change existing validations.
