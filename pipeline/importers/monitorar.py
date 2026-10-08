"""
MonitorAr, Brazil's national air-quality system (Ministry of the Environment,
MMA), whose yearly files of hourly values from the state and municipal
networks (CETESB, FEAM, Rio's MonitorAr-Rio, …) are published on the ministry's
open-data portal under CC BY. OpenAQ carries the São Paulo and Rio stations
too but with no licence, so it is not used for Brazil.

The yearly zip (one CSV of 0.8 GB, 20 MB zipped) is downloaded once to the
cache and read in chunks. Values are folded to month × local hour as
air_stations.py does, in µg/m³ (the files give no unit; the values agree with
OpenAQ's CETESB series in µg/m³ — see the pull request).

Honesty notes. The files state no station type or area, so both are
"unknown". The ministry warns the values are automatic and may not have been
validated; only rows the networks flag valid (st_situacao VA) are used, and
readings outside what a monitor can plausibly give (sentinels such as -9999,
99999) are dropped. Times are read as the local clock of the station: the
ozone peak falls at 13–16 h in every state, which UTC could not give. Coverage
starts in 2022; earlier years are NoData. Years beyond the published ones are
skipped.
"""

from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd

sys.path[:0] = [str(Path(__file__).resolve().parents[1])]
from . import Importer, VARS  # noqa: E402
from sg.errors import NoData  # noqa: E402

log = logging.getLogger("monitorar")

PACKAGE = "https://dados.mma.gov.br/api/3/action/package_show?id=5be05b46-3bda-4f6e-9bf2-810e716fff33"
CACHE = Path(__file__).resolve().parents[1] / "cache" / "stations" / "monitorar"
# The files' parameter codes → the app's. No unit is given: µg/m³ for all four.
PARAM = {"NO2": "nitrogen_dioxide", "MP10": "pm10", "MP2,5": "pm2_5", "O3": "ozone"}
# Beyond this a reading is a broken sensor or a sentinel, whatever the pollutant's usual range.
CEILING = {"nitrogen_dioxide": 1000.0, "pm10": 1000.0, "pm2_5": 1000.0, "ozone": 600.0}
WHO_DAILY = {"pm2_5": 15, "pm10": 45, "nitrogen_dioxide": 25, "ozone": 100}
USECOLS = ["dh_medicao", "nu_concentracao", "st_situacao", "cd_normalizado", "id_estacao", "no_estacao", "nu_latitude", "nu_longitude"]


