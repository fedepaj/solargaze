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
            n = self._call(lambda: pt.put(self.s3, self.bucket, key, p))
            if self.log:
                self.log.count("bytes_published", n)
        for p in paths:
            if self.state_dir in p.parents:
                key = "state/" + p.relative_to(self.state_dir).as_posix()
                self._call(lambda: pt.put(self.s3, self.ops, key, p))

    def publish_index(self) -> None:
        p = self.data_dir / "index.json"
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
