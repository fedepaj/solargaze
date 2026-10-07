import gzip
import json
import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import importers  # noqa: E402
from importers import validate  # noqa: E402
from importers.openaq import OpenAQ  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "openaq_mx"


class FromFixture(OpenAQ):
    """OpenAQ answered from real files fetched with curl: SINAICA station 675 and AirNow station 338
    (three days of March 2024), plus a low-cost AirGradient sensor and a SPARTAN site with no
    licence, which must be left out."""
    MIN_CAPTURE = 0.0   # three days of data, not a year
    MIN_CELL = 2
    workers = 2
    cache = None

    def _api(self, path, **params):
        return json.loads((FIX / f"{path}.json").read_text())

    def _day(self, loc, day):
        f = FIX / "days" / f"{loc}-{day:%Y%m%d}.csv.gz"
        return gzip.decompress(f.read_bytes()).decode() if f.exists() else None


class OpenAQMexico(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = FromFixture().stations("MX", [2024])

    def test_passes_the_common_schema(self):
        validate(self.df, "openaq")

    def test_only_licensed_government_monitors(self):
        self.assertEqual(set(self.df.station), {"675", "338"})
        lic = dict(zip(self.df.station, self.df.licence))
        self.assertIn("SINAICA", lic["675"])
        self.assertEqual(lic["338"], "US Public Domain")
        self.assertEqual(set(self.df[self.df.station == "675"]["var"]), {"pm10", "ozone"})
        self.assertEqual(set(self.df[self.df.station == "338"]["var"]), {"pm10", "pm2_5", "ozone"})

    def test_ppm_becomes_ug_per_m3(self):
        o3 = self.df[self.df["var"] == "ozone"].annual_mean
        self.assertTrue(len(o3) and o3.between(5, 200).all())  # ppm taken as µg/m³ would be ~0.03

    def test_local_hours_and_registered(self):
        for mh in self.df.by_month_hour:
            self.assertEqual(len(mh), 288)
        for months in self.df.by_month:
            self.assertEqual([i for i, x in enumerate(months) if x is not None], [2])  # March
        self.assertIsInstance(importers.importer_for("MX"), OpenAQ)


if __name__ == "__main__":
    unittest.main()
