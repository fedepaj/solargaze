"""
Run products over tiles, unattended.

For each product, ``prepare`` once for the tiles that need it, then for each
tile: build in staging → validate → install → record → publish. Transient
failures are retried in place with backoff; a tile that still fails is
recorded and the run moves on. The run stops cleanly — never mid-install —
when its time or disk budget runs out, on SIGTERM, or when the last few
tiles all failed for the same reason, which means the cause is not the
tiles (a service down, credentials gone, a bug) and the rest of the budget
would only be burnt on it.
"""

from __future__ import annotations

import shutil
import signal
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import errors
from .log import Run
from .product import Context, Product
from .state import DONE, EMPTY, FAILED, State


class Systemic(RuntimeError):
    """The run's own failure, as opposed to a tile's."""


@dataclass
class Budget:
    seconds: float = float("inf")
    min_free_gb: float = 2.0
    started: float = 0.0

    def __post_init__(self):
        self.started = self.started or time.time()

    def left(self) -> float:
        return self.seconds - (time.time() - self.started)

    def free_gb(self, path: Path) -> float:
        return shutil.disk_usage(path).free / 1e9


@dataclass
class Job:
    tile: str
    product: Product
    reason: str


def plan(products: list[Product], tiles: Iterable[str], state: State, git: str) -> list[Job]:
    """Every (tile, product) that needs work, products in the order given
    (dependencies first), tiles in the order given within each — except that
    tiles being retried after a failure go last: a few that can never succeed
    (a border tile no single extract covers) must not trip the breaker ahead
    of the fresh tiles and starve them night after night."""
    tiles = list(tiles)
    jobs = []
    for product in products:
        fresh, retries = [], []
        for tile in tiles:
            why = product.reason(tile, state.get(product.name, tile), git=git)
            if why:
                (retries if why.startswith("retry") else fresh).append(Job(tile, product, why))
        jobs += fresh + retries
    return jobs


class _Stop:
    """SIGTERM (a cancelled workflow, a runner being reclaimed) becomes a
    polite request: the tile in progress is abandoned, nothing half-written
    is recorded, and the summary still gets written."""

    def __init__(self):
        self.requested = False

    def __enter__(self):
        self.previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, self._handle)
        return self

    def _handle(self, signum, frame):
        self.requested = True
        raise KeyboardInterrupt("SIGTERM")

    def __exit__(self, *exc):
        signal.signal(signal.SIGTERM, self.previous)
        return False


