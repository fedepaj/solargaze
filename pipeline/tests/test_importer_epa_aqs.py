import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import importers  # noqa: E402
from importers import validate  # noqa: E402
from importers.epa_aqs import EPAAQS, monitor_type, tz_of  # noqa: E402
from sg.errors import NoData  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "epa_aqs"


class FromFixture(EPAAQS):
    """AQS with its downloads answered from real files (Los Angeles - North Main Street, 1–4 March 2023)."""
    MIN_CAPTURE = 0.0   # four days of data, not a year
    MIN_CELL = 2
    cache = None

    def _download(self, name):
        return (FIX / name).read_bytes()

    def _monitors(self):
        return (FIX / "aqs_monitors.csv").read_text(encoding="latin-1")


class EPAAQSUnitedStates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = FromFixture().stations("US", [2023], ["nitrogen_dioxide", "pm2_5", "ozone"])

    def test_passes_the_common_schema(self):
        validate(self.df, "epa-aqs")

    def test_one_station_three_pollutants(self):
        self.assertEqual(set(self.df.station), {"06-037-1103"})
        self.assertEqual(set(self.df["var"]), {"nitrogen_dioxide", "pm2_5", "ozone"})

    def test_units_are_ug_per_m3(self):
        o3 = self.df[self.df["var"] == "ozone"].annual_mean
        no2 = self.df[self.df["var"] == "nitrogen_dioxide"].annual_mean
        self.assertTrue(o3.between(10, 150).all())   # ppm taken as µg/m³ would be ~0.03
        self.assertTrue(no2.between(2, 120).all())

    def test_months_and_hours(self):
        for months in self.df.by_month:
            self.assertEqual([i for i, x in enumerate(months) if x is not None], [2])  # March
        for mh in self.df.by_month_hour:
            self.assertEqual(len(mh), 288)

    def test_provenance(self):
        self.assertTrue((self.df.source == "epa-aqs").all())
        self.assertTrue(self.df.licence.str.contains("Public Domain").all())

    def test_classification_and_zones(self):
        self.assertEqual(monitor_type("SOURCE ORIENTED", "NEIGHBORHOOD"), "industrial")
        self.assertEqual(monitor_type("HIGHEST CONCENTRATION", "MIDDLE SCALE"), "traffic")
        self.assertEqual(monitor_type("HIGHEST CONCENTRATION", "NEIGHBORHOOD"), "unknown")
        self.assertEqual(tz_of("06", -8), "America/Los_Angeles")
        self.assertEqual(tz_of("04", -7), "America/Phoenix")

    def test_registered_and_only_the_us(self):
        self.assertIsInstance(importers.importer_for("US"), EPAAQS)
        self.assertFalse(EPAAQS().covers("CA"))
        with self.assertRaises(NoData):
            EPAAQS().stations("CA", [2023])


if __name__ == "__main__":
    unittest.main()
