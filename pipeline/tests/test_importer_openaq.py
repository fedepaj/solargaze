import gzip
import json
import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import importers  # noqa: E402
from importers import validate  # noqa: E402
from importers.openaq import OpenAQ  # noqa: E402
from sg.errors import NoData  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "openaq"


class FromFixture(OpenAQ):
    """OpenAQ with its network calls answered from real files fetched with curl."""
    MIN_CAPTURE = 0.0   # four days of data, not a year
    MIN_CELL = 2
    workers = 2
    cache = None

    def _api(self, path, **params):
        return json.loads((FIX / f"{path}.json").read_text())

    def _day(self, loc, day):
        f = FIX / "days" / f"{loc}-{day:%Y%m%d}.csv.gz"
        return gzip.decompress(f.read_bytes()).decode() if f.exists() else None


class OpenAQJapan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = FromFixture().stations("JP", [2024], ["nitrogen_dioxide", "pm10", "pm2_5", "ozone"])

    def test_passes_the_common_schema(self):
        validate(self.df, "openaq")

    def test_two_stations_two_pollutants(self):
        self.assertEqual(set(self.df.station), {"1214487", "1214507"})
        self.assertEqual(set(self.df["var"]), {"nitrogen_dioxide", "pm2_5"})
        self.assertEqual(len(self.df), 4)

    def test_units_are_ug_per_m3_and_months_are_the_ones_with_data(self):
        no2 = self.df[self.df["var"] == "nitrogen_dioxide"]
        self.assertTrue(no2.annual_mean.between(1, 100).all())  # ppm taken as µg/m³ would be ~0.01
        for months in self.df.by_month:
            self.assertEqual([i for i, x in enumerate(months) if x is not None], [2])  # March
        for mh in self.df.by_month_hour:
            self.assertEqual(len(mh), 288)

    def test_provenance_and_unknowns(self):
        self.assertTrue(self.df.licence.str.contains("Government Standard Terms").all())
        self.assertTrue((self.df.source == "openaq").all())
        self.assertTrue((self.df["type"] == "unknown").all() and (self.df.area == "unknown").all())

    def test_only_vetted_countries_and_registered(self):
        self.assertIsInstance(importers.importer_for("JP"), OpenAQ)
        self.assertFalse(OpenAQ().covers("FR"))
        with self.assertRaises(NoData):
            OpenAQ().stations("FR", [2024])

    def test_a_licence_without_redistribution_is_left_out(self):
        class Closed(FromFixture):
            def _api(self, path, **params):
                rows = super()._api(path, **params)
                if path == "licenses":
                    for r in rows:
                        r["redistributionAllowed"] = False
                return rows
        with self.assertRaises(NoData):
            Closed().stations("JP", [2024])

    def test_years_before_openaq_has_the_country(self):
        with self.assertRaises(NoData):
            FromFixture().stations("JP", [2020, 2021])


if __name__ == "__main__":
    unittest.main()
