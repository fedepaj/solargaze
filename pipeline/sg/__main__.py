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

def region_poly(path: str) -> Path:
    from . import regions
    poly = CACHE / "geofabrik" / f"{regions.name_of(path)}.poly"
    if not poly.exists():
        poly.parent.mkdir(parents=True, exist_ok=True)
        regions._get(f"{regions.GEOFABRIK}/{path}.poly", poly)
    return poly


def select_tiles(args) -> list[str]:
    if args.tiles:
        return [t.strip() for t in args.tiles.split(",") if t.strip()]
    if getattr(args, "region", None):
        from . import regions
        ids = regions.tiles_of(region_poly(args.region))
        return _shard(ids, args.shard)
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
    return _shard(list(dict.fromkeys(t.id for t in tiles)), args.shard)


def _shard(ids: list[str], shard: str | None) -> list[str]:
    if not shard:
        return ids
    i, n = map(int, shard.split("/"))
    # Stable across runs and machines: the same tile always lands in the same shard.
    return [t for t in ids if int(hashlib.md5(t.encode()).hexdigest(), 16) % n == i - 1]


def products_of(args):
    names = args.products.split(",") if args.products else ORDER
    unknown = [n for n in names if n not in PRODUCTS]
    if unknown:
        sys.exit(f"unknown product(s): {', '.join(unknown)}; known: {', '.join(ORDER)}")
    out = []
    for n in ORDER:
        if n not in names:
            continue
        why = PRODUCTS[n].unavailable()
        if why:
            print(f"{n}: left out: {why}", file=sys.stderr)
            continue
        out.append(PRODUCTS[n])
    return out


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
    """Build what the plan says. Whatever happens — a tile failing, the run
    stopping as systemic, a crash in the run's own code — the summary is
    written, the index is published merged, and the logs go to the bucket."""
    from .remote import Remote
    state = State(STATE)
    name = args.name or ("-".join(args.what) or "bbox" if not args.tiles else "tiles")
    remote = None
    code = 0
    run = None
    try:
        with start_run(name, LOGS, level="debug" if args.verbose else "info") as run:
            try:
                code = _run_body(args, run, state, remote_box := {})
            except Exception as exc:  # noqa: BLE001 — the run's own failure, recorded as such
                run.error("crash", kind=errors.classify(exc), sig=errors.signature(exc), msg=str(exc)[:500],
                          traceback=__import__("traceback").format_exc(limit=12))
                run.systemic = f"crash: {errors.signature(exc)}"
                code = 1
            finally:
                remote = remote_box.get("remote")
                try:
                    with run.step("index"):
                        refresh_index()
                        from . import catalog
                        cat = catalog.write(DATA_DIR)
                        if remote:
                            remote.publish_index()
                            remote.publish_file(cat)
                except Exception as exc:  # noqa: BLE001
                    run.error("index.failed", sig=errors.signature(exc), msg=str(exc)[:300])
                    code = code or 1
                if remote:
                    # Every record this run made — done, empty or failed — in
                    # its delta file, so the next runner neither rebuilds a
                    # tile that has nothing nor loses count of the attempts on
                    # one that keeps failing.
                    try:
                        with run.step("state.push"):
                            run.info("state.pushed", records=remote.push_delta(run.id, state))
                    except Exception as exc:  # noqa: BLE001
                        run.error("state.push.failed", sig=errors.signature(exc), msg=str(exc)[:300])
                        code = code or 1
    finally:
        if remote and run is not None:
            try:
                remote.push_logs(run.id, LOGS)
            except Exception as exc:  # noqa: BLE001 — the logs are on disk regardless
                print(f"could not upload the logs: {exc}", file=sys.stderr)
    if run is not None:
        print(f"\nlog: {run.path}\nsummary: {LOGS / (run.id + '.summary.json')}")
    return code


