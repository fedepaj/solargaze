import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import importers  # noqa: E402
from importers import validate  # noqa: E402
from sg import gaps  # noqa: E402
from sg.errors import Invalid  # noqa: E402


def frame(**over):
    row = {"station": "S1", "point": "S1-no2", "var": "nitrogen_dioxide", "lat": 35.7, "lon": 139.7, "alt": 40.0,
           "type": "traffic", "area": "urban", "resolution": "hour", "values": 40000, "capture": 0.9,
           "annual_mean": 32.0, "days_over_who": 0.3, "by_month": [30.0] * 12, "by_month_hour": [30.0] * 288,
           "source": "test", "licence": "CC-BY"}
    row.update(over)
    return pd.DataFrame([row])


class Validate(unittest.TestCase):
    def test_a_good_table_passes(self):
        validate(frame())
        validate(frame(by_month_hour=None, resolution="day"))

    def test_what_must_never_be_fitted_on(self):
        for bad in (dict(annual_mean=1800.0),            # ppb read as µg/m³, ×… or mg/m³ mix-up
                    dict(var="co"), dict(lat=95.0), dict(type="kerbside"),
                    dict(capture=1.4), dict(by_month=[1.0] * 11), dict(by_month_hour=[1.0] * 24)):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                validate(frame(**bad))
        with self.assertRaises(Invalid):
            validate(pd.concat([frame(), frame()]))       # duplicate point and pollutant
        with self.assertRaises(Invalid):
            validate(frame().drop(columns=["licence"]))   # every source states its licence


class Gaps(unittest.TestCase):
    def test_a_country_no_importer_covers_is_a_gap(self):
        class Only(importers.Importer):
            name = "only-it"
            def covers(self, c): return c == "IT"
        saved = importers.REGISTRY[:]
        importers.REGISTRY[:] = [Only()]
        try:
            found = gaps.station_gaps(["IT", "JP"])
        finally:
            importers.REGISTRY[:] = saved
        self.assertEqual([g["id"] for g in found], ["stations-JP"])
        self.assertIn("<!-- gap:stations-JP -->", found[0]["body"])

    def test_repeated_upstream_failures_become_one_gap(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "wind").mkdir()
            for i, tile in enumerate(["N45.50E13.75", "N45.75E13.25", "N45.75E13.50", "N41.75E12.25"]):
                rec = {"tile": tile, "product": "wind", "status": "failed", "kind": "upstream" if i < 3 else "bug",
                       "sig": "MissingData: the window runs outside the indexed extracts"}
                (root / "wind" / f"{tile}.json").write_text(json.dumps(rec))
            found = gaps.failure_gaps(root)
        self.assertEqual(len(found), 1)
        self.assertIn("3 tiles", found[0]["body"])


if __name__ == "__main__":
    unittest.main()


class PublishedMD5(unittest.TestCase):
    """The Germany job of the first European run died on an empty answer."""

    def _with(self, status, text):
        from unittest import mock
        from sg import regions, errors
        resp = mock.Mock(status_code=status, text=text)
        resp.raise_for_status = mock.Mock()
        with mock.patch.object(regions.requests, "get", return_value=resp):
            return regions._published_md5("https://example/x.md5")

    def test_a_checksum_is_read(self):
        self.assertEqual(self._with(200, "0123456789abcdef0123456789abcdef  germany-latest.osm.pbf\n"),
                         "0123456789abcdef0123456789abcdef")

    def test_an_empty_or_odd_answer_is_transient(self):
        from sg import errors
        for status, text in ((200, ""), (200, "<html>busy</html>"), (503, "")):
            with self.subTest(status=status, text=text), self.assertRaises(errors.Transient):
                self._with(status, text)


class MirrorChecksum(unittest.TestCase):
    """Geofabrik serves Germany from a mirror, with the MD5 beside it there."""

    def test_the_checksum_is_found_beside_the_mirrored_file(self):
        from unittest import mock
        from sg import regions

        def get(url, timeout=None, **kw):
            ok = url == "https://mirror/germany-latest.osm.pbf.md5"
            r = mock.Mock(status_code=200 if ok else 404, text="0123456789abcdef0123456789abcdef  g.osm.pbf\n" if ok else "")
            r.raise_for_status = mock.Mock()
            return r
        head = mock.Mock(status_code=307, headers={"location": "https://mirror/germany-latest.osm.pbf"})
        with mock.patch.object(regions.requests, "get", side_effect=get), \
                mock.patch.object(regions.requests, "head", return_value=head):
            self.assertEqual(regions._checksum_for("https://geofabrik/europe/germany-latest.osm.pbf"),
                             "0123456789abcdef0123456789abcdef")
