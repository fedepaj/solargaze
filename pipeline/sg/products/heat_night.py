"""Surface heat at night: ECOSTRESS surface temperature by month (heat_ecostress.py).
Same grid and packing as the morning product, so it validates the same way."""

from __future__ import annotations

from pathlib import Path

from ..product import Context
from .heat import Heat


class HeatNight(Heat):
    name, version, subdir = "heat_night", 1, "heat_night"
    plausible_c = (-45, 45)      # a clear night in the Alps in January, a city in July
    card = {
        "theme": "heat", "variant": "night", "order": 20, "kind": "raster-months",
        "label": "Night", "title": "Surface heat on clear nights",
        "when": "nights", "span_c": 3, "resolution_m": 70,
        "source": "ECOSTRESS ECO_L2T_LSTE v002 land surface temperature (NASA LP DAAC)",
        "licence": "public domain",
        "note": "A per-pixel median of clear-night ECOSTRESS passes (21:00–05:00 local) since 2018, by month: the "
                "heat the city gives back at night, when the gap between dense blocks and parks is widest. "
                "Only between about 52° south and north, the reach of the Space Station it flies on.",
    }

    def unavailable(self) -> str | None:
        """Without Earthdata credentials every tile would fail alike. The plan
        job holds no credentials, only whether they are set
        (EARTHDATA_CONFIGURED, from the workflow)."""
        import os
        if (os.environ.get("EARTHDATA_TOKEN") or os.environ.get("EARTHDATA_CONFIGURED") == "true"
                or (os.environ.get("EARTHDATA_USERNAME") and os.environ.get("EARTHDATA_PASSWORD"))):
            return None
        return "no Earthdata credentials (EARTHDATA_TOKEN, or EARTHDATA_USERNAME and EARTHDATA_PASSWORD)"

    def build(self, tile: str, stage: Path, ctx: Context) -> dict:
        import heat_ecostress
        from tiles import Tile
        return heat_ecostress.build(Tile.parse(tile), stage)
