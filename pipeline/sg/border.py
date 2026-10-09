"""
Tiles across a border, finished from pieces.

A runner holds one region's extract, so a tile across a border has only
part of its buildings and roads there, and fails as NotCovered. Each
region's run therefore leaves, on the private bucket, its *piece* of every
tile it shares with another region — the lines its extract wrote for that
tile — with its boundary, and picks up the pieces the neighbours left. A tile whose land
the pieces cover, together with the region's own extract, is merged and
built like any other; one still waiting for a neighbour stays NotCovered
until that neighbour's run has come by.

    border/polys/<region>.poly            the region's boundary
    border/regions/<region>.json          when its pieces were left, and for which tiles
    border/tiles/<tile>/<region>.jsonl.gz "b <building line>" / "r <road line>"
    border/gathered/<region>.json         when it last picked up its neighbours' pieces

Regions outside the build catalogue but bordering it (Ukraine, Turkey,
Russia's north-west …) run with ``--pieces-only``: they leave their pieces
and build nothing.
"""

from __future__ import annotations

import gzip
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError

PREFIX = "border/"
REFRESH_DAYS = 30   # pieces older than this are left again; OSM changes slowly at a border
FORMAT = 2          # 2: road lines carry `h`, the highway value the noise model needs; older pieces are left again


def _region_key(name: str) -> str:
    return f"{PREFIX}regions/{name}.json"


def _piece_key(tile: str, name: str) -> str:
    return f"{PREFIX}tiles/{tile}/{name}.jsonl.gz"


def border_tiles(tiles: list[str]) -> list[str]:
    """The tiles whose land the indexed extract does not wholly cover."""
    import osm_extract
    from tiles import Tile
    return [t for t in tiles if not osm_extract.covered(Tile.parse(t))]


def piece_of(tile: str) -> bytes:
    """This region's piece of a tile, from the index, gzipped."""
    import osm_extract
    lines = []
    for tag, path in (("b", osm_extract.OUT / f"{tile}.jsonl"), ("r", osm_extract.OUT / "roads" / f"{tile}.jsonl")):
        if path.exists():
            with open(path) as f:
                lines += [f"{tag} {line}" if line.endswith("\n") else f"{tag} {line}\n" for line in f]
    return gzip.compress("".join(lines).encode(), 6)


def split_piece(raw: bytes) -> tuple[list[str], list[str]]:
    b, r = [], []
    for line in gzip.decompress(raw).decode().splitlines(keepends=True):
        (b if line.startswith("b ") else r).append(line[2:])
    return b, r


def pieces_age_days(remote, name: str) -> float:
    """Days since this region last left its pieces; inf if never, or if
    they were left in an older format."""
    meta = remote._get_json(remote.ops, _region_key(name))
    if not meta or not meta.get("at") or meta.get("format", 1) < FORMAT:
        return float("inf")
    return (time.time() - datetime.fromisoformat(meta["at"]).timestamp()) / 86400


def leave(remote, name: str, poly: Path, tiles: list[str], log=None, force: bool = False) -> int:
    """Put this region's pieces of these tiles on the bucket, unless
    it did so less than REFRESH_DAYS ago. Returns how many were left."""
    if not force and pieces_age_days(remote, name) < REFRESH_DAYS:
        if log:
            log.info("border.fresh", region=name, msg="pieces left recently; not again")
        return 0
    put = lambda key, body: remote._call(lambda: remote.s3.put_object(Bucket=remote.ops, Key=key, Body=body))
    put(f"{PREFIX}polys/{name}.poly", poly.read_bytes())

    def one(tile):
        body = piece_of(tile)
        put(_piece_key(tile, name), body)
        return len(body)
    with ThreadPoolExecutor(16) as ex:
        sizes = list(ex.map(one, tiles))
    # Last: a region's record says its pieces are all there.
    put(_region_key(name), json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                       "format": FORMAT, "tiles": sorted(tiles)}).encode())
    if log:
        log.info("border.left", region=name, tiles=len(tiles), mb=round(sum(sizes) / 1e6, 1))
        log.count("bytes_border_left", sum(sizes))
    return len(tiles)


