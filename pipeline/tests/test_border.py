import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from botocore.exceptions import ClientError

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
import osm_extract  # noqa: E402
from sg import border  # noqa: E402
from sg.product import Product  # noqa: E402
from sg.state import FAILED, Record  # noqa: E402

TILE = "N45.00E7.00"   # the Alps west of Turin: land everywhere


def poly(name, w, s, e, n):
    return f"{name}\n1\n  {w} {s}\n  {e} {s}\n  {e} {n}\n  {w} {n}\n  {w} {s}\nEND\nEND\n"


def building(i, x, y):
    ring = [[x, y], [x + .0001, y], [x + .0001, y + .0001], [x, y + .0001], [x, y]]
    return json.dumps({"type": "polygon", "id": f"w{i}/0", "tags": {"building": "yes"}, "coordinates": [ring]}) + "\n"


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def list_objects_v2(self, Bucket, Prefix):
        return {"Contents": [{"Key": k} for b, k in sorted(self.objects) if b == Bucket and k.startswith(Prefix)]}


class FakeRemote:
    ops = "ops"

    def __init__(self):
        self.s3 = FakeS3()

    def _call(self, fn):
        return fn()

    def _get_json(self, bucket, key):
        try:
            return json.loads(self.s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except ClientError:
            return None


class Border(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = osm_extract.OUT, osm_extract.MANIFEST, osm_extract.GEOFABRIK
        self.remote = FakeRemote()

    def tearDown(self):
        osm_extract.OUT, osm_extract.MANIFEST, osm_extract.GEOFABRIK = self.saved
        osm_extract.coverage.cache_clear()
        osm_extract.piece_cover.cache_clear()

    def machine(self, name, bounds, buildings):
        """A runner that has indexed one region's extract."""
        root = self.tmp / name
        osm_extract.GEOFABRIK = root / "geofabrik"
        osm_extract.OUT = root / "extract_tiles"
        osm_extract.MANIFEST = osm_extract.OUT / "extracts.json"
        osm_extract.GEOFABRIK.mkdir(parents=True)
        (osm_extract.OUT / "roads").mkdir(parents=True)
        (osm_extract.GEOFABRIK / f"{name}.poly").write_text(poly(name, *bounds))
        (osm_extract.OUT / f"{TILE}.jsonl").write_text("".join(buildings))
        osm_extract.MANIFEST.write_text(json.dumps({"extracts": [{"name": name, "pbf": f"{name}.pbf",
                                                                  "poly": f"{name}.poly"}]}))
        osm_extract.coverage.cache_clear()
        osm_extract.piece_cover.cache_clear()
        return osm_extract.GEOFABRIK / f"{name}.poly"

    def test_a_tile_split_between_two_regions_is_finished_from_the_pieces(self):
        # East of 7.125 is "east"; it indexes, and leaves its piece.
        east = self.machine("east", (7.12, 44, 9, 46), [building(1, 7.2, 45.1), building(9, 7.124, 45.1)])
        self.assertEqual(border.border_tiles([TILE]), [TILE])
        self.assertEqual(border.leave(self.remote, "east", east, [TILE]), 1)
        # Left recently: not again.
        self.assertEqual(border.leave(self.remote, "east", east, [TILE]), 0)

        # West of 7.13 is "west": alone it cannot answer for the tile.
        self.machine("west", (5, 44, 7.13, 46), [building(2, 7.0, 45.1), building(9, 7.124, 45.1)])
        from tiles import Tile
        self.assertIsNone(osm_extract.read_tile(Tile.parse(TILE)))
        self.assertEqual(border.gather(self.remote, "west", [TILE]), {TILE: ["east"]})
        ids = sorted(b["id"] for b in osm_extract.read_tile(Tile.parse(TILE)))
        self.assertEqual(ids, ["w1/0", "w2/0", "w9/0"])   # the one on the line, once

        # Gathered twice, written once.
        border.gather(self.remote, "west", [TILE])
        self.assertEqual(len((osm_extract.OUT / f"{TILE}.jsonl").read_text().splitlines()), 4)

        # The pieces answer for this tile only, not for the land east of it.
        self.assertFalse(osm_extract.covered_box((7.2, 45.0, 7.4, 45.2)))

    def test_pieces_of_an_older_format_are_left_again_at_once(self):
        east = self.machine("east", (7.12, 44, 9, 46), [building(1, 7.2, 45.1)])
        self.assertEqual(border.leave(self.remote, "east", east, [TILE]), 1)
        self.assertEqual(border.leave(self.remote, "east", east, [TILE]), 0)
        # A record from before the format was written: as if never left.
        key = ("ops", border._region_key("east"))
        rec = json.loads(self.remote.s3.objects[key]); rec.pop("format")
        self.remote.s3.objects[key] = json.dumps(rec).encode()
        self.assertEqual(border.pieces_age_days(self.remote, "east"), float("inf"))
        self.assertEqual(border.leave(self.remote, "east", east, [TILE]), 1)

    def test_a_tile_still_missing_a_third_region_waits(self):
        # "east" covers only the north-east quarter: west + east leave land uncovered.
        east = self.machine("east", (7.12, 45.12, 9, 46), [building(1, 7.2, 45.2)])
        border.leave(self.remote, "east", east, [TILE])
        self.machine("west", (5, 44, 7.13, 46), [building(2, 7.0, 45.1)])
        self.assertEqual(border.gather(self.remote, "west", [TILE]), {})
        self.assertFalse(osm_extract.covered_box((7.0, 45.0, 7.25, 45.25)))

    def test_a_tile_the_boundary_only_touches_is_not_the_regions(self):
        from sg import regions
        path = self.tmp / "edge.poly"
        path.write_text(poly("edge", 6.5, 45.0, 7.0, 45.25))   # ends exactly where TILE begins
        self.assertNotIn(TILE, regions.tiles_of(path))
        self.assertIn("N45.00E6.75", regions.tiles_of(path))

    def test_a_region_waiting_on_its_border_runs_only_when_a_neighbour_has_news(self):
        left = {"france": "2026-10-05T07:30:00+00:00", "ireland-and-northern-ireland": ""}
        self.assertTrue(border.ripe("united-kingdom", ["france"], left, {"united-kingdom": "2026-10-05T07:22:55+00:00"}))
        self.assertFalse(border.ripe("united-kingdom", ["france"], left, {"united-kingdom": "2026-10-06T07:00:00+00:00"}))
        self.assertFalse(border.ripe("united-kingdom", ["ireland-and-northern-ireland"], left, {}))
        self.assertTrue(border.ripe("italy", ["france"], left, {}))   # never gathered

    def test_waiting_tiles_are_the_ones_failed_for_coverage(self):
        from sg.state import State
        st = State(self.tmp / "state")
        st.record(TILE, "wind", FAILED, version=1, kind="upstream", sig="NotCovered: <tile> is not wholly inside")
        st.record("N45.00E7.25", "wind", FAILED, version=1, kind="upstream", sig="Upstream: <url> not found")
        self.assertEqual(border.waiting_tiles(st), {TILE})

    def test_a_border_tile_is_never_given_up_on(self):
        p = Product()
        p.max_attempts = 3
        rec = Record(tile=TILE, product="wind", status=FAILED, version=p.version, attempts=99, kind="upstream",
                     sig="NotCovered: <tile> is not wholly inside the indexed extracts", git="abc")
        self.assertIsNotNone(p.reason(TILE, rec, git="abc"))
        rec.sig = "Upstream: <url> not found"
        self.assertIsNone(p.reason(TILE, rec, git="abc"))


if __name__ == "__main__":
    unittest.main()