def _run_body(args, run, state, box) -> int:
    from .remote import Remote
    remote = None if args.no_publish else Remote.maybe(DATA_DIR, STATE, run)
    box["remote"] = remote
    if remote and not args.no_pull:
        with run.step("state.pull"):
            run.info("state.pulled", records=remote.pull_state(state))
    if args.region:
        from . import regions
        import osm_extract
        with run.step("extract", region=args.region):
            pbf, poly = regions.fetch(args.region, CACHE / "geofabrik", run)
            manifest = json.loads(osm_extract.MANIFEST.read_text()) if osm_extract.MANIFEST.exists() else {}
            if [e.get("pbf") for e in manifest.get("extracts", [])] == [pbf.name] and not args.reindex:
                run.info("extract.indexed", msg="index already holds this extract")
            else:
                osm_extract.index_extracts([str(pbf)])
        if remote:
            done = _border(args, run, remote, poly)
            if args.pieces_only:
                return done
    elif args.pieces_only:
        raise SystemExit("--pieces-only needs --region")
    tiles = select_tiles(args)
    jobs = plan(products_of(args), tiles, state, run.context["git"])
    if args.limit:
        jobs = jobs[:args.limit]
    run.info("selected", tiles=len(tiles), jobs=len(jobs), shard=args.shard or "all")
    ctx = Context(DATA_DIR, CACHE, run=run, before_tile=remote.pull_meta if remote else None)
    try:
        counts = execute(jobs, ctx, state, run, budget=Budget(seconds=args.hours * 3600, min_free_gb=args.min_free_gb),
                         publish=remote.publish if remote else None,
                         checkpoint=(lambda: remote.push_delta(run.id, state)) if remote else None)
        run.info("counts", **counts)
        return 0
    except Systemic as exc:
        run.error("systemic", msg=str(exc))
        run.systemic = str(exc)
        return 2


def others_shape(path: str):
    """The union of the boundaries of every other region of the scope this
    region belongs to or neighbours: catalogue and neighbours alike."""
    from shapely.ops import unary_union
    from . import regions
    import osm_extract
    scope = next(k for k, v in regions.SCOPES.items() if path in v + regions.NEIGHBOURS.get(k, []))
    others = [r for r in regions.SCOPES[scope] + regions.NEIGHBOURS.get(scope, []) if r != path]
    return unary_union([osm_extract.read_poly(region_poly(r)) for r in others])


def _border(args, run, remote, poly) -> int:
    """Leave this region's pieces of every tile it shares with another
    region; pick up the neighbours' pieces of the tiles it cannot cover
    alone. A failure here costs the border tiles, never the run — except in
    a pieces-only run, where the pieces are the whole point.

    The two sets differ: a tile can be wholly inside this region's boundary
    and still be a border tile for a neighbour whose boundary overlaps it
    (Geofabrik's boundaries overlap along borders), so the pieces left are
    those of every tile another boundary reaches into."""
    from shapely.geometry import box
    from . import border, regions
    name = regions.name_of(args.region)
    try:
        with run.step("border", region=args.region):
            tiles = regions.tiles_of(poly)
            others = others_shape(args.region)
            shared = [t for t in tiles if others.intersects(tb := box(*Tile.parse(t).bounds))
                      and not others.touches(tb)]
            border.leave(remote, name, poly, shared, run)
            if not args.pieces_only:
                border.gather(remote, name, border.border_tiles(tiles), run)
        return 0
    except Exception as exc:  # noqa: BLE001 — classified and logged; the border tiles wait
        run.error("border.failed", kind=errors.classify(exc), sig=errors.signature(exc), msg=str(exc)[:300])
        if args.pieces_only:
            raise
        return 0


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
    """State both ways: what the bucket has newer than this machine, then
    what this machine has newer (adopted tiles, a run without --publish).
    With --compact, then fold the bucket's deltas into its base — the report
    job's last step, the base's only writer."""
    from .remote import Remote
    r = Remote.maybe(DATA_DIR, STATE)
    if not r:
        print("no R2 credentials")
        return 1
    state = State(STATE)
    print(f"pulled {r.pull_state(state)} record(s), pushed {r.push_state(state)}")
    if args.compact:
        print("compacted:", r.compact())
    return 0


