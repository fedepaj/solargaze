"""
Canada's National Air Pollution Surveillance (NAPS) programme, run by
Environment and Climate Change Canada with the provinces, territories and
regional networks: every continuous monitor that reports NO₂, PM10, PM2.5 or
O₃ hourly, one CSV per pollutant and year (wide: one row per station and day,
H01–H24), plus the station table with each station's type and urbanisation.
Both are read from the ECCC data catalogue
(https://data-donnees.az.ec.gc.ca/data/air/monitor/national-air-pollution-surveillance-naps-program/).

Open Government Licence – Canada: free to use, adapt and publish, with
attribution (https://open.canada.ca/en/open-government-licence-canada).

The files give hours *ending*, in local *standard* time all year, -999 for no
data, NO₂ and O₃ in ppb and PM in µg/m³. Here each hour becomes an instant
(standard offset from the station table) and is folded in the station's civil
time, daylight saving included, with OpenAQ.fold (month × local hour, mean of
monthly means, µg/m³; NO₂ × 1.88, O₃ × 1.96 at 20 °C).

Honesty notes. NAPS site types are mapped, not equal: PE (general population
exposure) and RB (regional background) → background, T (transportation) →
traffic, PS (point source) → industrial. Urbanisation: large and medium urban
areas → urban, small urban (1 000–29 999 people) → suburban, non-urban → rural;
this is a population-centre size, not a distance to the city. The civil time
zone is taken from the standard offset, with Yukon and Saskatchewan (no
daylight saving) set apart; a few places that differ from their neighbours
(Lloydminster, the Peace River region, Atikokan) are not.
"""

from __future__ import annotations

import io
import logging
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from . import Importer, VARS  # noqa: E402
from .openaq import OpenAQ  # noqa: E402
from sg.errors import NoData, Upstream  # noqa: E402

log = logging.getLogger("naps")

BASE = "https://data-donnees.az.ec.gc.ca/api/file"
ROOT = "/air/monitor/national-air-pollution-surveillance-naps-program/"
STATIONS = ROOT + "ProgramInformation-InformationProgramme/StationsNAPS-StationsSNPA.csv"
CACHE = Path(__file__).resolve().parents[1] / "cache" / "stations" / "naps"
FILE = {"nitrogen_dioxide": "NO2", "pm10": "PM10", "pm2_5": "PM25", "ozone": "O3"}
UNIT = {"ppb": "ppb", "ppm": "ppm", "µg/m3": "µg/m³", "ug/m3": "µg/m³", "µg/m³": "µg/m³"}
TYPE = {"PE": "background", "RB": "background", "T": "traffic", "PS": "industrial"}
AREA = {"LU": "urban", "MU": "urban", "SU": "suburban", "NU": "rural"}
# standard-time offset from UTC (hours) → the civil time zone; provinces that differ come first.
PROVINCE_TZ = {"YT": "America/Whitehorse", "SK": "America/Regina"}
OFFSET_TZ = {-3.5: "America/St_Johns", -4: "America/Halifax", -5: "America/Toronto", -6: "America/Winnipeg",
             -7: "America/Edmonton", -8: "America/Vancouver"}
# a station missing from the table: the standard offset of its province
PROVINCE_OFFSET = {"NL": -3.5, "NS": -4, "NB": -4, "PE": -4, "QC": -5, "ON": -5, "MB": -6, "SK": -6, "AB": -7,
                   "BC": -8, "YT": -7, "NT": -7, "NU": -5}


def tz_of(province: str, offset: float) -> str:
    return PROVINCE_TZ.get(province) or OFFSET_TZ.get(offset, "UTC")


def parse_stations(text: str) -> pd.DataFrame:
    """The station table → one row per station (int id) with offset, type and area."""
    s = pd.read_csv(io.StringIO(text), skiprows=4, dtype=str, keep_default_na=False)
    s = s[s["NAPS_ID"].str.fullmatch(r"\d+")].copy()
    s["id"] = s["NAPS_ID"].astype(int)
    s["offset"] = pd.to_numeric(s["Timezone_UTC"], errors="coerce")
    s["type"] = s["Site_Type"].map(TYPE).fillna("unknown")
    s["area"] = s["Urbanization"].map(AREA).fillna("unknown")
    s["lat"] = pd.to_numeric(s["Latitude"], errors="coerce")
    s["lon"] = pd.to_numeric(s["Longitude"], errors="coerce")
    s["alt"] = pd.to_numeric(s["Elevation_m"], errors="coerce")
    return s.drop_duplicates("id", keep="last").set_index("id")[["offset", "type", "area", "lat", "lon", "alt"]]


