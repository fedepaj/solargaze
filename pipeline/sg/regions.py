"""
The unit of remote work: a Geofabrik region. A runner has the disk for one
country's OpenStreetMap extract, not a continent's, so work is cut along the
same lines the extracts are: download and verify the extract, index it,
build its tiles.

A tile across a national border is covered by neither country's extract on
its own; its OSM-based products fall back to Overpass (wind) or are
recorded as upstream failures until a run with both extracts comes by.
"""

from __future__ import annotations

import hashlib
import math
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE / "wind")]
from tiles import STEP, Tile  # noqa: E402

from . import errors  # noqa: E402

GEOFABRIK = "https://download.geofabrik.de"

# Europe, by Geofabrik's paths, in the order the work is taken: where most
# people live first. Microstates come with their neighbours.
EUROPE = [f"europe/{c}" for c in (
    "italy", "germany", "france", "united-kingdom", "spain", "poland", "netherlands", "belgium", "romania",
    "portugal", "czech-republic", "greece", "hungary", "austria", "switzerland", "sweden", "denmark",
    "ireland-and-northern-ireland", "bulgaria", "serbia", "slovakia", "croatia", "finland", "norway",
    "slovenia", "bosnia-herzegovina", "lithuania", "latvia", "estonia", "albania", "macedonia", "moldova",
    "luxembourg", "montenegro", "malta", "cyprus", "iceland",
)]
SCOPES = {"europe": EUROPE, "italy": ["europe/italy"]}


def name_of(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _get(url: str, dest: Path, log=None) -> None:
    def once():
        tmp = dest.with_suffix(dest.suffix + ".part")
        with requests.get(url, stream=True, timeout=(30, 600)) as r:
            if r.status_code in (429, 500, 502, 503, 504):
                raise errors.Transient(f"{url}: HTTP {r.status_code}")
            if r.status_code == 404:
                raise errors.Upstream(f"{url}: not found")
            r.raise_for_status()
            want = r.headers.get("content-length")
            n = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 22):
                    f.write(chunk)
                    n += len(chunk)
        # A connection that closes early leaves a short file and no error.
        if want is not None and int(want) != n:
            tmp.unlink(missing_ok=True)
            raise errors.Transient(f"{url}: {n} bytes of {want}")
        tmp.rename(dest)
    errors.retry(once, tries=6, base=20,
                 on_retry=lambda n, e, w: log and log.warn("retry", stage="download", attempt=n, wait=w, msg=str(e)[:200]))


def _published_md5(url: str) -> str:
    """Geofabrik's checksum for an extract. An answer that is not one — an
    empty body, an error page from a busy server — is transient: asked again,
    never taken for a checksum."""
    import re
    r = requests.get(url, timeout=60)
    if r.status_code in (429, 500, 502, 503, 504):
        raise errors.Transient(f"{url}: HTTP {r.status_code}")
    if r.status_code == 404:
        raise errors.Upstream(f"{url}: not found")
    r.raise_for_status()
    parts = r.text.split()
    if not parts or not re.fullmatch(r"[0-9a-f]{32}", parts[0]):
        raise errors.Transient(f"{url}: no MD5 in the answer ({r.text[:60]!r})")
    return parts[0]


def _checksum_for(pbf_url: str, log=None) -> str | None:
    """The extract's published MD5: beside it on Geofabrik, or — for the
    big countries Geofabrik serves from a mirror (Germany redirects to
    gwdg.de) — beside it on the mirror. None when neither has one; the
    download is then checked for its length only, and the log says so."""
    retry = dict(tries=6, base=15,
                 on_retry=lambda n, e, w: log and log.warn("retry", stage="md5", attempt=n, wait=w, msg=str(e)[:200]))
    try:
        return errors.retry(lambda: _published_md5(pbf_url + ".md5"), **retry)
    except errors.Upstream:
        pass
    head = errors.retry(lambda: requests.head(pbf_url, timeout=60, allow_redirects=False), tries=5, base=10)
    where = head.headers.get("location")
    if head.status_code in (301, 302, 303, 307, 308) and where:
        try:
            return errors.retry(lambda: _published_md5(where + ".md5"), **retry)
        except errors.Upstream:
            pass
    if log:
        log.warn("extract.no_checksum", msg=f"no MD5 published for {pbf_url}; checking its length only")
    return None


def fetch(path: str, geofabrik_dir: Path, log=None) -> tuple[Path, Path]:
    """The region's .osm.pbf, verified against Geofabrik's MD5, and its .poly.
    A download that does not match is deleted and fetched again; one that
    still does not match is an upstream failure, never indexed."""
    geofabrik_dir.mkdir(parents=True, exist_ok=True)
    name = name_of(path)
    pbf, poly = geofabrik_dir / f"{name}-latest.osm.pbf", geofabrik_dir / f"{name}.poly"
    if not poly.exists():
        _get(f"{GEOFABRIK}/{path}.poly", poly, log)
    want = _checksum_for(f"{GEOFABRIK}/{path}-latest.osm.pbf", log)
    for attempt in (1, 2):
        if not pbf.exists():
            _get(f"{GEOFABRIK}/{path}-latest.osm.pbf", pbf, log)
        h = hashlib.md5()
        with open(pbf, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 24), b""):
                h.update(chunk)
        if want is None or h.hexdigest() == want:
            if log:
                log.info("extract.verified", region=path, mb=round(pbf.stat().st_size / 1e6))
            return pbf, poly
        if log:
            log.warn("extract.mismatch", region=path, attempt=attempt, msg="MD5 differs from Geofabrik's; downloading again")
        pbf.unlink()
    raise errors.Upstream(f"{path}: extract does not match its published MD5 after two downloads")


def tiles_of(poly_path: Path) -> list[str]:
    """Every tile that touches the region's land."""
    import osm_extract
    from global_land_mask import globe
    from shapely.geometry import box
    shape = osm_extract.read_poly(poly_path)
    w, s, e, n = shape.bounds
    out = []
    for la in range(math.floor(s / STEP), math.floor(n / STEP) + 1):
        for lo in range(math.floor(w / STEP), math.floor(e / STEP) + 1):
            t = Tile(la * STEP, lo * STEP)
            if not shape.intersects(box(*t.bounds)):
                continue
            tw, ts, te, tn = t.bounds
            pts = [t.centre, (ts + .02, tw + .02), (ts + .02, te - .02), (tn - .02, tw + .02), (tn - .02, te - .02)]
            if any(globe.is_land(a, o) for a, o in pts):
                out.append(t.id)
    return out