def cmd_matrix(args) -> int:
    """The regions with the most work, as a GitHub Actions matrix."""
    from . import regions
    from .log import git_sha
    from . import border
    state = State(STATE)
    products = products_of(args)
    waiting = border.waiting_tiles(state)
    rows, idle = [], []
    for path in regions.SCOPES[args.scope]:
        try:
            tiles = regions.tiles_of(region_poly(path))
        except Exception as exc:  # noqa: BLE001 — one region's boundary is one region
            print(f"{path}: {errors.signature(exc)}", file=sys.stderr)
            continue
        jobs = plan(products, tiles, state, git_sha())
        row = {"region": path, "name": regions.name_of(path), "tiles": len(tiles), "jobs": len(jobs),
               "support": False, "real": sum(j.tile not in waiting for j in jobs)}
        if row["real"]:
            rows.append(row)
        elif jobs:
            idle.append(row)
    # The catalogue's order is the priority: where people live first, so that
    # a week of runs has covered the cities rather than the tundra. A region
    # whose only work is border tiles waiting for a neighbour comes after,
    # and only once a neighbour has left pieces since it last looked —
    # otherwise the first six would hold their slots for ever on tiles that
    # wait for the seventh.
    if idle:
        rows += _ripe(idle, args.scope)
    chosen = rows[:args.max]
    # Neighbours whose border pieces are missing or old go first: they take
    # minutes, and the builds after them find their pieces.
    if chosen:
        support = _stale_neighbours(args.scope)
        rows = support + rows
        chosen = support + chosen
    out = json.dumps({"include": chosen})
    print(out)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write(f"matrix={out}\nany={'true' if chosen else 'false'}\n")
    for r in rows:
        what = "pieces only" if r["support"] else f"{r['tiles']:5} tiles {r['jobs']:6} jobs ({r['real']} not waiting)"
        print(f"  {r['region']:38} {what}", file=sys.stderr)
    return 0


def _ripe(idle: list[dict], scope: str) -> list[dict]:
    from . import border, regions
    from .remote import Remote
    import osm_extract
    remote = Remote.maybe(DATA_DIR, STATE)
    if not remote:
        return []
    left, gathered = border.stamps(remote)
    paths = regions.SCOPES[scope] + regions.NEIGHBOURS.get(scope, [])
    shapes = {p: osm_extract.read_poly(region_poly(p)) for p in paths}
    out = []
    for row in idle:
        me = shapes[row["region"]]
        near = [regions.name_of(p) for p, sh in shapes.items() if p != row["region"] and sh.intersects(me)]
        if border.ripe(row["name"], near, left, gathered):
            out.append(row)
    return out


def _stale_neighbours(scope: str) -> list[dict]:
    from . import border, regions
    from .remote import Remote
    remote = Remote.maybe(DATA_DIR, STATE)
    if not remote:
        return []
    out = []
    for path in regions.NEIGHBOURS.get(scope, []):
        name = regions.name_of(path)
        try:
            age = border.pieces_age_days(remote, name)
        except Exception as exc:  # noqa: BLE001 — a neighbour skipped tonight is retried tomorrow
            print(f"{path}: {errors.signature(exc)}", file=sys.stderr)
            continue
        if age >= border.REFRESH_DAYS:
            out.append({"region": path, "name": name, "tiles": 0, "jobs": 0, "support": True})
    return out


