"""
Monitoring stations and what is around them, on the bucket, for the
street-scale air model of each country.

    air/stations/<CC>_<years>.parquet       every station of a country, in the
                                            importers' common shape (importers/)
    air/features/<CC>/<region>.parquet      the predictors (air_features.py) at
                                            the stations of one region, or why
                                            not, one row a station

The table of a country is fetched once, by an air job (it needs no
extract, and the importers take their time: the EEA's series, the EPA's
yearly files). The predictors need the roads and buildings around each
station, so they are computed by the region's own build job, which has the
extract indexed, a few minutes a night until the region's stations are
done. A station whose window runs outside the extract (a border), or whose
rasters cannot be read, is noted with the reason and not asked again; a
new extract format or a new station table starts the region again.
"""

from __future__ import annotations

import io
import time

YEARS = list(range(2020, 2025))
PREFIX = "air/"


def table_key(country: str) -> str:
    return f"{PREFIX}stations/{country}_{YEARS[0]}-{YEARS[-1]}.parquet"


def features_key(country: str, region: str) -> str:
    return f"{PREFIX}features/{country}/{region}.parquet"


def _get(remote, key: str):
    import pandas as pd
    from botocore.exceptions import ClientError
    try:
        raw = remote._call(lambda: remote.s3.get_object(Bucket=remote.ops, Key=key)["Body"].read())
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    return pd.read_parquet(io.BytesIO(raw))


def _put(remote, key: str, df) -> None:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    remote._call(lambda: remote.s3.put_object(Bucket=remote.ops, Key=key, Body=buf.getvalue()))


def table(remote, country: str):
    """The country's stations, or None if not fetched yet."""
    return _get(remote, table_key(country))


def fetch_table(remote, country: str, log=None):
    """The country's stations from its importer, validated and kept on the
    bucket; None where no importer covers it."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from importers import importer_for, validate
    imp = importer_for(country)
    if imp is None:
        if log:
            log.warn("stations.none", country=country, msg="no importer covers this country")
        return None
    t0 = time.time()
    df = validate(imp.stations(country, YEARS), imp.name)
    _put(remote, table_key(country), df)
    if log:
        log.info("stations.fetched", country=country, importer=imp.name, rows=len(df),
                 stations=int(df.station.nunique()), seconds=round(time.time() - t0))
    return df


def features_for_region(remote, country: str, region: str, tiles: list[str], seconds: float, log=None,
                        compute=None) -> tuple[int, int, int]:
    """The predictors at the stations of the region's tiles that have none
    yet, for at most `seconds`. Returns (computed now, noted as not
    computable now, left for another night)."""
    import pandas as pd
    from tiles import Tile
    stations = table(remote, country)
    if stations is None:
        return 0, 0, 0
    if compute is None:
        import air_features as af
        compute = af.features_at
    here = set(tiles)
    sites = stations.groupby("station").agg(lat=("lat", "first"), lon=("lon", "first")).reset_index()
    sites = sites[[Tile.containing(a, o).id in here for a, o in zip(sites.lat, sites.lon)]]
    have = _get(remote, features_key(country, region))
    done = set(have.station) if have is not None else set()
    todo = sites[~sites.station.isin(done)]
    rows, noted, t0 = [], 0, time.time()
    for r in todo.itertuples():
        if time.time() - t0 > seconds:
            break
        try:
            rows.append({"station": r.station, "lat": r.lat, "lon": r.lon, "reason": "", **compute(r.lat, r.lon)})
        except Exception as exc:  # noqa: BLE001 — a station is a station; the reason is kept with it
            from . import errors
            if errors.classify(exc) == errors.TRANSIENT:
                continue        # the network's, not the station's: asked again another night
            rows.append({"station": r.station, "lat": r.lat, "lon": r.lon,
                         "reason": f"{errors.classify(exc)}: {errors.signature(exc)}"[:200]})
            noted += 1
    if rows:
        new = pd.DataFrame(rows)
        _put(remote, features_key(country, region), new if have is None else pd.concat([have, new], ignore_index=True))
    left = len(todo) - len(rows)   # those not reached, and those the network kept
    if log and (rows or left):
        log.info("stations.features", country=country, region=region, computed=len(rows) - noted, noted=noted, left=left,
                 seconds=round(time.time() - t0))
    return len(rows) - noted, noted, left


# ---------------------------------------------------------------- CAMS at the stations
#
#     air/cams/<CC>/<region>.parquet    CAMS's annual mean at each station of
#                                       the region, per pollutant: what the
#                                       model of the street is fitted against
#
# Taken from the air job's own fold, with the nodes round the stations added
# to the tiles': a rural station whose tile has too few buildings for an air
# tile still gets its CAMS, and nothing is asked of the Data Store twice.

def importable() -> set[str]:
    """The countries an importer covers, known without asking the network."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from importers.eea import KNOWN
    from importers.openaq import COUNTRIES as OPENAQ
    return set(KNOWN) | set(OPENAQ) | {"US", "CA", "BR"}


