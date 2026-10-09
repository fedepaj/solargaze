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


class Mosaic(unittest.TestCase):
    def test_a_light_spreads_glow_around_it_and_the_tile_reads_it(self):
        def cell(path):
            a = np.zeros((2400, 2400), np.float32)
            if path == "h19v04":
                a[1224, 1224] = 1000.0         # a town at 44.9°N 15.1°E (rows from 50°N, columns from 10°E)
            return a
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(lv, "_file", lambda year, h, v, cache: f"h{h}v{v:02d}"), \
                mock.patch.object(lv, "_cell", cell):
            near, _ = lv.artificial(2025, Tile.parse("N44.75E15.00").bounds, Path(d))
            far, _ = lv.artificial(2025, Tile.parse("N44.75E16.50").bounds, Path(d))
            info = lv.build(Tile.parse("N44.75E15.00"), Path(d) / "out", Path(d), year=2025)
            img = np.asarray(Image.open(Path(d) / "out" / "sky.png"))
        self.assertEqual(near.shape, (60, 60))
        self.assertGreater(near.max(), 10 * far.max())
        self.assertGreater(far.max(), 0)                          # 110 km away the glow still reaches
        self.assertEqual(img.shape, (info["rows"], info["cols"]))
        self.assertLess(info["mag_min"], info["mag_max"])
        self.assertEqual(info["year"], 2025)


if __name__ == "__main__":
    unittest.main()
