import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE / "wind")]

import numpy as np
from PIL import Image

import light_viirs as lv
from tiles import Tile


class Units(unittest.TestCase):
    def test_natural_sky_is_22_and_ten_times_is_2_5_brighter(self):
        self.assertAlmostEqual(float(lv.mag(np.float32(0))), 22.0, places=5)
        self.assertAlmostEqual(float(lv.mag(np.float32(9 * lv.NATURAL_MCD))), 19.5, places=4)

    def test_encoding_round_trips_to_a_fortieth_of_a_magnitude(self):
        m = np.array([[22.0, 21.3], [18.05, 15.0]], np.float32)
        b = np.asarray(lv._encode(m))
        e = lv.ENCODING
        back = e["value_at_byte1"] + (b.astype(np.float32) - 1) * e["step"]
        self.assertTrue((b >= 1).all())
        self.assertLess(float(np.abs(back[:, :1] - m[:, :1]).max()), 0.0126)
        self.assertEqual(int(b[1, 1]), 255)   # brighter than the scale holds: clipped, not wrapped


class Kernel(unittest.TestCase):
    def test_kernel_is_symmetric_and_falls_with_distance(self):
        k = lv.kernel(45.0)
        cy, cx = k.shape[0] // 2, k.shape[1] // 2
        self.assertTrue(np.allclose(k, k[::-1, ::-1]))
        self.assertGreater(k[cy, cx], k[cy, cx + 10])
        self.assertGreater(k[cy, cx + 10], k[cy, cx + 100])
        self.assertGreater(k[cy, cx + 100], 0)
        self.assertEqual(float(k[0, 0]), 0.0)   # the corners are beyond its reach

    def test_cells_for_a_window_across_cell_edges(self):
        self.assertEqual(lv.cells_for((9.5, 39.5, 10.5, 40.5)), [(18, 4), (18, 5), (19, 4), (19, 5)])
        self.assertEqual(lv.cells_for((12.0, 41.0, 12.25, 41.25)), [(19, 4)])
        w, s, e, n = lv.window((12.0, 41.0, 12.25, 41.25))
        self.assertAlmostEqual(n - 41.25, 200 / 111.32, delta=1 / 120)
        self.assertEqual(round(w * 120), w * 120)                  # on the 30″ grid


class Mosaic(unittest.TestCase):
    def test_a_light_spreads_glow_around_it_year_by_year(self):
        def cell(year, h, v, cache):
            if (h, v) != (19, 4):
                return None                     # open sea: no composite
            a = np.zeros((1200, 1200), np.float32)
            a[612, 612] = 1000.0 if year == 2013 else 4000.0   # a town at 44.9°N 15.1°E that grew
            return a
        with tempfile.TemporaryDirectory() as d, mock.patch.object(lv, "_cell", cell):
            near, rad = lv.artificial([2012, 2013, 2014], Tile.parse("N44.75E15.00").bounds, Path(d))
            far, _ = lv.artificial([2012, 2013, 2014], Tile.parse("N44.75E16.50").bounds, Path(d))
            info = lv.build(Tile.parse("N44.75E15.00"), Path(d) / "out", Path(d), last_year=2014)
            img = np.asarray(Image.open(Path(d) / "out" / "sky.png"))
        self.assertEqual(near.shape, (3, 60, 60))
        self.assertEqual(rad.shape, (3, 30, 30))
        self.assertAlmostEqual(float(near[2].max() / near[1].max()), 4.0, places=3)   # the glow is linear in the lights
        self.assertGreater(near.max(), 10 * far.max())
        self.assertGreater(far.max(), 0)                          # 110 km away the glow still reaches
        self.assertEqual(info["years"], [2012, 2013, 2014])
        self.assertEqual(img.shape, (3 * info["rows"], info["cols"]))
        self.assertLess(info["mag_median_by_year"]["2014"], info["mag_median_by_year"]["2013"])

    def test_the_store_serves_a_cell_once_downloaded_and_the_sea_is_remembered(self):
        class Store(dict):
            def get(self, k): return dict.get(self, k)
            def put(self, k, v): self[k] = v
        store = Store()
        with tempfile.TemporaryDirectory() as d:
            h5 = Path(d) / "x.h5"
            with mock.patch.object(lv, "_download", side_effect=[h5, None]) as dl, \
                    mock.patch.object(lv, "_reduce", return_value=np.ones((1200, 1200), np.float32)):
                h5.write_bytes(b"x")
                a = lv.r30(2020, 19, 4, Path(d) / "a", store)
                sea = lv.r30(2020, 30, 9, Path(d) / "a", store)
                self.assertEqual(dl.call_count, 2)
                self.assertFalse(h5.exists())                         # the 15″ file is dropped
                again = lv.r30(2020, 19, 4, Path(d) / "b", store)     # another runner: from the store
                self.assertIsNone(lv.r30(2020, 30, 9, Path(d) / "b", store))
                self.assertEqual(dl.call_count, 2)
        self.assertEqual(float(a.sum()), 1200 * 1200)
        self.assertTrue(np.array_equal(a, again))
        self.assertIsNone(sea)


if __name__ == "__main__":
    unittest.main()
