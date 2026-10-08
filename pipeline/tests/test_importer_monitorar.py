import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import importers  # noqa: E402
from importers import validate  # noqa: E402
from importers.monitorar import MonitorAr, USECOLS  # noqa: E402
from sg.errors import NoData  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "monitorar" / "dados_monitorar_2023_sample.csv"


class FromFixture(MonitorAr):
    """MonitorAr with its download answered by three real days of two stations (curl, MMA 2023 file)."""
    MIN_CAPTURE = 0.0   # three days of data, not a year
    MIN_CELL = 1

    def _chunks(self, year):
        if year == 2023:
            yield pd.read_csv(FIX, usecols=USECOLS, dtype=str)


class MonitorArBrazil(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = FromFixture().stations("BR", [2023])

    def test_passes_the_common_schema(self):
        validate(self.df, "monitorar")

    def test_two_stations_with_their_coordinates(self):
        self.assertEqual(set(self.df.station), {"104", "386"})
        tijuca = self.df[self.df.station == "386"].iloc[0]
        self.assertAlmostEqual(tijuca.lat, -22.9028, 3)
        self.assertAlmostEqual(tijuca.lon, -43.2075, 3)

    def test_hourly_folding_in_micrograms(self):
        row = self.df[(self.df.station == "386") & (self.df["var"] == "ozone")].iloc[0]
        self.assertEqual(row.resolution, "hour")
        self.assertEqual(len(row.by_month_hour), 288)
        self.assertIsNone(row.by_month[0])           # the fixture is all March
        self.assertIsNotNone(row.by_month[2])
        self.assertEqual(row.licence, MonitorAr.licence)

    def test_only_valid_rows_and_no_sentinels(self):
        raw = pd.read_csv(FIX, dtype=str)
        self.assertTrue((self.df.annual_mean >= 0).all())
        n = (raw.cd_normalizado.isin(["NO2", "MP10", "MP2,5", "O3"]) & (raw.st_situacao == "VA")).sum()
        self.assertLessEqual(self.df["values"].sum(), n)

    def test_outside_brazil_and_before_2022(self):
        self.assertTrue(MonitorAr().covers("br"))
        self.assertFalse(MonitorAr().covers("AR"))
        with self.assertRaises(NoData):
            FromFixture().stations("BR", [2019])

    def test_registered(self):
        self.assertTrue(any(isinstance(i, MonitorAr) for i in importers.REGISTRY))
