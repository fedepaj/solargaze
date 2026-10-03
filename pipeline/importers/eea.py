"""
The European Environment Agency's verified air-quality data (E1a), for the
39 countries that report to it: hourly and daily series per sampling point,
station metadata from the EEA table. The folding is air_stations.py's.
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from . import Importer, VARS  # noqa: E402

# The countries the download service lists, as of 2026-10; asked again at run time.
KNOWN = set("AD AL AT BA BE BG CH CY CZ DE DK EE ES FI FR GB GE GI GR HR HU IE IS IT LI LT LU LV ME MK MT NL NO PL PT RO RS SE SI SK TR XK".split())


class EEA(Importer):
    name = "eea"
    source = "https://eeadmz1-downloads-api-appservice.azurewebsites.net (EEA Air Quality Download Service, E1a)"
    licence = "EEA standard re-use policy: free re-use with attribution (CC-BY 4.0 compatible)"

    @functools.lru_cache(maxsize=1)
    def _countries(self) -> frozenset:
        import requests
        try:
            r = requests.get("https://eeadmz1-downloads-api-appservice.azurewebsites.net/Country", timeout=60)
            r.raise_for_status()
            return frozenset(c["countryCode"] for c in r.json())
        except Exception:  # noqa: BLE001 — the service down is no reason to forget who it covers
            return frozenset(KNOWN)

    def covers(self, country: str) -> bool:
        return country.upper() in self._countries()

    def stations(self, country: str, years: list[int], variables=VARS) -> pd.DataFrame:
        import air_stations
        t = air_stations.build_table(country.upper(), years, list(variables))
        kind = t.AirQualityStationType.fillna("unknown").where(t.AirQualityStationType.isin(["traffic", "background", "industrial"]), "unknown")
        area = t.AirQualityStationArea.fillna("unknown").str.split("-").str[0]
        area = area.where(area.isin(["urban", "suburban", "rural"]), "unknown")
        return pd.DataFrame({
            "station": t.AirQualityStation, "point": t.point, "var": t["var"],
            "lat": t.Latitude.astype(float), "lon": t.Longitude.astype(float), "alt": pd.to_numeric(t.Altitude, errors="coerce"),
            "type": kind, "area": area, "resolution": t.resolution, "values": t["values"].astype(int),
            "capture": t.capture.astype(float), "annual_mean": t.annual_mean.astype(float),
            "days_over_who": pd.to_numeric(t.days_over_who, errors="coerce"),
            "by_month": t.by_month, "by_month_hour": t.by_month_hour,
            "source": self.name, "licence": self.licence,
        })