def parse_hourly(text: str) -> tuple[str, pd.DataFrame]:
    """One pollutant-year file → (unit, long table: id, method, province, lat, lon, datetime in UTC-less local
    standard time, value). Hours end at H01…H24, so H01 is the hour starting at 00:00."""
    lines = text.lstrip("﻿").splitlines()
    unit = next((l.split(",")[1].strip() for l in lines[:8] if l.startswith("Units")), "")
    if unit.lower() not in UNIT:
        raise Upstream(f"NAPS hourly file in unit {unit!r}: no conversion to µg/m³")
    head = next((i for i, l in enumerate(lines[:20]) if l.startswith("Pollutant//")), None)
    if head is None:
        raise Upstream("NAPS hourly file: no header row (the format changed?)")
    df = pd.read_csv(io.StringIO("\n".join(lines[head:])), dtype=str, keep_default_na=False)
    df.columns = [c.split("//")[0].strip() for c in df.columns]
    hours = [f"H{h:02d}" for h in range(1, 25)]
    if not set(hours + ["NAPS ID", "Date", "Latitude", "Longitude"]) <= set(df.columns):
        raise Upstream(f"NAPS hourly file: unexpected columns {list(df.columns)[:10]}")
    vals = df[hours].apply(pd.to_numeric, errors="coerce").to_numpy(float, copy=True)
    vals[vals <= -999] = np.nan
    day = pd.to_datetime(df["Date"]).to_numpy("datetime64[ns]")
    stamp = day[:, None] + (np.arange(24) * np.timedelta64(1, "h"))[None, :]
    n = len(df)
    long = pd.DataFrame({
        "id": np.repeat(df["NAPS ID"].astype(int).to_numpy(), 24), "method": np.repeat((df["Method Code"].str.strip() if "Method Code" in df else pd.Series("", index=df.index)).to_numpy(), 24),
        "province": np.repeat(df["Province/Territory"].str.strip().to_numpy(), 24),
        "lat": np.repeat(pd.to_numeric(df["Latitude"], errors="coerce").to_numpy(), 24),
        "lon": np.repeat(pd.to_numeric(df["Longitude"], errors="coerce").to_numpy(), 24),
        "local": stamp.reshape(n * 24), "value": vals.reshape(n * 24),
    })
    return UNIT[unit.lower()], long.dropna(subset=["value"])


class NAPS(Importer):
    name = "naps"
    source = "https://data-donnees.az.ec.gc.ca/data/air/monitor/national-air-pollution-surveillance-naps-program/ (ECCC, National Air Pollution Surveillance programme: hourly continuous data and station table)"
    licence = "Open Government Licence – Canada (https://open.canada.ca/en/open-government-licence-canada)"
    provinces: list[str] | None = None   # province codes ("QC"), to try a sample; None: the whole country
    cache: Path | None = CACHE
    MIN_CAPTURE = 0.25

    def covers(self, country: str) -> bool:
        return country.upper() == "CA"

    # -- network access: everything that touches the network is in this one --

    def _download(self, path: str) -> str:
        cached = self.cache / path.rsplit("/", 1)[1] if self.cache else None
        if cached and cached.exists():
            return cached.read_text(encoding="utf-8")
        from .openaq import _get
        r = _get(BASE, params={"path": path})
        if r is None:
            raise NoData(f"NAPS has no file {path}")
        text = r.content.decode("utf-8-sig", errors="replace")
        if text.lstrip().startswith("<"):
            raise Upstream(f"NAPS {path}: got a web page, not a CSV")
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(text, encoding="utf-8")
        return text

    def _hourly(self, var: str, year: int) -> str:
        return self._download(f"{ROOT}Data-Donnees/{year}/ContinuousData-DonneesContinu/HourlyData-DonneesHoraires/{FILE[var]}_{year}.csv")

    # -- folding --

    def fold_long(self, long: pd.DataFrame, unit: str, var: str, years: list[int], table: pd.DataFrame) -> list[dict]:
        if long.empty:
            return []
        if self.provinces:
            long = long[long["province"].isin(self.provinces)]
        folder = OpenAQ()
        folder.MIN_CAPTURE, folder.MIN_CELL = self.MIN_CAPTURE, getattr(self, "MIN_CELL", folder.MIN_CELL)
        start, end = date(years[0], 1, 1), date(years[-1], 12, 31)
        rows = []
        for (sid, method), g in long.groupby(["id", "method"], sort=False):
            meta = table.loc[sid] if sid in table.index else None
            prov = g["province"].iloc[0]
            offset = meta["offset"] if meta is not None and pd.notna(meta["offset"]) else PROVINCE_OFFSET.get(prov)
            if offset is None:
                log.warning("station %s: no time zone, left out", sid)
                continue
            # hour H ends at H: the hour starting at H-1 (the arrays are already shifted), standard time → UTC
            utc = pd.DatetimeIndex(g["local"]).tz_localize("UTC") - pd.Timedelta(hours=float(offset))
            frame = pd.DataFrame({"datetime": utc, "value": g["value"].to_numpy()})
            f = folder.fold([frame], years, var, unit, tz_of(prov, float(offset)), start, end)
            if f is None or f["annual_mean"] is None:
                continue
            lat, lon = (meta["lat"], meta["lon"]) if meta is not None and pd.notna(meta["lat"]) else (g["lat"].iloc[0], g["lon"].iloc[0])
            rows.append({
                "station": str(sid), "point": f"{sid}-{method or 0}-{var}", "lat": float(lat), "lon": float(lon),
                "alt": float(meta["alt"]) if meta is not None and pd.notna(meta["alt"]) else float("nan"),
                "type": meta["type"] if meta is not None else "unknown", "area": meta["area"] if meta is not None else "unknown",
                **f, "source": self.name, "licence": self.licence,
            })
        return rows

    def stations(self, country: str, years: list[int], variables=VARS) -> pd.DataFrame:
        if not self.covers(country):
            raise NoData(f"{country}: NAPS covers Canada only")
        table = parse_stations(self._download(STATIONS))
        rows = []
        for var in variables:
            frames, unit = [], None
            for y in years:
                log.info("NAPS %s %d", var, y)
                try:
                    unit, long = parse_hourly(self._hourly(var, y))
                except NoData:
                    continue   # a year not published yet
                frames.append(long)
            if frames:
                rows += self.fold_long(pd.concat(frames), unit, var, years, table)
        if not rows:
            raise NoData(f"CA: no NAPS station with usable data in {years[0]}–{years[-1]}")
        out = pd.DataFrame(rows)
        # a station's two instruments for one pollutant stay two points: the model weighs them
        return out.sort_values("capture", ascending=False).drop_duplicates(["point", "var"]).reset_index(drop=True)
