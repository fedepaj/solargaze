"""The air by year (air_eac4.py): CAMS global reanalysis monthly means,
2003–2024, on its 80 km grid; the app also shows its last year wherever no
street has been modelled. Not a tile product: a few small files, one an area
(Europe, North America, Japan), built on a machine with an ADS key and
published beside the catalog. It is here for its card — how the app finds and draws it — and is
never planned by the runner."""

from __future__ import annotations

from ..product import Product


class AirPast(Product):
    name, version, subdir = "air_past", 1, "air_past"
    card = {
        "theme": "air", "variant": "past", "order": 20, "kind": "grid-past",
        "label": "Year", "title": "The air by year", "years": [2003, 2024], "coarse": True,
        "resolution_m": 80000, "meta": "air_past/meta.json",
        "source": "CAMS global reanalysis (EAC4) monthly means, Copernicus Atmosphere Monitoring Service",
        "licence": "Copernicus licence",
        "note": "The month of the year on the slider, 2003–2024, from the CAMS global reanalysis — and the air "
                "of the present wherever no street has been modelled, from its last year: a model on an 80 km grid, "
                "a region's background rather than a street, NO₂ especially far below what a road saw. Over Europe, "
                "North America and Japan, drawn in its own coarse blocks.",
    }

    def unavailable(self) -> str | None:
        return "built by air_eac4.py on a machine with an ADS key, not by the runner"
