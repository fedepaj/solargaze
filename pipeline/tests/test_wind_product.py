import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import numpy as np  # noqa: E402
from sg import errors  # noqa: E402
from sg.products.wind import Wind  # noqa: E402


class Mask(unittest.TestCase):
    def build(self, count, mask):
        info = {"osm_buildings": count, "heights_png": {"file": "h.webp"}}
        mods = {"run": mock.Mock(build=mock.Mock(return_value=info)),
                "tiles": mock.Mock(Tile=mock.Mock(parse=lambda t: t)),
                "stored": mock.Mock(read=lambda f: mask)}
        with mock.patch.dict(sys.modules, mods), tempfile.TemporaryDirectory() as d:
            return Wind().build("N33.75E-116.00", Path(d), None)

    def test_a_few_sheds_are_no_data_not_a_bug(self):
        with self.assertRaises(errors.NoData):
            self.build(4, np.zeros((8, 8)))

    def test_many_buildings_with_an_empty_mask_still_reach_validation(self):
        self.assertEqual(self.build(500, np.zeros((8, 8)))["osm_buildings"], 500)

    def test_a_drawn_mask_passes(self):
        self.assertEqual(self.build(4, np.ones((8, 8)))["osm_buildings"], 4)


if __name__ == "__main__":
    unittest.main()
