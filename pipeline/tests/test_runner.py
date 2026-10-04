import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sg import errors  # noqa: E402
from sg.log import start_run  # noqa: E402
from sg.product import Context, Product  # noqa: E402
from sg.runner import Budget, Systemic, execute, plan  # noqa: E402
from sg.state import DONE, EMPTY, FAILED, State  # noqa: E402

TILES = ["N41.75E12.25", "N42.00E12.50", "N41.50E12.00"]


def write_meta_into(data_dir):
    def wm(tile, product, info):
        p = data_dir / tile / "meta.json"
        meta = json.loads(p.read_text()) if p.exists() else {"products": {}}
        meta["products"][product] = info
        p.write_text(json.dumps(meta))
    return wm


class Scripted(Product):
    """Behaves per tile as told: 'ok', 'nodata', 'bug', 'invalid', or a list
    consumed one attempt at a time (e.g. ['net', 'ok'])."""
    name, version, subdir = "heat", 1, "heat"

    def __init__(self, script):
        self.script = {k: (list(v) if isinstance(v, list) else v) for k, v in script.items()}

    def build(self, tile, stage, ctx):
        act = self.script.get(tile, "ok")
        if isinstance(act, list):
            act = act.pop(0)
        if act == "net":
            raise RuntimeError("HTTPSConnectionPool: Max retries exceeded (Could not resolve host)")
        if act == "nodata":
            raise errors.NoData("open sea")
        if act == "upstream":
            raise errors.Upstream("not wholly inside the indexed extracts")
        if act == "border":
            raise errors.NotCovered(f"{tile} is not wholly inside the indexed extracts")
        if act == "bug":
            raise ZeroDivisionError("division by zero")
        (stage / "months.png").write_bytes(b"png" if act != "invalid" else b"")
        return {"files": {"months": "heat/months.png"}}

    def validate(self, tile, stage, info):
        if (stage / "months.png").stat().st_size == 0:
            raise errors.Invalid("empty raster")


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data, self.cache = root / "data", root / "cache"
        self.data.mkdir()
        self.state = State(self.cache / "state")
        self.ctx = Context(self.data, self.cache)

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, product, tiles=TILES, **kw):
        published = []
        with start_run("t", self.cache / "logs", console=io.StringIO()) as run:
            jobs = plan([product], tiles, self.state, run.context["git"])
            counts = execute(jobs, self.ctx, self.state, run, publish=published.extend,
                             write_meta=write_meta_into(self.data), sleep=lambda s: None, **kw)
        return counts, published

    def test_outcomes_are_recorded_and_isolated(self):
        counts, published = self.go(Scripted({TILES[0]: ["net", "ok"], TILES[1]: "nodata", TILES[2]: "bug"}))
        self.assertEqual(counts, {DONE: 1, EMPTY: 1, FAILED: 1, "skipped": 0})
        self.assertTrue((self.data / TILES[0] / "heat" / "months.png").exists())
        self.assertEqual(self.state.get("heat", TILES[1]).status, EMPTY)
        rec = self.state.get("heat", TILES[2])
        self.assertEqual((rec.status, rec.kind), (FAILED, errors.BUG))
        self.assertTrue(any(str(p).endswith("months.png") for p in published))
        # Nothing staged is left behind.
        self.assertEqual(list(self.data.glob("*/.heat.stage-*")), [])

    def test_a_bug_is_not_retried_until_the_code_changes(self):
        self.go(Scripted({TILES[2]: "bug"}), tiles=[TILES[2]])
        product = Scripted({})
        with start_run("t", self.cache / "logs", console=io.StringIO()) as run:
            self.assertEqual(plan([product], [TILES[2]], self.state, run.context["git"]), [])
            self.assertEqual(len(plan([product], [TILES[2]], self.state, "another-commit")), 1)

    def test_invalid_output_never_replaces_a_good_one(self):
        self.go(Scripted({}), tiles=[TILES[0]])
        good = (self.data / TILES[0] / "heat" / "months.png").read_bytes()
        p2 = Scripted({TILES[0]: "invalid"})
        p2.version = 2
        counts, _ = self.go(p2, tiles=[TILES[0]])
        self.assertEqual(counts[FAILED], 1)
        self.assertEqual((self.data / TILES[0] / "heat" / "months.png").read_bytes(), good)

    def test_the_breaker_stops_a_systemic_failure(self):
        tiles = [f"N4{i}.00E12.00" for i in range(8)]
        with self.assertRaises(Systemic):
            self.go(Scripted({t: "bug" for t in tiles}), tiles=tiles, breaker=4)

    def test_tiles_that_keep_failing_do_not_starve_the_fresh_ones(self):
        border = [f"N43.{i}0E7.00" for i in range(3)]
        fresh = [f"N40.{i}0E12.00" for i in range(6)]
        script = {t: "upstream" for t in border}
        self.go(Scripted(script), tiles=border)           # first night: all fail
        counts, _ = self.go(Scripted(script), tiles=border + fresh, breaker=3)
        self.assertEqual(counts[DONE], 6)
        self.assertEqual(counts[FAILED], 3)

    def test_a_mountain_range_of_border_tiles_is_not_systemic(self):
        border = [f"N46.{i}0E7.00" for i in range(10)]      # first attempts, all in a row
        fresh = [f"N40.{i}0E12.00" for i in range(3)]
        counts, _ = self.go(Scripted({t: "border" for t in border}), tiles=border + fresh, breaker=4)
        self.assertEqual((counts[FAILED], counts[DONE]), (10, 3))
        self.assertEqual(self.state.get("heat", border[0]).kind, errors.UPSTREAM)

    def test_out_of_time_skips_without_recording(self):
        counts, _ = self.go(Scripted({}), budget=Budget(seconds=30))
        self.assertEqual(counts["skipped"], 3)
        self.assertEqual(self.state.get("heat", TILES[0]).status, "")

    def test_an_interrupted_swap_is_restored(self):
        self.go(Scripted({}), tiles=[TILES[0]])
        tile = self.data / TILES[0]
        (tile / "heat").rename(tile / ".heat.old-abc123")   # killed between the two renames
        (tile / ".heat.stage-def456").mkdir()
        p = Scripted({})
        p.version = 2
        self.go(p, tiles=[TILES[0]])
        self.assertTrue((tile / "heat" / "months.png").exists())
        self.assertEqual(sorted(x.name for x in tile.iterdir() if x.name.startswith(".")), [])


if __name__ == "__main__":
    unittest.main()
