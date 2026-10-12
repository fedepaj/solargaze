"""
Re-encode the rasters of tiles already built from one-channel PNG to what
the builders write now (stored.py): heat and noise lossy, the rest exact.

The PNG stays where it is until ``--prune``: a tile's meta names the files
to read, so a tile is switched by its meta alone, and switched back the
same way. A product re-encoded gets a new ``generated`` stamp, so that a
machine still holding the tile's older meta does not put the PNG names back
when it publishes (merge_meta keeps the product stamped last).

    pipeline/.venv/bin/python pipeline/reencode.py                     # every tile under data/tiles
    pipeline/.venv/bin/python pipeline/reencode.py N41.75E12.25
    pipeline/.venv/bin/python pipeline/reencode.py --root /some/copy   # another folder of tiles
    pipeline/.venv/bin/python pipeline/reencode.py --remote --shard 0/16   # the published tiles, a sixteenth of them
    pipeline/.venv/bin/python pipeline/reencode.py --prune             # remove the PNGs no meta names any more
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

import stored

EXACT = ("wind", "built", "light", "air_street")


def how(product: str) -> str | None:
    if product == "noise" or product.startswith("heat"):
        return "lossy"
    return "exact" if product in EXACT else None


def refs(product: str, info: dict) -> list[tuple[dict, str, str]]:
    """Where a product's meta names its images: (the dict, the key, the path under the tile)."""
    if product == "wind":
        h = info.get("heights_png") or {}
        return [(h, "file", f"wind/{h['file']}")] if h.get("file") else []
    files = info.get("files") or {}
    return [(files, name, file if "/" in file else f"{product}/{file}") for name, file in files.items()]


def renamed(value: str, suffix: str) -> str:
    return value[:-len(".png")] + suffix


def convert(meta: dict, fetch, store) -> tuple[int, int, list[str]]:
    """Re-encode what a tile's meta names as PNG. ``fetch(path)`` gives a
    file's bytes or None, ``store(path, data)`` keeps one; the meta is
    changed in place. Returns the bytes before and after, and what was left
    as it was and why."""
    before = after = 0
    left = []
    for product, info in meta.get("products", {}).items():
        kind = how(product)
        todo = [r for r in refs(product, info) if r[2].endswith(".png")] if kind else []
        if not todo:
            continue
        if kind == "lossy" and info.get("encoding", {}).get("nodata_byte") != 0:
            left.append(f"{product}: not the one-channel format")
            continue
        pngs = {path: fetch(path) for _, _, path in todo}
        if any(data is None for data in pngs.values()):
            left.append(f"{product}: a file the meta names is missing")
            continue
        images = {path: Image.open(io.BytesIO(data)) for path, data in pngs.items()}
        if any(img.mode != "L" for img in images.values()):
            left.append(f"{product}: not a one-channel image")
            continue
        suffix = ".avif" if kind == "lossy" else ".webp"
        for holder, key, path in todo:
            byte = np.asarray(images[path])
            data = stored.encode_lossy(byte) if kind == "lossy" else stored.encode_exact(byte)
            store(renamed(path, suffix), data)
            holder[key] = renamed(holder[key], suffix)
            before += len(pngs[path])
            after += len(data)
        if kind == "lossy":
            info["encoding"]["lossy"] = stored.LOSSY
        info["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return before, after, left


def stale(meta: dict) -> list[str]:
    """The PNGs a tile's meta no longer names: the path under the tile of each one replaced."""
    out = []
    for product, info in meta.get("products", {}).items():
        if how(product):
            out += [str(Path(path).with_suffix(".png")) for _, _, path in refs(product, info)
                    if path.endswith((".avif", ".webp"))]
    return out


# ------------------------------------------------------------ a folder

def reencode(tile: Path) -> tuple[int, int, list[str]]:
    meta_path = tile / "meta.json"
    meta = json.loads(meta_path.read_text())
    fetch = lambda path: (tile / path).read_bytes() if (tile / path).exists() else None
    before, after, left = convert(meta, fetch, lambda path, data: (tile / path).write_bytes(data))
    if before:
        tmp = tile / ".meta.json.tmp"
        tmp.write_text(json.dumps(meta, indent=2) + "\n")
        os.replace(tmp, meta_path)
    return before, after, left


def prune(tile: Path) -> int:
    freed = 0
    for path in stale(json.loads((tile / "meta.json").read_text())):
        old = tile / path
        if old.exists():
            freed += old.stat().st_size
            old.unlink()
    return freed


# ------------------------------------------------------------ the bucket

class Bucket:
    """The published tiles, read and written the way publish_tiles.py does."""

    def __init__(self):
        import publish_tiles as pt
        from sg import errors
        pt.load_env()
        self.pt, self.retry = pt, lambda fn: errors.retry(fn, tries=4, base=5)
        self.s3, self.bucket = pt.client()

    def tiles(self) -> list[str]:
        return [t["id"] for t in json.loads(self.retry(lambda: self.pt._get(self.s3, self.bucket, "index.json")))["tiles"]]

    def get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError
        try:
            return self.retry(lambda: self.pt._get(self.s3, self.bucket, key))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def put(self, key: str, data: bytes) -> None:
        self.retry(lambda: self.pt.put(self.s3, self.bucket, key, Path(key), data=data))

    def reencode(self, tile: str) -> tuple[int, int, list[str]]:
        raw = self.get(f"{tile}/meta.json")
        if raw is None:
            return 0, 0, ["no meta"]
        meta = json.loads(raw)
        before, after, left = convert(meta, lambda path: self.get(f"{tile}/{path}"),
                                      lambda path, data: self.put(f"{tile}/{path}", data))
        if before:
            # The meta as it is now, not as it was read: a product another machine added meanwhile stays.
            from sg.remote import merge_meta
            fresh = self.get(f"{tile}/meta.json")
            meta = merge_meta(meta, json.loads(fresh) if fresh else None)
            self.put(f"{tile}/meta.json", gzip.compress((json.dumps(meta, indent=2) + "\n").encode(), 9, mtime=0))
        return before, after, left

    def prune(self, tile: str) -> int:
        raw = self.get(f"{tile}/meta.json")
        gone = [f"{tile}/{path}" for path in stale(json.loads(raw))] if raw else []
        if not gone:
            return 0
        there = {o["Key"]: o["Size"] for o in self.retry(
            lambda: self.s3.list_objects_v2(Bucket=self.bucket, Prefix=f"{tile}/")).get("Contents", [])}
        # A PNG goes only when what replaced it is there.
        gone = [k for k in gone if k in there and any(str(Path(k).with_suffix(s)) in there for s in (".avif", ".webp"))]
        if gone:
            self.retry(lambda: self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in gone]}))
        return sum(there[k] for k in gone)


