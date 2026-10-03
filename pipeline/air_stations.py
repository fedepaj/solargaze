"""
Air-quality monitoring stations, as the ground truth the street-scale air
layer is fitted to.

CAMS says how the regional background behaves on an ~11 km grid; the
stations say what was actually breathed at a few hundred points, next to a
road or in a park. The difference between the two, explained by what is
around each station (roads, buildings, people), is what the 50 m product
adds. This script only gathers the stations, reduced to the same shape as
the CAMS tables: means by calendar month × local hour over the same years,
or by month alone for the samplers that report one value a day.

Source: the EEA Air Quality Download Service, verified data (E1a), one
Parquet file per sampling point with its whole hourly series; station
metadata (coordinates, altitude, traffic / background / industrial, urban /
suburban / rural, kerb distance) from the EEA metadata table. No key.

    pipeline/.venv/bin/python pipeline/air_stations.py                  # Italy, 2020–2024, four pollutants
    pipeline/.venv/bin/python pipeline/air_stations.py --vars nitrogen_dioxide --workers 8

Each series is downloaded, folded and thrown away (Italy's are ~3.5 GB raw,
a few MB folded); a folded point is kept under ``cache/stations/points/``
so an interrupted run carries on. The result is one table,
``cache/stations/IT_2020-2024.parquet``, a row per sampling point.

Honesty notes. Station times are reported in UTC+01 all year and are moved
to Europe/Rome here, as CAMS's are. Only values flagged valid count. A
point with less than a quarter of the period's hours is left out; the
capture of the others is in the table, for the fit to weigh. Annual means
are the mean of the 288 month × hour cells, as for CAMS, so a station that
was down every August is not biased towards winter.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from air_cams import VARS, WHO_DAILY  # noqa: E402
import netdns  # noqa: E402,F401  (falls back to DNS over HTTPS when the system resolver drops a name)

log = logging.getLogger("stations")

API = "https://eeadmz1-downloads-api-appservice.azurewebsites.net"
META_URL = "https://discomap.eea.europa.eu/map/fme/metadata/PanEuropean_metadata.csv"
CACHE = Path(__file__).resolve().parent / "cache" / "stations"
# The app's names for the pollutants → EIONET pollutant codes.
CODES = {"pm2_5": 6001, "pm10": 5, "nitrogen_dioxide": 8, "ozone": 7}
MIN_CAPTURE = 0.25
STATION_TZ = "Etc/GMT-1"  # "UTC+01" in the EEA metadata; POSIX names have the sign the other way


def get(url: str, method: str = "GET", tries: int = 6, **kw) -> requests.Response:
    """A request that waits out a DNS or connection hiccup instead of failing."""
    for i in range(tries):
        try:
            r = requests.request(method, url, timeout=(20, 300), **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.ConnectionError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except requests.ConnectionError as e:
            if i == tries - 1:
                raise
            wait = 5 * 3 ** i
            log.warning("%s: %s; again in %d s", url.rsplit("/", 1)[-1][:60], str(e)[:100], wait)
            time.sleep(wait)
    raise AssertionError


def station_meta(country: str) -> pd.DataFrame:
    """One row per sampling point and pollutant, for the country."""
    path = CACHE / "PanEuropean_metadata.csv"
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(get(META_URL).content)
    m = pd.read_csv(path, sep="\t", low_memory=False)
    m = m[m.Countrycode == country].copy()
    m["code"] = m.AirPollutantCode.str.rsplit("/", n=1).str[-1].astype(int)
    keep = ["SamplingPoint", "AirQualityStation", "AirQualityStationEoICode", "code", "Longitude", "Latitude",
            "Altitude", "AirQualityStationType", "AirQualityStationArea", "KerbDistance", "BuildingDistance"]
    return m[keep].drop_duplicates("SamplingPoint")


def series_urls(country: str, code: int, years: list[int]) -> list[str]:
    """Sampling points with data in the years, hourly or daily; each file
    still holds its whole series. Many particulate samplers are gravimetric
    and report one value a day: asking for hours alone loses half of them."""
    urls: set[str] = set()
    for agg in ("hour", "day"):
        body = {"countries": [country], "cities": [],
                "pollutants": [f"http://dd.eionet.europa.eu/vocabulary/aq/pollutant/{code}"],
                "dataset": 2, "source": "Api", "aggregationType": agg, "email": "",
                "dateTimeStart": f"{years[0]}-01-01T00:00:00Z", "dateTimeEnd": f"{years[-1]}-12-31T23:59:59Z"}
        text = get(f"{API}/ParquetFile/urls", method="POST", json=body).text
        urls |= {u.strip() for u in text.lstrip("﻿").splitlines()[1:] if u.strip()}
    return sorted(urls)


def _values(df: pd.DataFrame, agg: str, years: list[int]):
    d = df[df.AggType == agg]
    t = d.Start.dt.tz_localize(STATION_TZ).dt.tz_convert("Europe/Rome")
    v = pd.to_numeric(d.Value, errors="coerce").to_numpy(dtype=float)
    keep = t.dt.year.isin(years).to_numpy() & np.isfinite(v) & (v > -5)
    return t[keep], np.clip(v[keep], 0, None)


def fold(raw: Path, years: list[int], var: str) -> dict | None:
    """One sampling point's series → monthly means, and month × local-hour
    means when the series is hourly. Hours are used when they cover the
    period well enough; otherwise daily values, if there are any."""
    df = pd.read_parquet(raw, columns=["Samplingpoint", "Start", "Value", "Validity", "AggType"])
    df = df[df.Validity > 0]
    if df.empty:
        return None
    hours_in = sum(8784 if y % 4 == 0 else 8760 for y in years)
    t, v = _values(df, "hour", years)
    if len(v) / hours_in >= MIN_CAPTURE:
        resolution, capture = "hour", len(v) / hours_in
        m, h = t.dt.month.to_numpy() - 1, t.dt.hour.to_numpy()
        s = np.zeros((12, 24)); n = np.zeros((12, 24))
        np.add.at(s, (m, h), v); np.add.at(n, (m, h), 1)
        with np.errstate(invalid="ignore"):
            mh = np.where(n >= 10, s / n, np.nan)
            monthly = np.nanmean(mh, axis=1) if np.isfinite(mh).any() else np.full(12, np.nan)
        daily = pd.Series(v, index=t.dt.tz_localize(None)).resample("D")
        dm = daily.mean()[daily.count() >= 18]
    else:
        t, v = _values(df, "day", years)
        if len(v) / (hours_in / 24) < MIN_CAPTURE:
            return None
        resolution, capture, mh = "day", len(v) / (hours_in / 24), None
        m = t.dt.month.to_numpy() - 1
        s = np.bincount(m, v, 12); n = np.bincount(m, minlength=12)
        with np.errstate(invalid="ignore"):
            monthly = np.where(n >= 5, s / n, np.nan)
        dm = pd.Series(v)
    return {
        "point": df.Samplingpoint.iloc[0].split("/", 1)[-1],
        "var": var,
        "resolution": resolution,
        "values": int(len(v)),
        "capture": round(capture, 3),
        "annual_mean": round(float(np.nanmean(monthly)), 2) if np.isfinite(monthly).any() else None,
        "days_over_who": round(float((dm > WHO_DAILY[var]).mean()), 3) if len(dm) else None,
        "by_month": [None if np.isnan(x) else round(float(x), 1) for x in monthly],
        "by_month_hour": None if mh is None else [None if np.isnan(x) else round(float(x), 1) for x in mh.ravel()],
    }


def one(url: str, years: list[int], var: str) -> dict | None:
    name = url.rsplit("/", 1)[-1]
    done = CACHE / "points" / var / (name + ".json")
    if done.exists():
        out = json.loads(done.read_text())
        return out or None
    raw = CACHE / "raw" / name
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(get(url).content)
    try:
        out = fold(raw, years, var)
    finally:
        raw.unlink(missing_ok=True)
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text(json.dumps(out or {}))  # {} = looked at, nothing usable in the years
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="IT")
    ap.add_argument("--years", default="2020-2024")
    ap.add_argument("--vars", default=",".join(VARS))
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    y0, y1 = map(int, args.years.split("-"))
    years = list(range(y0, y1 + 1))
    meta = station_meta(args.country)

    rows = []
    for var in args.vars.split(","):
        urls = series_urls(args.country, CODES[var], years)
        log.info("%s: %d sampling points to look at", var, len(urls))
        t0 = time.time()
        with ThreadPoolExecutor(args.workers) as pool:
            futures = {pool.submit(one, u, years, var): u for u in urls}
            for i, f in enumerate(as_completed(futures), 1):
                try:
                    r = f.result()
                except Exception as e:  # one bad file is one station fewer, not a dead run
                    log.error("%s: %s", futures[f].rsplit("/", 1)[-1], e)
                    continue
                if r:
                    rows.append(r)
                if i % 100 == 0:
                    log.info("  %s %d/%d in %.0f s", var, i, len(urls), time.time() - t0)
        log.info("%s: %d points with data in %s", var, sum(r["var"] == var for r in rows), args.years)

    table = pd.DataFrame(rows).merge(meta, left_on="point", right_on="SamplingPoint", how="left")
    lost = table.Latitude.isna().sum()
    if lost:
        log.warning("%d points have no metadata (no coordinates) and are dropped", lost)
    table = table.dropna(subset=["Latitude", "Longitude"]).drop(columns=["SamplingPoint", "code"])
    out = CACHE / f"{args.country}_{y0}-{y1}.parquet"
    table.to_parquet(out, index=False)
    log.info("wrote %s: %d rows; %s", out.name, len(table), table.groupby("var").size().to_dict())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    main()
