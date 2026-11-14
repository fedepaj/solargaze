import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd  # noqa: E402
import air_stations  # noqa: E402
from sg.errors import NODATA, UPSTREAM, classify  # noqa: E402


def meta():
    return pd.DataFrame({"SamplingPoint": ["GB/x"], "Latitude": [51.0], "Longitude": [0.0]})


class CountryWithoutPoints(unittest.TestCase):
    """GB's stations failed the run with KeyError: 'point' when no sampling point had data."""

    def build(self, urls, one):
        with mock.patch.object(air_stations, "station_meta", return_value=meta()), \
                mock.patch.dict(sys.modules, {"air_cams_bulk": mock.Mock(zone_at=lambda a, o: "Europe/London")}), \
                mock.patch.object(air_stations, "series_urls", return_value=urls), \
                mock.patch.object(air_stations, "one", one):
            return air_stations.build_table("GB", [2023], ["pm2_5"], workers=1)

    def test_no_points_is_no_data(self):
        with self.assertRaises(Exception) as cm:
            self.build([], lambda *a, **k: None)
        self.assertEqual(classify(cm.exception), NODATA)

    def test_every_file_unreadable_is_upstream(self):
        def boom(*a, **k):
            raise ValueError("bad parquet")
        with self.assertRaises(Exception) as cm:
            self.build(["u/1", "u/2"], boom)
        self.assertEqual(classify(cm.exception), UPSTREAM)


if __name__ == "__main__":
    unittest.main()
