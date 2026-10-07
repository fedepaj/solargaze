import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import heat_ecostress as he  # noqa: E402
from sg.errors import NoData, Upstream  # noqa: E402
from tiles import Tile  # noqa: E402


def granule(orbit, scene, mgrs, iso, layers=("LST", "cloud", "QC", "err")):
    base = f"ECOv002_L2T_LSTE_{orbit}_{scene}_{mgrs}_{iso.replace('-', '').replace(':', '')[:15]}_0710_01"
    links = [{"href": f"https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/ECO_L2T_LSTE.002/{base}/{base}_{l}.tif"}
             for l in layers]
    links += [{"href": f"s3://lp-prod-protected/ECO_L2T_LSTE.002/{base}/{base}_LST.tif"}]
    return {"title": base, "time_start": iso + ".000Z", "links": links}


class Night(unittest.TestCase):
    def test_night_is_read_in_local_solar_time(self):
        # 23:35 UTC at 12.4° E is 00:25 solar: night. 20:00 UTC at 140° E (Tokyo) is 05:20 solar: not.
        self.assertTrue(he.is_night(datetime(2023, 7, 4, 23, 35, tzinfo=timezone.utc), 12.4))
        self.assertFalse(he.is_night(datetime(2023, 7, 4, 20, 0, tzinfo=timezone.utc), 140))
        self.assertTrue(he.is_night(datetime(2023, 7, 4, 20, 30, tzinfo=timezone.utc), 12.4))   # 21:20 solar
        self.assertFalse(he.is_night(datetime(2023, 7, 4, 10, 0, tzinfo=timezone.utc), 12.4))

    def test_two_granules_of_one_pass_are_one_pass_and_day_passes_are_dropped(self):
        feed = {"feed": {"entry": [
            granule("28320", "007", "32TQM", "2023-07-04T23:35:58"),
            granule("28320", "007", "33TTG", "2023-07-04T23:35:58"),   # the same pass, the next MGRS tile
            granule("28400", "011", "32TQM", "2023-07-10T10:12:00"),   # mid-morning: not night
            granule("28500", "003", "32TQM", "2023-08-01T02:10:00", layers=("LST", "err")),  # no cloud mask: unusable
        ]}}
        resp = mock.Mock(status_code=200, json=lambda: feed, raise_for_status=lambda: None)
        with mock.patch.object(he.requests, "get", return_value=resp):
            ps = he.passes(Tile.parse("N41.75E12.25"))
        self.assertEqual([p["id"] for p in ps], ["28320_007"])
        self.assertEqual(len(ps[0]["granules"]), 2)
        self.assertEqual(sorted(ps[0]["granules"][0]), ["LST", "cloud"])

    def test_beyond_the_iss_is_empty_not_failed(self):
        with self.assertRaises(NoData):
            he.build(Tile.parse("N59.75E10.50"), Path("/nonexistent"))   # Oslo

    def test_no_credentials_is_upstream(self):
        with mock.patch.dict(he.os.environ, {}, clear=True), mock.patch.object(he, "_token", None):
            with self.assertRaises(Upstream):
                he.token()


if __name__ == "__main__":
    unittest.main()
