"""A country whose sampling points report nothing in the years (GB stopped
reporting to the EEA) is NoData, not a KeyError that stops the run."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import air_stations  # noqa: E402
from sg import errors  # noqa: E402


class NoPointsTest(unittest.TestCase):
    def test_no_rows_is_nodata(self):
        meta = pd.DataFrame({"SamplingPoint": ["GB/x"], "Latitude": [51.5], "Longitude": [-0.1]})
        cams = mock.Mock(zone_at=lambda a, o: "Europe/London")
        with mock.patch.object(air_stations, "station_meta", return_value=meta), \
                mock.patch.object(air_stations, "series_urls", return_value=[]), \
                mock.patch.dict(sys.modules, {"air_cams_bulk": cams}):
            with self.assertRaises(errors.NoData) as cm:
                air_stations.build_table("GB", [2020, 2024], ["pm10"])
        self.assertEqual(errors.classify(cm.exception), errors.NODATA)


if __name__ == "__main__":
    unittest.main()
