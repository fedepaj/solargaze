import sys
import unittest
from pathlib import Path

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import heat_landsat_l1 as l1  # noqa: E402

MTL = """    RADIANCE_MULT_BAND_10 = 3.3420E-04
    RADIANCE_ADD_BAND_10 = 0.10000
    K1_CONSTANT_BAND_10 = 774.8853
    K2_CONSTANT_BAND_10 = 1321.0789
"""


class Physics(unittest.TestCase):
    def test_brightness_temperature_of_a_known_radiance(self):
        mtl = l1.parse_mtl(MTL)
        # A radiance of 10 W/(m²·sr·µm) is about 303 K in band 10.
        dn = np.array([(10 - 0.1) / 3.342e-4, 0])
        bt = l1.brightness_temperature(dn, mtl)
        self.assertAlmostEqual(bt[0], 1321.0789 / np.log(774.8853 / 10 + 1), places=2)
        self.assertTrue(np.isnan(bt[1]))   # DN 0 is no data

    def test_emissivity_by_cover_and_the_correction_it_makes(self):
        e = l1.emissivity(np.array([-0.3, 0.1, 0.35, 0.8]))
        self.assertEqual(list(np.round(e, 3)), [0.991, 0.973, 0.987, 0.99])
        # A less emissive surface is hotter than its brightness temperature.
        t = l1.surface_temperature(np.array([300.0, 300.0]), np.array([0.973, 0.99]))
        self.assertGreater(t[0], t[1])
        self.assertTrue(26.8 < t[1] < 27.8)

    def test_an_mtl_missing_a_constant_is_upstream(self):
        from sg.errors import Upstream
        with self.assertRaises(Upstream):
            l1.parse_mtl("RADIANCE_MULT_BAND_10 = 1")


if __name__ == "__main__":
    unittest.main()
