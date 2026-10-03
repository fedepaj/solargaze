import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
from sg.log import start_run  # noqa: E402
from sg.product import Context, Product  # noqa: E402
from sg.remote import merge_index, merge_meta  # noqa: E402
from sg.runner import execute, plan  # noqa: E402
from sg.state import DONE, State  # noqa: E402

TILE = "N41.75E12.25"


class Merge(unittest.TestCase):
    def test_newest_product_wins_and_none_is_lost(self):
        published = {"id": TILE, "products": {"heat": {"generated": "2026-10-03T20:00:00+00:00"},
                                             "air": {"generated": "2026-10-01T00:00:00+00:00"}}}
        laptop = {"id": TILE, "products": {"heat": {"generated": "2026-09-30T00:00:00+00:00"},      # older
                                          "air": {"generated": "2026-10-02T00:00:00+00:00"},       # newer
                                          "wind": {"generated": "2026-10-02T00:00:00+00:00"}}}     # new
        m = merge_meta(laptop, published)
        self.assertEqual(set(m["products"]), {"heat", "air", "wind"})
        self.assertEqual(m["products"]["heat"]["generated"], "2026-10-03T20:00:00+00:00")
        self.assertEqual(m["products"]["air"]["generated"], "2026-10-02T00:00:00+00:00")

    def test_index_keeps_every_published_tile(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            (data / TILE).mkdir()
            (data / TILE / "meta.json").write_text(json.dumps({"id": TILE, "bounds": [12.25, 41.75, 12.5, 42.0],
                                                               "products": {"heat": {}, "wind": {}}}))
            published = {"step": 0.25, "tiles": [
                {"id": TILE, "bounds": [12.25, 41.75, 12.5, 42.0], "products": ["wind"]},
                {"id": "N52.50E13.25", "bounds": [13.25, 52.5, 13.5, 52.75], "products": ["heat", "wind"]}]}
            idx = merge_index(published, data)
            ids = {t["id"]: t["products"] for t in idx["tiles"]}
            self.assertEqual(ids, {TILE: ["heat", "wind"], "N52.50E13.25": ["heat", "wind"]})


class Fresh(Product):
    name, version, subdir = "wind", 1, "wind"

    def build(self, tile, stage, ctx):
        (stage / "heights.png").write_bytes(b"png")
        return {"generated": "2026-10-04T00:00:00+00:00"}


class EmptyRunner(unittest.TestCase):
    """A runner with no data folder at all: the published meta comes down
    first, and the new product joins it instead of replacing it."""

    def test_new_product_joins_the_published_meta(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            data = root / "data" / "tiles"          # does not exist yet
            published = {TILE: {"id": TILE, "bounds": [12.25, 41.75, 12.5, 42.0],
                                "products": {"heat": {"generated": "2026-10-03T00:00:00+00:00"}}}}

            def pull_meta(tile):
                p = data / tile / "meta.json"
                if not p.exists() and tile in published:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps(published[tile]))

            def write_meta(tile, product, info):
                p = data / tile / "meta.json"
                meta = json.loads(p.read_text()) if p.exists() else {"id": tile, "products": {}}
                meta["products"][product] = info
                p.write_text(json.dumps(meta))

            state = State(root / "state")
            with start_run("t", root / "logs", console=io.StringIO()) as run:
                ctx = Context(data, root / "cache", run=run, before_tile=pull_meta)
                counts = execute(plan([Fresh()], [TILE], state, "g"), ctx, state, run,
                                 write_meta=write_meta, sleep=lambda s: None)
            self.assertEqual(counts[DONE], 1)
            meta = json.loads((data / TILE / "meta.json").read_text())
            self.assertEqual(set(meta["products"]), {"heat", "wind"})


if __name__ == "__main__":
    unittest.main()
