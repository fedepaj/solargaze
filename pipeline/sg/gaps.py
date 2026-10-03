"""
What the pipeline cannot do yet, as a list someone can act on.

A gap is an input that is not there: a country whose monitoring stations no
importer reads, a source that keeps refusing us on many tiles. Gaps are kept
as GitHub issues labelled ``gap`` — one per gap, found again by a marker in
its body, never duplicated — because that is where a person and the agent
can both read them, write their findings, and close them with the pull
request that fills them. Each nightly run scans for gaps, opens the new ones,
and hands the oldest open one with no pull request yet to the agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path

COUNTRIES = {
    "JP": "Japan", "US": "United States", "CA": "Canada", "MX": "Mexico", "BR": "Brazil", "AR": "Argentina",
    "CL": "Chile", "CO": "Colombia", "PE": "Peru", "KR": "South Korea", "AU": "Australia", "NZ": "New Zealand",
    "IT": "Italy", "DE": "Germany", "FR": "France", "ES": "Spain", "GB": "United Kingdom", "MD": "Moldova",
}
SCOPES = {
    "test": ["JP"],
    "americas": ["US", "CA", "MX", "BR", "AR", "CL", "CO", "PE"],
    "next": ["US", "CA", "MX", "JP", "BR"],
}
LABEL, BLOCKED = "gap", "gap-blocked"


def _marker(gap_id: str) -> str:
    return f"<!-- gap:{gap_id} -->"


def openaq_no2_locations(country: str) -> int | None:
    """How many NO₂ locations OpenAQ lists for a country, or None if unknown."""
    key = os.environ.get("OPENAQ_API_KEY")
    if not key:
        return None
    import requests
    try:
        countries = requests.get("https://api.openaq.org/v3/countries", params={"limit": 300},
                                 headers={"X-API-Key": key}, timeout=60).json()["results"]
        cid = next(c["id"] for c in countries if c["code"] == country)
        n = 0
        for pid in (5, 7, 15):   # NO₂ in µg/m³, ppm, ppb
            r = requests.get("https://api.openaq.org/v3/locations", headers={"X-API-Key": key}, timeout=60,
                             params={"countries_id": cid, "parameters_id": pid, "limit": 1000}).json()
            n += len(r.get("results", []))
        return n
    except Exception:  # noqa: BLE001 — a hint, not a requirement
        return None


def station_gaps(countries: list[str]) -> list[dict]:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from importers import importer_for
    out = []
    for c in countries:
        if importer_for(c):
            continue
        name = COUNTRIES.get(c, c)
        n = openaq_no2_locations(c)
        hint = ("OpenAQ lists none: the national network will have to be found (environment ministry, "
                "national institute, open-data portal)." if n == 0 else
                f"OpenAQ lists {n} NO₂ locations: an OpenAQ importer (api.openaq.org v3, key in OPENAQ_API_KEY) "
                "may cover it, and would serve other countries too." if n else
                "Whether OpenAQ has it is unknown (no key at scan time).")
        gid = f"stations-{c}"
        out.append({
            "id": gid,
            "title": f"Gap: air-quality stations for {name} ({c})",
            "body": "\n".join([
                _marker(gid),
                f"No importer in `pipeline/importers/` covers **{name} ({c})**, so the street-scale air model "
                "cannot be fitted or checked there. The EEA covers only its 39 member and cooperating countries.",
                "",
                f"**What is known.** {hint}",
                "",
                "**What would close this.** An importer per `pipeline/importers/__init__.py` returning the common "
                "schema for NO₂, PM10, PM2.5 and O₃, 2020–2024: hourly series folded to month × local hour where "
                "the network has them. It must state its source and licence (we publish what we derive), pass "
                "`importers.validate`, come with a small fixture and a test that runs without the network, and "
                "with a comparison against any stations another source already gives for the same place.",
            ]),
        })
    return out


def failure_gaps(state_root: Path, min_tiles: int = 3) -> list[dict]:
    """Upstream failures that repeat over many tiles: a source changed or
    lacks something, which is not any one tile's problem."""
    groups = defaultdict(list)
    for p in state_root.glob("*/*.json"):
        try:
            r = json.loads(p.read_text())
        except ValueError:
            continue
        if r.get("status") == "failed" and r.get("kind") == "upstream":
            groups[(r["product"], r.get("sig", ""))].append(r["tile"])
    out = []
    for (product, sig), tiles in groups.items():
        if len(tiles) < min_tiles:
            continue
        gid = f"upstream-{product}-{hashlib.md5(sig.encode()).hexdigest()[:8]}"
        out.append({
            "id": gid,
            "title": f"Gap: {product} — {sig[:70]}",
            "body": "\n".join([
                _marker(gid),
                f"**{len(tiles)} tiles** of `{product}` fail upstream with the same cause:",
                "", f"    {sig}", "",
                "Tiles: " + ", ".join(sorted(tiles)[:30]) + (" …" if len(tiles) > 30 else ""),
                "",
                "An upstream failure is an input that is not there or not what we expect: a source that changed, "
                "a region no extract covers, data that needs another source or another preprocessing.",
            ]),
        })
    return out


# ---------------------------------------------------------------- GitHub issues

def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def sync_issues(gaps: list[dict]) -> list[dict]:
    """Open an issue for every gap that has none (open or closed). Returns the
    open gap issues: number, title, labels, and whether a pull request is on it."""
    try:
        _gh("label", "create", LABEL, "--color", "FBCA04", "--description", "Something the pipeline cannot do yet")
    except subprocess.CalledProcessError:
        pass
    try:
        _gh("label", "create", BLOCKED, "--color", "D93F0B", "--description", "No source found; needs a person")
    except subprocess.CalledProcessError:
        pass
    existing = json.loads(_gh("issue", "list", "--label", LABEL, "--state", "all", "--limit", "500",
                              "--json", "number,state,body,title,labels,createdAt"))
    known = {}
    for issue in existing:
        body = issue.get("body") or ""
        if "<!-- gap:" in body:
            known[body.split("<!-- gap:", 1)[1].split(" -->", 1)[0]] = issue
    for g in gaps:
        if g["id"] in known:
            continue
        _gh("issue", "create", "--title", g["title"], "--label", LABEL, "--body", g["body"])
    issues = json.loads(_gh("issue", "list", "--label", LABEL, "--state", "open", "--limit", "500",
                            "--json", "number,title,labels,createdAt"))
    prs = json.loads(_gh("pr", "list", "--state", "open", "--limit", "200", "--json", "number,body,title"))
    for i in issues:
        ref = f"#{i['number']}"
        i["pr"] = next((p["number"] for p in prs if ref in (p.get("body") or "") or ref in p["title"]), None)
        i["blocked"] = any(l["name"] == BLOCKED for l in i["labels"])
    return sorted(issues, key=lambda i: i["createdAt"])


def pick(issues: list[dict]) -> int | None:
    """The oldest open gap with no pull request and not marked blocked."""
    for i in issues:
        if not i["pr"] and not i["blocked"]:
            return i["number"]
    return None
