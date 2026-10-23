import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image, ImageDraw  # noqa: E402

from ps import apply, imaging, layout  # noqa: E402
from ps.config import Config  # noqa: E402
from ps.download import Downloader  # noqa: E402
from ps.index import Index  # noqa: E402
from ps.report import Report  # noqa: E402
from ps.sources import SetInfo  # noqa: E402


def jpeg(seed, size=(600, 900), quality=90):
    img = Image.new("RGB", size, (seed * 37 % 255, 80, 160))
    d = ImageDraw.Draw(img)
    for i in range(6):
        x = (seed * 53 + i * 97) % size[0]
        d.ellipse([x, i * 120, x + 200, i * 120 + 260], fill=((seed + i) * 61 % 255, i * 40, 255 - i * 30))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def resized(data, size, quality=70):
    with Image.open(io.BytesIO(data)) as img:
        buf = io.BytesIO()
        img.resize(size).save(buf, "JPEG", quality=quality)
        return buf.getvalue()


class Layout(unittest.TestCase):
    def test_set_dir_name(self):
        info = SetInfo("elitebabes", "u", "19431", 'Call: Girl "5"?', studio="Penthouse", date="2018-12-24")
        self.assertEqual(layout.set_dir_name(info), "2018-12-24 Penthouse - Call Girl 5 [elitebabes-19431]")
        bare = SetInfo("pornpics", "u", "1", "x" * 400)
        name = layout.set_dir_name(bare)
        self.assertTrue(name.endswith(" [pornpics-1]"))
        self.assertLessEqual(len(name.encode()), layout.MAX_COMPONENT_BYTES)

    def test_performer_dir_reuses_id_folder(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, "Old Name [7]"))
            self.assertEqual(layout.performer_dir(root, {"id": "7", "name": "New"}), f"{root}/Old Name [7]")
            self.assertEqual(layout.performer_dir(root, {"id": "17", "name": "A/B"}), f"{root}/A-B [17]")

    def test_norm(self):
        self.assertEqual(layout.norm("Zoë  D'Arc"), layout.norm("zoe darc"))
        self.assertEqual(layout.performer_keys({"name": "Jane Doe", "alias_list": ["J. Doe", ""]}), {"janedoe", "jdoe"})


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = Config.from_settings({})
        self.assertTrue(c.dry_run and c.include_favorites and c.promo_images)
        self.assertEqual((c.min_rating100, c.tags, c.sources, c.max_sets), (0, [], ["pornpics", "elitebabes"], 50))

    def test_values(self):
        c = Config.from_settings({"min_rating": "3.5", "tags": "A, B ,", "sources": "EliteBabes", "include_favorites": False,
                                  "max_sets_per_source": 0, "download_root": "/x/"})
        self.assertEqual((c.min_rating100, c.tags, c.sources, c.include_favorites, c.max_sets, c.root),
                         (70, ["A", "B"], ["elitebabes"], False, 0, "/x"))

    def test_unknown_source(self):
        with self.assertRaises(SystemExit):
            Config.from_settings({"sources": "pornpics,nope"})


class Dedupe(unittest.TestCase):
    def test_resized_copy_is_near(self):
        a = jpeg(1)
        ma, mb = imaging.inspect(a), imaging.inspect(resized(a, (1000, 1500)))
        self.assertNotEqual(ma.sha256, mb.sha256)
        self.assertLessEqual(imaging.distance(ma.dhash, mb.dhash), 4)
        self.assertTrue(imaging.same_shape(ma.width, ma.height, mb.width, mb.height))

    def test_different_images_are_far(self):
        self.assertGreater(imaging.distance(imaging.inspect(jpeg(1)).dhash, imaging.inspect(jpeg(2)).dhash), 4)

    def test_not_an_image(self):
        with self.assertRaises(ValueError):
            imaging.inspect(b"<html>")

    def test_index_near_and_exclude(self):
        with tempfile.TemporaryDirectory() as d:
            idx = Index(os.path.join(d, "i.db"))
            m = imaging.inspect(jpeg(3))
            s = idx.set_id("/r/a")
            idx.add_image(s, "/r/a/001.jpg", m)
            other = imaging.inspect(resized(jpeg(3), (1000, 1500)))
            self.assertEqual(idx.near(other, 4), {s})
            self.assertEqual(idx.near(other, 4, exclude_set=s), set())
            cropped = imaging.inspect(resized(jpeg(3), (900, 900)))
            self.assertEqual(idx.near(cropped, 64), set())  # different aspect ratio never matches
            idx.close()
            idx = Index(os.path.join(d, "i.db"))
            self.assertEqual(idx.near(other, 4), {s})  # survives reopen (signed storage)
            idx.close()


class FakeStash:
    def __init__(self, known):
        self.known = known
        self.created = []

    def lookup(self, kind, name):
        return self.known.get((kind, name))

    def ensure_tag(self, name):
        self.created.append(name)
        self.known[("tag", name)] = "900"
        return "900"


