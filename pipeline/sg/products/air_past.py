"""The air of earlier years (air_eac4.py): CAMS global reanalysis monthly
means, 2003–2019, on its 80 km grid. Not a tile product: one small file for
Europe, built on a machine with an ADS key and published beside the
catalog. It is here for its card — how the app finds and draws it — and is
never planned by the runner."""

from __future__ import annotations

from ..product import Product


class AirPast(Product):
    name, version, subdir = "air_past", 1, "air_past"
    card = {
        "theme": "air", "variant": "past", "order": 20, "kind": "grid-past",
        "label": "Year", "title": "The air of earlier years", "years": [2003, 2019], "coarse": True,
        "resolution_m": 80000, "meta": "air_past/meta.json",
        "source": "CAMS global reanalysis (EAC4) monthly means, Copernicus Atmosphere Monitoring Service",
        "licence": "Copernicus licence",
        "note": "Before 2020, the month of the year on the slider from the CAMS global reanalysis: a model on an "
                "80 km grid, a region's background rather than a street — NO₂ especially reads far below what a "
                "road saw. There to show how a region's air changed since 2003, drawn in its own coarse blocks.",
    }

    def unavailable(self) -> str | None:
        return "built by air_eac4.py on a machine with an ADS key, not by the runner"
