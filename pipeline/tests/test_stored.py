import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE / "wind")]

import numpy as np
from PIL import Image

import reencode
import stored


def field(rows=120, cols=96, seed=1) -> np.ndarray:
    """A smooth level with a building-shaped hole and a strip of no data."""
    y, x = np.mgrid[0:rows, 0:cols]
    rng = np.random.default_rng(seed)
    a = 90 + 40 * np.sin(x / 17) * np.cos(y / 23) + rng.normal(0, 1, (rows, cols))
    a = np.clip(np.round(a), 1, 255).astype(np.uint8)
    a[30:44, 20:51] = 0
    a[:, :3] = 0
    return a


class Lossy(unittest.TestCase):
    def test_no_data_comes_back_exactly_where_it_was(self):
        a = field()
        self.assertTrue(np.array_equal(stored.decode(stored.encode_lossy(a), ".avif") > 0, a > 0))

    def test_the_level_comes_back_close(self):
        a = field()
        b = stored.decode(stored.encode_lossy(a), ".avif")
        e = (b.astype(int) - a)[a > 0]
        self.assertLess(np.sqrt((e ** 2).mean()), 1.5)

    def test_a_cell_with_data_is_never_read_as_none(self):
        a = np.ones((64, 64), np.uint8)       # the lowest level there is, everywhere
        a[10:20, 10:20] = 0
        b = stored.decode(stored.encode_lossy(a), ".avif")
        self.assertTrue((b[a > 0] >= 1).all())
        self.assertTrue((b[a == 0] == 0).all())

    def test_all_data_and_no_data_at_all(self):
        full = np.full((32, 32), 100, np.uint8)
        self.assertTrue((stored.decode(stored.encode_lossy(full), ".avif") > 0).all())
        empty = np.zeros((32, 32), np.uint8)
        self.assertFalse(stored.decode(stored.encode_lossy(empty), ".avif").any())