class MonitorAr(Importer):
    name = "monitorar"
    source = "https://dados.mma.gov.br/dataset/sistema-nacional-de-gestao-da-qualidade-do-ar-monitorar (Ministério do Meio Ambiente e Mudança do Clima, MonitorAr; stations of CETESB, FEAM, Rio de Janeiro city, and other state agencies)"
    licence = "CC BY (Creative Commons Atribuição; licence id cc-by on the MMA open-data portal)"
    MIN_CAPTURE = 0.25   # of the period; a point with less is left out
    MIN_CELL = 10        # hours in a month × hour cell for it to count
    cache: Path | None = CACHE

    def covers(self, country: str) -> bool:
        return country.upper() == "BR"

    # -- network access: everything that touches the network is here --

    def _chunks(self, year: int) -> Iterator[pd.DataFrame]:
        """The year's file, as chunks of rows (all columns as text), or nothing if not published."""
        import requests
        import zipfile
        r = requests.get(PACKAGE, timeout=60)
        r.raise_for_status()
        urls = [x["url"] for x in r.json()["result"]["resources"] if x["url"].endswith(f"dados_monitorar_{year}.zip")]
        if not urls:
            return
        path = (self.cache or Path("/tmp")) / f"dados_monitorar_{year}.zip"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            with requests.get(urls[0], stream=True, timeout=(20, 120)) as d:
                d.raise_for_status()
                with open(tmp, "wb") as f:
                    for block in d.iter_content(1 << 20):
                        f.write(block)
            tmp.rename(path)
        z = zipfile.ZipFile(path)
        with z.open(z.namelist()[0]) as f:
            yield from pd.read_csv(f, usecols=USECOLS, dtype=str, chunksize=1_000_000)

    # -- reading and folding --

    def read(self, chunks: Iterator[pd.DataFrame], variables) -> pd.DataFrame:
        """Valid hourly values of the wanted gases: station, var, time, µg/m³."""
        wanted = {k: v for k, v in PARAM.items() if v in variables}
        out = []
        for ch in chunks:
            ch = ch[ch.cd_normalizado.isin(wanted) & (ch.st_situacao == "VA")]
            if ch.empty:
                continue
            var = ch.cd_normalizado.map(wanted)
            v = pd.to_numeric(ch.nu_concentracao, errors="coerce")
            ok = v.notna() & (v >= 0) & (v <= var.map(CEILING))
            out.append(pd.DataFrame({
                "station": ch.id_estacao[ok], "name": ch.no_estacao[ok], "var": var[ok], "t": pd.to_datetime(ch.dh_medicao[ok]),
                "v": v[ok], "lat": pd.to_numeric(ch.nu_latitude[ok], errors="coerce"), "lon": pd.to_numeric(ch.nu_longitude[ok], errors="coerce"),
            }))
        if not out:
            return pd.DataFrame(columns=["station", "name", "var", "t", "v", "lat", "lon"])
        return pd.concat(out, ignore_index=True).drop_duplicates(["station", "var", "t"])

    def fold(self, g: pd.DataFrame, years: list[int]) -> dict | None:
        """One station's values of one gas → the common schema's numbers."""
        g = g[g.t.dt.year.isin(years)]
        hours_in = sum(366 if (y % 4 == 0 and y % 100 != 0) or y % 400 == 0 else 365 for y in years) * 24
        if len(g) / hours_in < self.MIN_CAPTURE:
            return None
        var = g["var"].iloc[0]
        m, h, v = g.t.dt.month.to_numpy() - 1, g.t.dt.hour.to_numpy(), g.v.to_numpy(dtype=float)
        s = np.zeros((12, 24)); n = np.zeros((12, 24))
        np.add.at(s, (m, h), v); np.add.at(n, (m, h), 1)
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # a month with no cell is NaN, as intended
            mh = np.where(n >= self.MIN_CELL, s / n, np.nan)
            monthly = np.nanmean(mh, axis=1) if np.isfinite(mh).any() else np.full(12, np.nan)
        if not np.isfinite(monthly).any():
            return None
        daily = g.set_index("t").v.resample("D")
        dm = daily.mean()[daily.count() >= 18]
        return {
            "var": var, "resolution": "hour", "values": int(len(v)), "capture": round(min(len(v) / hours_in, 1.0), 3),
            "annual_mean": round(float(np.nanmean(monthly)), 2),
            "days_over_who": round(float((dm > WHO_DAILY[var]).mean()), 3) if len(dm) else float("nan"),
            "by_month": [None if np.isnan(x) else round(float(x), 1) for x in monthly],
            "by_month_hour": [None if np.isnan(x) else round(float(x), 1) for x in mh.ravel()],
        }

    def table(self, df: pd.DataFrame, years: list[int]) -> pd.DataFrame:
        rows = []
        for (station, var), g in df.groupby(["station", "var"], sort=True):
            f = self.fold(g, years)
            if f is None:
                continue
            lat, lon = g.lat.median(), g.lon.median()
            if not (np.isfinite(lat) and np.isfinite(lon)):
                continue
            rows.append({
                "station": station, "point": f"{station}-{var}", "lat": float(lat), "lon": float(lon), "alt": float("nan"),
                "type": "unknown", "area": "unknown", **f, "source": self.name, "licence": self.licence,
            })
        return pd.DataFrame(rows)

    def stations(self, country: str, years: list[int], variables=VARS) -> pd.DataFrame:
        if not self.covers(country):
            raise NoData(f"{country}: MonitorAr is Brazil's")
        parts = []
        for y in years:
            part = self.read(self._chunks(y), variables)
            log.info("%d: %d valid hourly values, %d stations", y, len(part), part.station.nunique())
            parts.append(part)
        df = pd.concat(parts, ignore_index=True)
        if df.empty:
            raise NoData(f"BR: MonitorAr has no valid values for {years[0]}–{years[-1]} (it has 2022 onwards)")
        out = self.table(df, years)
        if out.empty:
            raise NoData(f"BR: no MonitorAr station with enough data in {years[0]}–{years[-1]}")
        return out
