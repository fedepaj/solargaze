import sys
import unittest
from pathlib import Path

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import noise_osm as no  # noqa: E402

# A small synthetic place, 2 km a side near Rome: one primary road running
# north–south through the middle, and a long building 60 m east of it.
BOUNDS = (12.49, 41.89, 12.514, 41.908)
LAT0 = 41.899


def lon(dx_m):
    return 12.502 + dx_m / (111_320 * np.cos(np.radians(LAT0)))


ROAD = [("primary", [(lon(0), 41.885), (lon(0), 41.913)])]
WALL = [[(lon(60), 41.897), (lon(75), 41.897), (lon(75), 41.901), (lon(60), 41.901), (lon(60), 41.897)]]


class Model(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.grid = no.Grid(BOUNDS)
        cls.open = no.levels(no.fields(cls.grid, ROAD, [], no.EXCESS_FITTED), no.LW_FITTED)
        cls.walled = no.levels(no.fields(cls.grid, ROAD, WALL, no.EXCESS_FITTED), no.LW_FITTED)

    def at(self, levels, dx_m):
        x, y = self.grid.px(lon(dx_m), LAT0)
        r0, c0, _, _ = self.grid.inner
        return float(levels[int(y) - r0, int(x) - c0])

    def test_the_level_falls_with_distance(self):
        near, mid, far = (self.at(self.open, d) for d in (20, 100, 400))
        self.assertGreater(near, mid)
        self.assertGreater(mid, far)
        # A line source loses about 3 dB a doubling, more with the excess attenuation.
        self.assertTrue(5 < near - mid < 14, near - mid)

    def test_a_building_screens_what_is_behind_it(self):
        behind = 120   # metres east of the road, past the building
        drop = self.at(self.open, behind) - self.at(self.walled, behind)
        self.assertGreater(drop, 8, drop)
        # In front of it nothing changes but a little.
        self.assertLess(abs(self.at(self.open, 30) - self.at(self.walled, 30)), 1.0)

    def test_encoding_round_trips_to_half_a_decibel(self):
        l = np.array([[np.nan, 25.0, 61.3, 140.0]], np.float32)
        b = np.asarray(no._encode(l))
        self.assertEqual(b[0, 0], 0)
        decoded = no.ENCODING["byte1_db"] + (b[0, 1:].astype(float) - 1) * no.ENCODING["step_db"]
        self.assertTrue(np.allclose(decoded, l[0, 1:], atol=0.25))

    def test_bands_are_the_end_maps(self):
        self.assertEqual(list(no.band_index(np.array([54.9, 55, 64.9, 75]))), [0, 1, 2, 5])


if __name__ == "__main__":
    unittest.main()
