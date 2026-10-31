import io
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
from sg import errors, stations  # noqa: E402
from tests.test_border import FakeRemote  # noqa: E402

ROME = "N41.75E12.25"          # tiles of the region
SITES = pd.DataFrame({
    "station": ["IT001", "IT002", "IT003", "IT004", "IT001"],
    "var": ["nitrogen_dioxide"] * 4 + ["pm10"],
    "lat": [41.90, 41.80, 45.46, 41.85, 41.90], "lon": [12.45, 12.30, 9.19, 12.40, 12.45],
})


def with_table(remote):
    buf = io.BytesIO()
    SITES.to_parquet(buf, index=False)
    remote.s3.put_object(Bucket="ops", Key=stations.table_key("IT"), Body=buf.getvalue())


def features_of(remote, region="lazio"):
    raw = remote.s3.objects[("ops", stations.features_key("IT", region))]
    return pd.read_parquet(io.BytesIO(raw)).set_index("station")


class Stations(unittest.TestCase):
    def test_the_region_s_stations_once_each_and_why_not(self):
        remote = FakeRemote()
        with_table(remote)
        offline = {"IT004"}

        def compute(lat, lon):
            if (lat, lon) == (41.80, 12.30):
                raise errors.NotCovered("the window runs outside the indexed extracts")
            if (lat, lon) == (41.85, 12.40) and offline:
                raise errors.Transient("ConnectionError: name resolution")
            return {"road_major_100": 120.0}

        self.assertEqual(stations.features_for_region(remote, "IT", "lazio", [ROME], 60, compute=compute), (1, 1, 1))
        f = features_of(remote)
        self.assertEqual(sorted(f.index), ["IT001", "IT002"])          # Milan's is not Lazio's; IT004 waits
        self.assertEqual(f.loc["IT001", "road_major_100"], 120.0)
        self.assertTrue(f.loc["IT002", "reason"].startswith("upstream: NotCovered"))
        offline.clear()                                                 # the network is back
        self.assertEqual(stations.features_for_region(remote, "IT", "lazio", [ROME], 60, compute=compute), (1, 0, 0))
        self.assertEqual(sorted(features_of(remote).index), ["IT001", "IT002", "IT004"])
        self.assertEqual(stations.features_for_region(remote, "IT", "lazio", [ROME], 60, compute=compute), (0, 0, 0))

    def test_no_table_no_work_and_no_time_no_work(self):
        remote = FakeRemote()
        self.assertEqual(stations.features_for_region(remote, "IT", "lazio", [ROME], 60, compute=lambda a, o: {}), (0, 0, 0))
        with_table(remote)
        self.assertEqual(stations.features_for_region(remote, "IT", "lazio", [ROME], 0, compute=lambda a, o: {}), (0, 0, 3))


class LocalProjection(unittest.TestCase):
    def test_a_window_measures_true_metres_wherever_it_is(self):
        import air_features as af
        for lat, lon in ((35.68, 139.77), (59.91, 10.75), (41.9, 12.5), (34.05, -118.25)):
            win, x, y = af.Window.around([lon, lon, lon + 0.01], [lat, lat + 0.01, lat])
            north = float(np.hypot(x[1] - x[0], y[1] - y[0]))
            east = float(np.hypot(x[2] - x[0], y[2] - y[0]))
            self.assertAlmostEqual(north, 1111, delta=5)                               # 0.01° of latitude
            self.assertAlmostEqual(east, 1113 * np.cos(np.radians(lat)), delta=5)    # and of longitude
            w, s, e, n = win.lonlat_bounds()
            self.assertTrue(w < lon and e > lon + 0.01 and s < lat and n > lat + 0.01)


class NoSamplingPointTest(unittest.TestCase):
    def test_a_country_with_no_data_is_nodata_not_a_keyerror(self):
        # GB: the EEA lists its points but none has data in the years (#34: KeyError 'point')
        from unittest import mock
        import air_stations
        meta = pd.DataFrame(dict(SamplingPoint=["GB1"], Latitude=[51.5], Longitude=[-0.1]))
        with mock.patch.object(air_stations, "station_meta", return_value=meta), \
             mock.patch.object(air_stations, "series_urls", return_value=[]):
            with self.assertRaises(errors.NoData):
                air_stations.build_table("GB", [2023, 2024], ["pm10"])


if __name__ == "__main__":
    unittest.main()
