import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from ps import apply, curate, imaging, layout  # noqa: E402
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
        self.assertEqual((c.min_rating100, c.tags, c.sources, c.promo_sources, c.max_sets),
                         (0, [], ["pornpics", "elitebabes", "babepedia"], ["stashbox", "babepedia"], 50))

    def test_values(self):
        c = Config.from_settings({"min_rating": "3.5", "tags": "A, B ,", "sources": "EliteBabes", "include_favorites": False,
                                  "max_sets_per_source": 0, "download_root": "/x/", "evict_rating": "1.5",
                                  "exclude_studios": "A, B", "max_sets_per_performer": 0})
        self.assertEqual((c.evict_rating100, c.exclude_studios, c.max_per_performer), (30, ["A", "B"], 0))
        self.assertEqual((Config().evict_rating100, Config().max_per_performer, Config().recheck_days), (20, 50, 7))
        w = Config.from_settings({"tag_weights": "Lingerie=3, Outdoor, Hardcore=-3, Gangbang=x, Big=99"}).tag_weights
        self.assertEqual(w, [("Lingerie", 3.0), ("Outdoor", 2.0), ("Hardcore", -3.0), ("Gangbang", None), ("Big", 10.0)])
        self.assertEqual((c.min_rating100, c.tags, c.sources, c.include_favorites, c.max_sets, c.root),
                         (70, ["A", "B"], ["elitebabes"], False, 0, "/x"))

    def test_unknown_source(self):
        with self.assertRaises(SystemExit):
            Config.from_settings({"sources": "pornpics,nope"})
        with self.assertRaises(SystemExit):
            Config.from_settings({"promo_sources": "theporndb"})


