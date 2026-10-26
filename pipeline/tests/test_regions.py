import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sg import regions  # noqa: E402


class Regions(unittest.TestCase):
    def test_names_are_unique_and_europes_unchanged(self):
        self.assertEqual(regions.name_of("europe/germany"), "germany")          # its pieces are on the bucket
        self.assertEqual(regions.name_of("north-america/us/georgia"), "us-georgia")
        self.assertEqual(regions.name_of("asia/japan/kanto"), "japan-kanto")
        self.assertEqual(regions.name_of("north-america/mexico"), "mexico")
        every = [p for v in regions.SCOPES.values() for p in v] + [p for v in regions.NEIGHBOURS.values() for p in v]
        names = {regions.name_of(p): p for p in every}
        self.assertEqual(len(names), len(set(every)))

    def test_the_world_does_without_noise(self):
        names = ["wind", "noise", "light", "heat"]
        self.assertEqual(regions.products_for("world", names), ["wind", "light", "heat"])
        self.assertEqual(regions.products_for("europe", names), names)

    def test_no_region_across_the_date_line(self):
        self.assertNotIn("north-america/us/alaska", regions.WORLD)


if __name__ == "__main__":
    unittest.main()
