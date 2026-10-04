"""
What went wrong, in four classes that decide what happens next (see
ARCHITECTURE.md, *Errors*):

- ``transient`` — the network, a busy server: retry now with backoff, and
  next run if it persists;
- ``nodata``   — there is nothing to build here (sea, no clear scene, no
  station): recorded as empty, not retried until something changes;
- ``upstream`` — a source refused us or changed: failed, retried next run;
- ``bug``      — everything else, validation failures included: failed, and
  not retried until the code changes.

Code that knows better raises the specific class; everything else goes
through ``classify``, which reads exception types first and the message last.
"""

from __future__ import annotations

import re
import socket
import time
from typing import Callable, TypeVar

T = TypeVar("T")

TRANSIENT, NODATA, UPSTREAM, BUG = "transient", "nodata", "upstream", "bug"


class PipelineError(Exception):
    kind = BUG
    per_tile = False   # True: says something about this tile only, never about the run


class Transient(PipelineError):
    kind = TRANSIENT


class NoData(PipelineError):
    kind = NODATA


class Upstream(PipelineError):
    kind = UPSTREAM


class NotCovered(Upstream):
    """This tile lacks an input that other tiles have — a border tile no
    single extract covers. A property of the tile, however many of them come
    in a row (a whole mountain range of border tiles does), so it never
    counts as evidence that the run itself is broken."""
    per_tile = True


class Invalid(PipelineError):
    """A product failed its own validation: never published, always a bug."""
    kind = BUG


# Words that mean "the network", whatever library said them.
_NETWORK = re.compile(
    r"resolve|Resolution|nodename nor servname|timed? ?out|Timeout|Connection (?:reset|refused|aborted|broken)|"
    r"Max retries exceeded|RemoteDisconnected|IncompleteRead|ChunkedEncodingError|SSLError|EOF occurred|"
    r"CURL error|response_code=0|Temporary failure|Network is unreachable|overpass busy",
    re.I,
)
_HTTP = re.compile(r"\b(?:HTTP(?: response code)?|status)[: ]+(\d{3})\b|\b(\d{3}) (?:Client|Server) Error", re.I)


def http_status(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if isinstance(code, int):
        return code
    m = _HTTP.search(str(exc))
    return int(m.group(1) or m.group(2)) if m else None


def classify(exc: BaseException) -> str:
    if isinstance(exc, PipelineError):
        return exc.kind
    if isinstance(exc, (socket.gaierror, TimeoutError, ConnectionError)):
        return TRANSIENT
    code = http_status(exc)
    if code is not None:
        if code == 429 or code >= 500:
            return TRANSIENT
        if 400 <= code < 500:
            return UPSTREAM
    if _NETWORK.search(f"{type(exc).__name__}: {exc}"):
        return TRANSIENT
    return BUG


# Strip what differs between two occurrences of the same failure.
_NOISE = [
    (re.compile(r"https?://[^\s'\"]+"), "<url>"),
    (re.compile(r"N-?\d+\.\d+E-?\d+\.\d+"), "<tile>"),
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"/[\w./-]+"), "<path>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
]


def signature(exc: BaseException | str) -> str:
    """A short, stable key for grouping failures: the type and the message
    with URLs, tile ids, paths, hashes and numbers replaced by placeholders."""
    text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    text = text.splitlines()[0] if text else ""
    for pattern, repl in _NOISE:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()[:160]


def retry(fn: Callable[[], T], *, tries: int = 5, base: float = 5.0, cap: float = 300.0,
          on_retry: Callable[[int, BaseException, float], None] | None = None,
          sleep: Callable[[float], None] = time.sleep) -> T:
    """Call fn, retrying transient failures with exponential backoff (base,
    3·base, 9·base … capped). Anything else is raised at once."""
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — classified right here
            if classify(exc) != TRANSIENT or attempt == tries:
                raise
            wait = min(cap, base * 3 ** (attempt - 1))
            if on_retry:
                on_retry(attempt, exc, wait)
            sleep(wait)
    raise AssertionError("unreachable")
