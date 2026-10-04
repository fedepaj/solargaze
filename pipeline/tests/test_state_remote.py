import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "wind")]
from sg.remote import BASE_KEY, DELTAS, Remote  # noqa: E402
from sg.state import DONE, FAILED, State  # noqa: E402


class FakeS3:
    """Just what Remote's state code calls."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, Bucket, Key):
        data = self.objects[Key]
        return {"Body": type("B", (), {"read": lambda self: data})()}

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)

    def get_paginator(self, name):
        objs = self.objects

        class P:
            def paginate(self, Bucket, Prefix=""):
                return [{"Contents": [{"Key": k} for k in sorted(objs) if k.startswith(Prefix)]}]
        return P()


def remote(state_dir):
    r = Remote.__new__(Remote)
    r.s3, r.bucket, r.ops, r.log, r.state_dir, r.data_dir = FakeS3(), "tiles", "ops", None, state_dir, None
    r._state_cache = None
    return r


class BulkState(unittest.TestCase):
    def test_legacy_deltas_compaction_newest_wins(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            r = remote(root / "a")
            # a record in the old layout, from before
            old = {"tile": "T1", "product": "wind", "status": FAILED, "at": "2026-10-01T00:00:00+00:00", "attempts": 2}
            r.s3.put_object(Bucket="ops", Key="state/wind/T1.json", Body=json.dumps(old).encode())
            # run A records T1 done and T2 done
            a = State(root / "a")
            a.record("T1", "wind", DONE, version=1, run="A")
            a.record("T2", "wind", DONE, version=1, run="A")
            self.assertEqual(r.push_delta("A", a), 2)
            r._state_cache = None
            # a fresh machine reads base + deltas + legacy in a few requests
            b = State(root / "b")
            self.assertEqual(r.pull_state(b), 2)
            self.assertEqual(b.get("wind", "T1").status, DONE)        # newer than the legacy failure
            # compaction folds everything into the base and removes what it folded
            summary = r.compact()
            self.assertEqual(summary, {"records": 2, "deltas": 1, "legacy": 1})
            self.assertEqual(sorted(r.s3.objects), [BASE_KEY])
            # run C fails T2 later: its delta wins over the base
            c = State(root / "c")
            c.record("T2", "wind", FAILED, version=1, run="C", kind="upstream")
            r.push_delta("C", c)
            r._state_cache = None
            fresh = State(root / "d")
            r.pull_state(fresh)
            self.assertEqual(fresh.get("wind", "T2").status, FAILED)
            self.assertEqual(fresh.get("wind", "T1").status, DONE)
            self.assertEqual(len([k for k in r.s3.objects if k.startswith(DELTAS)]), 1)

    def test_push_state_sends_only_what_is_newer_here(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            r = remote(root / "a")
            a = State(root / "a")
            a.record("T1", "heat", DONE, version=3, run="laptop")
            self.assertEqual(r.push_state(a), 1)
            r._state_cache = None
            self.assertEqual(r.push_state(a), 0)          # already there: nothing to send


if __name__ == "__main__":
    unittest.main()
