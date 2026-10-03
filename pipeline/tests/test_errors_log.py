import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sg import errors  # noqa: E402
from sg.log import start_run  # noqa: E402


class FakeHTTPError(Exception):
    def __init__(self, code):
        super().__init__(f"{code} Client Error: for url: https://x.example/y")
        self.response = type("R", (), {"status_code": code})()


class Classify(unittest.TestCase):
    def test_messages_seen_on_this_network(self):
        cases = {
            "HTTPSConnectionPool(host='planetarycomputer.microsoft.com', port=443): Max retries exceeded": errors.TRANSIENT,
            "CURL error: Could not resolve host: landsateuwest.blob.core.windows.net": errors.TRANSIENT,
            "overpass busy: 504": errors.TRANSIENT,
            "HTTP response code: 403": errors.UPSTREAM,
            "Request for 1856559-1890912 failed with response_code=0": errors.TRANSIENT,
            "division by zero": errors.BUG,
        }
        for msg, kind in cases.items():
            self.assertEqual(errors.classify(RuntimeError(msg)), kind, msg)

    def test_types_and_status_codes(self):
        self.assertEqual(errors.classify(socket.gaierror(8, "nodename nor servname provided")), errors.TRANSIENT)
        self.assertEqual(errors.classify(TimeoutError()), errors.TRANSIENT)
        self.assertEqual(errors.classify(FakeHTTPError(503)), errors.TRANSIENT)
        self.assertEqual(errors.classify(FakeHTTPError(429)), errors.TRANSIENT)
        self.assertEqual(errors.classify(FakeHTTPError(404)), errors.UPSTREAM)
        self.assertEqual(errors.classify(errors.NoData("open sea")), errors.NODATA)
        self.assertEqual(errors.classify(errors.Invalid("no month")), errors.BUG)

    def test_signature_groups_the_same_cause(self):
        a = errors.signature(RuntimeError("N41.75E12.25: 12 of 214 scenes failed to read; tile not written"))
        b = errors.signature(RuntimeError("N45.50E9.00: 40 of 199 scenes failed to read; tile not written"))
        self.assertEqual(a, b)
        self.assertIn("<tile>", a)
        c = errors.signature(RuntimeError("Could not resolve host https://a.b/c?sig=0SZr0pi"))
        self.assertNotIn("0SZr", c)

    def test_retry_only_transient(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError("timed out")
            return "ok"
        self.assertEqual(errors.retry(flaky, base=0, sleep=lambda s: None), "ok")
        self.assertEqual(len(calls), 3)
        with self.assertRaises(ValueError):
            errors.retry(lambda: (_ for _ in ()).throw(ValueError("bug")), sleep=lambda s: None)


class RunLog(unittest.TestCase):
    def test_events_summary_and_digest(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with start_run("test", Path(d), console=out) as run:
                with run.step("heat", tile="N41.75E12.25", product="heat"):
                    pass
                for t in ("N41.75E12.25", "N42.00E12.50"):
                    try:
                        with run.step("heat", tile=t, product="heat"):
                            raise RuntimeError(f"{t}: 30 of 200 scenes failed to read; tile not written")
                    except RuntimeError as e:
                        run.outcome(t, "heat", "failed", 3.0, exc=e)
                run.outcome("N41.50E12.00", "heat", "empty", exc=errors.NoData("open sea"))
                import logging
                logging.getLogger("wind.extract").info("hello from an old script")
                run_id = run.id
            lines = [json.loads(l) for l in (Path(d) / f"{run_id}.jsonl").read_text().splitlines()]
            evs = [l["ev"] for l in lines]
            self.assertEqual(evs[0], "run.start")
            self.assertEqual(evs[-1], "run.end")
            self.assertTrue(any(l["ev"] == "log" and l["logger"] == "wind.extract" for l in lines))
            errs = [l for l in lines if l["ev"] == "heat.error"]
            self.assertEqual({e["tile"] for e in errs}, {"N41.75E12.25", "N42.00E12.50"})
            s = json.loads((Path(d) / f"{run_id}.summary.json").read_text())
            self.assertEqual(s["outcomes"]["heat"], {"failed": 2, "empty": 1})
            self.assertEqual(len(s["failures"]), 1)            # two tiles, one cause
            self.assertEqual(s["failures"][0]["count"], 2)
            self.assertIn("run.end", out.getvalue())


if __name__ == "__main__":
    unittest.main()
