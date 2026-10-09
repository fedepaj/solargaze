"""Surface heat on clear mornings in earlier decades: Landsat 5 (and 7) by
month, one product a decade since 1984 (heat_landsat.py with their years).
The app shows the decade the year slider is in; the same grid, packing and
checks as the present morning, so it validates the same way."""

from __future__ import annotations

from pathlib import Path

from ..product import Context
from .heat import Heat

DECADES = ((1984, 1993, ("landsat-5",)),
           (1994, 2003, ("landsat-5", "landsat-7")),
           (2004, 2013, ("landsat-5", "landsat-7")))
MAX_PER_MONTH = 8   # the clearest scenes of each calendar month in the decade: enough for a median


class HeatPast(Heat):
    version = 1

    def __init__(self, start: int, end: int, platforms: tuple[str, ...]):
        self.start, self.end, self.platforms = start, end, platforms
        self.name = self.subdir = f"heat_{start}"
        sats = " and ".join({"landsat-5": "Landsat 5 TM", "landsat-7": "Landsat 7 ETM+"}[p] for p in platforms)
        self.card = {
            "theme": "heat", "variant": f"morning-{start}", "order": 10 + (2014 - start) // 10, "kind": "raster-months",
            "label": f"Mornings {start}–{end % 100:02d}", "title": f"Surface heat on clear mornings, {start}–{end}",
            "when": f"mornings {start}–{end}", "daypart": "day", "years": [start, end],
            "span_c": 6, "resolution_m": 120, "coarse": True,
            "source": f"{sats} Collection 2 Level-2 surface temperature (USGS), via Microsoft Planetary Computer",
            "licence": "public domain",
            "note": f"The same morning surface heat for {start}–{end}: a per-pixel median of the clearest "
                    f"{sats} passes of each month in the decade, read at 120 m. Earlier satellites passed a little "
                    "earlier in the morning (about 9:30–10:00), so a decade-to-decade difference is partly the "
                    "clock and the weather of those years, not only the city.",
        }

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import heat_landsat
        from tiles import Tile
        return heat_landsat.build(Tile.parse(tile), stage, years=f"{self.start}-01-01/{self.end}-12-31",
                                  platforms=self.platforms, max_per_month=MAX_PER_MONTH, subdir=self.subdir)


PAST = [HeatPast(*d) for d in reversed(DECADES)]   # the nearest decade first