def gather(remote, name: str, tiles: list[str], log=None) -> dict:
    """Merge the neighbours' pieces of these border tiles where, with the
    region's own extract, they cover all the land. Returns {tile: regions}
    for the tiles now covered."""
    import osm_extract
    from shapely.geometry import box
    from shapely.ops import unary_union
    from tiles import Tile

    def listing(tile):
        page = remote._call(lambda: remote.s3.list_objects_v2(Bucket=remote.ops, Prefix=f"{PREFIX}tiles/{tile}/"))
        return tile, [o["Key"].rsplit("/", 1)[-1].removesuffix(".jsonl.gz") for o in page.get("Contents", [])]
    with ThreadPoolExecutor(16) as ex:
        offered = {t: [r for r in rs if r != name] for t, rs in ex.map(listing, tiles)}

    polys = {}
    for region in sorted({r for rs in offered.values() for r in rs}):
        dest = osm_extract.GEOFABRIK / f"{region}.poly"
        try:
            if not dest.exists():
                dest.write_bytes(remote._call(lambda: remote.s3.get_object(
                    Bucket=remote.ops, Key=f"{PREFIX}polys/{region}.poly")["Body"].read()))
            polys[region] = osm_extract.read_poly(dest)
        except ClientError:
            if log:
                log.warn("border.no_poly", region=region, msg="pieces without a boundary are not used")

    own = osm_extract.coverage()
    plans = {}
    for tile, regions in offered.items():
        regions = [r for r in regions if r in polys]
        if not regions:
            continue
        tbox = box(*Tile.parse(tile).bounds)
        if osm_extract.land_outside(tbox, unary_union([own, *(polys[r] for r in regions)])) == 0:
            plans[tile] = regions

    def fetch(item):
        tile, regions = item
        return tile, {r: remote._call(lambda r=r: remote.s3.get_object(
            Bucket=remote.ops, Key=_piece_key(tile, r))["Body"].read()) for r in regions}
    merged = {}
    with ThreadPoolExecutor(8) as ex:
        for tile, raws in ex.map(fetch, plans.items()):
            for region, raw in raws.items():
                b, r = split_piece(raw)
                osm_extract.merge_piece(tile, region, b, r)
            merged[tile] = sorted(raws)
    remote._call(lambda: remote.s3.put_object(Bucket=remote.ops, Key=f"{PREFIX}gathered/{name}.json", Body=json.dumps(
        {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "covered": len(merged)}).encode()))
    if log:
        waiting = sorted(set(tiles) - set(merged))
        log.info("border.gathered", region=name, covered=len(merged), waiting=len(waiting),
                 sample=waiting[:10])
    return merged


def waiting_tiles(state, product: str = "wind") -> set[str]:
    """Tiles failed as NotCovered: border tiles waiting for a neighbour.
    Every product on them waits with them, since all depend on the mask."""
    return {t for t, r in state.all(product).items() if r.status == "failed" and r.sig.startswith("NotCovered")}


def stamps(remote) -> tuple[dict, dict]:
    """When each region last left its pieces, and last gathered its
    neighbours': two dicts of region name → ISO time."""
    out = ({}, {})
    for i, sub in enumerate(("regions/", "gathered/")):
        for page in remote.s3.get_paginator("list_objects_v2").paginate(Bucket=remote.ops, Prefix=PREFIX + sub):
            for o in page.get("Contents", []):
                meta = remote._get_json(remote.ops, o["Key"]) or {}
                if meta.get("at"):
                    out[i][o["Key"].rsplit("/", 1)[-1].removesuffix(".json")] = meta["at"]
    return out


def ripe(name: str, neighbours: list[str], left: dict, gathered: dict) -> bool:
    """Has a neighbour left pieces since this region last gathered? Only
    then can a run of a region whose sole work is waiting border tiles
    close any of them."""
    since = gathered.get(name, "")
    return any(left.get(n, "") > since for n in neighbours)
