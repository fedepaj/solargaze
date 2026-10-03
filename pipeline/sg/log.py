"""
Structured logging for unattended runs.

A ``Run`` writes every event as one JSON object per line to
``cache/logs/<run id>.jsonl`` and a readable line to the console, keeps the
counts the summary needs, and groups failures by signature for the digest.
``step()`` times a stage; ``outcome()`` records what happened to one
(tile, product). The stdlib ``logging`` of the older scripts is forwarded
into the same stream, so their messages carry the run id and the tile.

    with start_run("europe") as run:
        with run.step("heat", tile="N41.75E12.25"):
            ...
        run.outcome("N41.75E12.25", "heat", "done", duration=212.4)
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import socket
import subprocess
import sys
import time
import traceback
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import errors

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_current: "Run | None" = None
# The tile and product being worked on, so that any message logged deep in
# an importer still says where it came from.
_where: contextvars.ContextVar[dict] = contextvars.ContextVar("where", default={})


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parent, timeout=5).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 — a missing git is not worth a failed run
        return "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Forward(logging.Handler):
    """stdlib logging → run events."""

    def emit(self, record: logging.LogRecord) -> None:
        run = _current
        if run is None or record.name.startswith(("urllib3", "botocore", "boto3", "s3transfer", "rasterio._env")):
            return
        level = "error" if record.levelno >= 40 else "warn" if record.levelno >= 30 else "info" if record.levelno >= 20 else "debug"
        run.event("log", level=level, logger=record.name, msg=record.getMessage())


class Run:
    def __init__(self, name: str, log_dir: Path, console=sys.stderr, level: str = "info"):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.id = f"{stamp}-{name}-{uuid.uuid4().hex[:6]}"
        self.name = name
        self.started = time.time()
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"{self.id}.jsonl"
        self._fh = open(self.path, "a", buffering=1, encoding="utf-8")
        self.console = console
        self.level = LEVELS[level]
        self.context = {"host": socket.gethostname(), "git": git_sha(),
                        "ci": bool(os.environ.get("GITHUB_ACTIONS")), "pid": os.getpid()}
        self.outcomes: Counter = Counter()          # (product, status) → n
        self.stage_time: defaultdict = defaultdict(float)
        self.durations: list[tuple[float, str, str]] = []  # (seconds, tile, product)
        self.failures: defaultdict = defaultdict(list)     # signature → [(tile, product, kind, message)]
        self.counters: Counter = Counter()
        self.systemic: str | None = None   # why the run stopped itself, if it did

    # -- events --------------------------------------------------------------

    def event(self, ev: str, level: str = "info", **fields) -> None:
        rec = {"t": _now(), "run": self.id, "lvl": level, "ev": ev, **_where.get(), **fields}
        self._fh.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
        if LEVELS[level] >= self.level and self.console:
            where = " ".join(str(rec[k]) for k in ("tile", "product") if rec.get(k))
            rest = " ".join(f"{k}={v}" for k, v in fields.items() if k not in ("msg", "tile", "product", "traceback"))
            msg = fields.get("msg", "")
            print(f"{rec['t'][11:19]} {level.upper():5} {ev:18} {where:26} {msg} {rest}".rstrip(), file=self.console)

    def debug(self, ev, **f): self.event(ev, "debug", **f)
    def info(self, ev, **f): self.event(ev, "info", **f)
    def warn(self, ev, **f): self.event(ev, "warn", **f)
    def error(self, ev, **f): self.event(ev, "error", **f)

    def count(self, key: str, n: int = 1) -> None:
        """A free counter for the summary: bytes sent, scenes read, …"""
        self.counters[key] += n

    @contextlib.contextmanager
    def where(self, **fields):
        """Attach fields (tile, product …) to every event inside the block."""
        token = _where.set({**_where.get(), **fields})
        try:
            yield
        finally:
            _where.reset(token)

    @contextlib.contextmanager
    def step(self, stage: str, **fields):
        """Time a stage; an exception is logged with its class and re-raised."""
        t0 = time.time()
        with self.where(**fields):
            self.debug(f"{stage}.start")
            try:
                yield
            except BaseException as exc:
                dur = time.time() - t0
                self.stage_time[stage] += dur
                kind = errors.classify(exc) if isinstance(exc, Exception) else "interrupted"
                self.event(f"{stage}.error", "warn" if kind in (errors.TRANSIENT, errors.NODATA) else "error",
                           dur=round(dur, 2), kind=kind, sig=errors.signature(exc) if isinstance(exc, Exception) else type(exc).__name__,
                           msg=str(exc)[:500], traceback=traceback.format_exc(limit=8) if kind == errors.BUG else None)
                raise
            dur = time.time() - t0
            self.stage_time[stage] += dur
            self.debug(f"{stage}.end", dur=round(dur, 2))

    def outcome(self, tile: str, product: str, status: str, duration: float = 0.0,
                exc: BaseException | None = None, **fields) -> None:
        """The verdict on one (tile, product): done, empty, failed, skipped."""
        self.outcomes[(product, status)] += 1
        if duration:
            self.durations.append((duration, tile, product))
        rec = {"tile": tile, "product": product, "status": status, "dur": round(duration, 2), **fields}
        if exc is not None:
            kind = errors.classify(exc)
            sig = errors.signature(exc)
            rec.update(kind=kind, sig=sig, msg=str(exc)[:500])
            if status == "failed":
                self.failures[sig].append((tile, product, kind, str(exc)[:300]))
        self.event("outcome", "error" if status == "failed" else "info", **rec)

    # -- summary -------------------------------------------------------------

    def summary(self) -> dict:
        by_product: dict = defaultdict(dict)
        for (product, status), n in self.outcomes.items():
            by_product[product][status] = n
        digest = sorted(
            ({"signature": sig, "count": len(hits), "kind": hits[0][2],
              "products": sorted({p for _, p, _, _ in hits}),
              "tiles": sorted({t for t, _, _, _ in hits})[:20], "example": hits[0][3]}
             for sig, hits in self.failures.items()),
            key=lambda d: -d["count"])
        return {
            "run": self.id, "name": self.name, **self.context,
            "started": datetime.fromtimestamp(self.started, timezone.utc).isoformat(timespec="seconds"),
            "seconds": round(time.time() - self.started, 1),
            "outcomes": dict(by_product),
            "totals": dict(Counter(status for (_, status) in self.outcomes.elements())),
            "stage_seconds": {k: round(v, 1) for k, v in sorted(self.stage_time.items(), key=lambda kv: -kv[1])},
            "slowest": [{"tile": t, "product": p, "seconds": round(s, 1)} for s, t, p in sorted(self.durations, reverse=True)[:10]],
            "counters": dict(self.counters),
            "systemic": self.systemic,
            "failures": digest,
            "log": str(self.path),
        }

    def markdown(self, s: dict | None = None) -> str:
        s = s or self.summary()
        lines = [f"### {s['name']} — run `{s['run']}`", "",
                 f"{s['seconds'] / 60:.1f} min on `{s['host']}` at `{s['git']}`", ""]
        if s.get("systemic"):
            lines += [f"**Stopped as systemic:** {s['systemic']}", ""]
        lines += [
                 "| product | done | empty | failed | skipped |", "|---|---:|---:|---:|---:|"]
        for p, c in sorted(s["outcomes"].items()):
            lines.append(f"| {p} | {c.get('done', 0)} | {c.get('empty', 0)} | {c.get('failed', 0)} | {c.get('skipped', 0)} |")
        if s["failures"]:
            lines += ["", "**Failures, by cause**", "", "| n | class | cause | tiles |", "|---:|---|---|---|"]
            for f in s["failures"][:15]:
                tiles = ", ".join(f["tiles"][:5]) + (" …" if f["count"] > 5 else "")
                lines.append(f"| {f['count']} | {f['kind']} | `{f['signature']}` | {tiles} |")
        if s["slowest"]:
            lines += ["", "Slowest: " + ", ".join(f"{d['tile']} {d['product']} {d['seconds']:.0f}s" for d in s["slowest"][:5])]
        return "\n".join(lines) + "\n"

    def write_summary(self, out_dir: Path) -> dict:
        s = self.summary()
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{self.id}.summary.json").write_text(json.dumps(s, indent=2, default=str) + "\n")
        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if step_summary:
            with open(step_summary, "a", encoding="utf-8") as f:
                f.write(self.markdown(s))
        return s

    def close(self) -> None:
        self._fh.close()


@contextlib.contextmanager
def start_run(name: str, log_dir: Path, console=sys.stderr, level: str = "info"):
    """Open a run, forward stdlib logging into it, and always close it with
    a summary — also when the run is interrupted."""
    global _current
    run = Run(name, log_dir, console=console, level=level)
    previous, _current = _current, run
    handler = _Forward()
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    run.info("run.start", **run.context)
    try:
        yield run
    except BaseException as exc:
        run.error("run.abort", kind=errors.classify(exc) if isinstance(exc, Exception) else type(exc).__name__,
                  msg=str(exc)[:500], traceback=traceback.format_exc(limit=12))
        raise
    finally:
        s = run.write_summary(log_dir)
        run.info("run.end", seconds=s["seconds"], totals=s["totals"])
        root.removeHandler(handler)
        run.close()
        _current = previous


def current() -> Run | None:
    """The run in progress, for code deep in an importer that wants to log."""
    return _current
