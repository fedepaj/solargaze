"""
The US EPA's Air Quality System (AQS), from its pre-generated hourly files
(https://aqs.epa.gov/aqsweb/airdata/): every regulatory monitor in the United
States that reports NO₂, PM10, PM2.5 or O₃ hourly, one zip per pollutant and
year, plus the monitor table with each monitor's objective and scale.

US federal government work: public domain. The files state no restriction
and the OpenAQ licence list carries them as "US Public Domain".

Folding is OpenAQ.fold's (month × local hour, mean of monthly means, µg/m³;
ppb → µg/m³ at 20 °C, NO₂ × 1.88, O₃ × 1.96, and ppm × 1000). AQS "local"
times are local *standard* time all year; here the instant (the GMT columns)
is converted to the station's civil time zone, daylight saving included, the
zone being read from the offset AQS gives (Arizona and Hawaii, which keep
standard time, are set apart).

Honesty notes. AQS has no "traffic / background" classification. ``type`` is
read from the monitoring objective ("SOURCE ORIENTED" → industrial, "GENERAL/
BACKGROUND" and "REGIONAL TRANSPORT" → background, "HIGHEST CONCENTRATION" at
micro or middle scale → traffic; the rest unknown), ``area`` from the
metropolitan area (CBSA) the site lies in (urban) or not (rural), regional-
scale monitors being rural. Both are proxies; suburban is never assigned.
"""

from __future__ import annotations

import io
import logging
import sys
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from . import Importer, VARS  # noqa: E402
from .openaq import OpenAQ  # noqa: E402
from sg.errors import NoData  # noqa: E402

log = logging.getLogger("epa_aqs")

BASE = "https://aqs.epa.gov/aqsweb/airdata"
CACHE = Path(__file__).resolve().parents[1] / "cache" / "stations" / "aqs"
CODE = {"nitrogen_dioxide": "42602", "pm10": "81102", "pm2_5": "88101", "ozone": "44201"}
UNIT = {"parts per billion": "ppb", "parts per million": "ppm"}   # anything else is taken as µg/m³
COLS = ["State Code", "County Code", "Site Num", "POC", "Latitude", "Longitude", "Date GMT", "Time GMT",
        "Time Local", "Date Local", "Sample Measurement", "Units of Measure"]
# standard-time offset from UTC (hours) → the civil time zone; the states that differ come first.
STATE_TZ = {"04": "America/Phoenix", "15": "Pacific/Honolulu", "72": "America/Puerto_Rico", "78": "America/Puerto_Rico"}
OFFSET_TZ = {-4: "America/Puerto_Rico", -5: "America/New_York", -6: "America/Chicago", -7: "America/Denver",
             -8: "America/Los_Angeles", -9: "America/Anchorage", -10: "Pacific/Honolulu"}


def tz_of(state: str, offset: int) -> str:
    return STATE_TZ.get(state) or OFFSET_TZ.get(offset, "UTC")


def monitor_type(objective: str, scale: str) -> str:
    objective, scale = (objective or "").upper(), (scale or "").upper()
    if "SOURCE" in objective:
        return "industrial"
    if "BACKGROUND" in objective or "TRANSPORT" in objective:
        return "background"
    if "HIGHEST" in objective and scale in ("MICROSCALE", "MIDDLE SCALE"):
        return "traffic"
    return "unknown"


def parse_monitors(text: str) -> pd.DataFrame:
    """The monitor table → one row per (site, parameter, POC) with type and area."""
    m = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    m = m[m["Parameter Code"].isin(CODE.values())].copy()
    m["site"] = m["State Code"] + "-" + m["County Code"] + "-" + m["Site Number"]
    m["type"] = [monitor_type(o, s) for o, s in zip(m["Monitoring Objective"], m["Measurement Scale"])]
    m["area"] = ["rural" if (s.upper() == "REGIONAL SCALE" or not c) else "urban" for s, c in zip(m["Measurement Scale"], m["CBSA Name"])]
    return m.drop_duplicates(["site", "Parameter Code", "POC"], keep="last")[["site", "Parameter Code", "POC", "type", "area"]]


