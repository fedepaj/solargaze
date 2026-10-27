import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import air_cams_bulk as acb  # noqa: E402
from tiles import Tile  # noqa: E402

ROME, LA = (41.9, 12.5), (34.0, -118.2)


class LocalHours(unittest.TestCase):
    def test_each_node_reads_its_own_wall_clock_summer_time_included(self):
        times = pd.date_range("2023-01-01", "2023-12-31 23:00", freq="1h", tz="UTC")
        fold = acb.Fold([ROME, LA])
        self.assertEqual(fold.zones, ["Europe/Rome", "America/Los_Angeles"])
        # Each node's value is the hour on its own clock: the table must read
        # h at hour h, every month, through both changes of the clock.
        vals = np.stack([times.tz_convert(z).hour.to_numpy() for z in fold.zones], axis=1).astype(float)
        fold.add("ozone", times, vals)
        for k in range(2):
            table = np.array(fold.node(k)["ozone"]["by_month_hour"], dtype=float)
            np.testing.assert_allclose(table, np.tile(np.arange(24.0), (12, 1)))

    def test_the_share_of_days_over_the_guideline_is_by_local_day(self):
        # Ten days at 30 µg/m³ of NO₂ and ten at 10, by Rome's days; the
        # guideline is 25.
        local = pd.date_range("2023-03-01", periods=20 * 24, freq="1h", tz="Europe/Rome")
        vals = np.where(np.arange(len(local)) < 10 * 24, 30.0, 10.0)[:, None]
        fold = acb.Fold([ROME])
        fold.add("nitrogen_dioxide", local.tz_convert("UTC"), vals)
        self.assertEqual(fold.node(0)["nitrogen_dioxide"]["days_over_who"], 0.5)

    def test_missing_values_are_left_out_not_counted_as_zero(self):
        times = pd.date_range("2023-06-01", periods=48, freq="1h", tz="UTC")
        vals = np.full((48, 1), 40.0)
        vals[::2] = np.nan
        fold = acb.Fold([ROME])
        fold.add("pm10", times, vals)
        self.assertEqual(fold.node(0)["pm10"]["annual_mean"], 40.0)

    def test_at_sea_the_nautical_zone(self):
        self.assertEqual(acb.zone_at(40.0, -30.0), "Etc/GMT+2")


class Sources(unittest.TestCase):
    def test_the_canaries_are_spain_and_outside_the_european_reanalysis_stations_too(self):
        from sg.products.air import fold_for
        madrid, canaries = Tile.parse("N40.25E-3.75"), Tile.parse("N28.00E-15.50")
        self.assertTrue(acb.in_europe(madrid.bounds))
        self.assertFalse(acb.in_europe(canaries.bounds))
        points = pd.DataFrame({"station": ["ES_MAD", "ES_LPA"], "lat": [40.42, 28.11], "lon": [-3.70, -15.42]})
        seen = {}

        def fake(bbox, years, nodes):
            fold = acb.Fold(sorted(nodes))
            hours = pd.date_range("2023-01-01", periods=24, freq="1h", tz="UTC")
            for v in acb.VARS:          # 30 µg/m³ in Europe's fold, 10 outside it
                fold.add(v, hours, np.full((24, len(fold.wanted)), 30.0 if acb.in_europe(bbox) else 10.0))
            seen[acb.in_europe(bbox)] = bbox
            return fold, "note"

        with mock.patch.object(acb, "fold_nodes", fake):
            folds, at = fold_for([madrid, canaries], points)
        self.assertEqual(set(folds), {True, False})
        self.assertTrue(acb.in_europe(seen[True]))      # its ring stays inside the domain
        no2 = at[at["var"] == "nitrogen_dioxide"].set_index("station").cams
        self.assertEqual((no2["ES_MAD"], no2["ES_LPA"]), (30.0, 10.0))
        # Without points, no CAMS at points.
        with mock.patch.object(acb, "fold_nodes", fake):
            self.assertIsNone(fold_for([madrid], None)[1])


if __name__ == "__main__":
    unittest.main()
