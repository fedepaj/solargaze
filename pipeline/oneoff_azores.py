"""
One-off: the Azores' morning surface heat from Landsat Level-1 (heat_landsat_l1.py),
run on a laptop, published like any pipeline build.

    pipeline/.venv/bin/python pipeline/oneoff_azores.py [--wait] [--no-publish]

With --wait it checks every 30 minutes whether the USGS account has M2M
download access yet, and starts when it has. It first measures, on Madeira,
how far its Level-1 surface temperature is from the USGS's own and writes
that into the log; then downloads band 10 of every scene over the tiles
(once, into cache/landsat_l1/), and hands the tiles to the pipeline's
runner as the heat product: staged, validated, installed, recorded as done
and published, so the nightly runs leave them be.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "wind")]
import netdns  # noqa: E402,F401
import publish_tiles  # noqa: E402

import heat_landsat_l1 as l1  # noqa: E402
from sg.log import start_run  # noqa: E402
from sg.product import Context  # noqa: E402
from sg.products.heat import Heat  # noqa: E402
from sg.remote import Remote  # noqa: E402
from sg.runner import Budget, Job, execute  # noqa: E402
from sg.state import State  # noqa: E402
from tiles import DATA_DIR, Tile, refresh_index  # noqa: E402

CACHE = HERE / "cache"
# Every Azores tile with the buildings for heat whose Landsat scenes are
# reflectance only (the 4–7 October runs): São Miguel, Terceira, Faial and
# Pico, São Jorge, Flores.
TILES = ["N37.50E-25.50", "N37.50E-25.75", "N37.75E-25.25", "N37.75E-25.50", "N37.75E-25.75",
         "N38.25E-28.25", "N38.25E-28.50", "N38.50E-27.25", "N38.50E-27.50", "N38.50E-28.25",
         "N38.50E-28.50", "N38.50E-28.75", "N38.75E-27.25", "N38.75E-27.50", "N39.25E-31.25"]


class HeatL1(Heat):
    """The heat product, built from Level-1. Same name, version and folder:
    to the app and the state it is heat; the meta says how it was made."""

    def prepare(self, tiles: list[str], ctx: Context) -> None:
        m2m = ctx.shared["m2m"]
        by_tile, union = {}, {}
        for t in tiles:
            sc = m2m.scenes(Tile.parse(t).bounds)
            by_tile[t] = sc
            for s in sc:
                union[s["displayId"]] = s
        ctx.run.info("l1.scenes", tiles=len(tiles), scenes=len(union))
        ctx.shared["folders"] = m2m.fetch(list(union.values()), log=lambda m: ctx.run.info("l1.download", msg=m))
        ctx.shared["scenes"] = by_tile

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        return l1.build(Tile.parse(tile), stage, ctx.shared["folders"], ctx.shared["scenes"][tile])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", action="store_true", help="wait for M2M download access, checking every 30 minutes")
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--tiles", help="comma list instead of all the Azores")
    args = ap.parse_args()
    publish_tiles.load_env()
    tiles = args.tiles.split(",") if args.tiles else TILES

    with start_run("azores-l1", CACHE / "logs") as run:
        while True:
            m2m = l1.M2M()
            if m2m.can_download():
                break
            m2m.close()
            if not args.wait:
                run.error("l1.no_access", msg="the USGS account has no M2M download access yet")
                return 2
            run.info("l1.waiting", msg="no M2M download access yet; checking again in 30 minutes")
            time.sleep(1800)

        try:
            with run.step("verify"):
                v = l1.verify(m2m)
                run.info("l1.verify", **v)
                print(f"Level-1 vs USGS surface temperature on Madeira: {json.dumps(v)}", flush=True)

            remote = None if args.no_publish else Remote.maybe(DATA_DIR, CACHE / "state", run)
            state = State(CACHE / "state")
            if remote:
                remote.pull_state(state)
            ctx = Context(DATA_DIR, CACHE, run=run, before_tile=remote.pull_meta if remote else None)
            ctx.shared["m2m"] = m2m
            product = HeatL1()
            jobs = [Job(t, product, "azores: Level-1, no surface temperature in Level-2") for t in tiles]
            counts = execute(jobs, ctx, state, run, budget=Budget(), publish=remote.publish if remote else None,
                             checkpoint=(lambda: remote.push_delta(run.id, state)) if remote else None)
            run.info("counts", **counts)
            refresh_index()
            if remote:
                remote.publish_index()
                remote.push_delta(run.id, state)
        finally:
            m2m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