class Apply(unittest.TestCase):
    def gallery(self, **kw):
        g = {"id": "5", "title": "", "date": None, "urls": [], "studio": None, "performers": [], "tags": [],
             "folder": {"path": "/r/p/s"}}
        g.update(kw)
        return g

    def test_fills_empty_fields(self):
        stash = FakeStash({("studio", "Penthouse"): "3", ("performer", "Mary"): "12"})
        sc = {"title": "T", "date": "2020-01-02", "studio": "Penthouse", "urls": ["u1"], "performer_id": "7",
              "other_performers": ["Mary"]}
        values, problems = apply.plan(stash, Config(), self.gallery(), sc)
        self.assertEqual(problems, [])
        self.assertEqual(values, {"id": "5", "studio_id": "3", "title": "T", "date": "2020-01-02", "urls": ["u1"],
                                  "performer_ids": ["7", "12"]})

    def test_keeps_manual_edits_and_flags_unknowns(self):
        stash = FakeStash({})
        g = self.gallery(title="Mine", studio={"id": "1"}, urls=["u0"], performers=[{"id": "7"}])
        sc = {"title": "T", "studio": "Nope", "urls": ["u0", "u1"], "performer_id": "7", "other_performers": ["Ghost"]}
        values, problems = apply.plan(stash, Config(), g, sc, dry=False)
        self.assertEqual(values, {"id": "5", "urls": ["u0", "u1"], "tag_ids": ["900"]})
        self.assertEqual(problems, ["performer 'Ghost' not in Stash"])  # studio already set by hand: not a problem

    def test_dry_run_creates_no_tag(self):
        stash = FakeStash({})
        _, problems = apply.plan(stash, Config(), self.gallery(), {"studio": "Nope"}, dry=True)
        self.assertEqual((problems, stash.created), (["studio 'Nope' not in Stash"], []))

    def test_review_tag_removed_once_resolved(self):
        stash = FakeStash({("tag", apply.REVIEW_TAG): "900", ("studio", "S"): "3"})
        values, _ = apply.plan(stash, Config(), self.gallery(tags=[{"id": "900"}, {"id": "1"}]), {"studio": "S"})
        self.assertEqual(values, {"id": "5", "studio_id": "3", "tag_ids": ["1"]})


class FakeFetcher:
    def __init__(self, blobs):
        self.blobs = blobs

    def get(self, url, **_kw):
        return type("R", (), {"content": self.blobs[url]})()


class DownloadFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.idx = Index(os.path.join(self.root, ".i.db"))
        self.cfg = Config(root=self.root, min_size=500)

    def tearDown(self):
        self.idx.close()
        self.tmp.cleanup()

    def source(self, info):
        from ps.sources import ImageRef  # noqa: F401
        return type("Src", (), {"NAME": info.source, "get_set": staticmethod(lambda ref, f: info),
                                "referer": staticmethod(lambda i: None)})

    def run_set(self, info, blobs, performer=None):
        from ps.sources import SetRef
        p = performer or {"id": "7", "name": "Jane Doe", "alias_list": []}
        d = Downloader(None, self.cfg, FakeFetcher(blobs), self.idx, Report(), dry=False)
        d.sources = []
        pdir = layout.performer_dir(self.root, p)
        d.one_set(p, pdir, self.source(info), SetRef(info.source, info.url, info.source_id), layout.performer_keys(p), "who")
        return d, pdir

    def info(self, source, sid, seeds, size=(600, 900)):
        from ps.sources import ImageRef
        urls = [f"https://{source}/{sid}/{s}.jpg" for s in seeds]
        return SetInfo(source, f"https://{source}/set/{sid}", sid, f"Set {sid}", studio="Acme",
                       models=["Jane Doe", "Mary"], images=[ImageRef(u, *size) for u in urls]), urls

    def test_new_set_then_same_set_elsewhere(self):
        a, a_urls = self.info("pornpics", "1", [1, 2, 3, 4])
        blobs = {u: jpeg(s) for u, s in zip(a_urls, [1, 2, 3, 4])}
        self.run_set(a, blobs)
        folder = f"{self.root}/Jane Doe [7]/Acme - Set 1 [pornpics-1]"
        self.assertEqual(sorted(os.listdir(folder)), ["001.jpg", "002.jpg", "003.jpg", "004.jpg", "set.json"])
        sc = layout.read_sidecar(folder)
        self.assertEqual((sc["other_performers"], sc["urls"]), (["Mary"], ["https://pornpics/set/1"]))
        self.assertEqual(self.idx.status(a.url), "done")

        b, b_urls = self.info("elitebabes", "9", [1, 2, 3, 4], size=(1000, 1500))
        blobs.update({u: resized(jpeg(s), (1000, 1500)) for u, s in zip(b_urls, [1, 2, 3, 4])})
        d, _ = self.run_set(b, blobs)
        self.assertEqual(self.idx.status(b.url), "duplicate")
        self.assertFalse(any("elitebabes" in e for e in os.listdir(f"{self.root}/Jane Doe [7]")))
        self.assertEqual(layout.read_sidecar(folder)["urls"], ["https://pornpics/set/1", "https://elitebabes/set/9"])
        self.assertEqual(d.refresh, {folder})

    def test_rejects_set_without_performer(self):
        a, urls = self.info("pornpics", "2", [5])
        a.models = ["Someone Else"]
        self.run_set(a, {})
        self.assertEqual(self.idx.status(a.url), "rejected")

    def test_alias_counts_as_performer(self):
        a, urls = self.info("pornpics", "3", [6])
        a.models = ["J. Doe"]
        self.run_set(a, {urls[0]: jpeg(6)}, {"id": "7", "name": "Jane Doe", "alias_list": ["J Doe"]})
        self.assertEqual(self.idx.status(a.url), "done")

    def test_small_set(self):
        a, _ = self.info("pornpics", "4", [7], size=(300, 450))
        self.run_set(a, {})
        self.assertEqual(self.idx.status(a.url), "small")


if __name__ == "__main__":
    unittest.main()
