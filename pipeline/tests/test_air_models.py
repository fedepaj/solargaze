import io
import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import air_cams_bulk as acb  # noqa: E402
from sg import stations  # noqa: E402
from tests.test_border import FakeRemote  # noqa: E402


def put(remote, key, df):
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    remote.s3.put_object(Bucket="ops", Key=key, Body=buf.getvalue())


def country(remote, cc, n, region, seed, lat0=41.0, lon0=12.0):
    """n stations whose NO₂ and PM10 are CAMS × e^(0.002 · major road metres within 100 m)."""
    rng = np.random.default_rng(seed)
    lat = lat0 + rng.uniform(0, 2, n)
    lon = lon0 + rng.uniform(0, 2, n)
    road = rng.uniform(0, 400, n)
    green = rng.uniform(0, 1, n)
    ids = [f"{cc}{i:03d}" for i in range(n)]
    cams = 20 + rng.uniform(-3, 3, n)
    obs = (cams + 1) * np.exp(0.002 * road + rng.normal(0, 0.03, n)) - 1
    table = pd.DataFrame({"station": ids * 2, "var": ["nitrogen_dioxide"] * n + ["pm10"] * n,
                          "lat": np.r_[lat, lat], "lon": np.r_[lon, lon], "type": "background", "area": "urban",
                          "capture": 0.9, "annual_mean": np.r_[obs, obs]})
    put(remote, stations.table_key(cc), table)
    put(remote, stations.features_key(cc, region), pd.DataFrame({
        "station": ids, "lat": lat, "lon": lon, "reason": "", "road_major_100": road, "green_300": green}))
    put(remote, stations.cams_key(cc, region), pd.DataFrame({
        "station": ids * 2, "var": ["nitrogen_dioxide"] * n + ["pm10"] * n, "cams": np.r_[cams, cams]}))


class CamsAtStations(unittest.TestCase):
    def test_bilinear_between_the_four_nodes(self):
        nodes = stations.corners(41.93, 12.47)
        self.assertEqual(nodes, [(41.9, 12.4), (41.9, 12.5), (42.0, 12.4), (42.0, 12.5)])
        fold = acb.Fold(nodes)
        hours = pd.date_range("2023-01-01", periods=24, freq="1h", tz="UTC")
        # 10 µg/m³ on the southern nodes, 20 on the northern: 0.3 of the way is 13.
        fold.add("nitrogen_dioxide", hours, np.tile([10.0, 10.0, 20.0, 20.0], (24, 1)))
        at = stations.cams_at(fold, pd.DataFrame({"station": ["X"], "lat": [41.93], "lon": [12.47]}))
        self.assertAlmostEqual(float(at[at["var"] == "nitrogen_dioxide"].cams.iloc[0]), 13.0, places=3)


class Fitting(unittest.TestCase):
    def test_own_models_pooled_ones_and_no_refit_when_nothing_changed(self):
        remote = FakeRemote()
        country(remote, "IT", 60, "italy", 1)
        country(remote, "LU", 8, "luxembourg", 2, lat0=49.4, lon0=5.8)       # too few for a model of its own
        done = stations.fit_all(remote, {"europe": ["IT", "LU"]})
        it = json.loads(remote.s3.objects[("ops", stations.models_key("IT"))])
        lu = json.loads(remote.s3.objects[("ops", stations.models_key("LU"))])
        no2 = it["models"]["nitrogen_dioxide"]
        self.assertIn("road_major_100", no2["terms"])                        # the road is found
        self.assertTrue(no2["beats_cams"])
        self.assertNotIn("pooled_from", no2)
        self.assertEqual(lu["models"]["nitrogen_dioxide"]["pooled_from"], "europe")
        self.assertIn("own", done["IT"])
        self.assertEqual(stations.fit_all(remote, {"europe": ["IT", "LU"]}), {"IT": "unchanged", "LU": "unchanged"})


class Street(unittest.TestCase):
    def test_only_what_beats_cams_and_nothing_when_nothing_does(self):
        from types import SimpleNamespace
        from unittest import mock
        from sg.errors import NoData
        from sg.products.air_street import AirStreet
        remote = FakeRemote()
        model = {"fitted": "2026-10-27T00:00:00+00:00", "models": {
            "nitrogen_dioxide": {"beats_cams": True, "stations": 60},
            "pm10": {"beats_cams": False, "stations": 60}}}
        remote.s3.put_object(Bucket="ops", Key=stations.models_key("IT"), Body=json.dumps(model).encode())
        ctx = SimpleNamespace(shared={"country": "IT"}, data_dir=Path("/tmp"), cache_dir=Path("/tmp"))
        with mock.patch("sg.remote.Remote.maybe", return_value=remote):
            AirStreet().prepare(["N41.75E12.25"], ctx)
        models, note, stamp = ctx.shared["air_street"]
        self.assertEqual(list(models), ["nitrogen_dioxide"])
        self.assertIn("monitoring stations of IT", note)
        self.assertEqual(AirStreet().inputs("N41.75E12.25", ctx), {"model": stamp})
        ctx.shared["air_street"] = ({}, note, stamp)
        with self.assertRaises(NoData):
            AirStreet().build("N41.75E12.25", Path("/tmp/x"), ctx)


class Planning(unittest.TestCase):
    def test_stations_first_then_air(self):
        import sg.__main__ as m
        ops = {"tables": set(), "cams": set(), "importable": {"IT", "DE", "US"}}
        rows = m._station_rows("europe", ops)
        self.assertEqual([(r["country"], r["stations"]) for r in rows], [("IT", True)])
        ops["tables"] = {stations.table_key("IT")}
        self.assertEqual([r["country"] for r in m._station_rows("europe", ops)], ["DE"])


if __name__ == "__main__":
    unittest.main()
