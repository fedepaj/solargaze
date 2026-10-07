"""
OpenAQ (api.openaq.org v3 and its public S3 archive) for the countries where
its providers are national networks that allow redistribution. Today: Japan,
whose Ministry of the Environment network (Soramame / AEROS) OpenAQ carries
from 2023-07-14 under the Government Standard Terms of Use v2.0; and Mexico,
whose national SINAICA network (INECC) and the US embassy monitors (AirNow) it
carries under Libre Uso MX and US Public Domain. Low-cost sensor providers
(AirGradient, Clarity: isMonitor false) and providers with no licence are left out.

Licence is a property of the provider, not of OpenAQ: a location is used only
if its licence has ``redistributionAllowed`` (read from /v3/licenses), and
each row states the licence of its own location. Station lists come from the
API (key in $OPENAQ_API_KEY); hourly values from the archive's one file per
location and day, which needs no key. Values are folded to month × local hour
as air_stations.py does, in the location's own time zone, in µg/m³.

Honesty notes. OpenAQ has no station type or area for these networks, so both
are "unknown". Capture is measured over the requested years, clipped to the
dates OpenAQ has data for the country (it starts in 2023 for Japan).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from . import Importer, VARS  # noqa: E402
from sg.errors import NoData, Upstream  # noqa: E402

log = logging.getLogger("openaq")

API = "https://api.openaq.org/v3"
ARCHIVE = "https://openaq-data-archive.s3.amazonaws.com/records/csv.gz"
CACHE = Path(__file__).resolve().parents[1] / "cache" / "stations" / "openaq"
# Countries whose OpenAQ providers were read and found to be national networks
# with a licence that allows redistribution (checked again per location).
COUNTRIES = {"JP", "MX"}
# OpenAQ's parameter names → the app's; and µg/m³ per unit of the reported one
# (ppb → µg/m³ at 20 °C: NO₂ × 1.88, O₃ × 1.96; ppm is a thousand ppb).
PARAM = {"no2": "nitrogen_dioxide", "pm10": "pm10", "pm25": "pm2_5", "o3": "ozone"}
FACTOR = {
    ("nitrogen_dioxide", "ppb"): 1.88, ("nitrogen_dioxide", "ppm"): 1880.0,
    ("ozone", "ppb"): 1.96, ("ozone", "ppm"): 1960.0,
    ("pm10", "µg/m³"): 1.0, ("pm2_5", "µg/m³"): 1.0,
    ("nitrogen_dioxide", "µg/m³"): 1.0, ("ozone", "µg/m³"): 1.0,
}
WHO_DAILY = {"pm2_5": 15, "pm10": 45, "nitrogen_dioxide": 25, "ozone": 100}


_local = threading.local()


def _get(url: str, tries: int = 6, headers: dict | None = None, params: dict | None = None):
    """A request that waits out a hiccup or the rate limit; None for a 404."""
    import requests
    if not hasattr(_local, "session"):   # one connection per thread: a TLS handshake per daily file is three times the time
        _local.session = requests.Session()
    for i in range(tries):
        try:
            r = _local.session.get(url, headers=headers, params=params, timeout=(20, 120))
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.ConnectionError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout):
            if i == tries - 1:
                raise
            time.sleep(5 * 3 ** i if i else 2)
    raise AssertionError


def _date(stamp: str) -> date:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()


class OpenAQ(Importer):
    name = "openaq"
    source = "https://api.openaq.org/v3 and https://openaq-data-archive.s3.amazonaws.com (OpenAQ; Japan: japan-soramame, Ministry of the Environment; Mexico: SINAICA, INECC, and AirNow)"
    licence = "per provider, as listed by OpenAQ; Japan: Government Standard Terms of Use v2.0 (CC BY 4.0 compatible); Mexico: Libre Uso MX (SINAICA), US Public Domain (AirNow)"
    MIN_CAPTURE = 0.25   # of the period; a point with less is left out
    MIN_CELL = 10        # hours in a month × hour cell for it to count
    workers = 64
    cache: Path | None = CACHE

    def covers(self, country: str) -> bool:
        return country.upper() in COUNTRIES

    # -- network access: everything that touches the network is in these three --

    def _api(self, path: str, **params) -> list[dict]:
        key = os.environ.get("OPENAQ_API_KEY")
        if not key:
            raise Upstream("OPENAQ_API_KEY is not set")
        out, page = [], 1
        while True:
            r = _get(f"{API}/{path}", headers={"X-API-Key": key}, params={**params, "limit": 1000, "page": page})
            res = r.json()["results"]
            out += res
            if len(res) < 1000:
                return out
            page += 1

    def _day(self, loc: int, day: date) -> str | None:
        """One location's hourly values for one local day, CSV text, or None."""
        r = _get(f"{ARCHIVE}/locationid={loc}/year={day.year}/month={day:%m}/location-{loc}-{day:%Y%m%d}.csv.gz")
        return None if r is None else gzip.decompress(r.content).decode()

    # -- what OpenAQ says about the country --

    def _country(self, country: str) -> dict:
        rows = [c for c in self._api("countries") if c["code"] == country]
        if not rows:
            raise NoData(f"OpenAQ has no country {country}")
        return rows[0]

    def _locations(self, country_id: int) -> list[dict]:
        allowed = {l["id"] for l in self._api("licenses") if l.get("redistributionAllowed")}
        locs = self._api("locations", countries_id=country_id)
        return [l for l in locs if l.get("licenses") and l["licenses"][0]["id"] in allowed and l.get("isMonitor", True)]

    # -- folding --

    def fold(self, frames: list[pd.DataFrame], years: list[int], var: str, unit: str, tz: str, start: date, end: date) -> dict | None:
        """One location's daily files for one pollutant → the common schema's numbers."""
        factor = FACTOR.get((var, unit))
        if factor is None:
            raise Upstream(f"{var} in unit {unit!r}: no conversion to µg/m³")
        if not frames:
            return None
        df = pd.concat(frames).drop_duplicates("datetime")
        t = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert(tz)
        v = pd.to_numeric(df["value"], errors="coerce").to_numpy(dtype=float)
        keep = t.dt.year.isin(years).to_numpy() & np.isfinite(v) & (v > -0.0001)
        t, v = t[keep], np.clip(v[keep], 0, None) * factor
        hours_in = ((end - start).days + 1) * 24
        if len(v) / hours_in < self.MIN_CAPTURE:
            return None
        m, h = t.dt.month.to_numpy() - 1, t.dt.hour.to_numpy()
        s = np.zeros((12, 24)); n = np.zeros((12, 24))
        np.add.at(s, (m, h), v); np.add.at(n, (m, h), 1)
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # a month with no cell is NaN, as intended
            mh = np.where(n >= self.MIN_CELL, s / n, np.nan)
            monthly = np.nanmean(mh, axis=1) if np.isfinite(mh).any() else np.full(12, np.nan)
        daily = pd.Series(v, index=t.dt.tz_localize(None)).resample("D")
        dm = daily.mean()[daily.count() >= 18]
        return {
            "var": var, "resolution": "hour", "values": int(len(v)), "capture": round(min(len(v) / hours_in, 1.0), 3),
            "annual_mean": round(float(np.nanmean(monthly)), 2) if np.isfinite(monthly).any() else None,
            "days_over_who": round(float((dm > WHO_DAILY[var]).mean()), 3) if len(dm) else float("nan"),
            "by_month": [None if np.isnan(x) else round(float(x), 1) for x in monthly],
            "by_month_hour": [None if np.isnan(x) else round(float(x), 1) for x in mh.ravel()],
        }

    def _location(self, loc: dict, years: list[int], variables, start: date, end: date) -> list[dict]:
        sensors = {}
        for s in loc["sensors"]:
            var = PARAM.get(s["parameter"]["name"])
            if var in variables:
                sensors[var] = s["parameter"]["units"]
        if not sensors:
            return []
        if not loc.get("datetimeFirst") or not loc.get("datetimeLast"):
            return []
        first = max(start, _date(loc["datetimeFirst"]["utc"]) - timedelta(1))   # a day of margin for the time zone
        last = min(end, _date(loc["datetimeLast"]["utc"]) + timedelta(1))
        days = [first + timedelta(i) for i in range((last - first).days + 1)]
        # a folded location is kept, so a run that was killed carries on (the file names the days it covers)
        done = self.cache / f"{loc['id']}_{first}_{last}_{years[0]}-{years[-1]}_{'-'.join(sorted(sensors))}.json" if self.cache else None
        if done and done.exists():
            return json.loads(done.read_text())
        per_var: dict[str, list[pd.DataFrame]] = {v: [] for v in sensors}
        for day in days:
            text = self._day(loc["id"], day)
            if not text:
                continue
            df = pd.read_csv(io.StringIO(text), usecols=["datetime", "parameter", "units", "value"])
            for var in sensors:
                part = df[df["parameter"].map(PARAM) == var]
                if len(part):
                    # a location can report one gas in two units: convert each row by its own
                    f = part["units"].map(lambda u: FACTOR.get((var, u)))
                    if f.isna().any():
                        raise Upstream(f"{var} in unit {part['units'][f.isna()].iloc[0]!r}: no conversion to µg/m³")
                    per_var[var].append(part.assign(value=pd.to_numeric(part["value"], errors="coerce") * f))
        rows = []
        for var, frames in per_var.items():
            f = self.fold(frames, years, var, "µg/m³", loc["timezone"], start, end)
            if f is None or f["annual_mean"] is None:
                continue
            c = loc["coordinates"]
            rows.append({
                "station": str(loc["id"]), "point": f"{loc['id']}-{var}", "lat": float(c["latitude"]), "lon": float(c["longitude"]),
                "alt": float("nan"), "type": "unknown", "area": "unknown", **f,
                "source": self.name, "licence": loc["licenses"][0]["name"],
            })
        if done:
            done.parent.mkdir(parents=True, exist_ok=True)
            done.write_text(json.dumps(rows))
        return rows

    def stations(self, country: str, years: list[int], variables=VARS) -> pd.DataFrame:
        country = country.upper()
        if not self.covers(country):
            raise NoData(f"{country}: no OpenAQ provider with a redistributable licence has been vetted")
        info = self._country(country)
        first = _date(info["datetimeFirst"])
        start = max(date(years[0], 1, 1), first)
        end = min(date(years[-1], 12, 31), datetime.now(timezone.utc).date())
        if start > end:
            raise NoData(f"OpenAQ has {country} only from {first}, after {years[-1]}")
        locs = self._locations(info["id"])
        log.info("%s: %d locations with a redistributable licence", country, len(locs))
        rows: list[dict] = []
        with ThreadPoolExecutor(self.workers) as pool:
            futures = [pool.submit(self._location, l, years, list(variables), start, end) for l in locs]
            for i, f in enumerate(futures, 1):
                try:
                    rows += f.result()
                except Upstream:
                    raise
                except Exception as e:  # noqa: BLE001 — one bad location is one station fewer
                    log.error("location: %s", e)
                if i % 100 == 0:
                    log.info("  %d/%d locations", i, len(futures))
        if not rows:
            raise NoData(f"{country}: no OpenAQ location with usable data in {years[0]}–{years[-1]}")
        return pd.DataFrame(rows)
