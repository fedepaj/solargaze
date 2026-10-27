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
    def test_the_canaries_are_spain_and_outside_the_european_reanalysis(self):
        from sg.products.air import Air
        madrid, canaries = "N40.25E-3.75", "N28.00E-15.50"
        self.assertTrue(acb.in_europe(Tile.parse(madrid).bounds))
        self.assertFalse(acb.in_europe(Tile.parse(canaries).bounds))
        seen = []
        with mock.patch.object(acb, "fold_region_cached",
                               lambda bbox, years, ts: seen.append((bbox, [t.id for t in ts])) or ("fold", "note")):
            ctx = SimpleNamespace(shared={})
            Air().prepare([madrid, canaries], ctx)
        self.assertEqual(sorted(ids for _, ids in seen), [[canaries], [madrid]])
        europe_box = next(b for b, ids in seen if ids == [madrid])
        self.assertTrue(acb.in_europe(europe_box))      # its ring stays inside the domain too
        self.assertEqual(set(ctx.shared["air"]), {True, False})


if __name__ == "__main__":
    unittest.main()
