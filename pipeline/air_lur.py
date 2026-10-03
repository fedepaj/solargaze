"""
Street-scale air: a land-use regression on top of CAMS.

CAMS gets the countryside right and misses the street: at Italian traffic
stations it reads about half the NO₂ that is measured (see the station
comparison in air_stations.py's output). The correction is learned here as

    ln(station / CAMS) = b0 + Σ b_k · predictor_k

with the predictors of air_features.py — what roads, buildings, land cover
and terrain surround the station — and then applied at every 50 m cell of a
tile, where the app multiplies the CAMS month × hour tables by it.

    pipeline/.venv/bin/python pipeline/air_lur.py features        # predictors at every station, cached
    pipeline/.venv/bin/python pipeline/air_lur.py fit             # one model per pollutant, with its validation
    pipeline/.venv/bin/python pipeline/air_lur.py tiles lazio     # the 50 m correction for every built tile of a region

Method, kept classical so it can be read: forward selection, one predictor
at a time, of the one that most lowers the cross-validated error, among
those whose coefficient has the sign the physics expects (more road, more
pollution; more green, less), stopping when the gain is under 1 % or at
eight predictors. Fits are weighted by data capture. Validation is by
spatial blocks — every station of a quarter-degree tile left out together —
because stations a street apart share their errors, and leaving one out at a
time flatters the model. Both the model and CAMS alone are scored the same
way, on concentrations, so the gain is a number, not a claim.

Ozone is fitted too, but only to check the titration the app uses instead:
where NO₂ rises at street level, O₃ falls by about as much (Ox = O₃ + NO₂ is
conserved at that scale), which needs no model of its own.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiles import STEP, Tile  # noqa: E402
import netdns  # noqa: E402,F401  (falls back to DNS over HTTPS when the system resolver drops a name)

log = logging.getLogger("air.lur")

CACHE = Path(__file__).resolve().parent / "cache"
STATIONS = CACHE / "stations"
FEATURES = STATIONS / "features"
YEARS = "2020-2024"
MAX_TERMS = 8
MIN_GAIN = 0.01
EPS = 1.0  # µg/m³ added to both sides of the ratio, so a clean rural zero does not explode the log


# ---------------------------------------------------------------- predictors at the stations

def station_sites() -> pd.DataFrame:
    st = pd.read_parquet(STATIONS / f"IT_{YEARS}.parquet")
    sites = st.groupby("AirQualityStation").agg(lat=("Latitude", "first"), lon=("Longitude", "first")).reset_index()
    sites["tile"] = [Tile.containing(a, o).id for a, o in zip(sites.lat, sites.lon)]
    return sites.sort_values("tile")  # neighbours share their OSM tiles in the cache


def compute_features() -> None:
    import air_features as af
    FEATURES.mkdir(parents=True, exist_ok=True)
    sites = station_sites()
    todo = [r for r in sites.itertuples() if not (FEATURES / f"{r.AirQualityStation}.json").exists()]
    log.info("%d stations, %d to do", len(sites), len(todo))
    t0, missing = time.time(), 0
    for i, r in enumerate(todo, 1):
        try:
            f = af.features_at(r.lat, r.lon)
        except af.MissingData as e:
            missing += 1
            log.warning("%s: %s", r.AirQualityStation, e)
            continue
        (FEATURES / f"{r.AirQualityStation}.json").write_text(json.dumps(f))
        if i % 50 == 0:
            log.info("  %d/%d in %.0f s", i, len(todo), time.time() - t0)
    log.info("done; %d stations not covered yet", missing)


# ---------------------------------------------------------------- CAMS at the stations

def cams_annual() -> dict[str, dict[tuple[float, float], float]]:
    """Per pollutant: annual mean at every 0.1° node of every cached fold."""
    from air_cams import VARS
    nodes, vals = [], {v: [] for v in VARS}
    for p in sorted((CACHE / "cams_bulk").glob(f"folded_*_{YEARS}.npz")):
        z = np.load(p, allow_pickle=True)
        nodes.append(np.array([tuple(x) for x in z["wanted"]], dtype=float))
        for v in VARS:
            s, n = z[f"{v}_sum"], z[f"{v}_n"]
            with np.errstate(invalid="ignore"):
                vals[v].append(np.nanmean(np.where(n > 0, s / n, np.nan).reshape(len(s), -1), axis=1))
    allnodes = [(round(a, 2), round(o, 2)) for a, o in np.concatenate(nodes)]
    return {v: dict(zip(allnodes, np.concatenate(vals[v]))) for v in VARS}


def cams_at(lookup: dict, lat: float, lon: float) -> float:
    """Bilinear between the four 0.1° nodes around the point, as the app interpolates."""
    la0, lo0 = math.floor(lat * 10) / 10, math.floor(lon * 10) / 10
    fy, fx = (lat - la0) * 10, (lon - lo0) * 10
    corners = [lookup.get((round(la0 + dy, 2), round(lo0 + dx, 2))) for dy in (0, .1) for dx in (0, .1)]
    if any(c is None or not np.isfinite(c) for c in corners):
        return float("nan")
    c00, c01, c10, c11 = corners
    return float(c00 * (1 - fy) * (1 - fx) + c01 * (1 - fy) * fx + c10 * fy * (1 - fx) + c11 * fy * fx)


# ---------------------------------------------------------------- the regression

def design(f: dict) -> dict:
    """Predictors in the units the coefficients are read in."""
    d = {}
    for k, v in f.items():
        if k.startswith("road_"):
            d[k] = np.asarray(v, float) / 1000  # km of road
        elif k.startswith("bld_vol"):
            d[k] = np.asarray(v, float) / 1e6  # millions of m³
        elif k == "dist_major":
            d["log_dist_major"] = np.log(np.asarray(v, float) + 10)
        else:
            d[k] = np.asarray(v, float)
    return d


def expected_sign(name: str, var: str) -> int:
    """+1, -1, or 0 for either. Primary pollutants rise with traffic and
    building, fall with green and altitude; ozone the other way round."""
    if name.startswith(("road_", "bld_", "builtup_")):
        s = 1
    elif name.startswith(("green_", "log_dist_major", "elev")):
        s = -1
    else:
        return 0
    return -s if var == "ozone" else s


def wls(X, y, w):
    sw = np.sqrt(w)
    beta, *_ = np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)
    return beta


def cv_predict(X, y, w, groups) -> np.ndarray:
    """Out-of-block predictions of ln ratio, one block (tile) at a time."""
    pred = np.empty_like(y)
    for g in np.unique(groups):
        test = groups == g
        beta = wls(X[~test], y[~test], w[~test])
        pred[test] = X[test] @ beta
    return pred


def scores(obs, pred) -> dict:
    res = obs - pred
    return {"rmse": round(float(np.sqrt(np.mean(res ** 2))), 2),
            "bias": round(float(np.mean(res)), 2),
            "r2": round(float(1 - np.sum(res ** 2) / np.sum((obs - obs.mean()) ** 2)), 3)}


def fit(var: str, table: pd.DataFrame) -> dict:
    d = table[table["var"] == var].dropna(subset=["cams", "annual_mean"])
    # One sampler per station: the one that covered the period best.
    d = d.sort_values("capture", ascending=False).drop_duplicates("AirQualityStation")
    feats = design({c: d[c].to_numpy() for c in FEATURE_COLS})
    names = sorted(feats)
    y = np.log((d.annual_mean.to_numpy() + EPS) / (d.cams.to_numpy() + EPS))
    w = d.capture.to_numpy()
    groups = d.tile.to_numpy()
    obs, cams = d.annual_mean.to_numpy(), d.cams.to_numpy()
    to_conc = lambda lr: (cams + EPS) * np.exp(lr) - EPS  # noqa: E731

    chosen: list[str] = []
    X = np.ones((len(d), 1))
    best = scores(obs, to_conc(cv_predict(X, y, w, groups)))
    log.info("%s: %d stations; intercept only: CV RMSE %.2f", var, len(d), best["rmse"])
    while len(chosen) < MAX_TERMS:
        trial = None
        for name in names:
            if name in chosen:
                continue
            Xt = np.column_stack([X, feats[name]])
            beta = wls(Xt, y, w)
            sign = expected_sign(name, var)
            if sign and np.sign(beta[-1]) != sign:
                continue
            # A term must not flip the sign of one already in.
            if any(expected_sign(n, var) and np.sign(b) != expected_sign(n, var) for n, b in zip(chosen, beta[1:-1])):
                continue
            s = scores(obs, to_conc(cv_predict(Xt, y, w, groups)))
            if trial is None or s["rmse"] < trial[1]["rmse"]:
                trial = (name, s)
        if trial is None or trial[1]["rmse"] > best["rmse"] * (1 - MIN_GAIN):
            break
        chosen.append(trial[0])
        X = np.column_stack([X, feats[trial[0]]])
        best = trial[1]
        log.info("  + %-18s CV RMSE %.2f  R² %.3f", trial[0], best["rmse"], best["r2"])

    beta = wls(X, y, w)
    fitted = to_conc(X @ beta)
    cv_conc = to_conc(cv_predict(X, y, w, groups))
    kinds = d.AirQualityStationType + "/" + d.AirQualityStationArea
    return {
        "var": var,
        "stations": int(len(d)),
        "terms": ["intercept"] + chosen,
        "coefficients": [round(float(b), 6) for b in beta],
        "units": {"road_*": "km within the radius", "bld_vol_*": "million m³", "log_dist_major": "ln(m + 10)",
                  "*_frac_*, green_*, builtup_*, water_*": "share 0–1", "elev*": "m"},
        "target": f"ln((station + {EPS}) / (CAMS + {EPS})), annual means {YEARS}",
        # Applied away from the stations, the model is held to the range of
        # ratios it was fitted on: a motorway interchange denser than any
        # station's surroundings is not evidence of a ratio no station saw.
        "ln_ratio_range": [round(float((X @ beta).min()), 4), round(float((X @ beta).max()), 4)],
        "validation": {
            "method": "blocked cross-validation, every station of a quarter-degree tile left out together",
            "cams_alone": scores(obs, cams),
            "cams_bias_corrected": scores(obs, to_conc(cv_predict(np.ones((len(d), 1)), y, w, groups))),
            "lur": best,
            "lur_in_sample": scores(obs, fitted),
        },
        "by_station_type": {
            k: {"n": int(m.sum()), "mean_obs": round(float(obs[m].mean()), 1),
                "cams_rmse": scores(obs[m], cams[m])["rmse"], "lur_cv_rmse": scores(obs[m], cv_conc[m])["rmse"],
                "cams_bias": scores(obs[m], cams[m])["bias"], "lur_cv_bias": scores(obs[m], cv_conc[m])["bias"]}
            for k in sorted(kinds.unique()) for m in [(kinds == k).to_numpy()] if m.sum() >= 10
        },
    }


FEATURE_COLS: list[str] = []


def fit_all() -> None:
    global FEATURE_COLS
    st = pd.read_parquet(STATIONS / f"IT_{YEARS}.parquet")
    rows = []
    for p in FEATURES.glob("*.json"):
        rows.append({"AirQualityStation": p.stem, **json.loads(p.read_text())})
    feats = pd.DataFrame(rows)
    FEATURE_COLS = [c for c in feats.columns if c != "AirQualityStation"]
    table = st.merge(feats, on="AirQualityStation", how="inner")
    table["tile"] = [Tile.containing(a, o).id for a, o in zip(table.Latitude, table.Longitude)]
    cams = cams_annual()
    table["cams"] = [cams_at(cams[v], a, o) for v, a, o in zip(table["var"], table.Latitude, table.Longitude)]
    log.info("%d station rows with predictors; %d without CAMS cover yet", len(table), table.cams.isna().sum())
    out = {}
    for var in ("nitrogen_dioxide", "pm10", "pm2_5", "ozone"):
        out[var] = fit(var, table)
        v = out[var]["validation"]
        log.info("%s: CAMS RMSE %.2f → bias-corrected %.2f → LUR %.2f µg/m³ (R² %.2f → %.2f)", var,
                 v["cams_alone"]["rmse"], v["cams_bias_corrected"]["rmse"], v["lur"]["rmse"],
                 v["cams_alone"]["r2"], v["lur"]["r2"])
    (STATIONS / "lur_models.json").write_text(json.dumps(out, indent=2) + "\n")
    log.info("models written to %s", STATIONS / "lur_models.json")


# ---------------------------------------------------------------- the 50 m product

# Pollutants published at 50 m: those whose model beats CAMS in validation.
# PM2.5 does not (it is a regional pollutant; the street adds little), and
# ozone is drawn by the app from NO₂ by titration.
STREET_VARS = ("nitrogen_dioxide", "pm10")
CELLS = 556          # per tile side: 0.25° / 556 ≈ 0.00045°, 50 m north–south
LR_MIN, LR_STEP = -1.0, 0.01  # byte b ≥ 1 encodes ln ratio = LR_MIN + (b - 1) · LR_STEP, up to +1.5


def apply_model(model: dict, f: dict) -> np.ndarray:
    d = design(f)
    lr = np.full(len(next(iter(d.values()))), model["coefficients"][0])
    for name, b in zip(model["terms"][1:], model["coefficients"][1:]):
        lr = lr + b * d[name]
    return np.clip(lr, *model["ln_ratio_range"])


def street_tile(tile: Tile, models: dict) -> None:
    """The old command line: build straight into the tile's folder."""
    from tiles import write_meta
    write_meta(tile, "air_street", build_street(tile, tile.path / "air_street", models))


