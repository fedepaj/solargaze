"""
Run the products for many tiles, one after another, and survive the night.

    pipeline/.venv/bin/python pipeline/run_region.py italy
    pipeline/.venv/bin/python pipeline/run_region.py 41.75,12.25 45.0,7.5 45.25,9.0
    pipeline/.venv/bin/python pipeline/run_region.py --bbox 6.5,36.5,18.6,47.2 --products heat,wind

Tiles are taken north to south, west to east. Each product of each tile is
skipped when meta.json already records it, so the script can be stopped and
restarted and it carries on where it was; a failure is logged and the loop
moves to the next tile rather than dying. Progress goes to
``pipeline/cache/region.log`` and to stdout.

On sizing, because it decides what is realistic in a night:

- ``heat`` (Landsat) is about five minutes a tile, all of it reading
  windows of scenes from Planetary Computer; the limit is patience.
- ``wind`` (OSM mask) is seconds a tile once Overpass answers, and Overpass
  is the fragile one: the script pauses between tiles and backs off on 429.
- ``air`` (CAMS via Open-Meteo) is 36 nodes × 5 years per tile, and the
  free tier weights long requests, so a handful of tiles a day is the
  honest ceiling. For a whole country the CAMS data should come in bulk
  from the Copernicus Atmosphere Data Store instead — a different script.

``--only-built`` keeps tiles with fewer than a threshold of OSM buildings
(checked first, cheaply) out of the heat run, which is what makes a
country-sized bbox tractable: most quarter-degree squares are fields or
sea, and nobody is looking at a house there.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "wind"))

from tiles import STEP, Tile, read_meta  # noqa: E402

log = logging.getLogger("region")

REGIONS = {
    # Mainland Italy plus the islands, generously.
    "italy": (6.5, 36.5, 18.6, 47.2),
    "lazio": (11.4, 40.8, 14.0, 42.9),
    "piemonte-lombardia": (6.6, 44.0, 11.5, 46.7),
}


def tiles_in_bbox(west, south, east, north):
    lat = (north // STEP) * STEP
    while lat >= (south // STEP) * STEP:
        lon = (west // STEP) * STEP
        while lon <= (east // STEP) * STEP:
            yield Tile(round(lat, 2), round(lon, 2))
            lon += STEP
        lat -= STEP


def has_product(tile: Tile, product: str) -> bool:
    return product in read_meta(tile).get("products", {})


def run_product(tile: Tile, product: str) -> None:
    if product == "heat":
        import heat_landsat
        heat_landsat.run(tile)
    elif product == "air":
        import air_cams
        air_cams.run(tile)
    elif product == "wind":
        import run as wind_run
        wind_run.main(tile.id)
    else:
        raise ValueError(product)


def building_count(tile: Tile) -> int:
    """Cheap first look through the wind product's fetch, cached by it."""
    import buildings
    return len(buildings.fetch_buildings(tile))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", nargs="*", help="a region name, or lat,lon of places whose tiles to run")
    ap.add_argument("--bbox", help="west,south,east,north")
    ap.add_argument("--products", default="wind,heat", help="comma list of heat,air,wind (in this order)")
    ap.add_argument("--only-built", type=int, default=2000,
                    help="skip heat/air for tiles with fewer OSM buildings than this (0 = never skip)")
    ap.add_argument("--pause", type=float, default=15.0, help="seconds between tiles, for Overpass's sake")
    args = ap.parse_args()

    if args.bbox:
        tiles = list(tiles_in_bbox(*map(float, args.bbox.split(","))))
    elif len(args.what) == 1 and args.what[0] in REGIONS:
        tiles = list(tiles_in_bbox(*REGIONS[args.what[0]]))
    else:
        tiles = [Tile.containing(*map(float, w.split(","))) for w in args.what]
    products = [p for p in ("wind", "heat", "air") if p in args.products.split(",")]

    log.info("%d tiles, products %s", len(tiles), products)
    t0 = time.time()
    done = 0
    for i, tile in enumerate(tiles):
        todo = [p for p in products if not has_product(tile, p)]
        if not todo:
            continue
        log.info("[%d/%d] %s: %s", i + 1, len(tiles), tile.id, todo)
        try:
            if args.only_built and any(p != "wind" for p in todo):
                n = building_count(tile)
                if n < args.only_built:
                    log.info("  %d buildings: keeping only the mask", n)
                    todo = [p for p in todo if p == "wind"]
            for product in todo:
                run_product(tile, product)
            done += 1
        except KeyboardInterrupt:
            raise
        except Exception as e:  # keep going: a bad tile is a line in the log
            log.error("  %s failed: %s", tile.id, e)
        time.sleep(args.pause)
    log.info("finished: %d tiles touched in %.0f min", done, (time.time() - t0) / 60)


if __name__ == "__main__":
    Path(__file__).resolve().parent.joinpath("cache").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(Path(__file__).resolve().parent / "cache" / "region.log")],
    )
    main()
