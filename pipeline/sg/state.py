"""
What is known about every (tile, product): done, empty or failed, built by
which version, when, after how many attempts, and why it failed.

One small JSON file per record, ``cache/state/<product>/<tile>.json``,
written atomically; mirrored to the bucket under ``state/`` by the runner so
that the next machine starts from what the last one did. One file per record
keeps two workers on different tiles from ever writing the same thing.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DONE, EMPTY, FAILED = "done", "empty", "failed"


@dataclass
class Record:
    tile: str
    product: str
    status: str = ""
    version: int = 0
    at: str = ""
    attempts: int = 0          # consecutive failed attempts; 0 after a success
    kind: str = ""             # error class of the last failure
    sig: str = ""              # its signature
    msg: str = ""
    seconds: float = 0.0
    run: str = ""
    git: str = ""
    inputs: dict = field(default_factory=dict)  # stamps of what it was built from

    @property
    def age_days(self) -> float:
        if not self.at:
            return float("inf")
        return (time.time() - datetime.fromisoformat(self.at).timestamp()) / 86400


def atomic_write(path: Path, text: str) -> None:
    """Write a file so that a reader sees the old content or the new, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class State:
    def __init__(self, root: Path):
        self.root = root
        # What this process recorded, for the run's delta file on the bucket.
        self.touched: set[tuple[str, str]] = set()

    def path(self, product: str, tile: str) -> Path:
        return self.root / product / f"{tile}.json"

    def get(self, product: str, tile: str) -> Record:
        p = self.path(product, tile)
        if not p.exists():
            return Record(tile=tile, product=product)
        try:
            return Record(**json.loads(p.read_text()))
        except (ValueError, TypeError):
            # A corrupt record is a record we do not trust: start over.
            return Record(tile=tile, product=product)

    def put(self, rec: Record) -> Path:
        p = self.path(rec.product, rec.tile)
        atomic_write(p, json.dumps(asdict(rec), indent=1) + "\n")
        self.touched.add((rec.product, rec.tile))
        return p

    def put_raw(self, rec: dict) -> None:
        """A record from elsewhere (the bucket), written as it is and not
        counted as this process's own."""
        atomic_write(self.path(rec["product"], rec["tile"]), json.dumps(rec, indent=1) + "\n")

    def raw(self, product: str, tile: str) -> dict | None:
        p = self.path(product, tile)
        try:
            return json.loads(p.read_text()) if p.exists() else None
        except ValueError:
            return None

    def every(self):
        """Every local record, as dicts."""
        for p in self.root.glob("*/*.json"):
            try:
                yield json.loads(p.read_text())
            except ValueError:
                continue

    def record(self, tile: str, product: str, status: str, *, version: int, run: str = "", git: str = "",
               seconds: float = 0.0, kind: str = "", sig: str = "", msg: str = "", inputs: dict | None = None) -> Record:
        prev = self.get(product, tile)
        rec = Record(tile=tile, product=product, status=status, version=version,
                     at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     attempts=prev.attempts + 1 if status == FAILED else 0,
                     kind=kind, sig=sig, msg=msg[:500], seconds=round(seconds, 1), run=run, git=git,
                     inputs=inputs or {})
        self.put(rec)
        return rec

    def all(self, product: str) -> dict[str, Record]:
        out = {}
        for p in (self.root / product).glob("*.json"):
            try:
                out[p.stem] = Record(**json.loads(p.read_text()))
            except (ValueError, TypeError):
                continue
        return out