def build_street(tile: Tile, out: Path, models: dict) -> dict:
    """The 50 m ratios into out/; returns the meta entry, writes nothing else."""
    import air_features as af
    from PIL import Image
    from datetime import datetime, timezone
    w, s, e, n = tile.bounds
    step = STEP / CELLS
    lats = n - (np.arange(CELLS) + 0.5) * step  # row 0 north, as the other rasters
    lons = w + (np.arange(CELLS) + 0.5) * step
    LON, LAT = np.meshgrid(lons, lats)
    x, y = af.TO_LAEA.transform(LON.ravel(), LAT.ravel())
    win = af.Window.around(x.min(), y.min(), x.max(), y.max())
    t0 = time.time()
    f = af.features(win, x, y)
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for var in STREET_VARS:
        lr = apply_model(models[var], f).reshape(CELLS, CELLS)
        byte = np.clip(np.round((lr - LR_MIN) / LR_STEP) + 1, 1, 255).astype(np.uint8)
        Image.fromarray(byte, "L").save(out / f"{var}.png", optimize=True)
        files[var] = f"air_street/{var}.png"
        log.info("  %s %s: ratio median %.2f, 99th pct %.2f", tile.id, var, float(np.exp(np.median(lr))),
                 float(np.exp(np.percentile(lr, 99))))
    info = {
        "product": "Street-scale correction to the CAMS air-quality tables: a land-use regression ratio at 50 m",
        "use": "concentration(cell, month, hour) = (CAMS(month, hour) + 1) * exp(ln_ratio(cell)) - 1, µg/m³",
        "rows": CELLS, "cols": CELLS, "degrees_per_pixel": step,
        "encoding": {"channel": "L", "nodata_byte": 0, "byte1_ln_ratio": LR_MIN, "step_ln_ratio": LR_STEP},
        "files": files,
        "models": {v: {k: models[v][k] for k in ("terms", "coefficients", "ln_ratio_range", "stations", "validation")}
                   for v in STREET_VARS},
        "predictors": "OpenStreetMap roads and buildings (Geofabrik), ESA WorldCover 2021, Copernicus DEM GLO-30",
        "stations": f"EEA verified (E1a) hourly and daily data, {YEARS}",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caveat": "A statistical model of annual means fitted to monitoring stations, applied as one ratio to every month and hour, "
                  "and held to the range of ratios the stations show; it knows the roads, not the traffic on them. "
                  "Validated by leaving out whole tiles of stations.",
    }
    log.info("  %s done in %.0f s", tile.id, time.time() - t0)
    return info


def street_tiles(region: str) -> None:
    from run_region import REGIONS, tiles_in_bbox
    from tiles import read_meta
    models = json.loads((STATIONS / "lur_models.json").read_text())
    tiles = [t for t in tiles_in_bbox(*REGIONS[region]) if "air" in read_meta(t).get("products", {})]
    log.info("%d tiles with air in %s", len(tiles), region)
    for i, t in enumerate(tiles, 1):
        log.info("[%d/%d] %s", i, len(tiles), t.id)
        try:
            street_tile(t, models)
        except Exception as e:  # one tile is one tile; the log says which
            log.error("  %s failed: %s", t.id, e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["features", "fit", "tiles"])
    ap.add_argument("region", nargs="?", default="lazio")
    args = ap.parse_args()
    if args.command == "tiles":
        street_tiles(args.region)
    else:
        {"features": compute_features, "fit": fit_all}[args.command]()
