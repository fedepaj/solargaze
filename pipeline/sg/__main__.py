"""
The pipeline's one command line, on a laptop and on a runner alike.

    python -m sg doctor                         # is everything in place to run?
    python -m sg plan lazio                     # what would be done, and why
    python -m sg run lazio --hours 5.5          # do it, publishing as it goes
    python -m sg run --bbox 6.5,36.5,18.6,47.2 --products wind --shard 2/8
    python -m sg status                         # where things stand, failures by cause
    python -m sg adopt                          # take tiles built before the framework into the state
    python -m sg sync                           # state both ways with the bucket

Run from pipeline/ (or with pipeline/ on the path).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE / "wind")]

import netdns  # noqa: E402,F401  (DNS over HTTPS when the system resolver drops a name)
from run_region import REGIONS, tiles_in_bbox  # noqa: E402
from tiles import DATA_DIR, Tile, read_meta, refresh_index  # noqa: E402

from . import errors  # noqa: E402
from .log import start_run  # noqa: E402
from .product import Context  # noqa: E402
from .products import ORDER, PRODUCTS  # noqa: E402
from .runner import Budget, Systemic, execute, plan  # noqa: E402
from .state import DONE, EMPTY, FAILED, State  # noqa: E402

CACHE = HERE / "cache"
STATE = CACHE / "state"
LOGS = CACHE / "logs"


# ---------------------------------------------------------------- tile selection

def select_tiles(args) -> list[str]:
    if args.tiles:
        return [t.strip() for t in args.tiles.split(",") if t.strip()]
    tiles: list[Tile] = []
    if args.bbox:
        tiles += list(tiles_in_bbox(*map(float, args.bbox.split(","))))
    for what in args.what:
        if what in REGIONS:
            tiles += list(tiles_in_bbox(*REGIONS[what]))
        else:
            tiles.append(Tile.containing(*map(float, what.split(","))))
    if args.only_land:
        from global_land_mask import globe

        def on_land(t: Tile) -> bool:
            w, s, e, n = t.bounds
            pts = [t.centre, (s + .02, w + .02), (s + .02, e - .02), (n - .02, w + .02), (n - .02, e - .02)]
            return any(globe.is_land(la, lo) for la, lo in pts)
        tiles = [t for t in tiles if on_land(t)]
    ids = list(dict.fromkeys(t.id for t in tiles))
    if args.shard:
        i, n = map(int, args.shard.split("/"))
        # Stable across runs and machines: the same tile always lands in the same shard.
        ids = [t for t in ids if int(hashlib.md5(t.encode()).hexdigest(), 16) % n == i - 1]
    return ids


def products_of(args):
    names = args.products.split(",") if args.products else ORDER
    unknown = [n for n in names if n not in PRODUCTS]
    if unknown:
        sys.exit(f"unknown product(s): {', '.join(unknown)}; known: {', '.join(ORDER)}")
    return [PRODUCTS[n] for n in ORDER if n in names]


# ---------------------------------------------------------------- commands

def cmd_plan(args) -> int:
    from .log import git_sha
    state = State(STATE)
    jobs = plan(products_of(args), select_tiles(args), state, git_sha())
    by = defaultdict(Counter)
    for j in jobs:
        by[j.product.name][j.reason.split(" (")[0]] += 1
    for product, reasons in by.items():
        print(f"{product:11} {sum(reasons.values()):5} jobs  " + ", ".join(f"{r}: {n}" for r, n in reasons.most_common()))
    if args.verbose:
        for j in jobs:
            print(f"  {j.product.name:11} {j.tile}  {j.reason}")
    print(f"{len(jobs)} jobs in all" if jobs else "nothing to do")
    return 0


def cmd_run(args) -> int:
    from .remote import Remote
    state = State(STATE)
    name = args.name or ("-".join(args.what) or "bbox" if not args.tiles else "tiles")
    remote = None
    code = 0
    with start_run(name, LOGS, level="debug" if args.verbose else "info") as run:
        if not args.no_publish:
            remote = Remote.maybe(DATA_DIR, STATE, run)
        if remote and not args.no_pull:
            with run.step("state.pull"):
                run.info("state.pulled", records=remote.pull_state())
        tiles = select_tiles(args)
        jobs = plan(products_of(args), tiles, state, run.context["git"])
        if args.limit:
            jobs = jobs[:args.limit]
        run.info("selected", tiles=len(tiles), jobs=len(jobs), shard=args.shard or "all")
        ctx = Context(DATA_DIR, CACHE, run=run)
        try:
            counts = execute(jobs, ctx, state, run, budget=Budget(seconds=args.hours * 3600, min_free_gb=args.min_free_gb),
                             publish=remote.publish if remote else None)
            run.info("counts", **counts)
        except Systemic as exc:
            run.error("systemic", msg=str(exc))
            code = 2
        finally:
            with run.step("index"):
                refresh_index()
                if remote:
                    remote.publish_index()
    if remote:
        try:
            remote.push_logs(run.id, LOGS)
        except Exception as exc:  # noqa: BLE001 — the logs are on disk regardless
            print(f"could not upload the logs: {exc}", file=sys.stderr)
    print(f"\nlog: {run.path}\nsummary: {LOGS / (run.id + '.summary.json')}")
    return code


def cmd_status(args) -> int:
    state = State(STATE)
    names = args.products.split(",") if args.products else ORDER
    for name in names:
        recs = state.all(name)
        c = Counter(r.status for r in recs.values())
        print(f"{name:11} done {c[DONE]:5}  empty {c[EMPTY]:5}  failed {c[FAILED]:5}")
        fails = defaultdict(list)
        for r in recs.values():
            if r.status == FAILED:
                fails[(r.kind, r.sig)].append(r.tile)
        for (kind, sig), tiles in sorted(fails.items(), key=lambda kv: -len(kv[1]))[:8]:
            print(f"    {len(tiles):4} × {kind:9} {sig}   e.g. {', '.join(sorted(tiles)[:3])}")
    return 0


# What each product looked like when its framework version was set, so a tile
# built before the framework is adopted at the version it really is.
def _adopt_version(name: str, info: dict) -> int:
    if name == "heat":
        return 3 if info.get("packing") and info.get("encoding", {}).get("version") == 2 else 0
    return PRODUCTS[name].version


def cmd_adopt(args) -> int:
    from .log import git_sha
    state = State(STATE)
    n = Counter()
    for meta_path in sorted(DATA_DIR.glob("N*E*/meta.json")):
        meta = json.loads(meta_path.read_text())
        for name, info in meta.get("products", {}).items():
            if name not in PRODUCTS or state.get(name, meta["id"]).status:
                continue
            state.record(meta["id"], name, DONE, version=_adopt_version(name, info), run="adopted", git=git_sha())
            n[name] += 1
    print("adopted:", dict(n) or "nothing new")
    return 0


def cmd_sync(args) -> int:
    """State both ways: what the bucket has that this machine does not, then
    what this machine has (adopted tiles, a run without --publish) that the
    bucket does not."""
    from .remote import Remote
    r = Remote.maybe(DATA_DIR, STATE)
    if not r:
        print("no R2 credentials")
        return 1
    print(f"pulled {r.pull_state()} record(s), pushed {r.push_state()}")
    return 0


def cmd_doctor(args) -> int:
    """Everything a run needs, checked before it is needed."""
    import importlib
    ok = True

    def row(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"  {'ok ' if good else 'BAD'}  {name:34} {detail}")

    print("storage")
    row("pipeline/cache reachable", CACHE.exists(), str(CACHE.resolve()))
    free = shutil.disk_usage(CACHE.resolve() if CACHE.exists() else HERE).free / 1e9
    row("free space on the cache disk", free > 10, f"{free:.0f} GB")
    free_data = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else HERE).free / 1e9
    row("free space for data/tiles", free_data > 3, f"{free_data:.0f} GB")
    print("credentials (present, not shown)")
    import publish_tiles
    publish_tiles.load_env()
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_OPS_BUCKET", "OPENAQ_API_KEY"):
        row(k, bool(os.environ.get(k)))
    row("~/.cdsapirc (CAMS ADS)", (Path.home() / ".cdsapirc").exists())
    print("network")
    for host in ("planetarycomputer.microsoft.com", "ads.atmosphere.copernicus.eu", "download.geofabrik.de",
                 "api.openaq.org", "eeadmz1-downloads-api-appservice.azurewebsites.net"):
        try:
            ip = socket.getaddrinfo(host, 443)[0][4][0]
            row(f"resolve {host}", True, ip)
        except OSError as exc:
            row(f"resolve {host}", False, str(exc)[:60])
    try:
        from .remote import Remote
        r = Remote.maybe(DATA_DIR, STATE)
        if r:
            r.s3.list_objects_v2(Bucket=r.bucket, MaxKeys=1)
            row("R2 tiles bucket", True, r.bucket)
            r.s3.list_objects_v2(Bucket=r.ops, MaxKeys=1)
            row("R2 ops bucket (state, logs)", True, r.ops)
        else:
            row("R2", False, "no credentials")
    except Exception as exc:  # noqa: BLE001
        row("R2", False, errors.signature(exc))
    print("python")
    for mod in ("numpy", "scipy", "rasterio", "pystac_client", "planetary_computer", "osmium", "shapely",
                "boto3", "pandas", "pyarrow", "cdsapi", "xarray", "global_land_mask"):
        try:
            importlib.import_module(mod)
            row(mod, True)
        except Exception as exc:  # noqa: BLE001
            row(mod, False, str(exc)[:60])
    print("\nall good" if ok else "\nfix the BAD lines before a run")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sg", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def tile_args(p):
        p.add_argument("what", nargs="*", help="region names (" + ", ".join(REGIONS) + ") or lat,lon points")
        p.add_argument("--bbox", help="west,south,east,north")
        p.add_argument("--tiles", help="comma list of tile ids")
        p.add_argument("--only-land", action="store_true", help="drop tiles that are open sea")
        p.add_argument("--shard", help="i/n: this worker's share of the tiles (stable hash)")
        p.add_argument("--products", help="comma list, default all: " + ",".join(ORDER))
        p.add_argument("-v", "--verbose", action="store_true")

    p = sub.add_parser("plan", help="what would be done, and why")
    tile_args(p)
    p = sub.add_parser("run", help="build, validate, install, record, publish")
    tile_args(p)
    p.add_argument("--hours", type=float, default=float("inf"), help="time budget; stops cleanly before it runs out")
    p.add_argument("--min-free-gb", type=float, default=2.0)
    p.add_argument("--limit", type=int, help="at most this many jobs")
    p.add_argument("--name", help="a name for the run, in its id")
    p.add_argument("--no-publish", action="store_true", help="build locally, publish nothing")
    p.add_argument("--no-pull", action="store_true", help="do not refresh the state from R2 first")
    p = sub.add_parser("status", help="where things stand")
    p.add_argument("--products")
    sub.add_parser("adopt", help="record tiles built before the framework")
    sub.add_parser("sync", help="state both ways between this machine and the bucket")
    sub.add_parser("doctor", help="check storage, credentials, network and modules")
    args = ap.parse_args(argv)
    return {"plan": cmd_plan, "run": cmd_run, "status": cmd_status, "adopt": cmd_adopt, "sync": cmd_sync, "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
