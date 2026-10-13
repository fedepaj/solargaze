"""The month's composite must not step where the set of clear scenes changes."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import heat_landsat  # noqa: E402


def scenes():
    """Flat ground at 20 °C. Three warm days (+4) clear on the west, three
    cold days (-4) on the east, overlapping over the middle columns."""
    cube = np.full((6, 10, 30), np.nan, np.float32)
    for i, (a, b, off) in enumerate([(0, 20, 4.0)] * 3 + [(10, 30, -4.0)] * 3):
        cube[i, :, a:b] = 20.0 + off
    return cube


class MonthMedian(unittest.TestCase):
    def test_plain_median_steps(self):
        m = np.nanmedian(scenes(), axis=0)
        self.assertGreater(abs(m[:, 2] - m[:, 27]).max(), 7)

    def test_no_step_at_the_footprint_edge(self):
        m = heat_landsat.month_median(scenes())
        self.assertLess(float(np.nanmax(m) - np.nanmin(m)), 1.0)

    def test_keeps_the_level(self):
        m = heat_landsat.month_median(scenes()[:, :, 10:20] + 3)
        self.assertAlmostEqual(float(np.nanmedian(m)), 23.0, delta=0.5)


if __name__ == "__main__":
    unittest.main()
