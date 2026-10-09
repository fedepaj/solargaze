"""
Road traffic noise, Lden in dB, at 10 m, from OpenStreetMap — a fast,
screened estimate in the spirit of CNOSSOS-EU, calibrated against the
official strategic noise maps of the Environmental Noise Directive (END).

What it is: for every road, a line source whose sound power per metre
depends on its class (the traffic it typically carries); for every 10 m
cell, the energy of all the roads within 1.2 km, spread over a hemisphere
(the ground reflects), and taken down where buildings stand in the way.
The screening is read from the extra distance sound must travel round the
buildings to reach a cell from the nearest road — the path difference of
Maekawa's barrier formula — once for the major roads and once for the
minor ones, so that a courtyard behind a boulevard is quiet and a side
street stays as loud as its own traffic.

What it is not: a noise map in the legal sense. It knows no traffic counts,
no speeds, no road surfaces, no noise barriers, no railways, trams or
aircraft. The sound power of each class is fitted to the END maps of one
city and checked on another (see calibrate() and the product's meta); where
a city has a real END map, that is the better source.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage, signal

sys.path.insert(0, str(Path(__file__).resolve().parent))

CELL_M = 10.0
REACH_M = 1200.0                     # sources farther than this add nothing that shows
MARGIN_M = REACH_M                   # roads outside the tile still reach into it
LAMBDA_M = 0.68                      # 500 Hz, the frequency road noise peaks near
MAX_SCREEN_DB = 22.0                 # multiple diffraction past a courtyard, no more

# OSM highway → class; links take their road's class.
CLASS_OF = {
    "motorway": "motorway", "motorway_link": "motorway",
    "trunk": "trunk", "trunk_link": "trunk",
    "primary": "primary", "primary_link": "primary",
    "secondary": "secondary", "secondary_link": "secondary",
    "tertiary": "tertiary", "tertiary_link": "tertiary",
    "residential": "local", "unclassified": "local", "living_street": "local", "road": "local",
    "service": "service",
}
CLASSES = ["motorway", "trunk", "primary", "secondary", "tertiary", "local", "service"]
# The extract's coarse classes (osm_extract.ROAD_CLASSES), for roads that carry
# nothing finer: each to the quieter end of what it groups.
FROM_COARSE = {"major": "primary", "secondary": "tertiary", "local": "local", "service": "service"}
MAJOR = {"motorway", "trunk", "primary", "secondary"}

# Sound power per metre, dB(A) re 1 pW/m, Lden-weighted. Starting values from
# typical CNOSSOS flows and speeds; calibrate() replaces them.
LW_DEFAULT = {"motorway": 101.0, "trunk": 97.0, "primary": 94.0, "secondary": 91.0,
              "tertiary": 87.0, "local": 79.0, "service": 74.0}

# Fitted (October 2026) to the END road Lden map of central Berlin
# (N52.50E13.25, 2.0 M open cells) with an excess attenuation of 12 dB/km,
# and checked on central Hamburg (N53.50E9.75, 1.8 M cells), never fitted on.
LW_FITTED = {"motorway": 88.26, "trunk": 86.32, "primary": 86.32, "secondary": 85.82,
             "tertiary": 77.33, "local": 66.61, "service": 66.61}
EXCESS_FITTED = 12.0
VALIDATION = {
    "reference": "EEA END strategic noise maps, roads Lden (2012 round), 5 dB bands",
    "fitted_on": {"place": "Berlin centre, N52.50E13.25", "same_band": 0.565, "within_one_band": 0.937},
    "checked_on": {"place": "Hamburg centre, N53.50E9.75", "same_band": 0.637, "within_one_band": 0.926,
                   "recall_65plus": 0.76, "precision_65plus": 0.72},
    # Italy reports no END map the EEA can show; Messina publishes its own
    # (2022 round, CC BY 4.0, dati.gov.it). The model is louder there: 56% of
    # the built-up cells at 55 dB or more against the map's 33%.
    "checked_on_italy": {"place": "Messina, N38.00E15.50 (END 2022, Comune di Messina)", "same_band": 0.494,
                         "within_one_band": 0.775, "recall_65plus": 0.79, "precision_65plus": 0.50},
}


# ---------------------------------------------------------------- inputs

def read_osm(pbf: Path, bounds) -> tuple[list, list]:
    """Roads (class, [(lon, lat)…]) and building outlines inside bounds."""
    import osmium
    w, s, e, n = bounds

    class H(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.roads, self.buildings = [], []

        def way(self, way):
            cls = CLASS_OF.get(way.tags.get("highway"))
            if cls and way.tags.get("tunnel") not in ("yes", "building_passage"):
                try:
                    pts = [(n_.lon, n_.lat) for n_ in way.nodes]
                except osmium.InvalidLocationError:
                    return
                if any(w <= x <= e and s <= y <= n for x, y in pts):
                    self.roads.append((cls, pts))
            if way.tags.get("building") and way.is_closed():
                try:
                    pts = [(n_.lon, n_.lat) for n_ in way.nodes]
                except osmium.InvalidLocationError:
                    return
                if any(w <= x <= e and s <= y <= n for x, y in pts):
                    self.buildings.append(pts)

    h = H()
    h.apply_file(str(pbf), locations=True)
    return h.roads, h.buildings


class Grid:
    """A 10 m grid over the tile plus a margin, in degrees."""

    def __init__(self, bounds, margin_m: float = MARGIN_M):
        w, s, e, n = bounds
        lat0 = (s + n) / 2
        self.dlat = CELL_M / 111_320
        self.dlon = CELL_M / (111_320 * math.cos(math.radians(lat0)))
        mlat, mlon = margin_m / 111_320, margin_m / (111_320 * math.cos(math.radians(lat0)))
        self.bounds = (w - mlon, s - mlat, e + mlon, n + mlat)
        self.cols = int(round((self.bounds[2] - self.bounds[0]) / self.dlon))
        self.rows = int(round((self.bounds[3] - self.bounds[1]) / self.dlat))
        self.inner = (int(round(mlat / self.dlat)), int(round(mlon / self.dlon)),
                      int(round((n - s) / self.dlat)), int(round((e - w) / self.dlon)))

    def px(self, lon, lat):
        return (np.asarray(lon) - self.bounds[0]) / self.dlon, (self.bounds[3] - np.asarray(lat)) / self.dlat

    def crop(self, a):
        r0, c0, h, wd = self.inner
        return a[r0:r0 + h, c0:c0 + wd]


def rasterise_buildings(grid: Grid, buildings) -> np.ndarray:
    img = Image.new("1", (grid.cols, grid.rows), 0)
    d = ImageDraw.Draw(img)
    for pts in buildings:
        x, y = grid.px([p[0] for p in pts], [p[1] for p in pts])
        if len(pts) >= 3:
            d.polygon(list(zip(x.tolist(), y.tolist())), fill=1)
    return np.asarray(img, dtype=bool)


def rasterise_roads(grid: Grid, roads) -> dict[str, np.ndarray]:
    """Metres of road of each class in each cell."""
    out = {c: np.zeros((grid.rows, grid.cols), np.float32) for c in CLASSES}
    for cls, pts in roads:
        x, y = grid.px([p[0] for p in pts], [p[1] for p in pts])
        for i in range(len(pts) - 1):
            dx, dy = x[i + 1] - x[i], y[i + 1] - y[i]
            length = math.hypot(dx, dy)
            if length == 0:
                continue
            k = max(1, int(length * 2))               # a sample every half cell
            t = (np.arange(k) + 0.5) / k
            cx = np.floor(x[i] + dx * t).astype(int)
            cy = np.floor(y[i] + dy * t).astype(int)
            ok = (cx >= 0) & (cy >= 0) & (cx < grid.cols) & (cy < grid.rows)
            np.add.at(out[cls], (cy[ok], cx[ok]), length * CELL_M / k)
    return out


# ---------------------------------------------------------------- propagation

# Excess attenuation with distance — the ground's absorption, the air's, the
# clutter no map holds — in dB per km, fitted with the sound powers.
EXCESS_DB_PER_KM = 0.0


def spreading_kernel(excess_db_per_km: float = EXCESS_DB_PER_KM) -> np.ndarray:
    """Energy at distance r from a point source on reflecting ground, 1/(2πr²),
    less an excess attenuation growing with r; per cell of source, r floored
    at half a cell."""
    n = int(REACH_M / CELL_M)
    yy, xx = np.mgrid[-n:n + 1, -n:n + 1] * CELL_M
    r2 = np.maximum(xx ** 2 + yy ** 2, (CELL_M / 2) ** 2)
    k = 1.0 / (2 * np.pi * r2) * 10 ** (-excess_db_per_km * np.sqrt(r2) / 1000 / 10)
    k[xx ** 2 + yy ** 2 > REACH_M ** 2] = 0
    return k.astype(np.float32)


def screening_db(blocked: np.ndarray, sources: np.ndarray) -> np.ndarray:
    """dB lost behind buildings, from the path difference between going round
    them and going straight to the nearest source (Maekawa: 10·log10(3+20N),
    N = 2δ/λ, less its 4.8 dB at δ = 0), capped.

    Both distances are measured on the same 8-neighbour grid, once round the
    buildings and once through them: the grid's own error (up to 8 % off the
    diagonals) is then the same in both and cancels, where comparing with a
    true straight line read it as screening and drew rays."""
    from skimage.graph import MCP_Geometric
    if not sources.any():
        return np.zeros(sources.shape, np.float32)
    starts = np.argwhere(sources & ~blocked).tolist()
    if not starts:
        return np.zeros(sources.shape, np.float32)
    around, _ = MCP_Geometric(np.where(blocked, np.inf, 1.0)).find_costs(starts)
    through, _ = MCP_Geometric(np.ones(blocked.shape)).find_costs(starts)
    delta = np.clip((around - through) * CELL_M, 0, None)
    db = 10 * np.log10(3 + 40 * delta / LAMBDA_M) - 10 * np.log10(3)
    db = np.where(np.isfinite(around), db, MAX_SCREEN_DB)
    return np.minimum(db, MAX_SCREEN_DB).astype(np.float32)


def fields(grid: Grid, roads, buildings, excess_db_per_km: float = EXCESS_DB_PER_KM) -> dict:
    """Everything the levels are made of, independent of the sound powers:
    per class the screened energy field (W/m² per W/m of source), and the
    building mask. levels() combines them with a set of sound powers."""
    import logging
    import time
    t = [time.time()]
    log = logging.getLogger("noise")
    lap = lambda what: (t.append(time.time()), log.debug("%s %.1fs", what, t[-1] - t[-2]))
    blocked = rasterise_buildings(grid, buildings); lap("buildings")
    lengths = rasterise_roads(grid, roads); lap("roads")
    kernel = spreading_kernel(excess_db_per_km)
    major = sum(lengths[c] for c in CLASSES if c in MAJOR) > 0
    minor = sum(lengths[c] for c in CLASSES if c not in MAJOR) > 0
    screen = {"major": screening_db(blocked, major), "minor": screening_db(blocked, minor)}; lap("screening")
    per_class = {}
    for c in CLASSES:
        if not lengths[c].any():
            continue
        free = signal.fftconvolve(lengths[c], kernel, mode="same")
        att = screen["major" if c in MAJOR else "minor"]
        per_class[c] = grid.crop(np.clip(free, 0, None) * 10 ** (-att / 10)).astype(np.float32)
    lap("propagation")
    return {"per_class": per_class, "blocked": grid.crop(blocked),
            "_raw": (lengths, screen, grid)}


def repropagate(f: dict, excess_db_per_km: float) -> dict:
    """The same fields with another excess attenuation, reusing the screening."""
    lengths, screen, grid = f["_raw"]
    kernel = spreading_kernel(excess_db_per_km)
    per_class = {}
    for c in CLASSES:
        if not lengths[c].any():
            continue
        free = signal.fftconvolve(lengths[c], kernel, mode="same")
        att = screen["major" if c in MAJOR else "minor"]
        per_class[c] = grid.crop(np.clip(free, 0, None) * 10 ** (-att / 10)).astype(np.float32)
    return {**f, "per_class": per_class}


def levels(f: dict, lw: dict[str, float]) -> np.ndarray:
    """Lden, dB, from the fields and a sound power per class (dB re 1 pW/m)."""
    e = sum(10 ** (lw[c] / 10) * 1e-12 * v for c, v in f["per_class"].items())
    with np.errstate(divide="ignore"):
        return 10 * np.log10(np.maximum(e, 1e-20) / 1e-12)


# ---------------------------------------------------------------- calibration

BAND_EDGES = [55, 60, 65, 70, 75]


def band_index(l):
    """0: below 55, 1: 55–59 … 5: 75 and above."""
    return np.digitize(l, BAND_EDGES)


def calibrate(samples: list[tuple[dict, np.ndarray, np.ndarray]], start: dict = LW_DEFAULT) -> dict:
    """Sound powers that best match END bands. Each sample is (fields, end_mid,
    inside): end_mid the band's middle where END maps ≥ 55 and NaN below,
    inside the cells within the END agglomeration (elsewhere END says nothing)."""
    from scipy.optimize import minimize
    rng = np.random.default_rng(0)
    picks = []
    for f, end_mid, inside in samples:
        open_ = inside & ~f["blocked"]
        idx = np.flatnonzero(open_)
        idx = rng.choice(idx, min(len(idx), 150_000), replace=False)
        cols = {c: v.ravel()[idx] for c, v in f["per_class"].items()}
        picks.append((cols, end_mid.ravel()[idx]))
    present = sorted({c for cols, _ in picks for c in cols})

    def loss(x):
        lw = dict(zip(present, x))
        total, n = 0.0, 0
        for cols, mid in picks:
            e = sum(10 ** (lw[c] / 10) * cols[c] for c in cols)
            l = 10 * np.log10(np.maximum(e, 1e-20))
            above = np.isfinite(mid)
            total += np.sum(np.clip(np.abs(l[above] - mid[above]) - 2.5, 0, None) ** 2)
            total += np.sum(np.clip(l[~above] - 55, 0, None) ** 2)
            n += len(l)
        return total / n

    # A class a city barely has (Berlin's centre has almost no trunk roads)
    # would be fitted to anything; so every class stays within 10 dB of its
    # typical value, and a busier class is never quieter than a lesser one.
    order = [c for c in CLASSES if c in present]

    def penalised(x):
        lw = dict(zip(present, x))
        bumps = sum(max(0.0, lw[b] - lw[a]) ** 2 for a, b in zip(order, order[1:]))
        return loss(x) + 10 * bumps

    x0 = np.array([start[c] for c in present])
    bounds = [(start[c] - 15, start[c] + 10) for c in present]
    res = minimize(penalised, x0, method="Powell", bounds=bounds, options={"maxiter": 20000, "xtol": 0.02, "ftol": 1e-6})
    return {**start, **dict(zip(present, np.round(res.x, 2)))}


def compare(model: np.ndarray, end_mid: np.ndarray, inside: np.ndarray, blocked: np.ndarray) -> dict:
    """How a model map agrees with an END map, on open cells inside the agglomeration."""
    m = inside & ~blocked
    mb = band_index(model[m])
    eb = band_index(np.where(np.isfinite(end_mid[m]), end_mid[m], 50))
    return {
        "cells": int(m.sum()),
        "same_band": round(float((mb == eb).mean()), 3),
        "within_one_band": round(float((np.abs(mb - eb) <= 1).mean()), 3),
        "end_share_55plus": round(float((eb >= 1).mean()), 3),
        "model_share_55plus": round(float((mb >= 1).mean()), 3),
        "recall_65plus": round(float(((mb >= 3) & (eb >= 3)).sum() / max((eb >= 3).sum(), 1)), 3),
        "precision_65plus": round(float(((mb >= 3) & (eb >= 3)).sum() / max((mb >= 3).sum(), 1)), 3),
    }


# ---------------------------------------------------------------- a tile

ENCODING = {"byte1_db": 25.0, "step_db": 0.5, "nodata_byte": 0,
            "note": "Lden in dB; byte 0 = inside a building (no level is given there)"}
OVERVIEWS = (3, 9)


def from_index(tile, grid: Grid) -> tuple[list, list]:
    """Roads and buildings for a grid from the extract index: the tile and
    every neighbour its margin reaches into (as far as the index holds them)."""
    import osm_extract
    from tiles import STEP, Tile
    w, s, e, n = grid.bounds
    roads, buildings, seen_b, seen_r = [], [], set(), set()
    for la in range(math.floor(s / STEP), math.floor(n / STEP) + 1):
        for lo in range(math.floor(w / STEP), math.floor(e / STEP) + 1):
            t = Tile(la * STEP, lo * STEP)
            for el in osm_extract.read_roads(t, whole=False) or []:
                # A neighbour's border piece left before roads carried their
                # OSM value has only the coarse class; it is left again in the
                # new format on that region's next run (sg/border.py, FORMAT).
                cls = CLASS_OF.get(el["h"] or "") if "h" in el else FROM_COARSE.get(el.get("c") or "")
                if cls and not el.get("t") and el["id"] not in seen_r:
                    seen_r.add(el["id"])
                    roads.append((cls, [tuple(p) for p in el["coordinates"]]))
            for el in osm_extract.read_tile(t, whole=False) or []:
                if el["id"] not in seen_b:
                    seen_b.add(el["id"])
                    buildings.append([tuple(p) for p in el["coordinates"][0]])
    return roads, buildings


def _overview(l: np.ndarray, k: int) -> np.ndarray:
    """k × k mean in energy, buildings left out; a block of buildings stays NaN."""
    rows, cols = (l.shape[0] // k) * k, (l.shape[1] // k) * k
    e = 10 ** (l[:rows, :cols] / 10)
    blocks = e.reshape(rows // k, k, cols // k, k)
    with np.errstate(invalid="ignore", divide="ignore"):
        return 10 * np.log10(np.nanmean(blocks, axis=(1, 3)))


def _encode(l: np.ndarray) -> Image.Image:
    byte = np.clip(np.round((l - ENCODING["byte1_db"]) / ENCODING["step_db"]) + 1, 1, 255)
    return Image.fromarray(np.where(np.isfinite(l), byte, 0).astype(np.uint8), "L")


def build(tile, out_dir: Path) -> dict:
    """lden.png and its overviews into out_dir; returns the meta entry."""
    import osm_extract
    from datetime import datetime, timezone
    from sg.errors import NoData, NotCovered
    if not osm_extract.covered(tile):
        raise NotCovered(f"{tile.id} is not wholly inside the indexed extracts")
    grid = Grid(tile.bounds)
    roads, buildings = from_index(tile, grid)
    if not roads:
        raise NoData(f"{tile.id}: no road in or near the tile")
    f = fields(grid, roads, buildings, EXCESS_FITTED)
    l = levels(f, LW_FITTED).astype(np.float32)
    l[f["blocked"]] = np.nan
    out_dir.mkdir(parents=True, exist_ok=True)
    _encode(l).save(out_dir / "lden.png", optimize=True)
    files = {"lden": "noise/lden.png"}
    for k in OVERVIEWS:
        _encode(_overview(l, k)).save(out_dir / f"lden.o{k}.png", optimize=True)
        files[f"lden.o{k}"] = f"noise/lden.o{k}.png"
    open_ = np.isfinite(l)
    share = lambda db: round(float((l[open_] >= db).mean()), 3) if open_.any() else 0.0
    return {
        "product": "Road traffic noise, Lden, screened line-source model on OpenStreetMap roads and buildings",
        "rows": int(l.shape[0]), "cols": int(l.shape[1]), "metres_per_pixel": CELL_M,
        "encoding": ENCODING, "files": files, "overviews": list(OVERVIEWS),
        "roads": len(roads), "buildings": len(buildings),
        "share_55db_plus": share(55), "share_65db_plus": share(65),
        "model": {"sound_power_db_per_m": LW_FITTED, "excess_db_per_km": EXCESS_FITTED, "reach_m": REACH_M},
        "validation": VALIDATION,
        "source": "OpenStreetMap roads and buildings (Geofabrik extracts); calibrated on EEA END strategic noise maps",
        "licence": "ODbL (OpenStreetMap); EEA END maps used for calibration only",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caveat": "A model from road classes, not traffic counts: no speeds, surfaces, noise barriers, railways or aircraft. Where a city has an official END noise map, that is the better source.",
    }