def cmd_report(args) -> int:
    """Merge the summaries of every run whose name starts with a prefix (the
    jobs of one workflow run), print them as Markdown, and say whether a
    human should look: exit 3 when a run stopped as systemic, or when more
    than a fifth of the jobs failed — border tiles waiting for a neighbour's
    pieces left out of both counts, since they fail by design until it comes."""
    from .remote import Remote
    r = Remote.maybe(DATA_DIR, STATE)
    summaries = []
    if r:
        import gzip as _gz
        for page in r.s3.get_paginator("list_objects_v2").paginate(Bucket=r.ops, Prefix="logs/"):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                if k.endswith(".summary.json") and f"-{args.prefix}" in k:
                    raw = r.s3.get_object(Bucket=r.ops, Key=k)["Body"].read()
                    summaries.append(json.loads(_gz.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw))
    if not summaries:
        text = (f"## Pipeline run `{args.prefix}`\n\nNo job left a summary: they stopped before writing one "
                f"(a crash before the run started, a runner lost, or no credentials). The job logs on GitHub say which.\n")
        print(text)
        if args.out:
            Path(args.out).write_text(text)
        return 3
    from .log import Run
    total, systemic, waiting = Counter(), [], 0
    lines = [f"## Pipeline run `{args.prefix}`", ""]
    for s_ in sorted(summaries, key=lambda x: x["run"]):
        lines.append(Run.markdown(None, s_))
        for product, c in s_["outcomes"].items():
            total.update({k: v for k, v in c.items()})
        if s_.get("systemic"):
            systemic.append(s_["run"])
        waiting += sum(f["count"] for f in s_.get("failures", []) if f["signature"].startswith("NotCovered"))
    jobs = sum(v for k, v in total.items() if k != "skipped")
    failed = total.get("failed", 0) - waiting
    alert = bool(systemic) or (jobs - waiting > 0 and failed / (jobs - waiting) > 0.2)
    head = (f"**{jobs} jobs**: {total.get('done', 0)} done, {total.get('empty', 0)} empty, "
            f"{total.get('failed', 0)} failed" + (f" ({waiting} of them border tiles waiting for a neighbour)"
                                                  if waiting else "") +
            f", {total.get('skipped', 0)} left for the next run.")
    if systemic:
        head += f" Stopped as systemic: {', '.join(systemic)}."
    lines.insert(2, head + "\n")
    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    return 3 if alert else 0


def _check_scope(r, row) -> None:
    """The token must reach the two buckets and nothing else. A canary
    bucket (R2_CANARY_BUCKET), made for the purpose and kept empty, is the
    test: a scoped token is refused there; an account-wide one is not."""
    from botocore.exceptions import ClientError
    try:
        names = [b["Name"] for b in r.s3.list_buckets()["Buckets"]]
        listing = f"can list every bucket ({len(names)})"
    except ClientError as exc:
        listing = f"cannot list buckets ({exc.response['Error']['Code']})"
    canary = os.environ.get("R2_CANARY_BUCKET")
    if not canary:
        row("R2 token scope", True, f"{listing}; no canary bucket set, scope not tested")
        return
    try:
        r.s3.put_object(Bucket=canary, Key="doctor/should-be-refused", Body=b"x")
        r.s3.delete_object(Bucket=canary, Key="doctor/should-be-refused")
        row("R2 token limited to its buckets", False, f"it could write to {canary}: the token is not scoped; {listing}")
    except ClientError as exc:
        row("R2 token limited to its buckets", True, f"refused on {canary} ({exc.response['Error']['Code']}); {listing}")


def cmd_gaps(args) -> int:
    """What the pipeline cannot do yet: station networks no importer reads in
    the scope's countries, and upstream failures that repeat. With --sync,
    each becomes a GitHub issue labelled gap (once), and the oldest open one
    with no pull request is picked for the agent."""
    from . import gaps
    countries = gaps.SCOPES.get(args.scope) or [c.strip().upper() for c in args.scope.split(",")]
    found = gaps.station_gaps(countries) + gaps.failure_gaps(STATE)
    for g in found:
        print(f"  {g['id']:28} {g['title']}")
    result = {"found": [g["id"] for g in found]}
    if args.sync:
        issues = gaps.sync_issues(found)
        result["open"] = [{"number": i["number"], "title": i["title"], "pr": i["pr"], "blocked": i["blocked"]} for i in issues]
        result["pick"] = gaps.pick(issues)
        print(f"open gap issues: {len(issues)}; for the agent: {result['pick'] or 'none'}")
        gh = os.environ.get("GITHUB_OUTPUT")
        if gh:
            with open(gh, "a") as f:
                f.write(f"pick={result['pick'] or ''}\n")
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    return 0


