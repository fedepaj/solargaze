"""
The Product interface, and the one way a product reaches the data folder.

A product builds into a staging directory, is validated there, and is then
moved into ``data/tiles/<tile>/<subdir>`` in one rename, its meta entry
written after — so a crash at any point leaves the previous version or
nothing, never a half-written one recorded as done (ARCHITECTURE.md,
principle 1).
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import errors
from .state import DONE, EMPTY, FAILED, Record


@dataclass
class Context:
    """What a product may want from the run: where things live, the run's
    log, and whatever prepare() left for the builds."""
    data_dir: Path
    cache_dir: Path
    run: object = None
    shared: dict = field(default_factory=dict)
    # Called with a tile id before anything reads or writes the tile: on a
    # runner with an empty data folder, this brings down the tile's meta.json
    # so that a new product is added to what is published, not put in its place.
    before_tile: object = None


class Product:
    name: str = ""
    version: int = 1
    subdir: str = ""             # under data/tiles/<tile>/; "" when the product has no folder of its own
    depends: tuple[str, ...] = ()  # products that must be done on the tile first
    max_attempts: int = 6         # failed this many times in a row: leave it until the code changes
    refresh_days: float | None = None  # rebuild a done tile after this long (sources that update)
    min_buildings: int = 0        # skip tiles whose wind product counted fewer OSM buildings
    # What the app is told about the product (catalog.json, sg/catalog.py):
    # the theme and variant it is shown under — none for a product other
    # products are built on — the kind of data, which decides how the app
    # reads and draws it, and the words and credits that go with it.
    card: dict = {}

    # -- what to do -----------------------------------------------------------

    def reason(self, tile: str, rec: Record, *, git: str = "") -> str | None:
        """Why this tile needs (re)building, or None. The default policy:
        missing → build; older version → rebuild; empty → only on a new
        version; failed → retry, unless it is a bug and the code has not
        changed, or it has failed too many times."""
        if not rec.status:
            return "missing"
        if rec.version < self.version:
            return f"version {rec.version}→{self.version}"
        if rec.status == DONE:
            if self.refresh_days is not None and rec.age_days > self.refresh_days:
                return "refresh"
            return None
        if rec.status == EMPTY:
            return None
        if rec.status == FAILED:
            if rec.kind == errors.BUG and rec.git == git:
                return None   # same code, same bug
            if rec.sig.startswith("NotCovered"):
                # Waiting for a neighbour's pieces, which may come any night;
                # asking again costs nothing, so it is never given up on.
                return "retry: border tile, waiting for a neighbour's pieces"
            if rec.attempts >= self.max_attempts and rec.git == git:
                return None
            return f"retry {rec.kind or 'failure'} (attempt {rec.attempts + 1})"
        return None

    def applies(self, tile: str, ctx: Context) -> str | None:
        """Why this product has no business on this tile, or None. The default:
        too few buildings, by the count the wind product recorded — which is
        what keeps a continent of fields and sea out of the expensive products."""
        if not self.min_buildings:
            return None
        from tiles import Tile, read_meta
        n = read_meta(Tile.parse(tile)).get("products", {}).get("wind", {}).get("osm_buildings")
        if n is None:
            return None   # unknown yet: the depends check decides
        return f"{n} OSM buildings, under {self.min_buildings}" if n < self.min_buildings else None

    # -- what a product implements -------------------------------------------

    def prepare(self, tiles: list[str], ctx: Context) -> None:
        """Region-wide inputs before the per-tile builds: download an extract,
        fold a reanalysis, fit a model. Raises to fail all of these tiles."""

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        """Write the product's files into stage/ and return its meta entry
        (file names relative to the tile folder). Raise errors.NoData when
        there is nothing to build here."""
        raise NotImplementedError

    def validate(self, tile: str, stage: Path, info: dict) -> None:
        """Raise errors.Invalid for anything that must not be published."""

    def inputs(self, tile: str, ctx: Context) -> dict:
        """Stamps of what the build read, for the state record."""
        return {}

    # -- the one way into the data folder -------------------------------------

    def install(self, tile: str, stage: Path, info: dict, ctx: Context, write_meta) -> list[Path]:
        """Swap stage/ in for the product's folder and record the meta entry.
        Returns the files now in place, for publishing."""
        base = ctx.data_dir / tile
        base.mkdir(parents=True, exist_ok=True)
        placed: list[Path] = []
        if self.subdir:
            final = base / self.subdir
            old = base / f".{self.subdir}.old-{uuid.uuid4().hex[:6]}"
            if final.exists():
                final.rename(old)
            try:
                stage.rename(final)
            except BaseException:
                if old.exists() and not final.exists():
                    old.rename(final)  # put the previous version back
                raise
            shutil.rmtree(old, ignore_errors=True)
            placed = [p for p in final.rglob("*") if p.is_file()]
        write_meta(tile, self.name, info)
        placed.append(base / "meta.json")
        return placed

    def staging(self, tile: str, ctx: Context) -> Path:
        stage = ctx.data_dir / tile / f".{self.subdir or self.name}.stage-{uuid.uuid4().hex[:6]}"
        stage.mkdir(parents=True)
        return stage
