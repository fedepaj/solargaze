import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
from sg import errors  # noqa: E402
from sg.products.wind import Wind  # noqa: E402


class Lone(unittest.TestCase):
    def test_a_hut_too_small_for_a_cell_is_nodata_not_a_failed_validation(self):
        fake = mock.Mock()
        fake.build.return_value = {"osm_buildings": 1, "mask_cells": 0}
        with mock.patch.dict(sys.modules, {"run": fake}):
            with self.assertRaises(errors.NoData):
                Wind().build("N67.75E27.00", Path("."), None)

    def test_a_tile_with_a_mask_is_built(self):
        fake = mock.Mock()
        fake.build.return_value = {"osm_buildings": 5, "mask_cells": 3}
        with mock.patch.dict(sys.modules, {"run": fake}):
            self.assertEqual(Wind().build("N67.75E27.00", Path("."), None)["mask_cells"], 3)


if __name__ == "__main__":
    unittest.main()
