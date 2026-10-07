"""
Surface heat where the USGS publishes no surface temperature: computed here
from Landsat Level-1, for the morning product (heat), as heat_landsat.py
would have read it from Level-2.

Where the USGS lacks the ancillary data its surface-temperature algorithm
needs — the Azores, which ASTER's emissivity map leaves out — it publishes
reflectance only (L2SR). The thermal band itself is there, in Level-1. From
it, per scene:

- brightness temperature from band 10's digital numbers, with the scene's
  own calibration (MTL: RADIANCE_MULT/ADD, K1, K2);
- emissivity from NDVI (Sobrino et al. 2004, the threshold method), NDVI
  from the same scene's L2SR red and near-infrared, read from Planetary
  Computer like the rest of the heat product; water 0.991;
- surface temperature = BT / (1 + (λ·BT/ρ)·ln ε), λ = 10.895 µm, ρ = 14388 µm·K;
- clouds and shadow masked with the L2SR QA_PIXEL band, as heat_landsat.py does.

What it lacks against the USGS product: an atmospheric correction. Water
vapour makes the surface read a few degrees cooler than it is, more in
humid summers. The app colours surface heat against the rest of the area,
so the pattern — which block is hotter than which — is what matters, and
it survives; the absolute numbers carry the bias, and the meta says so.
``verify()`` measures it on a scene where both exist.

Level-1 is downloaded through the USGS M2M API (USGS_USERNAME, USGS_TOKEN,
and an account with M2M download access), one band file per scene, kept in
cache/landsat_l1/ — it cannot be read by windows.
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import planetary_computer
import pystac_client
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT

sys.path.insert(0, str(Path(__file__).resolve().parent))
import heat_landsat as hl  # noqa: E402
from sg.errors import NoData, Transient, Upstream  # noqa: E402
from tiles import Tile  # noqa: E402

API = "https://m2m.cr.usgs.gov/api/api/json/stable/"
DATASET = "landsat_ot_c2_l1"
CACHE = Path(__file__).resolve().parent / "cache" / "landsat_l1"
LAMBDA_UM, RHO = 10.895, 14388.0


# ---------------------------------------------------------------- physics

def brightness_temperature(dn: np.ndarray, mtl: dict) -> np.ndarray:
    """Band 10 digital numbers → top-of-atmosphere brightness temperature, K."""
    radiance = mtl["RADIANCE_MULT_BAND_10"] * dn.astype(np.float32) + mtl["RADIANCE_ADD_BAND_10"]
    with np.errstate(divide="ignore", invalid="ignore"):
        bt = mtl["K2_CONSTANT_BAND_10"] / np.log(mtl["K1_CONSTANT_BAND_10"] / radiance + 1)
    bt[dn == 0] = np.nan
    return bt


def emissivity(ndvi: np.ndarray) -> np.ndarray:
    """Sobrino's NDVI thresholds: bare below 0.2, full cover above 0.5, a
    mix weighted by the vegetation fraction between; water below 0."""
    pv = np.clip((ndvi - 0.2) / 0.3, 0, 1) ** 2
    e = np.where(ndvi < 0.2, 0.973, np.where(ndvi > 0.5, 0.99, 0.004 * pv + 0.986))
    return np.where(ndvi < 0, 0.991, e).astype(np.float32)


def surface_temperature(bt_k: np.ndarray, eps: np.ndarray) -> np.ndarray:
    """Single-channel emissivity correction, no atmosphere: °C."""
    return bt_k / (1 + (LAMBDA_UM * bt_k / RHO) * np.log(eps)) - 273.15


def parse_mtl(text: str) -> dict:
    want = ("RADIANCE_MULT_BAND_10", "RADIANCE_ADD_BAND_10", "K1_CONSTANT_BAND_10", "K2_CONSTANT_BAND_10")
    out = {}
    for k in want:
        m = re.search(rf"\b{k}\s*=\s*([-0-9.Ee+]+)", text)
        if not m:
            raise Upstream(f"MTL without {k}")
        out[k] = float(m.group(1))
    return out


# ---------------------------------------------------------------- M2M

class M2M:
    def __init__(self):
        if not (os.environ.get("USGS_USERNAME") and os.environ.get("USGS_TOKEN")):
            raise Upstream("no USGS credentials: USGS_USERNAME and USGS_TOKEN")
        self.key = self._call("login-token", {"username": os.environ["USGS_USERNAME"], "token": os.environ["USGS_TOKEN"]})

    def _call(self, endpoint: str, data: dict):
        headers = {"X-Auth-Token": self.key} if getattr(self, "key", None) else {}
        r = requests.post(API + endpoint, json=data, headers=headers, timeout=300)
        if r.status_code == 403:
            raise Upstream(f"M2M {endpoint}: forbidden — the account has no M2M download access yet")
        j = r.json()
        if j.get("errorCode"):
            raise Upstream(f"M2M {endpoint}: {j['errorCode']} {j['errorMessage']}")
        return j["data"]

    def can_download(self) -> bool:
        return "download" in (self._call("permissions", {}) or [])

    def scenes(self, bounds, start="2018-01-01", end="2025-12-31", max_cloud=hl.MAX_CLOUD) -> list[dict]:
        w, s, e, n = bounds
        sf = {"spatialFilter": {"filterType": "mbr", "lowerLeft": {"latitude": s, "longitude": w},
                                "upperRight": {"latitude": n, "longitude": e}},
              "acquisitionFilter": {"start": start, "end": end},
              "cloudCoverFilter": {"min": 0, "max": max_cloud, "includeUnknown": False}}
        res = self._call("scene-search", {"datasetName": DATASET, "sceneFilter": sf, "maxResults": 10000,
                                          "metadataType": "summary"})["results"]
        # Landsat 8 and 9 with their thermal sensor (LC; LO has none).
        return [r for r in res if r["displayId"][:4] in ("LC08", "LC09")]

    def fetch(self, scenes: list[dict], log=print) -> dict[str, Path]:
        """Band 10 and the MTL of each scene into the cache; returns displayId → folder."""
        out, todo = {}, []
        for sc in scenes:
            d = CACHE / sc["displayId"]
            if (d / "B10.TIF").exists() and (d / "MTL.txt").exists():
                out[sc["displayId"]] = d
            else:
                todo.append(sc)
        for i in range(0, len(todo), 50):
            batch = todo[i:i + 50]
            opts = self._call("download-options", {"datasetName": DATASET, "entityIds": [s["entityId"] for s in batch],
                                                   "includeSecondaryFileGroups": True})
            wanted = []
            for o in opts or []:
                for f in o.get("secondaryDownloads") or []:
                    if f.get("available") and f["displayId"].endswith(("_B10.TIF", "_MTL.txt")):
                        wanted.append({"entityId": f["entityId"], "productId": f["id"], "name": f["displayId"]})
            if not wanted:
                continue
            req = self._call("download-request", {"downloads": [{"entityId": w["entityId"], "productId": w["productId"]} for w in wanted],
                                                  "label": f"solargaze-{int(time.time())}"})
            urls = {d["entityId"] + d["productId"]: d["url"] for d in req.get("availableDownloads", [])}
            for _ in range(30):
                if req.get("preparingDownloads"):
                    time.sleep(20)
                    ret = self._call("download-retrieve", {"label": req.get("label") or ""}) or {}
                    for d in ret.get("available", []):
                        urls[d["entityId"] + d["productId"]] = d["url"]
                    if len(urls) >= len(wanted):
                        break
                else:
                    break
            for w in wanted:
                url = urls.get(w["entityId"] + w["productId"])
                if not url:
                    continue
                display = w["name"].rsplit("_", 1)[0]
                d = CACHE / display
                d.mkdir(parents=True, exist_ok=True)
                dest = d / ("B10.TIF" if w["name"].endswith("_B10.TIF") else "MTL.txt")
                if dest.exists():
                    continue
                tmp = dest.with_suffix(".part")
                with requests.get(url, stream=True, timeout=(30, 600)) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(1 << 22):
                            f.write(chunk)
                tmp.rename(dest)
            log(f"  downloaded {min(i + 50, len(todo))}/{len(todo)} scenes")
            for sc in batch:
                d = CACHE / sc["displayId"]
                if (d / "B10.TIF").exists() and (d / "MTL.txt").exists():
                    out[sc["displayId"]] = d
        return out

    def close(self):
        try:
            self._call("logout", {})
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- one scene

_cat = None


def _l2_item(display_id: str):
    """The same acquisition in Collection 2 Level-2 on Planetary Computer
    (L2SR where surface temperature is missing, L2SP elsewhere)."""
    global _cat
    _cat = _cat or pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1",
                                             modifier=planetary_computer.sign_inplace)
    sat, _, pathrow, acquired, *_ , tier = display_id.split("_")
    for level in ("L2SR", "L2SP"):
        for t in (tier, "T1", "T2"):
            items = list(_cat.search(collections=["landsat-c2-l2"], ids=[f"{sat}_{level}_{pathrow}_{acquired}_02_{t}"]).items())
            if items:
                return items[0]
    return None


def _warp(href: str, tile: Tile, shape, resampling) -> np.ndarray:
    w, s, e, n = tile.bounds
    with rasterio.open(href) as src:
        with WarpedVRT(src, crs="EPSG:4326", transform=from_bounds(w, s, e, n, shape[1], shape[0]),
                       width=shape[1], height=shape[0], resampling=resampling, nodata=0) as vrt:
            return vrt.read(1)


def scene_lst(folder: Path, tile: Tile, shape, item=None) -> np.ndarray | None:
    """°C over the tile for one scene, NaN under cloud, shadow or outside; None
    when the scene's reflectance (for NDVI and the cloud mask) is missing."""
    item = item or _l2_item(folder.name)
    if item is None or not {"red", "nir08", "qa_pixel"} <= set(item.assets):
        return None
    mtl = parse_mtl((folder / "MTL.txt").read_text())
    dn = _warp(str(folder / "B10.TIF"), tile, shape, Resampling.bilinear)
    red = _warp(item.assets["red"].href, tile, shape, Resampling.bilinear).astype(np.float32) * 0.0000275 - 0.2
    nir = _warp(item.assets["nir08"].href, tile, shape, Resampling.bilinear).astype(np.float32) * 0.0000275 - 0.2
    qa = _warp(item.assets["qa_pixel"].href, tile, shape, Resampling.nearest)
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (nir - red) / (nir + red)
    t = surface_temperature(brightness_temperature(dn, mtl), emissivity(np.nan_to_num(ndvi, nan=0.0)))
    t[~(hl.qa_is_clear(qa) & (dn > 0))] = np.nan
    return t