def shard_of(tile: str, n: int) -> int:
    return zlib.crc32(tile.encode()) % n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tiles", nargs="*")
    ap.add_argument("--root", type=Path, help="a folder of tiles (default: data/tiles)")
    ap.add_argument("--remote", action="store_true", help="the published tiles, on the bucket")
    ap.add_argument("--shard", default="0/1", help="i/n: the i-th of n parts of the tiles")
    ap.add_argument("--hours", type=float, default=0, help="stop cleanly after this long (run again for the rest)")
    ap.add_argument("--prune", action="store_true", help="remove the PNGs that were replaced, re-encode nothing")
    args = ap.parse_args()
    i, n = (int(x) for x in args.shard.split("/"))

    if args.remote:
        bucket = Bucket()
        ids = args.tiles or bucket.tiles()
        work = bucket.prune if args.prune else bucket.reencode
    else:
        from tiles import DATA_DIR
        root = args.root or DATA_DIR
        ids = args.tiles or sorted(p.parent.name for p in root.glob("N*E*/meta.json"))
        work = (lambda t: prune(root / t)) if args.prune else (lambda t: reencode(root / t))
    ids = [t for t in ids if shard_of(t, n) == i]

    t0 = time.time()
    done = failed = before = after = 0
    for k, tile in enumerate(ids):
        if args.hours and time.time() - t0 > args.hours * 3600:
            print(f"out of time with {len(ids) - k} tile(s) left: run again for the rest")
            break
        try:
            out = work(tile)
        except Exception as exc:   # one tile's trouble is that tile's: the rest go on, and the exit code says so
            failed += 1
            print(f"{tile}: FAILED {type(exc).__name__}: {exc}", flush=True)
            continue
        if args.prune:
            before += out
            done += bool(out)
            continue
        b, a, left = out
        before, after, done = before + b, after + a, done + bool(b)
        if b or left:
            print(f"{tile}: {b / 1e3:.0f} kB -> {a / 1e3:.0f} kB" + (f"   left: {'; '.join(left)}" if left else ""), flush=True)
    if args.prune:
        print(f"{done} tile(s) pruned, {before / 1e6:.1f} MB of PNG removed, {failed} failed")
    else:
        print(f"{done} of {len(ids)} tile(s) re-encoded in {(time.time() - t0) / 60:.0f} min: "
              f"{before / 1e6:.1f} MB -> {after / 1e6:.1f} MB" + (f" ({100 * after / before:.0f} %)" if before else "")
              + f", {failed} failed")
    sys.exit(1 if failed else 0)
