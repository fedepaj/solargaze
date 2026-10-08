"""
Monitoring stations from every network, in one shape.

Each module here reads one network — the EEA for Europe, and whatever a
country needs beyond it — and returns its stations as a DataFrame in the
common schema below, which is what the street-scale air model is fitted on.
A network that is not here is a gap (sg/gaps.py): the pipeline reports it,
and the agent may propose an importer for it, as a pull request.

An importer:

    class MyNetwork(Importer):
        name = "my-network"
        source = "https://…"                  # where the data come from
        licence = "CC-BY 4.0"                # what allows us to publish what we derive
        def covers(self, country) -> bool    # ISO 3166-1 alpha-2
        def stations(self, country, years, variables) -> pd.DataFrame

and is added to REGISTRY, in order of preference. ``validate`` is applied to
whatever it returns, before anything uses it.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sg.errors import Invalid  # noqa: E402

VARS = ("nitrogen_dioxide", "pm10", "pm2_5", "ozone")
TYPES = ("traffic", "background", "industrial", "unknown")
AREAS = ("urban", "suburban", "rural", "unknown")

# column → (dtype family, what it is)
SCHEMA = {
    "station": ("str", "the network's station id, stable across years"),
    "point": ("str", "the sampling point (one station can have several samplers)"),
    "var": ("str", "one of VARS"),
    "lat": ("float", "WGS84 degrees"),
    "lon": ("float", "WGS84 degrees"),
    "alt": ("float", "metres, NaN if unknown"),
    "type": ("str", "one of TYPES"),
    "area": ("str", "one of AREAS"),
    "resolution": ("str", "'hour' or 'day': what the means were folded from"),
    "values": ("int", "how many hourly or daily values went in"),
    "capture": ("float", "share of the period covered, 0–1"),
    "annual_mean": ("float", "µg/m³, the mean of the monthly means"),
    "days_over_who": ("float", "share of days over the WHO 2021 daily guideline, NaN if unknown"),
    "by_month": ("list", "12 monthly means, µg/m³, None where missing"),
    "by_month_hour": ("list|None", "288 means (month × local hour), µg/m³, or None for daily data"),
    "source": ("str", "the importer's name"),
    "licence": ("str", "the licence of the data"),
}
# Plausible annual means, µg/m³: anything outside is a unit error (ppb, mg/m³) or a broken parse.
PLAUSIBLE = {"nitrogen_dioxide": (0, 150), "pm10": (0, 200), "pm2_5": (0, 150), "ozone": (0, 200)}


class Importer:
    name = ""
    source = ""
    licence = ""

    def covers(self, country: str) -> bool:
        raise NotImplementedError

    def stations(self, country: str, years: list[int], variables=VARS) -> pd.DataFrame:
        raise NotImplementedError


def validate(df: pd.DataFrame, name: str = "") -> pd.DataFrame:
    """Raise Invalid for a table no model should be fitted on."""
    who = f"{name}: " if name else ""
    missing = [c for c in SCHEMA if c not in df.columns]
    if missing:
        raise Invalid(f"{who}missing columns {missing}")
    if df.empty:
        raise Invalid(f"{who}no station")
    if not df["var"].isin(VARS).all():
        raise Invalid(f"{who}unknown pollutants {sorted(set(df['var']) - set(VARS))}")
    if not (df.lat.between(-90, 90) & df.lon.between(-180, 180)).all():
        raise Invalid(f"{who}coordinates out of range")
    if not df["type"].isin(TYPES).all() or not df["area"].isin(AREAS).all():
        raise Invalid(f"{who}station type or area outside {TYPES} / {AREAS}")
    if not df.capture.between(0, 1).all():
        raise Invalid(f"{who}capture outside 0–1")
    for var, (lo, hi) in PLAUSIBLE.items():
        m = df[df["var"] == var].annual_mean
        bad = m[~m.between(lo, hi)]
        if len(bad):
            raise Invalid(f"{who}{len(bad)} {var} annual means outside {lo}–{hi} µg/m³ (units?) e.g. {bad.iloc[0]}")
    for months in df.by_month:
        if len(months) != 12:
            raise Invalid(f"{who}by_month must have 12 values")
    for mh in df.by_month_hour:
        if mh is not None and not (isinstance(mh, float) and math.isnan(mh)) and len(mh) != 288:
            raise Invalid(f"{who}by_month_hour must have 288 values or be None")
    if df.duplicated(["point", "var"]).any():
        raise Invalid(f"{who}duplicate (point, var) rows")
    return df


from .eea import EEA  # noqa: E402
from .epa_aqs import EPAAQS  # noqa: E402
from .monitorar import MonitorAr  # noqa: E402
from .naps import NAPS  # noqa: E402
from .openaq import OpenAQ  # noqa: E402

# In order of preference: the first that covers a country is used.
REGISTRY: list[Importer] = [EEA(), EPAAQS(), NAPS(), MonitorAr(), OpenAQ()]


def importer_for(country: str) -> Importer | None:
    for imp in REGISTRY:
        try:
            if imp.covers(country):
                return imp
        except Exception:  # noqa: BLE001 — a network down is not "does not cover"; try the next
            continue
    return None
