import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE / "wind")]
import numpy as np  # noqa: E402
import stored  # noqa: E402
from sg import errors  # noqa: E402
from sg.products.wind import Wind  # noqa: E402


class EmptyMask(unittest.TestCase):
    def test_a_few_tiny_buildings_are_no_data_not_a_bug(self):
        with tempfile.TemporaryDirectory() as d:
            stage = Path(d)
            stored.exact(np.zeros((8, 8), np.uint8), stage / "heights.webp")
            info = {"osm_buildings": 4, "heights_png": {"file": "heights.webp"}, "rows": 8, "cols": 8}
            fake = mock.Mock(build=mock.Mock(return_value=info))
            with mock.patch.dict(sys.modules, {"run": fake}):
                with self.assertRaises(errors.NoData):
                    Wind().build("N33.75E-116.00", stage, None)


if __name__ == "__main__":
    unittest.main()
