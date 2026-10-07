"""
What the app is told about the products: ``catalog.json``, beside
``index.json`` at the root of the tiles. One entry per product, from its
``card`` (sg/product.py): the theme and variant it is shown under, the kind
of data — which decides how the app reads and draws it — and the words and
credits that go with it. The app builds its theme variants from this, so a
new product of a kind it already reads appears without a change to the app.
"""

from __future__ import annotations

import json
from pathlib import Path

from .products import ORDER, PRODUCTS

VERSION = 1


def catalog() -> dict:
    return {
        "version": VERSION,
        "products": {name: {**PRODUCTS[name].card, "version": PRODUCTS[name].version}
                     for name in ORDER if PRODUCTS[name].card},
    }


def write(data_dir: Path) -> Path:
    p = data_dir / "catalog.json"
    data_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(catalog(), indent=2, ensure_ascii=False) + "\n")
    return p