class Exact(unittest.TestCase):
    def test_every_byte_comes_back(self):
        a = np.random.default_rng(3).integers(0, 256, (90, 70), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as d:
            stored.exact(a, Path(d) / "a.webp")
            self.assertTrue(np.array_equal(stored.read(Path(d) / "a.webp"), a))


class Builders(unittest.TestCase):
    def test_the_heat_months_are_written_lossy_and_pass_their_check(self):
        import heat_landsat
        from sg.products.heat import Heat
        rng = np.random.default_rng(5)
        t = {m: (20 + rng.normal(0, 2, (30, 24))).astype(np.float32) for m in (1, 7)}
        t[7][:5, :5] = np.nan
        with tempfile.TemporaryDirectory() as d:
            files = heat_landsat.write_packed(t, Path(d))
            self.assertEqual(files["months"], "heat/months.avif")
            months = stored.read(Path(d) / "months.avif")
            self.assertEqual(months.shape, (12 * 30, 24))
            self.assertFalse(months[6 * 30:6 * 30 + 5, :5].any())      # July's hole
            self.assertFalse(months[30:60].any())                        # February: no data
            info = {"rows": 30, "cols": 24, "files": files, "overviews": list(heat_landsat.OVERVIEWS),
                    "months": {"01": {"tile_median_c": 20.0}, "07": {"tile_median_c": 20.0}}}
            Heat().validate("N41.75E12.25", Path(d), info)


class Reencode(unittest.TestCase):
    def tile(self, d: Path) -> Path:
        t = d / "N41.75E12.25"
        for sub in ("noise", "light", "wind", "air"):
            (t / sub).mkdir(parents=True)
        Image.fromarray(field(), "L").save(t / "noise" / "lden.png")
        Image.fromarray(field(40, 32), "L").save(t / "noise" / "lden.o3.png")
        Image.fromarray(field(), "L").save(t / "light" / "sky.png")
        Image.fromarray(field(), "L").save(t / "wind" / "heights.png")
        (t / "air" / "climatology.json").write_text("{}")
        meta = {"id": t.name, "bounds": [12.25, 41.75, 12.5, 42.0], "products": {
            "noise": {"encoding": {"byte1_db": 25.0, "step_db": 0.5, "nodata_byte": 0}, "generated": "2026-01-01T00:00:00+00:00",
                      "files": {"lden": "noise/lden.png", "lden.o3": "noise/lden.o3.png"}},
            "light": {"encoding": {"nodata_byte": 0}, "files": {"sky": "light/sky.png"}, "generated": "2026-01-01T00:00:00+00:00"},
            "wind": {"heights_png": {"file": "heights.png", "metres_per_lsb": 1.0}, "generated": "2026-01-01T00:00:00+00:00"},
            "air": {"file": "air/climatology.json", "generated": "2026-01-01T00:00:00+00:00"}}}
        (t / "meta.json").write_text(json.dumps(meta))
        return t

    def test_the_meta_names_the_new_files_and_the_old_stay(self):
        with tempfile.TemporaryDirectory() as d:
            t = self.tile(Path(d))
            before, after, left = reencode.reencode(t)
            self.assertGreater(before, 0)
            self.assertEqual(left, [])
            p = json.loads((t / "meta.json").read_text())["products"]
            self.assertEqual(p["noise"]["files"], {"lden": "noise/lden.avif", "lden.o3": "noise/lden.o3.avif"})
            self.assertEqual(p["noise"]["encoding"]["lossy"], stored.LOSSY)
            self.assertEqual(p["light"]["files"], {"sky": "light/sky.webp"})
            self.assertNotIn("lossy", p["light"]["encoding"])
            self.assertEqual(p["wind"]["heights_png"]["file"], "heights.webp")
            self.assertTrue((t / "noise" / "lden.png").exists())
            self.assertTrue(np.array_equal(stored.read(t / "noise" / "lden.avif") > 0, field() > 0))
            self.assertTrue(np.array_equal(stored.read(t / "wind" / "heights.webp"), field()))
            self.assertTrue(np.array_equal(stored.read(t / "light" / "sky.webp"), field()))
            # Stamped again, so that an older copy of the meta does not win the merge; what was not touched is not.
            self.assertGreater(p["noise"]["generated"], "2026-01-01T00:00:00+00:00")
            self.assertEqual(p["air"]["generated"], "2026-01-01T00:00:00+00:00")
            self.assertEqual(reencode.reencode(t)[:2], (0, 0))      # a second pass finds nothing to do

    def test_an_older_meta_does_not_put_the_png_back(self):
        from sg.remote import merge_meta
        with tempfile.TemporaryDirectory() as d:
            t = self.tile(Path(d))
            older = json.loads((t / "meta.json").read_text())
            reencode.reencode(t)
            merged = merge_meta(older, json.loads((t / "meta.json").read_text()))
            self.assertEqual(merged["products"]["noise"]["files"]["lden"], "noise/lden.avif")

    def test_a_product_with_a_file_missing_is_left_whole(self):
        with tempfile.TemporaryDirectory() as d:
            t = self.tile(Path(d))
            (t / "noise" / "lden.o3.png").unlink()
            _, _, left = reencode.reencode(t)
            self.assertEqual(len(left), 1)
            p = json.loads((t / "meta.json").read_text())["products"]
            self.assertEqual(p["noise"]["files"]["lden"], "noise/lden.png")
            self.assertFalse((t / "noise" / "lden.avif").exists())
            self.assertEqual(p["light"]["files"], {"sky": "light/sky.webp"})

    def test_an_image_that_is_not_one_channel_is_left(self):
        with tempfile.TemporaryDirectory() as d:
            t = self.tile(Path(d))
            Image.fromarray(np.dstack([field()] * 4), "RGBA").save(t / "light" / "sky.png")
            _, _, left = reencode.reencode(t)
            self.assertEqual(left, ["light: not a one-channel image"])

    def test_prune_removes_only_what_was_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            t = self.tile(Path(d))
            self.assertEqual(reencode.prune(t), 0)      # nothing re-encoded yet: nothing to remove
            self.assertTrue((t / "noise" / "lden.png").exists())
            reencode.reencode(t)
            self.assertGreater(reencode.prune(t), 0)
            for gone in ("noise/lden.png", "noise/lden.o3.png", "light/sky.png", "wind/heights.png"):
                self.assertFalse((t / gone).exists(), gone)
            for kept in ("noise/lden.avif", "light/sky.webp", "wind/heights.webp", "air/climatology.json"):
                self.assertTrue((t / kept).exists(), kept)

    def test_the_parts_cover_every_tile_once(self):
        ids = [f"N{la}.00E{lo}.25" for la in range(36, 60) for lo in range(-9, 30)]
        parts = [[t for t in ids if reencode.shard_of(t, 16) == i] for i in range(16)]
        self.assertEqual(sorted(sum(parts, [])), sorted(ids))
        self.assertTrue(all(parts))


if __name__ == "__main__":
    unittest.main()