def cmd_doctor(args) -> int:
    """Everything a run needs, checked before it is needed. With --scope-only,
    just the R2 token's reach — the check a runner must pass before it holds
    the keys to anything."""
    import importlib
    ok = True
    if args.scope_only:
        from .remote import Remote

        def row(name, good, detail=""):
            nonlocal ok
            ok &= bool(good)
            print(f"  {'ok ' if good else 'BAD'}  {name:34} {detail}")
        r = Remote.maybe(DATA_DIR, STATE)
        if not r:
            print("no R2 credentials")
            return 1
        _check_scope(r, row)
        return 0 if ok else 1
    try:
        CACHE.mkdir(parents=True, exist_ok=True)   # a fresh runner has none yet
    except OSError:
        pass

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
            probe = f"doctor/probe-{socket.gethostname()}-{os.getpid()}"
            for name, b in (("R2 tiles bucket", r.bucket), ("R2 ops bucket (state, logs)", r.ops)):
                r.s3.put_object(Bucket=b, Key=probe, Body=b"probe")
                got = r.s3.get_object(Bucket=b, Key=probe)["Body"].read()
                r.s3.delete_object(Bucket=b, Key=probe)
                row(name, got == b"probe", f"{b}: write, read, delete")
            _check_scope(r, row)
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
    # pipeline/.env first, whatever the command: a credential a product needs
    # (Earthdata) must not depend on whether R2's are wanted (--no-publish).
    import publish_tiles
    publish_tiles.load_env()
    ap = argparse.ArgumentParser(prog="python -m sg", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def tile_args(p):
        p.add_argument("what", nargs="*", help="region names (" + ", ".join(REGIONS) + ") or lat,lon points")
        p.add_argument("--bbox", help="west,south,east,north")
        p.add_argument("--tiles", help="comma list of tile ids")
        p.add_argument("--only-land", action="store_true", help="drop tiles that are open sea")
        p.add_argument("--region", help="a Geofabrik region path, e.g. europe/france: fetch, verify and index its extract, then its tiles")
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
    p.add_argument("--reindex", action="store_true", help="index the region's extract even if the index holds it")
    p.add_argument("--pieces-only", action="store_true",
                   help="with --region: leave its pieces of the scope's border tiles on the bucket, build nothing")
    p = sub.add_parser("status", help="where things stand")
    p.add_argument("--products")
    sub.add_parser("adopt", help="record tiles built before the framework")
    p = sub.add_parser("sync", help="state both ways between this machine and the bucket")
    p.add_argument("--compact", action="store_true", help="then fold the bucket's deltas into its base")
    p = sub.add_parser("matrix", help="the regions with most work, as a GitHub Actions matrix")
    p.add_argument("--scope", default="europe")
    p.add_argument("--max", type=int, default=8)
    p.add_argument("--products")
    p = sub.add_parser("gaps", help="what the pipeline cannot do yet; --sync makes them GitHub issues")
    p.add_argument("--scope", default="test", help="a gap scope (test, americas, next) or ISO codes, comma-separated")
    p.add_argument("--sync", action="store_true", help="open issues for new gaps and pick one for the agent")
    p.add_argument("--out", help="write the result as JSON here")
    p = sub.add_parser("report", help="merge the summaries of one workflow run; exit 3 if a human should look")
    p.add_argument("prefix", help="the shared start of the runs' names, e.g. gha-123456")
    p.add_argument("--out", help="also write the Markdown here")
    p = sub.add_parser("doctor", help="check storage, credentials, network and modules")
    p.add_argument("--scope-only", action="store_true", help="only check that the R2 token reaches its buckets and no other")
    args = ap.parse_args(argv)
    return {"plan": cmd_plan, "run": cmd_run, "status": cmd_status, "adopt": cmd_adopt, "sync": cmd_sync,
            "matrix": cmd_matrix, "report": cmd_report, "gaps": cmd_gaps, "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
