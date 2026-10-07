import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
from sg import catalog  # noqa: E402

# The kinds of data the app knows how to read and draw (js/atmo/kinds.js).
APP_KINDS = {"raster-months", "street-air", "climatology", "building-mask"}


class Catalog(unittest.TestCase):
    def test_every_product_says_what_the_app_needs(self):
        products = catalog.catalog()["products"]
        self.assertIn("heat", products)
        for name, card in products.items():
            with self.subTest(product=name):
                self.assertIn(card["kind"], APP_KINDS)
                for key in ("title", "source", "licence", "note", "resolution_m", "version"):
                    self.assertTrue(card.get(key), key)
                if card.get("theme"):
                    for key in ("variant", "label", "order"):
                        self.assertIn(key, card)

    def test_variants_of_a_theme_are_distinct(self):
        seen = set()
        for card in catalog.catalog()["products"].values():
            if card.get("theme"):
                self.assertNotIn((card["theme"], card["variant"]), seen)
                seen.add((card["theme"], card["variant"]))


if __name__ == "__main__":
    unittest.main()