def cams_key(country: str, region: str) -> str:
    return f"{PREFIX}cams/{country}/{region}.parquet"


def keys(remote, sub: str) -> set[str]:
    """Every key under air/<sub>/ on the ops bucket."""
    out = set()
    for page in remote.s3.get_paginator("list_objects_v2").paginate(Bucket=remote.ops, Prefix=f"{PREFIX}{sub}/"):
        out |= {o["Key"] for o in page.get("Contents", [])}
    return out


def points_in(stations, tiles: list[str]):
    """The stations of the table inside the tiles: station, lat, lon."""
    from tiles import Tile
    here = set(tiles)
    sites = stations.groupby("station").agg(lat=("lat", "first"), lon=("lon", "first")).reset_index()
    return sites[[Tile.containing(a, o).id in here for a, o in zip(sites.lat, sites.lon)]].reset_index(drop=True)


def corners(lat: float, lon: float) -> list[tuple[float, float]]:
    """The four 0.1° nodes round a point, as air_cams.nodes_for names them."""
    import math
    la, lo = math.floor(lat * 10 + 1e-9) / 10, math.floor(lon * 10 + 1e-9) / 10
    return [(round(la + dy, 2), round(lo + dx, 2)) for dy in (0, 0.1) for dx in (0, 0.1)]


def cams_at(fold, points):
    """CAMS's annual mean at each point, per pollutant, bilinear between the
    four nodes round it as the app interpolates: station, var, cams."""
    import numpy as np
    import pandas as pd
    index = {node: k for k, node in enumerate(fold.wanted)}
    rows = []
    for var in fold.sum:
        with np.errstate(invalid="ignore", divide="ignore"):
            annual = np.nanmean(np.where(fold.n[var] > 0, fold.sum[var] / fold.n[var], np.nan).reshape(len(index), -1), axis=1)
        for p in points.itertuples():
            c = corners(p.lat, p.lon)
            if not all(n in index for n in c):
                continue
            c00, c01, c10, c11 = (annual[index[n]] for n in c)
            fy, fx = (p.lat - c[0][0]) * 10, (p.lon - c[0][1]) * 10
            v = c00 * (1 - fy) * (1 - fx) + c01 * (1 - fy) * fx + c10 * fy * (1 - fx) + c11 * fy * fx
            if np.isfinite(v):
                rows.append({"station": p.station, "var": var, "cams": round(float(v), 3)})
    return pd.DataFrame(rows, columns=["station", "var", "cams"])


def save_cams(remote, country: str, region: str, df) -> None:
    _put(remote, cams_key(country, region), df)


# ---------------------------------------------------------------- the models
#
#     air/models/<CC>.json    the street model of a country: one regression a
#                             pollutant (air_lur.fit), its validation, and
#                             whether it beats CAMS, which decides whether the
#                             street layer applies it
#
# A country with fewer than MIN_STATIONS stations ready for a pollutant
# borrows the model fitted on every station of its scope (Europe's, the
# world's), said so in the model.

MIN_STATIONS = 30


def models_key(country: str) -> str:
    return f"{PREFIX}models/{country}.json"


def _all(remote, keys_: set[str]):
    import pandas as pd
    frames = [f for f in (_get(remote, k) for k in sorted(keys_)) if f is not None and len(f)]
    return pd.concat(frames, ignore_index=True) if frames else None