class EPAAQS(Importer):
    name = "epa-aqs"
    source = "https://aqs.epa.gov/aqsweb/airdata/ (US EPA Air Quality System, pre-generated hourly files and monitor list)"
    licence = "US Public Domain (US federal government data)"
    states: list[str] | None = None   # AQS state codes ("06"), to try a sample; None: the whole country
    cache: Path | None = CACHE
    MIN_CAPTURE = 0.25

    def covers(self, country: str) -> bool:
        return country.upper() == "US"

    # -- network access: everything that touches the network is in these two --

    def _download(self, name: str) -> bytes:
        path = self.cache / name if self.cache else None
        if path and path.exists():
            return path.read_bytes()
        from .openaq import _get
        r = _get(f"{BASE}/{name}")
        if r is None:
            raise NoData(f"AQS has no file {name}")
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
        return r.content

    def _hourly(self, var: str, year: int) -> bytes:
        return self._download(f"hourly_{CODE[var]}_{year}.zip")

    def _monitors(self) -> str:
        z = zipfile.ZipFile(io.BytesIO(self._download("aqs_monitors.zip")))
        return z.read(z.namelist()[0]).decode("latin-1")

    # -- parsing and folding --

    def read_hourly(self, content: bytes) -> pd.DataFrame:
        z = zipfile.ZipFile(io.BytesIO(content))
        parts = []
        with z.open(z.namelist()[0]) as f:
            for chunk in pd.read_csv(f, usecols=COLS, dtype=str, chunksize=500_000, encoding="latin-1"):
                if self.states:
                    chunk = chunk[chunk["State Code"].isin(self.states)]
                parts.append(chunk)
        return pd.concat(parts) if parts else pd.DataFrame(columns=COLS)

    def fold_file(self, df: pd.DataFrame, var: str, years: list[int], meta: pd.DataFrame) -> list[dict]:
        if df.empty:
            return []
        folder = OpenAQ()
        folder.MIN_CAPTURE, folder.MIN_CELL = self.MIN_CAPTURE, getattr(self, "MIN_CELL", 10)
        start, end = date(years[0], 1, 1), date(years[-1], 12, 31)
        df = df.assign(site=df["State Code"] + "-" + df["County Code"] + "-" + df["Site Num"],
                       datetime=df["Date GMT"] + "T" + df["Time GMT"] + ":00Z", value=df["Sample Measurement"])
        lookup = {(r.site, r.POC): (r.type, r.area) for r in meta[meta["Parameter Code"] == CODE[var]].itertuples()}
        rows = []
        for (site, poc), g in df.groupby(["site", "POC"], sort=False):
            gmt = pd.to_datetime(g["Date GMT"] + " " + g["Time GMT"])
            loc = pd.to_datetime(g["Date Local"] + " " + g["Time Local"])
            offset = int(((loc - gmt).dt.total_seconds() / 3600).round().mode().iloc[0])
            unit = UNIT.get(g["Units of Measure"].iloc[0].lower(), "µg/m³")
            f = folder.fold([g], years, var, unit, tz_of(g["State Code"].iloc[0], offset), start, end)
            if f is None or f["annual_mean"] is None:
                continue
            kind, area = lookup.get((site, poc)) or ("unknown", "unknown")
            rows.append({"station": site, "point": f"{site}-{poc}-{var}", "lat": float(g["Latitude"].iloc[0]),
                         "lon": float(g["Longitude"].iloc[0]), "alt": float("nan"), "type": kind, "area": area,
                         **f, "source": self.name, "licence": self.licence})
        return rows

    def stations(self, country: str, years: list[int], variables=VARS) -> pd.DataFrame:
        if not self.covers(country):
            raise NoData(f"{country}: AQS covers the United States only")
        meta = parse_monitors(self._monitors())
        rows = []
        for var in variables:
            frames = []
            for y in years:
                log.info("AQS %s %d", var, y)
                frames.append(self.read_hourly(self._hourly(var, y)))
            rows += self.fold_file(pd.concat(frames), var, years, meta)
        if not rows:
            raise NoData(f"US: no AQS monitor with usable data in {years[0]}–{years[-1]}")
        out = pd.DataFrame(rows)
        # one monitor can appear under two parameter codes of the same pollutant: keep the better covered
        return out.sort_values("capture", ascending=False).drop_duplicates(["point", "var"]).reset_index(drop=True)
