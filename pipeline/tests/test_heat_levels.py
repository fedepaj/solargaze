"""A month's composite must not step where the set of clear scenes changes (#24),
and must not step at a tile's edge either (what sank #25)."""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import heat_landsat as hl  # noqa: E402
from tiles import Tile  # noqa: E402


def swaths(rows=40, cols=120):
    """Ground at 20 °C rising 1 °C every 40 columns. Three warm days (+4) clear
    on the west two thirds, three cold days (-4) on the east two thirds."""
    ground = 20.0 + np.arange(cols, dtype=np.float32)[None, :] / 40
    cube = np.full((6, rows, cols), np.nan, np.float32)
    for i, (a, b, off) in enumerate([(0, 80, 4.0)] * 3 + [(40, 120, -4.0)] * 3):
        cube[i, :, a:b] = ground[:, a:b] + off
    return cube, ground


def levelled(cube, level, sigma=24):
    with np.errstate(all="ignore"):
        return np.nanmedian(cube - level[:, None, None], axis=0) + hl.level_field(cube, level, sigma)


class Levels(unittest.TestCase):
    def test_the_plain_median_steps_at_the_swath(self):
        cube, ground = swaths()
        plain = np.nanmedian(cube, axis=0) - ground
        self.assertGreater(abs(plain[:, 20] - plain[:, 100]).max(), 7)

    def test_the_levels_are_the_days(self):
        cube, _ = swaths()
        level = hl.scene_levels(cube, min_px=1)
        self.assertAlmostEqual(float(level[0] - level[5]), 8.0, delta=0.5)

    def test_no_step_where_the_swaths_change(self):
        cube, ground = swaths()
        m = levelled(cube, hl.scene_levels(cube, min_px=1)) - ground
        step = np.abs(np.diff(np.nanmedian(m, axis=0)))
        self.assertLess(float(step.max()), 0.5)          # a ramp at most, not 8 °C at once

    def test_a_constant_in_the_levels_cancels(self):
        # Two tiles fitting the same days get levels that differ by a
        # constant; what they write must not.
        cube, _ = swaths()
        level = hl.scene_levels(cube, min_px=1)
        np.testing.assert_allclose(levelled(cube, level), levelled(cube, level + 5.0), atol=1e-4)

    def test_the_field_lands_on_the_tile(self):
        window = (9.75, 48.5, 10.5, 49.25)
        field = np.tile(np.arange(93, dtype=np.float32), (93, 1))     # rises west to east
        on = hl.onto(field, window, (10.0, 48.75, 10.25, 49.0), (9, 9))
        self.assertTrue(30 < on[4, 0] < on[4, 8] < 62)                 # the middle third of the window


class Build(unittest.TestCase):
    def test_build_takes_the_days_out(self):
        tile = Tile.parse("N48.75E10.00")
        shape = tile.shape(hl.DEG_PER_PX)
        days = [SimpleNamespace(id=f"LC08_{i}", datetime=datetime(2020 + i, 10, 5), assets={"lwir11": None},
                                properties={"eo:cloud_cover": 1.0}) for i in range(6)]
        warm = lambda i: i < 3      # noqa: E731

        def fine(it, t, shp):
            i = int(it.id[-1]); a = np.full(shp, np.nan, np.float32); c = shp[1]
            if warm(i):
                a[:, : 2 * c // 3] = 24.0
            else:
                a[:, c // 3:] = 16.0
            return a

        def coarse(it, t, window, shp):
            i = int(it.id[-1]); a = np.full(shp, np.nan, np.float32); c = shp[1]
            if warm(i):
                a[:, : 5 * c // 9] = 24.0       # up to two thirds of the tile, in the window
            else:
                a[:, 4 * c // 9:] = 16.0
            return a

        catalogue = mock.Mock()
        catalogue.search.return_value.items.return_value = days
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(hl.pystac_client.Client, "open", return_value=catalogue), \
                mock.patch.object(hl, "CACHE", Path(tmp) / "scenes"), \
                mock.patch.object(hl, "cached_scene", fine), mock.patch.object(hl, "cached_coarse", coarse):
            plain = hl.build(tile, Path(tmp) / "plain")
            with_levels = hl.build(tile, Path(tmp) / "levels", levels=True)
            import stored
            rows = plain["rows"]
            a = stored.read(Path(tmp) / "plain" / "months.avif")[9 * rows:10 * rows].astype(np.float32)
            b = stored.read(Path(tmp) / "levels" / "months.avif")[9 * rows:10 * rows].astype(np.float32)
        col = lambda img: np.median(img, axis=0) * hl.SCALE[1]   # noqa: E731
        jump = lambda img: float(np.abs(col(img)[4:] - col(img)[:-4]).max())   # over 360 m   # noqa: E731
        # The plain median jumps 4 °C twice where the swaths change (24, 20,
        # 16); with the days taken out what is left of their difference is a
        # ramp tens of kilometres wide, never a jump.
        self.assertGreater(jump(a), 3.5)
        self.assertLess(jump(b), 0.5)
        self.assertIn("levels", with_levels)


if __name__ == "__main__":
    unittest.main()
