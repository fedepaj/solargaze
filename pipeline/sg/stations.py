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