class Dedupe(unittest.TestCase):
    def test_resized_copy_is_near(self):
        a = jpeg(1)
        ma, mb = imaging.inspect(a), imaging.inspect(resized(a, (1000, 1500)))
        self.assertNotEqual(ma.sha256, mb.sha256)
        self.assertLessEqual(imaging.distance(ma.dhash, mb.dhash), 4)
        self.assertTrue(imaging.same_shape(ma.width, ma.height, mb.width, mb.height))

    def test_different_images_are_far(self):
        self.assertGreater(imaging.distance(imaging.inspect(jpeg(1)).dhash, imaging.inspect(jpeg(2)).dhash), 4)

    def test_blur_lowers_sharpness(self):
        with Image.open(io.BytesIO(jpeg(1, size=(1200, 1800)))) as img:
            img.load()
            sharp = imaging.sharpness(img)
            self.assertLess(imaging.sharpness(img.filter(ImageFilter.GaussianBlur(3))), sharp / 2)
            small = img.resize((400, 600)).resize((1200, 1800), Image.Resampling.BICUBIC)
            self.assertLess(imaging.sharpness(small), sharp)

    def test_old_index_gets_sharpness_column(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE images (id INTEGER PRIMARY KEY, set_id INTEGER NOT NULL, path TEXT UNIQUE NOT NULL, "
                       "sha256 TEXT NOT NULL, dhash INTEGER NOT NULL, width INTEGER, height INTEGER)")
            db.execute("INSERT INTO images(set_id, path, sha256, dhash, width, height) VALUES(1, '/a.jpg', 'x', 0, 10, 20)")
            db.commit()
            db.close()
            idx = Index(path)
            self.assertEqual(idx.stats(1), [(10, 20, None)])
            self.assertEqual(idx.unmeasured(1), [(1, "/a.jpg")])
            idx.close()

    def test_soft_threshold_is_a_library_percentile(self):
        with tempfile.TemporaryDirectory() as d:
            idx = Index(os.path.join(d, "i.db"))
            self.assertIsNone(idx.soft_threshold(10))
            for n in range(60):
                idx.add_image(idx.set_id(f"/r/{n}"), f"/r/{n}/001.jpg", imaging.Meta(f"{n}", n, 10, 20, ".jpg", float(n)))
            self.assertEqual((idx.soft_threshold(10), idx.soft_threshold(0)), (6.0, None))
            idx.close()

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


class Curate(unittest.TestCase):
    def item(self, key, kind="kept", **kw):
        kw.setdefault("images", 20)
        return curate.Item(kind, key, **kw)

    def test_score(self):
        cfg = Config(exclude_studios=["Bad Studio"], tag_weights=[("Gangbang", None), ("outdoor", 2.0)])
        studios = {"Fav": {"favorite": True, "rating100": 100}, "Meh": {"favorite": False, "rating100": 20}}
        scorer = curate.Scorer(cfg, studio_info=studios.get)
        s = lambda **kw: scorer.score(self.item("x", "candidate", **kw))  # noqa: E731
        self.assertGreater(s(long_edge=2400), s(long_edge=1700))
        self.assertGreater(s(long_edge=1700), s(long_edge=1300))
        self.assertGreater(s(long_edge=1300), s(long_edge=1050))
        self.assertGreater(s(long_edge=1050), s(long_edge=900))
        self.assertLess(s(soft=True), s())
        self.assertGreater(s(models=1), s(models=2))
        self.assertGreater(s(models=2), s(models=5))
        self.assertGreater(s(images=12), s(images=6))
        self.assertGreater(s(long_edge=1800), s(long_edge=900))
        self.assertGreater(s(studio="Fav"), s(studio="Unknown"))
        self.assertGreater(s(studio="Unknown"), s(studio="Meh"))
        self.assertGreater(s(tags=["Outdoor"]), s(tags=[]))
        self.assertIsNone(s(studio="bad-studio"))
        self.assertIsNone(s(tags=["gangbang", "x"]))

    def test_plan_budget_ratings_and_studio_cap(self):
        cfg = Config(max_per_performer=3, max_per_studio=1)
        items = [self.item("solo-a", studio="A", models=1), self.item("solo-a2", studio="A", models=1),
                 self.item("group", studio="B", models=5), self.item("rated", studio="B", models=5, rating100=80),
                 self.item("one-star", studio="C", models=1, rating100=20),
                 self.item("new", "candidate", studio="D", models=1, images=30)]
        chosen, evict, queued = curate.plan(items, cfg, curate.Scorer(cfg))
        self.assertEqual([i.key for i in chosen], ["rated", "solo-a", "new"])  # solo-a2: studio A already has one
        self.assertEqual(sorted(i.key for i in evict), ["group", "one-star", "solo-a2"])
        self.assertEqual([i.key for i in queued], ["new"])

    def test_studio_overflow_fills_free_slots(self):
        cfg = Config(max_per_performer=3, max_per_studio=1)
        items = [self.item(k, studio="A") for k in ("a", "b", "c")]
        _chosen, evict, _queued = curate.plan(items, cfg, curate.Scorer(cfg))
        self.assertEqual(evict, [])

    def test_protected_beyond_budget_are_kept(self):
        cfg = Config(max_per_performer=1)
        items = [self.item("r1", rating100=60), self.item("r2", organized=True), self.item("x")]
        _chosen, evict, _queued = curate.plan(items, cfg, curate.Scorer(cfg))
        self.assertEqual([i.key for i in evict], ["x"])

    def test_tag_weights_follow_stash_aliases_and_parents(self):
        stash = type("S", (), {
            "lookup": lambda self, kind, name: {"lingerie": "1", "stockings": "2", "nylons": "2"}.get(name.casefold()),
            "tag_lineage": lambda self, tid: {"1": {"1"}, "2": {"2", "1"}}[tid]})()
        tw = curate.TagWeights([("Lingerie", 3.0), ("Bad", None)], stash)
        self.assertEqual(tw.match(["Nylons"]), (False, 3.0))  # alias of a sub-tag of Lingerie
        self.assertEqual(tw.match([], {"2"}), (False, 3.0))  # a tag on the gallery itself
        self.assertEqual(tw.match(["bad", "Lingerie"]), (True, 0.0))
        self.assertEqual(tw.match(["Other"]), (False, 0.0))
        capped = curate.TagWeights([("A", 5.0), ("B", 5.0)])
        self.assertEqual(capped.match(["a", "b"]), (False, 6.0))

    def test_hearted_tags_count_with_their_sub_tags(self):
        stash = type("S", (), {
            "lookup": lambda self, kind, name: {"lingerie": "1", "stockings": "2", "outdoor": "3"}.get(name.casefold()),
            "tag_lineage": lambda self, tid: {"1": {"1"}, "2": {"2", "1"}, "3": {"3"}}[tid],
            "favorite_tags": lambda self: {"1", "3"}})()
        tw = curate.TagWeights([], stash, 2.0)
        self.assertEqual(tw.match(["Stockings"]), (False, 2.0))  # sub-tag of hearted Lingerie
        self.assertEqual(tw.match(["Stockings", "Outdoor"]), (False, 4.0))
        explicit = curate.TagWeights([("Outdoor", -1.0)], stash, 2.0)
        self.assertEqual(explicit.match(["Outdoor"]), (False, -1.0))  # tag_weights wins over the heart
        self.assertEqual(curate.TagWeights([], stash, 0.0).match(["Stockings"]), (False, 0.0))


class FakeStash:
    def __init__(self, known):
        self.known = known
        self.created = []

    def lookup(self, kind, name):
        return self.known.get((kind, name))

    def favorite_tags(self):
        return set()

    def ensure_tag(self, name):
        self.created.append(name)
        tid = str(899 + len(self.created))
        self.known[("tag", name)] = tid
        return tid


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

    def test_quality_tags(self):
        stash = FakeStash({("tag", apply.QUALITY_TAGS["soft"]): "50"})
        sc = {"kind": "set", "other_performers": [], "quality": {"edge": 1800, "soft": False}}
        values, _ = apply.plan(stash, Config(), self.gallery(tags=[{"id": "50"}, {"id": "1"}]), sc, dry=False)
        self.assertEqual(stash.created, [apply.QUALITY_TAGS["solo"], apply.QUALITY_TAGS["hires"]])
        self.assertEqual(values, {"id": "5", "tag_ids": ["1", "900", "901"]})  # Soft removed, Solo + High-res added
        group = {"kind": "set", "other_performers": ["A", "B"]}
        self.assertEqual(apply.quality_tags(group), {"group"})
        self.assertEqual(apply.quality_tags({"kind": "promo"}), set())

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

    def downloader(self, blobs, dry=False):
        d = Downloader(None, self.cfg, FakeFetcher(blobs), self.idx, Report(), dry=dry)
        d.sources = []
        return d

    def run_set(self, info, blobs, performer=None):
        p = performer or {"id": "7", "name": "Jane Doe", "alias_list": []}
        d = self.downloader(blobs)
        pdir = layout.performer_dir(self.root, p)
        d.one_set(p, pdir, info, layout.performer_keys(p), "who")
        return d, pdir

    def survey_set(self, info, performer=None):
        from ps.sources import SetRef
        p = performer or {"id": "7", "name": "Jane Doe", "alias_list": []}
        d = self.downloader({})
        d.survey_one(p, self.source(info), SetRef(info.source, info.url, info.source_id), layout.performer_keys(p), "who")
        return d

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

        b, b_urls = self.info("elitebabes", "9", [1, 2, 3, 4])
        blobs.update({u: resized(jpeg(s), (600, 900)) for u, s in zip(b_urls, [1, 2, 3, 4])})
        d, _ = self.run_set(b, blobs)
        self.assertEqual(self.idx.status(b.url), "duplicate")
        self.assertFalse(any("elitebabes" in e for e in os.listdir(f"{self.root}/Jane Doe [7]")))
        self.assertEqual(layout.read_sidecar(folder)["urls"], ["https://pornpics/set/1", "https://elitebabes/set/9"])
        self.assertEqual(d.refresh, {folder})

    def test_higher_resolution_copy_replaces_the_set(self):
        a, a_urls = self.info("pornpics", "1", [1, 2, 3, 4])
        blobs = {u: jpeg(s) for u, s in zip(a_urls, [1, 2, 3, 4])}
        _, pdir = self.run_set(a, blobs)
        old = f"{pdir}/Acme - Set 1 [pornpics-1]"
        old_id = self.idx.find_set(old)
        b, b_urls = self.info("elitebabes", "9", [1, 2, 3, 4], size=(1000, 1500))
        blobs.update({u: resized(jpeg(s), (1000, 1500), quality=90) for u, s in zip(b_urls, [1, 2, 3, 4])})
        d, _ = self.run_set(b, blobs)
        new = f"{pdir}/Acme - Set 9 [elitebabes-9]"
        self.assertEqual((d.upgraded, os.path.exists(old)), (1, False))
        self.assertEqual(sorted(os.listdir(new)), ["001.jpg", "002.jpg", "003.jpg", "004.jpg", "set.json"])
        sc = layout.read_sidecar(new)
        self.assertEqual((sc["urls"], sc["quality"]["edge"]), ([b.url, a.url], 1500))
        self.assertEqual((self.idx.status(a.url), self.idx.status(b.url)), ("duplicate", "done"))
        self.assertEqual((self.idx.stats(old_id), self.idx.set_of(a.url)), ([], self.idx.find_set(new)))

    def test_low_quality_sample_skips_the_set(self):
        a, urls = self.info("pornpics", "6", [1, 2, 3], size=(0, 0))  # sizes unknown until the sample
        blobs = {u: jpeg(s) for u, s in zip(urls, [1, 2, 3])}
        self.cfg.min_size = 1000
        d, pdir = self.run_set(a, blobs)
        self.assertEqual((self.idx.status(a.url), os.path.exists(pdir)), ("low-quality", False))
        self.cfg.min_size = 500
        b, b_urls = self.info("pornpics", "7", [4, 5, 6])
        blobs.update({u: jpeg(s) for u, s in zip(b_urls, [4, 5, 6])})
        d = self.downloader(blobs)
        d.soft_below = 1e9
        d.one_set({"id": "7", "name": "Jane Doe"}, pdir, b, {"janedoe"}, "who")
        self.assertEqual(self.idx.status(b.url), "low-quality")

    def test_rejects_set_without_performer(self):
        a, urls = self.info("pornpics", "2", [5])
        a.models = ["Someone Else"]
        self.survey_set(a)
        self.assertEqual((self.idx.status(a.url), self.idx.has_candidate(a.url)), ("rejected", False))

    def test_alias_counts_as_performer(self):
        a, urls = self.info("pornpics", "3", [6])
        a.models = ["J. Doe"]
        self.survey_set(a, {"id": "7", "name": "Jane Doe", "alias_list": ["J Doe"]})
        self.assertEqual([info.url for _s, info in self.idx.candidates("7")], [a.url])

    def run_promo(self, sources, blobs):
        p = {"id": "7", "name": "Jane Doe", "alias_list": []}
        d = Downloader(None, self.cfg, FakeFetcher(blobs), self.idx, Report(), dry=False)
        d.promo_refs = lambda _p, _boxes: iter([(n, refs) for n, refs in sources if n not in d.blocked])
        d.promo(p, layout.performer_dir(self.root, p), {}, "who")
        return d, f"{self.root}/Jane Doe [7]/{layout.PROMO}"

    def test_promo_sources_share_one_folder(self):
        from ps.sources import ImageRef
        blobs = {"https://box/1.jpg": jpeg(11), "https://bp/a.jpg": resized(jpeg(11), (800, 1200)), "https://bp/b.jpg": jpeg(12)}
        sources = [("stashbox", iter([("theporndb-1", ImageRef("https://box/1.jpg", 600, 900), None)])),
                   ("babepedia", iter([("babepedia-pics-a", ImageRef("https://bp/a.jpg"), "https://bp/"),
                                       ("babepedia-pics-b", ImageRef("https://bp/b.jpg"), "https://bp/")]))]
        _, folder = self.run_promo(sources, blobs)
        self.assertEqual(sorted(os.listdir(folder)), ["babepedia-pics-b.jpg", "set.json", "theporndb-1.jpg"])
        self.assertEqual(sorted(layout.read_sidecar(folder)["images"]), ["babepedia-pics-b", "theporndb-1"])
        self.assertEqual(self.idx.status("https://bp/a.jpg"), "duplicate")  # same photo as the TPDB one

        blobs["https://bp/c.jpg"] = jpeg(13)
        more = [("babepedia", iter([("babepedia-pics-b", ImageRef("https://bp/b.jpg"), None),
                                    ("babepedia-pics-c", ImageRef("https://bp/c.jpg"), None)]))]
        self.run_promo(more, blobs)  # re-run: only the new image is fetched, the sidecar keeps the old ones
        self.assertEqual(sorted(layout.read_sidecar(folder)["images"]), ["babepedia-pics-b", "babepedia-pics-c", "theporndb-1"])

    def test_blocked_promo_source_is_skipped(self):
        from ps.fetch import Blocked
        from ps.sources import ImageRef

        def blocked():
            raise Blocked("bot challenge")
            yield

        sources = [("babepedia", blocked()), ("stashbox", iter([("theporndb-1", ImageRef("https://box/1.jpg", 600, 900), None)]))]
        d, folder = self.run_promo(sources, {"https://box/1.jpg": jpeg(11)})
        self.assertEqual((d.blocked, os.listdir(folder).count("theporndb-1.jpg")), ({"babepedia"}, 1))

    def test_small_set(self):
        a, _ = self.info("pornpics", "4", [7], size=(300, 450))
        self.survey_set(a)
        self.assertEqual(self.idx.status(a.url), "small")

    def test_fill_takes_the_best_candidates_until_the_budget_is_full(self):
        self.cfg.max_per_performer = 2
        p = {"id": "7", "name": "Jane Doe", "alias_list": []}
        blobs = {}
        infos = {}
        for sid, seeds, models in (("1", [1, 2, 3], ["Jane Doe", "A", "B", "C"]), ("2", [4, 5, 6], ["Jane Doe"]),
                                   ("3", [1, 2, 3], ["Jane Doe"]), ("4", [7, 8, 9], ["Jane Doe", "Mary"])):
            info, urls = self.info("pornpics", sid, seeds)
            info.models = models
            blobs.update({u: jpeg(s) for u, s in zip(urls, seeds)})
            infos[sid] = info
        self.run_set(infos["1"], blobs)  # already on disk: a 4-model set
        for sid in ("2", "3", "4"):
            self.idx.add_candidate("7", infos[sid])
        self.idx.mark(infos["3"].url, "duplicate")  # handled before: not a candidate any more
        d = self.downloader(blobs)
        d.fill(p, layout.performer_dir(self.root, p), "who")
        self.assertEqual((self.idx.status(infos["2"].url), self.idx.status(infos["4"].url)), ("done", None))
        self.assertEqual(len(curate.kept_items(layout.performer_dir(self.root, p), self.idx)), 2)

    def test_dry_fill_downloads_nothing(self):
        info, _ = self.info("pornpics", "5", [1])
        self.idx.add_candidate("7", info)
        p = {"id": "7", "name": "Jane Doe", "alias_list": []}
        d = self.downloader({}, dry=True)
        d.fill(p, layout.performer_dir(self.root, p), "who")
        self.assertEqual((d.report.counts["WOULD-GET"], self.idx.status(info.url)), (1, None))

    def test_evicted_set_is_not_downloaded_again_from_another_source(self):
        a, a_urls = self.info("pornpics", "1", [1, 2, 3])
        blobs = {u: jpeg(s) for u, s in zip(a_urls, [1, 2, 3])}
        _, pdir = self.run_set(a, blobs)
        [item] = curate.kept_items(pdir, self.idx)
        curate.evict(item, self.idx, Report(), dry=False, who="who")
        self.assertFalse(os.path.exists(item.key))
        self.assertEqual(self.idx.status(a.url), "evicted")
        b, b_urls = self.info("elitebabes", "9", [1, 2, 3], size=(1000, 1500))
        blobs.update({u: resized(jpeg(s), (1000, 1500)) for u, s in zip(b_urls, [1, 2, 3])})
        self.run_set(b, blobs)
        self.assertEqual(self.idx.status(b.url), "duplicate")
        self.idx.reset()  # a rebuild keeps the eviction
        self.assertEqual(self.idx.status(a.url), "evicted")
        self.assertEqual(self.idx.near(imaging.inspect(jpeg(1)), 0), {item.set_id})

    def test_curate_all(self):
        self.cfg.max_per_performer = 1
        solo, solo_urls = self.info("pornpics", "1", [1, 2, 3])
        solo.models = ["Jane Doe"]
        group, group_urls = self.info("pornpics", "2", [4, 5, 6])
        group.models = ["Jane Doe", "A", "B", "C"]
        blobs = {u: jpeg(s) for u, s in zip(solo_urls + group_urls, [1, 2, 3, 4, 5, 6])}
        self.run_set(solo, blobs)
        _, pdir = self.run_set(group, blobs)
        stash = type("S", (), {"galleries_under": lambda self, root: [], "studio_info": lambda self, n: None,
                               "clean": lambda self, paths: "42", "favorite_tags": lambda self: set()})()
        report = Report()
        curate.curate_all(stash, self.cfg, self.idx, report, dry=True)
        self.assertEqual((report.counts["EVICT"], len(os.listdir(pdir))), (1, 2))
        curate.curate_all(stash, self.cfg, self.idx, report, dry=False)
        self.assertEqual((os.listdir(pdir), report.counts["CLEAN"]), (["Acme - Set 1 [pornpics-1]"], 1))
        self.assertEqual(self.idx.status(group.url), "evicted")


if __name__ == "__main__":
    unittest.main()
