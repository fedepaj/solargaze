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
from sg.errors import NoData, Upstream  # noqa: E402
from sg.products.heat_past import reference_months  # noqa: E402
from tiles import Tile  # noqa: E402

TILE = Tile.parse("N41.25E1.25")


def scene(n, month, cover, celsius):
    return SimpleNamespace(id=f"LT05_{month:02d}_{n:02d}", datetime=datetime(1988, month, 1 + n),
                           properties={"eo:cloud_cover": cover}, assets={"lwir": None}, celsius=celsius)


def build(scenes, reads=None, **kw):
    """heat_landsat.build on made-up scenes: no catalogue, no archive."""
    catalogue = mock.Mock()
    catalogue.search.return_value.items.return_value = scenes

    def read(it, tile, shape):
        if reads is not None:
            reads.append(it.id)
        return np.full(shape, it.celsius, np.float32)

    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(hl.pystac_client.Client, "open", return_value=catalogue), \
            mock.patch.object(hl, "CACHE", Path(tmp) / "scenes"), \
            mock.patch.object(hl, "cached_scene", read):
        return hl.build(TILE, Path(tmp) / "out", **kw)


class Reference(unittest.TestCase):
    def test_a_month_the_present_lacks_is_between_its_neighbours(self):
        months = {f"{m:02d}": {"tile_median_c": float(m)} for m in range(1, 13) if m not in (2, 12)}
        ref = reference_months({"products": {"heat": {"months": months}}})
        self.assertEqual(ref[2], 2.0)
        self.assertEqual(ref[12], 6.0)    # between November (11) and January (1), across the year's end
        self.assertEqual(ref[7], 7.0)

    def test_no_present_heat_is_upstream(self):
        with self.assertRaises(Upstream):
            reference_months({"products": {}})


class CloudCheck(unittest.TestCase):
    # January: the four "clearest" scenes are a deck of cloud the mask took
    # for ground, the next six are ground. Cap of four a month.
    SCENES = [scene(i, 1, 0.0, -50.0) for i in range(4)] + [scene(4 + i, 1, 10.0 + i, 4.0 + i) for i in range(6)]

    def test_without_the_present_the_clouds_are_the_month(self):
        info = build(self.SCENES, max_per_month=4)
        self.assertEqual(info["months"]["01"]["tile_median_c"], -50.0)

    def test_the_present_drops_the_clouds_and_keeps_the_cap_of_ground(self):
        reads = []
        info = build(self.SCENES, reads, max_per_month=4, reference_c={m: 6.0 for m in range(1, 13)})
        self.assertEqual(len(reads), 8)                 # four, then four more for the four clouds
        jan = info["months"]["01"]
        self.assertEqual((jan["scenes"], jan["clouds_dropped"]), (4, 4))
        self.assertEqual(jan["tile_median_c"], 5.5)     # median of 4, 5, 6, 7
        self.assertEqual(info["cloud_check"]["colder_c"], hl.COLDER_C)

    def test_a_cold_day_that_is_ground_is_kept(self):
        days = [scene(i, 1, 0.0, 6.0 - hl.COLDER_C + 1) for i in range(3)]
        info = build(days, max_per_month=4, reference_c={m: 6.0 for m in range(1, 13)})
        self.assertEqual(info["months"]["01"]["clouds_dropped"], 0)

    def test_scenes_without_three_clear_looks_anywhere_are_no_data(self):
        with self.assertRaises(NoData):
            build([scene(0, 1, 0.0, 5.0), scene(1, 1, 0.0, 5.0)], max_per_month=4)


class BrokenFile(unittest.TestCase):
    def test_a_broken_file_is_a_missing_scene_not_a_failure(self):
        def broken(item, tile, shape):
            raise RuntimeError("'/vsicurl/https://x/ST_B10.TIF' not recognized as being in a supported file format.")

        def offline(item, tile, shape):
            raise RuntimeError("HTTPSConnectionPool: Max retries exceeded")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(hl, "CACHE", Path(tmp)):
            with mock.patch.object(hl, "read_scene", broken):
                arr = hl.cached_scene(scene(0, 1, 0.0, 5.0), TILE, (4, 4))
                self.assertTrue(arr is not None and np.isnan(arr).all())
            with mock.patch.object(hl, "read_scene", offline):
                self.assertIsNone(hl.cached_scene(scene(1, 1, 0.0, 5.0), TILE, (4, 4)))


class Rescale(unittest.TestCase):
    def test_a_clipped_tile_is_rebuilt_once_and_no_other(self):
        from sg.products.heat import Heat, clipped
        from sg.products.heat_night import HeatNight
        from sg.state import DONE, Record
        hot = sorted(clipped())[0]
        old = Record(tile=hot, product="heat", status=DONE, version=Heat.version)
        self.assertTrue(Heat().reason(hot, old).startswith("rescale"))
        rebuilt = Record(tile=hot, product="heat", status=DONE, version=Heat.version, inputs={"scale": [-10.0, 0.3]})
        self.assertIsNone(Heat().reason(hot, rebuilt))
        self.assertIsNone(Heat().reason("N60.00E25.00", Record(tile="N60.00E25.00", product="heat", status=DONE,
                                                                version=Heat.version)))
        night = Record(tile=hot, product="heat_night", status=DONE, version=HeatNight.version)
        self.assertIsNone(HeatNight().reason(hot, night))


if __name__ == "__main__":
    unittest.main()
