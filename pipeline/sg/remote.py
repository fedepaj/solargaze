"""
The run's link to R2: tiles to the public bucket, state and logs to the
private one (``R2_OPS_BUCKET``, never served), so that what a runner did
outlives it and the next runner starts from there.

The state travels in bulk, because a continent is tens of thousands of
records and one request each would take a runner hours: ``state/base.jsonl.gz``
holds every record, each run writes the records it touched to
``state/deltas/<run>.jsonl.gz`` as it goes, and the report job — the only
writer of the base, after the builds — folds the deltas in. Reading is the
base plus the deltas; the newest record of a (product, tile) wins. Records
from before this layout (``state/<product>/<tile>.json``) are read once and
folded in by the first compaction.

Without credentials the run works locally and says it is not publishing.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publish_tiles as pt  # noqa: E402

from . import errors  # noqa: E402


class Remote:
    def __init__(self, data_dir: Path, state_dir: Path, log=None):
        self.s3, self.bucket = pt.client()
        self.ops = os.environ.get("R2_OPS_BUCKET", "solargaze-ops")
        self.data_dir, self.state_dir = data_dir, state_dir
        self.log = log
        self._state_cache = None

    @classmethod
    def maybe(cls, data_dir: Path, state_dir: Path, log=None) -> "Remote | None":
        pt.load_env()
        if not all(os.environ.get(k) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")):
            if log:
                log.warn("remote.off", msg="no R2 credentials: building locally, publishing nothing")
            return None
        return cls(data_dir, state_dir, log)

    def _call(self, fn):
        return errors.retry(fn, tries=4, base=5,
                            on_retry=lambda n, e, w: self.log and self.log.warn("retry", stage="r2", attempt=n, wait=w, msg=str(e)[:200]))

    def publish(self, paths: list[Path]) -> None:
        """Files of one tile, products before its meta."""
        data = sorted((p for p in paths if self.data_dir in p.parents), key=lambda p: p.name == "meta.json")
        for p in data:
            key = p.relative_to(self.data_dir).as_posix()
            if p.name == "meta.json":
                import json
                merged = merge_meta(json.loads(p.read_text()), self._get_json(self.bucket, key))
                p.write_text(json.dumps(merged, indent=2) + "\n")
            n = self._call(lambda: pt.put(self.s3, self.bucket, key, p))
            if self.log:
                self.log.count("bytes_published", n)

    def _get_json(self, bucket: str, key: str):
        from botocore.exceptions import ClientError
        import json
        try:
            raw = self._call(lambda: self.s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        return json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)

    def pull_meta(self, tile: str) -> None:
        """The tile's published meta.json, unless this machine has one: a
        product built here is then added to the published ones, not put in
        their place."""
        import json
        local = self.data_dir / tile / "meta.json"
        if local.exists():
            return
        meta = self._get_json(self.bucket, f"{tile}/meta.json")
        if meta is not None:
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(json.dumps(meta, indent=2) + "\n")

    def publish_index(self) -> None:
        """The published index merged with the tiles this machine holds —
        never this machine's alone, which may be a handful of tiles."""
        p = self.data_dir / "index.json"
        merged = merge_index(self._get_json(self.bucket, "index.json"), self.data_dir)
        import json
        p.write_text(json.dumps(merged, indent=2) + "\n")
        self._call(lambda: pt.put(self.s3, self.bucket, "index.json", p))

    # -- state ---------------------------------------------------------------

    def _state_keys(self) -> tuple[bool, list[str], list[str]]:
        base, deltas, legacy = False, [], []
        for page in self._call(lambda: list(self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.ops, Prefix="state/"))):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                if k == BASE_KEY:
                    base = True
                elif k.startswith(DELTAS):
                    deltas.append(k)
                elif k.endswith(".json"):
                    legacy.append(k)
        return base, sorted(deltas), legacy

    def _raw(self, key: str) -> bytes:
        raw = self._call(lambda: self.s3.get_object(Bucket=self.ops, Key=key)["Body"].read())
        return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw

    def remote_state(self, fresh: bool = False) -> tuple[dict, list[str], list[str]]:
        """Every record on the bucket, newest per (product, tile), with the
        delta and legacy keys it was read from. Read once per Remote unless
        asked fresh; the many small files (deltas, legacy records) in parallel."""
        if self._state_cache is not None and not fresh:
            return self._state_cache
        from concurrent.futures import ThreadPoolExecutor
        has_base, deltas, legacy = self._state_keys()
        merged: dict = {}
        if has_base:
            fold(merged, read_jsonl(self._raw(BASE_KEY)))
        with ThreadPoolExecutor(16) as pool:
            for recs in pool.map(lambda k: read_jsonl(self._raw(k)), deltas):
                fold(merged, recs)
            for rec in pool.map(lambda k: json.loads(self._raw(k)), legacy):
                fold(merged, [rec])
        self._state_cache = (merged, deltas, legacy)
        return self._state_cache

    def pull_state(self, state) -> int:
        """Write locally every bucket record newer than the local one."""
        merged, _, _ = self.remote_state()
        n = 0
        for (product, tile), rec in merged.items():
            mine = state.raw(product, tile)
            if mine is None or str(mine.get("at", "")) < str(rec.get("at", "")):
                state.put_raw(rec)
                n += 1
        return n

    def push_delta(self, run_id: str, state) -> int:
        """The records this process touched, as the run's delta file —
        rewritten whole each time, so the last write holds them all."""
        recs = [r for r in (state.raw(p, t) for p, t in sorted(state.touched)) if r]
        if recs:
            self._call(lambda: self.s3.put_object(Bucket=self.ops, Key=f"{DELTAS}{run_id}.jsonl.gz",
                                                  Body=write_jsonl(recs), ContentType="application/x-ndjson"))
        return len(recs)

    def push_state(self, state) -> int:
        """Local records the bucket lacks or has older (a laptop run without
        publishing, adopted tiles), as one delta file."""
        merged, _, _ = self.remote_state()
        newer = [r for r in state.every()
                 if str(r.get("at", "")) > str(merged.get((r["product"], r["tile"]), {}).get("at", ""))]
        if newer:
            key = f"{DELTAS}sync-{socket.gethostname()}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl.gz"
            self._call(lambda: self.s3.put_object(Bucket=self.ops, Key=key, Body=write_jsonl(newer),
                                                  ContentType="application/x-ndjson"))
        return len(newer)

    def compact(self) -> dict:
        """Fold the deltas (and any legacy records) into the base, then delete
        what was folded. Run by one writer at a time: the report job, after
        the builds; a delta written meanwhile is simply left for next time."""
        merged, deltas, legacy = self.remote_state(fresh=True)
        self._call(lambda: self.s3.put_object(Bucket=self.ops, Key=BASE_KEY, Body=write_jsonl(list(merged.values())),
                                              ContentType="application/x-ndjson"))
        gone = deltas + legacy
        for i in range(0, len(gone), 1000):
            chunk = gone[i:i + 1000]
            self._call(lambda: self.s3.delete_objects(Bucket=self.ops, Delete={"Objects": [{"Key": k} for k in chunk]}))
        self._state_cache = None
        return {"records": len(merged), "deltas": len(deltas), "legacy": len(legacy)}

    def push_logs(self, run_id: str, log_dir: Path) -> None:
        for p in (log_dir / f"{run_id}.jsonl", log_dir / f"{run_id}.summary.json"):
            if p.exists():
                self._call(lambda: pt.put(self.s3, self.ops, f"logs/{p.name}", p))