# ---------------------------------------------------------------- a tile

def build(tile: Tile, out_dir: Path, folders: dict[str, Path], scenes: list[dict]) -> dict:
    """The packed months for a tile from the Level-1 scenes over it, in the
    heat product's own shape; returns the meta entry."""
    shape = tile.shape(hl.DEG_PER_PX)
    by_month: dict[int, list[np.ndarray]] = {m: [] for m in range(1, 13)}
    used = failed = 0
    for sc in scenes:
        f = folders.get(sc["displayId"])
        if f is None:
            continue
        try:
            arr = scene_lst(f, tile, shape)
        except Exception as e:  # noqa: BLE001 — a scene that will not read is one we do without
            print(f"    skip {sc['displayId']}: {e}", file=sys.stderr)
            failed += 1
            continue
        if arr is None or np.isfinite(arr).mean() < 0.05:
            continue
        month = int(sc["displayId"].split("_")[3][4:6])
        by_month[month].append(arr.astype(np.float16))
        used += 1
    if failed > max(3, 0.05 * len(scenes)):
        raise Transient(f"{tile.id}: {failed} of {len(scenes)} Level-1 scenes failed")
    out_dir.mkdir(parents=True, exist_ok=True)
    months, medians = {}, {}
    for m, stack in by_month.items():
        if not stack:
            continue
        cube = np.stack(stack).astype(np.float32)
        with np.errstate(all="ignore"):
            median = np.nanmedian(cube, axis=0)
        median[np.isfinite(cube).sum(axis=0) < 3] = np.nan
        median = hl.downsample(median)
        if not np.isfinite(median).any():
            continue
        medians[m] = median
        months[f"{m:02d}"] = {"scenes": len(stack), "coverage": round(float(np.isfinite(median).mean()), 3),
                              "tile_median_c": round(float(np.nanmedian(median)), 2)}
    if not medians:
        raise NoData(f"{tile.id}: no month with three clear Level-1 looks")
    files = hl.write_packed(medians, out_dir)
    return {
        "product": "Landsat 8/9 Collection 2 Level-1 band 10, brightness temperature with NDVI emissivity, per-pixel median by calendar month",
        "method": "computed by SolarGaze from Level-1 where the USGS publishes no surface temperature (no ASTER GED emissivity); "
                  "emissivity from NDVI (Sobrino 2004), no atmospheric correction — reads a few degrees cool, the pattern holds",
        "years": hl.YEARS, "max_cloud_percent": hl.MAX_CLOUD, "overpass_local_time": "~10:30",
        "rows": shape[0] // hl.DOWNSAMPLE, "cols": shape[1] // hl.DOWNSAMPLE,
        "degrees_per_pixel": hl.DEG_PER_PX * hl.DOWNSAMPLE, "native_degrees_per_pixel": hl.DEG_PER_PX,
        "encoding": {"version": hl.ENCODING, "channel": "L", "nodata_byte": 0, "byte1_c": hl.T_MIN, "step_c": hl.T_STEP,
                     "note": "median at 30 m, then a 3 x 3 mean; byte 0 = fewer than 3 clear scenes"},
        "files": files, "packing": hl.PACKING, "overviews": list(hl.OVERVIEWS), "months": months,
        "scenes_used": used,
        "source": "USGS Landsat Collection 2 Level-1 via M2M; L2SR reflectance via Microsoft Planetary Computer",
        "licence": "Landsat data are in the public domain (USGS)",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caveat": "Surface temperature at a mid-morning overpass on clear days, without atmospheric correction: a few degrees cool in absolute terms; the differences across the area are what to read.",
    }


