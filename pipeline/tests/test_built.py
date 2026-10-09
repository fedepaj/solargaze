import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE / "wind")]

import numpy as np
from PIL import Image

import built_ghsl as bg
from tiles import Tile


class Grid(unittest.TestCase):
    def test_the_jrc_tiles_are_found_by_their_own_origin(self):
        self.assertEqual(bg.cell_of(41.9, 12.5), (5, 20))
        self.assertEqual(bg.cell_of(39.5, 12.5), (5, 20))      # R5 reaches down to 39.1°N, not 40°
        self.assertEqual(bg.cell_of(39.0, 12.5), (6, 20))
        self.assertEqual(bg.cells_for(Tile.parse("N41.75E12.25").bounds), [(5, 20)])
        self.assertEqual(len(bg.cells_for((9.5, 39.0, 10.5, 39.2))), 4)

    def test_epochs_stop_before_the_projections(self):
        self.assertEqual(bg.EPOCHS[0], 1975)
        self.assertEqual(bg.EPOCHS[-1], 2020)


class Build(unittest.TestCase):
    def test_shares_stack_oldest_first_and_round_trip(self):
        tile = Tile.parse("N45.00E09.00")
        def surface(epoch, bounds, cache):
            a = np.zeros((300, 300), np.float32)
            a[:, :150] = 3000.0 if epoch >= 2000 else 0.0   # half the tile built around 2000
            return a
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bg, "surface", surface):
            info = bg.build(tile, Path(d) / "out", Path(d))
            img = np.asarray(Image.open(Path(d) / "out" / "built.png"))
        self.assertEqual(img.shape, (300 * len(bg.EPOCHS), 300))
        share = bg.decode(img, len(bg.EPOCHS))
        k1995, k2000 = bg.EPOCHS.index(1995), bg.EPOCHS.index(2000)
        self.assertEqual(float(share[k1995].max()), 0.0)
        cell = (111_320 / 1200) ** 2 * np.cos(np.radians(45.1))
        self.assertAlmostEqual(float(share[k2000, 100, 10]), 3000 / cell, delta=0.006)
        self.assertAlmostEqual(float(share[-1, 100, 10]), float(share[k2000, 100, 10]))
        self.assertEqual(info["overviews"], [3, 9])
        self.assertGreater(info["grown_since_1975"], 0.2)
        self.assertEqual(info["years"], bg.EPOCHS)


if __name__ == "__main__":
    unittest.main()
