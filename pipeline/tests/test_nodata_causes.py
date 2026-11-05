"""Two failures of run 41990359530 that were no bug: a country the EEA holds
nothing of, and a tile whose few buildings fill no cell."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import air_stations  # noqa: E402
import stored  # noqa: E402
from sg import errors  # noqa: E402
from sg.products.wind import Wind  # noqa: E402


class NothingToSay(unittest.TestCase):
    def test_a_country_without_data_in_the_years_is_nodata_not_a_keyerror(self):
        import pandas as pd
        meta = pd.DataFrame({"SamplingPoint": [], "Latitude": [], "Longitude": []})
        with mock.patch.object(air_stations, "station_meta", return_value=meta), \
                mock.patch.dict(sys.modules, {"air_cams_bulk": mock.Mock(zone_at=lambda a, o: "Europe/London")}), \
                mock.patch.object(air_stations, "series_urls", return_value=[]):
            with self.assertRaises(errors.NoData):
                air_stations.build_table("GB", [2020, 2021], ["pm10"])

    def test_buildings_that_fill_no_cell_are_nodata_not_invalid(self):
        import run as wind_run
        with tempfile.TemporaryDirectory() as d:
            stage = Path(d)
            stored.exact(np.zeros((4, 4), np.uint8), stage / "heights.webp")
            info = {"osm_buildings": 1, "heights_png": {"file": "heights.webp"}}
            with mock.patch.object(wind_run, "build", return_value=info):
                with self.assertRaises(errors.NoData):
                    Wind().build("N58.75E12.75", stage, None)


if __name__ == "__main__":
    unittest.main()