def fit_table(stations, features, cams):
    """One row per (station, pollutant) with what the fit needs, in the
    column names air_lur.fit reads; and the predictor columns."""
    from tiles import Tile
    feats = features[features.reason.fillna("") == ""].drop(columns=["reason", "lat", "lon"], errors="ignore")
    cols = [c for c in feats.columns if c != "station"]
    t = (stations[["station", "var", "lat", "lon", "type", "area", "capture", "annual_mean"]]
         .merge(feats, on="station").merge(cams, on=["station", "var"]))
    t = t.rename(columns={"station": "AirQualityStation", "type": "AirQualityStationType", "area": "AirQualityStationArea"})
    t["tile"] = [Tile.containing(a, o).id for a, o in zip(t.lat, t.lon)]
    return t.dropna(subset=["annual_mean", "cams"]), cols


def fit_country(country: str, table, cols, pooled_from: str | None = None) -> dict:
    import air_lur
    from datetime import datetime, timezone
    air_lur.FEATURE_COLS = cols
    out = {"country": country, "fitted": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "pooled": pooled_from, "models": {}}
    for var in air_lur.STREET_VARS:
        d = table[table["var"] == var]
        if d.AirQualityStation.nunique() < MIN_STATIONS:
            continue
        m = air_lur.fit(var, d)
        v = m["validation"]
        m["beats_cams"] = bool(v["lur"]["rmse"] < v["cams_alone"]["rmse"] and v["lur"]["rmse"] < v["cams_bias_corrected"]["rmse"])
        out["models"][var] = m
    return out


def fit_all(remote, scopes: dict[str, list[str]], log=None) -> dict[str, str]:
    """Fit every country whose stations are ready and have changed since its
    last fit; a pollutant with too few stations of its own takes its scope's
    pooled model, fitted only when one is wanted. {country: what happened}."""
    import hashlib
    import json
    import pandas as pd
    tables, features, cams_keys = keys(remote, "stations"), keys(remote, "features"), keys(remote, "cams")
    ready = {}
    for key in tables:
        country = key.rsplit("/", 1)[-1].split("_", 1)[0]
        feats = _all(remote, {k for k in features if k.startswith(f"{PREFIX}features/{country}/")})
        cams = _all(remote, {k for k in cams_keys if k.startswith(f"{PREFIX}cams/{country}/")})
        if feats is None or cams is None:
            continue
        t, cols = fit_table(table(remote, country), feats, cams)
        if len(t):
            ready[country] = (t, cols)
    done = {}
    for scope, countries in scopes.items():
        here = [c for c in countries if c in ready]
        pool = None
        for c in here:
            t, cols = ready[c]
            sig = hashlib.md5("|".join(sorted(f"{a}/{v}" for a, v in zip(t.AirQualityStation, t["var"]))).encode()).hexdigest()[:12]
            old = _get_json(remote, models_key(c))
            if old and old.get("basis") == sig:
                done[c] = "unchanged"
                continue
            m = fit_country(c, t, cols)
            missing = [v for v in __import__("air_lur").STREET_VARS if v not in m["models"]]
            if missing:
                if pool is None:
                    pooled = pd.concat([ready[x][0] for x in here], ignore_index=True)
                    pool = fit_country(scope, pooled, sorted(set.intersection(*(set(ready[x][1]) for x in here))))
                for v in missing:
                    if v in pool["models"]:
                        m["models"][v] = dict(pool["models"][v], pooled_from=scope)
            m["basis"] = sig
            remote._call(lambda: remote.s3.put_object(Bucket=remote.ops, Key=models_key(c),
                                                      Body=json.dumps(m, indent=1).encode()))
            done[c] = ", ".join(
                f"{v} {'pooled' if 'pooled_from' in mv else 'own'} RMSE {mv['validation']['cams_alone']['rmse']}"
                f"→{mv['validation']['lur']['rmse']}{'' if mv['beats_cams'] else ' (not applied)'}"
                for v, mv in m["models"].items()) or "no pollutant with enough stations"
            if log:
                log.info("air.model", country=c, result=done[c])
    return done


def _get_json(remote, key: str):
    import json
    from botocore.exceptions import ClientError
    try:
        return json.loads(remote._call(lambda: remote.s3.get_object(Bucket=remote.ops, Key=key)["Body"].read()))
    except ClientError:
        return None


def model(remote, country: str) -> dict | None:
    return _get_json(remote, models_key(country))
