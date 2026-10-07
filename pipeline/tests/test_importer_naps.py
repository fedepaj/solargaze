import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import importers  # noqa: E402
from importers import validate  # noqa: E402
from importers.naps import NAPS, parse_hourly, parse_stations, tz_of  # noqa: E402
from sg.errors import NoData, Upstream  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "naps"


class FromFixture(NAPS):
    """NAPS with its downloads answered from real files (Brampton ON and St. John's NL, 8–11 March 2024)."""
    MIN_CAPTURE = 0.0   # four days of data, not a year
    cache = None

    def _download(self, path):
        return (FIX / path.rsplit("/", 1)[1]).read_text(encoding="utf-8")


class NAPSCanada(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.imp = FromFixture()
        cls.imp.MIN_CELL = 1
        cls.df = cls.imp.stations("CA", [2024], ["nitrogen_dioxide", "pm2_5", "ozone"])

    def test_passes_the_common_schema(self):
        validate(self.df, "naps")

    def test_the_stations_and_pollutants(self):
        self.assertEqual(set(self.df.station), {"10102", "60450"})
        self.assertEqual(set(self.df["var"]), {"nitrogen_dioxide", "pm2_5", "ozone"})

    def test_types_and_areas_come_from_the_station_table(self):
        s = parse_stations((FIX / "StationsNAPS-StationsSNPA.csv").read_text(encoding="utf-8"))
        self.assertEqual(set(s.index), {10102, 60450})
        self.assertTrue(s["type"].isin(["background", "traffic", "industrial"]).all())
        self.assertEqual(s.loc[10102, "offset"], -3.5)
        row = self.df.iloc[0]
        self.assertEqual(row["type"], s.loc[int(row.station), "type"])

    def test_units_are_ug_per_m3(self):
        unit, long = parse_hourly((FIX / "NO2_2024.csv").read_text(encoding="utf-8"))
        self.assertEqual(unit, "ppb")
        raw = long[long["id"] == 60450]["value"].mean()
        got = self.df[(self.df["var"] == "nitrogen_dioxide") & (self.df.station == "60450")].annual_mean.iloc[0]
        # the mean of month × hour means over the same hours: close to the raw mean × 1.88, never the ppb number
        self.assertGreater(got, raw * 1.5)
        self.assertLess(got, raw * 2.3)

    def test_pm_is_taken_as_is(self):
        unit, long = parse_hourly((FIX / "PM25_2024.csv").read_text(encoding="utf-8"))
        self.assertEqual(unit, "µg/m³")
        got = self.df[(self.df["var"] == "pm2_5") & (self.df.station == "60450")].annual_mean.iloc[0]
        self.assertAlmostEqual(got, long[long["id"] == 60450]["value"].mean(), delta=1.5)

    def test_months_and_hours(self):
        # the fixture has March only; the 8th–9th are standard time and the 10th–11th daylight time,
        # so some civil hours have fewer days behind them but all 24 cells are there
        row = self.df[(self.df["var"] == "nitrogen_dioxide") & (self.df.station == "60450")].iloc[0]
        march = row.by_month_hour[2 * 24:3 * 24]
        self.assertEqual(len(row.by_month_hour), 288)
        self.assertTrue(all(v is not None for v in march))
        self.assertTrue(all(v is None for v in row.by_month_hour[:2 * 24]))

    def test_time_zones(self):
        self.assertEqual(tz_of("ON", -5), "America/Toronto")
        self.assertEqual(tz_of("NL", -3.5), "America/St_Johns")
        self.assertEqual(tz_of("SK", -6), "America/Regina")
        self.assertEqual(tz_of("YT", -7), "America/Whitehorse")

    def test_registry_and_coverage(self):
        self.assertIsInstance(importers.importer_for("CA"), NAPS)
        self.assertFalse(NAPS().covers("US"))
        with self.assertRaises(NoData):
            FromFixture().stations("US", [2024])

    def test_a_changed_format_is_upstream_not_bug(self):
        with self.assertRaises(Upstream):
            parse_hourly("<html>not a csv</html>")


if __name__ == "__main__":
    unittest.main()
