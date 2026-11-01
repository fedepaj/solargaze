import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import air_stations  # noqa: E402


class JoinMeta(unittest.TestCase):
    META = pd.DataFrame({"SamplingPoint": ["P1"], "AirQualityStation": ["S1"], "code": [8],
                         "Longitude": [-0.1], "Latitude": [51.5]})

    def test_no_point_in_the_years_is_an_empty_table(self):
        # GB has no data in the EEA service after 2019: that was a KeyError: 'point'
        t = air_stations.join_meta([], self.META)
        self.assertTrue(t.empty)
        self.assertIn("point", t.columns)
        self.assertIn("Latitude", t.columns)

    def test_points_are_joined(self):
        rows = [{"point": "P1", "var": "nitrogen_dioxide"}]
        t = air_stations.join_meta(rows, self.META)
        self.assertEqual(list(t.AirQualityStation), ["S1"])


if __name__ == "__main__":
    unittest.main()