def merge_index(remote: dict | None, data_dir: Path) -> dict:
    """Every tile in the published index, with the entries of the tiles held
    locally taken from their meta.json (which is newer or the same)."""
    import json
    from tiles import STEP
    tiles = {t["id"]: t for t in (remote or {}).get("tiles", [])}
    for mp in sorted(data_dir.glob("N*E*/meta.json")):
        m = json.loads(mp.read_text())
        tiles[m["id"]] = {"id": m["id"], "bounds": m["bounds"], "products": sorted(m.get("products", {}))}
    return {"step": STEP, "tiles": [tiles[k] for k in sorted(tiles)]}


def merge_meta(local: dict, remote: dict | None) -> dict:
    """A tile's meta from two machines: each product from whichever built it
    last (its ``generated`` stamp), so a laptop's older copy never undoes a
    runner's newer product, nor the other way round."""
    if not remote:
        return local
    out = {**remote, **{k: v for k, v in local.items() if k != "products"}}
    products = dict(remote.get("products", {}))
    for name, info in local.get("products", {}).items():
        theirs = products.get(name)
        if theirs is None or str(info.get("generated", "")) >= str(theirs.get("generated", "")):
            products[name] = info
    out["products"] = products
    return out


BASE_KEY = "state/base.jsonl.gz"
DELTAS = "state/deltas/"


def read_jsonl(raw: bytes) -> list[dict]:
    return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]


def write_jsonl(records: list[dict]) -> bytes:
    text = "\n".join(json.dumps(r, sort_keys=True) for r in sorted(records, key=lambda r: (r["product"], r["tile"])))
    return gzip.compress((text + "\n").encode(), 9, mtime=0)


def fold(merged: dict, records) -> None:
    """Keep the newest record of each (product, tile)."""
    for r in records:
        k = (r["product"], r["tile"])
        if k not in merged or str(r.get("at", "")) >= str(merged[k].get("at", "")):
            merged[k] = r