def verify(m2m: M2M, tile_id: str = "N32.50E-17.00", n: int = 12) -> dict:
    """Our Level-1 surface temperature against the USGS's own, scene by
    scene, where both exist (Madeira by default: an Atlantic island like the
    Azores, with the USGS product). Returns the bias and the correlation."""
    tile = Tile.parse(tile_id)
    shape = tile.shape(hl.DEG_PER_PX * 3)
    scenes = m2m.scenes(tile.bounds)[-n:]
    folders = m2m.fetch(scenes)
    diffs, pairs = [], []
    for sc in scenes:
        f = folders.get(sc["displayId"])
        item = f and _l2_item(sc["displayId"])
        if not f or item is None or "lwir11" not in item.assets:
            continue
        ours = scene_lst(f, tile, shape, item)
        band = item.assets["lwir11"].extra_fields["raster:bands"][0]
        st = _warp(item.assets["lwir11"].href, tile, shape, Resampling.bilinear).astype(np.float32)
        usgs = np.where(st > 0, st * band["scale"] + band["offset"] - 273.15, np.nan)
        ok = np.isfinite(ours) & np.isfinite(usgs)
        if ok.sum() < 500:
            continue
        diffs.append(float(np.median(ours[ok] - usgs[ok])))
        pairs.append(float(np.corrcoef(ours[ok], usgs[ok])[0, 1]))
    return {"scenes": len(diffs), "median_bias_c": round(float(np.median(diffs)), 2) if diffs else None,
            "bias_range_c": [round(min(diffs), 2), round(max(diffs), 2)] if diffs else None,
            "median_correlation": round(float(np.median(pairs)), 3) if pairs else None}
