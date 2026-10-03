"""
The run's link to R2: tiles to the public bucket, state and logs to the
private one (``R2_OPS_BUCKET``, never served), so that what a runner did
outlives it and the next runner starts from there.

Without credentials the run works locally and says it is not publishing.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import sys
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
        """Files of one tile, products before its meta; state records to ops."""
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
        for p in paths:
            if self.state_dir in p.parents:
                key = "state/" + p.relative_to(self.state_dir).as_posix()
                self._call(lambda: pt.put(self.s3, self.ops, key, p))

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

    def pull_state(self) -> int:
        """Bring down every state record that differs from the local copy."""
        n = 0
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in self._call(lambda: list(paginator.paginate(Bucket=self.ops, Prefix="state/"))):
            for obj in page.get("Contents", []):
                local = self.state_dir / obj["Key"][len("state/"):]
                if local.exists() and hashlib.md5(pt.body(local)).hexdigest() == obj["ETag"].strip('"'):
                    continue
                raw = self._call(lambda: self.s3.get_object(Bucket=self.ops, Key=obj["Key"])["Body"].read())
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
                n += 1
        return n

    def push_state(self) -> int:
        """Send every local state record that differs from the bucket's."""
        remote = {}
        for page in self._call(lambda: list(self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.ops, Prefix="state/"))):
            for obj in page.get("Contents", []):
                remote[obj["Key"]] = obj["ETag"].strip('"')
        n = 0
        for p in sorted(self.state_dir.rglob("*.json")):
            key = "state/" + p.relative_to(self.state_dir).as_posix()
            if remote.get(key) != hashlib.md5(pt.body(p)).hexdigest():
                self._call(lambda: pt.put(self.s3, self.ops, key, p))
                n += 1
        return n

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