def execute(jobs: list[Job], ctx: Context, state: State, run: Run, *,
            budget: Budget | None = None,
            publish: Callable[[list[Path]], None] | None = None,
            write_meta: Callable = None,
            tries: int = 3, retry_base: float = 10.0,
            breaker: int = 8, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Do the jobs. Returns counts by status. Raises Systemic when the run
    itself has to stop for a reason that is not one tile's."""
    budget = budget or Budget()
    if write_meta is None:
        from tiles import write_meta as _wm
        write_meta = lambda tile, product, info: _wm(tile, product, info, refresh=False)  # noqa: E731
    git = run.context.get("git", "")
    counts = {DONE: 0, EMPTY: 0, FAILED: 0, "skipped": 0}
    recent: deque = deque(maxlen=breaker)
    ctx.data_dir.mkdir(parents=True, exist_ok=True)   # a fresh runner has none
    _sweep_stale_stages(ctx.data_dir, run)

    by_product: dict[str, list[Job]] = {}
    for job in jobs:
        by_product.setdefault(job.product.name, []).append(job)
    run.info("plan", jobs=len(jobs), by_product={k: len(v) for k, v in by_product.items()})

    with _Stop():
        for name, group in by_product.items():
            product = group[0].product
            kept = []
            if budget.left() < 60:
                # Out of time before this product even starts: nothing to
                # prepare, nothing to fetch, every tile waits for the next run.
                for job in group:
                    counts["skipped"] += 1
                    run.outcome(job.tile, name, "skipped", reason="time budget")
                run.warn("budget.time", product=name, msg="out of time; this product waits for the next run", left=len(group))
                continue
            for job in group:
                if budget.left() < 60:
                    counts["skipped"] += 1
                    run.outcome(job.tile, name, "skipped", reason="time budget")
                    continue
                if ctx.before_tile:
                    try:
                        ctx.before_tile(job.tile)
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:  # noqa: BLE001 — without the published meta we must not build
                        _record_failure(job, exc, 0.0, state, run, git, counts)
                        continue
                why_not = product.applies(job.tile, ctx)
                if why_not:
                    state.record(job.tile, name, EMPTY, version=product.version, run=run.id, git=git,
                                 kind=errors.NODATA, msg=why_not)
                    counts[EMPTY] += 1
                    run.outcome(job.tile, name, EMPTY, reason=why_not)
                else:
                    kept.append(job)
            group = kept
            if not group:
                continue
            try:
                with run.step("prepare", product=name):
                    errors.retry(lambda: product.prepare([j.tile for j in group], ctx), tries=tries,
                                 base=retry_base, sleep=sleep,
                                 on_retry=lambda n, e, w: run.warn("retry", product=name, stage="prepare",
                                                                    attempt=n, wait=w, msg=str(e)[:200]))
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 — recorded against every tile it would have built
                for job in group:
                    _record_failure(job, exc, 0.0, state, run, git, counts)
                continue

            for job in group:
                if budget.left() < 60:
                    run.warn("budget.time", msg="out of time; the rest waits for the next run", left=len(group))
                    counts["skipped"] += 1
                    run.outcome(job.tile, name, "skipped", reason="time budget")
                    continue
                if budget.free_gb(ctx.data_dir) < budget.min_free_gb:
                    raise Systemic(f"under {budget.min_free_gb} GB free on {ctx.data_dir}")
                waiting = [d for d in product.depends if state.get(d, job.tile).status != DONE]
                if waiting:
                    counts["skipped"] += 1
                    run.outcome(job.tile, name, "skipped", reason=f"waiting for {', '.join(waiting)}")
                    continue
                status, exc = _one(job, ctx, state, run, publish, write_meta, git, tries, retry_base, sleep, counts)
                if status == FAILED:
                    # A retry has nothing fresh behind it to protect; only
                    # first attempts say the cause is not the tile.
                    if not job.reason.startswith("retry"):
                        recent.append(errors.signature(exc))
                elif status == DONE:
                    recent.clear()
                if len(recent) == breaker and len(set(recent)) == 1:
                    raise Systemic(f"the last {breaker} tiles all failed with: {recent[0]}")
    return counts


def _one(job, ctx, state, run, publish, write_meta, git, tries, retry_base, sleep, counts):
    product, tile = job.product, job.tile
    t0 = time.time()
    with run.where(tile=tile, product=product.name):
        run.info("build.begin", reason=job.reason)

        def attempt():
            stage = product.staging(tile, ctx)
            try:
                info = product.build(tile, stage, ctx)
                product.validate(tile, stage, info)
                return stage, info
            except BaseException:
                shutil.rmtree(stage, ignore_errors=True)
                raise

        try:
            with run.step("build"):
                stage, info = errors.retry(
                    attempt, tries=tries, base=retry_base, sleep=sleep,
                    on_retry=lambda n, e, w: run.warn("retry", attempt=n, wait=w, sig=errors.signature(e), msg=str(e)[:200]))
            with run.step("install"):
                placed = product.install(tile, stage, info, ctx, write_meta)
        except KeyboardInterrupt:
            raise
        except errors.NoData as exc:
            dur = time.time() - t0
            state.record(tile, product.name, EMPTY, version=product.version, run=run.id, git=git,
                         seconds=dur, kind=errors.NODATA, msg=str(exc))
            counts[EMPTY] += 1
            run.outcome(tile, product.name, EMPTY, dur, exc=exc)
            return EMPTY, exc
        except Exception as exc:  # noqa: BLE001 — one tile's failure is one record
            _record_failure(job, exc, time.time() - t0, state, run, git, counts)
            return FAILED, exc

        dur = time.time() - t0
        state.record(tile, product.name, DONE, version=product.version, run=run.id, git=git,
                     seconds=dur, inputs=product.inputs(tile, ctx))
        counts[DONE] += 1
        run.outcome(tile, product.name, DONE, dur, files=len(placed))
        if publish:
            try:
                with run.step("publish"):
                    publish(placed + [state.path(product.name, tile)])
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 — built and recorded; the next publish catches up
                run.warn("publish.failed", sig=errors.signature(exc), msg=str(exc)[:200])
        return DONE, None


def _record_failure(job, exc, dur, state, run, git, counts):
    state.record(job.tile, job.product.name, FAILED, version=job.product.version, run=run.id, git=git,
                 seconds=dur, kind=errors.classify(exc), sig=errors.signature(exc), msg=str(exc))
    counts[FAILED] += 1
    run.outcome(job.tile, job.product.name, FAILED, dur, exc=exc)


def _sweep_stale_stages(data_dir: Path, run: Run) -> None:
    """Staging and swap folders left by a run that was killed. A swapped-out
    version whose replacement never landed is the only good copy: it goes
    back in place rather than in the bin."""
    for p in data_dir.glob("N*E*/.*.stage-*"):
        shutil.rmtree(p, ignore_errors=True)
        run.warn("sweep", msg=f"removed {p.relative_to(data_dir)} left by an interrupted run")
    for p in data_dir.glob("N*E*/.*.old-*"):
        final = p.parent / p.name[1:].split(".old-")[0]
        if final.exists():
            shutil.rmtree(p, ignore_errors=True)
            run.warn("sweep", msg=f"removed {p.relative_to(data_dir)} left by an interrupted run")
        else:
            p.rename(final)
            run.warn("sweep", msg=f"restored {final.relative_to(data_dir)} from an interrupted swap")
