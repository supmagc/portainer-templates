import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ss import apply, layout  # noqa: E402
from ss.config import Config  # noqa: E402
from ss.download import Downloader, redact, sidecar_for  # noqa: E402
from ss.fetch import Blocked  # noqa: E402
from ss.report import Report  # noqa: E402
from ss.sources import Archive, ImageRef, SetInfo, kink, match, vixen  # noqa: E402

# shapes as served on 2026-10-30
KINK_IMAGES = json.dumps([
    {"fullPath": "https://cdnp.kink.com/imagedb/35455/i/h/830/12.jpg", "thumbFullPath": "x", "sequence": 3, "isSfw": False},
    {"fullPath": "https://cdnp.kink.com/imagedb/35455/i/h/830/7.jpg", "thumbFullPath": "x", "sequence": 0, "isSfw": True},
    {"fullPath": "https://cdnp.kink.com/imagedb/35455/i/h/830/8.jpg", "thumbFullPath": "x", "sequence": 1, "isSfw": False},
])
VMG_BODY = {"data": {"findOneVideo": {
    "videoId": "101342", "title": "Anger", "picturesInSet": 89, "categories": [{"name": "Anal"}],
    "carousel": [
        {"main": [{"src": "https://cdn.deeper.com/scene/TOURPHOTO/101342/1/01.jpg?Policy=a", "width": 1200, "height": 800}]},
        {"main": [{"src": "https://cdn.deeper.com/scene/TOURPHOTO/101342/1/02s.jpg?Policy=b", "width": 600, "height": 400},
                  {"src": "https://cdn.deeper.com/scene/TOURPHOTO/101342/1/02.jpg?Policy=b", "width": 800, "height": 1200}]},
        {"main": []},
    ]}}}
SCENE = {"id": "94", "title": "Best of Sas: the Ranchers Daughter", "date": "2015-07-06", "details": "d", "director": "Marty",
         "studio": {"name": "Sex and Submission"}, "performers": [{"name": "James Deen"}, {"name": "Rose Red"}],
         "urls": ["https://kink.com/shoot/35455"], "galleries": []}


class Sources(unittest.TestCase):
    def test_match(self):
        self.assertEqual(kink.match("https://kink.com/shoot/35455"), "https://www.kink.com/shoot/35455")
        self.assertEqual(kink.match("https://www.KinkMen.com/shoot/12?x=1"), "https://www.kinkmen.com/shoot/12")
        self.assertIsNone(kink.match("https://www.kink.com/model/8935/James-Deen"))
        self.assertEqual(vixen.match("https://blacked.com/videos/anything-for-daddy"),
                         "https://www.blacked.com/videos/anything-for-daddy")
        self.assertEqual(vixen.match("https://www.blackedraw.com/videos/double-dose/"), "https://www.blackedraw.com/videos/double-dose")
        self.assertIsNone(vixen.match("https://www.vixenplus.com/videos/x"))
        hit = match(["https://www.iafd.com/title.rme/id=1", "https://tushy.com/videos/awards-night"], [kink, vixen])
        self.assertEqual(hit, (vixen, "https://www.tushy.com/videos/awards-night"))
        self.assertIsNone(match(["https://www.data18.com/scenes/1"], [kink, vixen]))
        self.assertEqual(kink.key("https://www.kink.com/shoot/35455"), "kink-35455")

    def test_kink_parse(self):
        url = "https://www.kink.com/shoot/35455"
        info = kink.parse(url, {"title": "BEST of SAS: The Rancher's Daughter | Kink.com", "href": url, "images": KINK_IMAGES})
        self.assertEqual([i.url.rsplit("/", 1)[1] for i in info.images], ["7.jpg", "8.jpg", "12.jpg"])
        self.assertEqual((info.key, info.title), ("kink-35455", "BEST of SAS: The Rancher's Daughter"))
        self.assertEqual(kink.parse(url, {"title": "x", "href": url, "images": None}).images, [])
        self.assertIsNone(kink.parse(url, {"title": "Channels", "href": "https://www.kink.com/channels", "images": None}))
        with self.assertRaises(Blocked):
            kink.parse(url, {"title": "Just a moment...", "href": url, "images": None})

    def test_kink_member_zips(self):
        url = "https://www.kink.com/shoot/29616"
        z = "https://cdn.kink.com/publicdisgrace/members/29616/pictures/hires/%s.zip?nva=1&token=t"
        zips = [[z % "Scene 3", "publicdisgrace_29616_hi_res_pictures_Scene 3.zip"],
                [z % "1 Glamour", "publicdisgrace_29616_hi_res_pictures_1 Glamour.zip"],
                [z % "Scene 3", "publicdisgrace_29616_hi_res_pictures_Scene 3.zip"]]  # rendered twice
        page = {"title": "x", "href": url, "images": KINK_IMAGES, "loggedIn": True, "zips": zips}
        info = kink.parse(url, page, member=True)
        self.assertEqual([(a.name.rsplit("_", 1)[1], a.grabs) for a in info.archives], [("1 Glamour.zip", False), ("Scene 3.zip", True)])
        self.assertTrue(info.logged_in)
        anon = kink.parse(url, page)  # no session offered: zips ignored, logged_in unknown
        self.assertEqual((anon.archives, anon.logged_in), ([], None))
        self.assertFalse(kink.parse(url, dict(page, loggedIn=False, zips=[]), member=True).logged_in)

    def test_vixen_parse(self):
        url = "https://www.deeper.com/videos/anger"
        info = vixen.parse(url, "deeper", VMG_BODY)
        self.assertEqual(info.key, "deeper-101342")
        self.assertEqual([(i.url.split("?")[0].rsplit("/", 1)[1], i.width) for i in info.images], [("01.jpg", 1200), ("02.jpg", 800)])
        self.assertEqual((info.tags, info.extra), (["Anal"], {"pictures_in_set": 89}))
        self.assertIsNone(vixen.parse(url, "deeper", {"data": {"findOneVideo": None}}))
        with self.assertRaises(ValueError):
            vixen.parse(url, "deeper", {"errors": [{"message": "boom"}]})


class Layout(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_set_folder_and_existing(self):
        info = SetInfo("kink", "kink", "35455", "u")
        folder = layout.set_folder(self.root, SCENE, info)
        self.assertEqual(os.path.relpath(folder, self.root).replace(os.sep, "/"),
                         "Sex and Submission/Sex and Submission - 2015-07-06 - Best of Sas the Ranchers Daughter [kink-35455]")
        os.makedirs(os.path.join(self.root, "Hog Tied", "renamed by hand [kink-34510]"))
        os.makedirs(os.path.join(self.root, "Hog Tied", "no key here"))
        self.assertEqual(list(layout.existing_sets(self.root)), ["kink-34510"])
        bare = layout.set_folder(self.root, {"id": "1", "title": None, "studio": None}, SetInfo("vixen", "tushy", "9", "u", title="T"))
        self.assertTrue(bare.endswith("Unknown Studio/Unknown Studio - T [tushy-9]"))


class Settings(unittest.TestCase):
    def test_defaults(self):
        cfg = Config.from_settings({})
        self.assertEqual((cfg.dry_run, cfg.root, cfg.min_rating100, cfg.include_favorite_performers, cfg.sources),
                         (True, "/data/SceneSets", 0, True, ["kink", "vixen"]))
        self.assertEqual(Config.from_settings({"min_rating": "3.5", "sources": "Vixen"}).min_rating100, 70)
        with self.assertRaises(SystemExit):
            Config.from_settings({"sources": "kink, brazzers"})

    def test_kink_cookie_forms(self):
        for raw in ('"kinky.sess=abc; Path=/; Expires=Sun, 29 Nov 2026 15:54:52 GMT; HttpOnly"', "kinky.sess=abc", "abc"):
            self.assertEqual(Config.from_settings({"kink_cookie": raw}).kink_session, ("kinky.sess", "abc"))
        self.assertIsNone(Config.from_settings({"kink_cookie": "  "}).kink_session)


class FakeStash:
    def __init__(self, scenes=("94",), performers=("James Deen",), studios=("Sex and Submission",)):
        self.scenes, self.performers, self.studios = set(scenes), list(performers), list(studios)
        self.tags = {}
        self.updates = []
        self.scanned = []

    def find_scene(self, sid):
        return {"id": sid} if sid in self.scenes else None

    def lookup(self, kind, name):
        pool = {"performer": self.performers, "studio": self.studios}.get(kind)
        if pool is None:
            return self.tags.get(name)
        return str(100 + pool.index(name)) if name in pool else None

    def ensure_tag(self, name):
        return self.tags.setdefault(name, "900")

    def update_gallery(self, values):
        self.updates.append(values)

    def keepalive(self):
        pass

    def scan(self, paths):
        self.scanned.append(paths)
        return "1"


class Apply(unittest.TestCase):
    def gallery(self, **kw):
        return {"id": "7", "title": "", "date": None, "details": "", "urls": [], "studio": None, "performers": [],
                "scenes": [], "tags": [], "folder": {"path": "/x"}, **kw}

    def test_links_scene_and_fills_empty_fields(self):
        sc = sidecar_for(SCENE, SetInfo("kink", "kink", "35455", "https://www.kink.com/shoot/35455"))
        values, problems = apply.plan(FakeStash(), self.gallery(), sc)
        self.assertEqual(problems, ["performer 'Rose Red' not in Stash"])
        self.assertEqual(values["scene_ids"], ["94"])
        self.assertEqual(values["performer_ids"], ["100"])
        self.assertEqual((values["studio_id"], values["title"], values["date"]), ("100", SCENE["title"], "2015-07-06"))
        self.assertEqual(values["urls"], ["https://www.kink.com/shoot/35455"])
        self.assertNotIn("tag_ids", values)  # dry: the review tag isn't created

    def test_manual_edits_win(self):
        sc = {"kind": "scene", "scene_id": "94", "title": "set.json title", "studio": "Sex and Submission", "urls": ["u"]}
        g = self.gallery(title="mine", studio={"id": "5"}, scenes=[{"id": "94"}], urls=["u"])
        self.assertEqual(apply.plan(FakeStash(), g, sc), (None, []))

    def test_missing_scene_tags_for_review(self):
        stash = FakeStash(scenes=())
        values, problems = apply.plan(stash, self.gallery(title="t"), {"kind": "scene", "scene_id": "1"}, dry=False)
        self.assertEqual(problems, ["scene 1 not in Stash"])
        self.assertEqual(values["tag_ids"], ["900"])


class FakeResponse:
    def __init__(self, data):
        self.content = data

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeFetcher:
    def __init__(self, data):
        self.data = data
        self.got = []

    def get(self, url, referer=None, stream=False):
        self.got.append(url)
        return FakeResponse(self.data)


class FakeIndex:
    def __init__(self):
        self.st = {}

    def status(self, url):
        return self.st.get(url)

    def mark(self, url, status, scene_id=None, folder=None):
        self.st[url] = status


class FakeSource:
    NAME = "kink"

    def __init__(self, info):
        self.info = info
        self.fetched = 0

    def match(self, url):
        return kink.match(url)

    def key(self, url):
        return kink.key(url)

    def fetch(self, url, ctx):
        self.fetched += 1
        return self.info


def _image(w, h, fmt):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 30, 60)).save(buf, fmt)
    return buf.getvalue()


def _jpeg(w, h):
    return _image(w, h, "JPEG")


def _png(w, h):
    return _image(w, h, "PNG")


def _zip(files):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class Download(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.url = "https://www.kink.com/shoot/35455"
        self.info = SetInfo("kink", "kink", "35455", self.url, [ImageRef(f"https://cdnp.kink.com/imagedb/35455/i/h/830/{n}.jpg?sig")
                                                                 for n in (7, 8)])

    def tearDown(self):
        shutil.rmtree(self.root)

    def run_one(self, dry=False, scene=SCENE, data=None):
        cfg = Config(dry_run=dry, root=self.root)
        stash, idx, src = FakeStash(), FakeIndex(), FakeSource(self.info)
        d = Downloader(stash, cfg, FakeFetcher(data or _jpeg(1400, 934)), None, idx, Report(dry=dry), dry)
        d.sources = [src]
        d.run([scene])
        return stash, idx, src, d

    def test_download_writes_set_and_queues_scan(self):
        stash, idx, src, d = self.run_one()
        folder = layout.existing_sets(self.root)["kink-35455"]
        self.assertEqual(sorted(os.listdir(folder)), ["001.jpg", "002.jpg", "set.json"])
        sc = layout.read_sidecar(folder)
        self.assertEqual((sc["kind"], sc["scene_id"], sc["images"]["001"]), ("scene", "94", "https://cdnp.kink.com/imagedb/35455/i/h/830/7.jpg"))
        self.assertEqual(idx.st[self.url], "preview")  # no member session: upgradable later
        self.assertEqual(stash.scanned, [[os.path.dirname(folder)]])

    def test_member_zips_replace_a_preview(self):
        self.run_one()
        folder = layout.existing_sets(self.root)["kink-35455"]
        self.info.logged_in = True
        self.info.archives = [Archive("https://cdn.kink.com/x/35455_images.zip?nva=1&token=secret", "35455_images.zip"),
                              Archive("https://cdn.kink.com/x/Scene 1.zip?token=s", "x_pictures_Scene 1.zip", grabs=True)]
        zipped = _zip({"10.png": _png(3840, 2160), "2.jpg": _jpeg(1400, 934), "performer-0.jpg": _jpeg(554, 830),
                       "__MACOSX/._2.jpg": b"junk"})
        cfg = Config(dry_run=False, root=self.root, kink_session=("kinky.sess", "v"))
        idx = FakeIndex()
        idx.st[self.url] = "preview"
        report = Report(dry=False)
        d = Downloader(FakeStash(), cfg, FakeFetcher(zipped), None, idx, report, False)
        d.sources = [FakeSource(self.info)]
        d.run([dict(SCENE, galleries=[{"id": "1"}])])  # its own preview gallery doesn't block the upgrade
        # several zips: all of them, the photo set first, natural order inside each
        self.assertEqual(sorted(os.listdir(folder)), ["001.jpg", "002.png", "003.jpg", "004.png", "set.json"])
        sc = layout.read_sidecar(folder)
        self.assertEqual((sc["member"], sc["archives"], sc["images"]["002"], sc["images"]["003"]),
                         (True, ["35455_images.zip", "x_pictures_Scene 1.zip"], "35455_images.zip/10.png", "x_pictures_Scene 1.zip/2.jpg"))
        self.assertEqual(idx.st[self.url], "done")
        self.assertNotIn("secret", " ".join(sc["images"].values()))

    def test_lone_grabs_zip_falls_back_to_public(self):
        self.info.logged_in = True
        self.info.archives = [Archive("https://cdn.kink.com/x/Scene 1.zip?token=s", "x_pictures_Scene 1.zip", grabs=True)]
        cfg = Config(dry_run=False, root=self.root, kink_session=("kinky.sess", "v"))
        idx = FakeIndex()
        d = Downloader(FakeStash(), cfg, FakeFetcher(_jpeg(1400, 934)), None, idx, Report(dry=False), False)
        d.sources = [FakeSource(self.info)]
        d.run([SCENE])
        sc = layout.read_sidecar(layout.existing_sets(self.root)["kink-35455"])
        self.assertEqual((sc["member"], len(sc["images"]), idx.st[self.url]), (False, 2, "done"))

    def test_rejected_session_falls_back_to_public(self):
        self.info.logged_in = False
        cfg = Config(dry_run=False, root=self.root, kink_session=("kinky.sess", "old"))
        idx, report = FakeIndex(), Report(dry=False)
        d = Downloader(FakeStash(), cfg, FakeFetcher(_jpeg(1400, 934)), None, idx, report, False)
        d.sources = [FakeSource(self.info)]
        d.run([SCENE])
        self.assertEqual((idx.st[self.url], report.counts["NOT-LOGGED-IN"], d.ctx.kink_session), ("preview", 1, None))

    def test_redact(self):
        self.assertEqual(redact("GET https://cdn.kink.com/a.zip?nva=1&token=abc failed"), "GET https://cdn.kink.com/a.zip?… failed")

    def test_dry_run_writes_nothing(self):
        _stash, idx, _src, d = self.run_one(dry=True)
        self.assertEqual(os.listdir(self.root), [])
        self.assertEqual((idx.st, d.report.counts["WOULD-GET"]), ({}, 1))

    def test_hand_made_folder_is_left_alone_without_fetching(self):
        hand = os.path.join(self.root, "Sex and Submission", "whatever [kink-35455]")
        os.makedirs(hand)
        layout.write_sidecar(hand, {"kind": "scene", "scene_id": "94"})
        _stash, idx, src, _d = self.run_one()
        self.assertEqual((idx.st[self.url], src.fetched, sorted(os.listdir(hand))), ("exists", 0, ["set.json"]))

    def test_small_photos_leave_no_folder(self):
        _stash, idx, _src, _d = self.run_one(data=_jpeg(400, 300))
        self.assertEqual(idx.st[self.url], "small")
        self.assertEqual(os.listdir(self.root), [])

    def test_scene_with_gallery_is_skipped(self):
        _stash, idx, src, _d = self.run_one(scene=dict(SCENE, galleries=[{"id": "1"}]))
        self.assertEqual((idx.st, src.fetched), ({}, 0))


if __name__ == "__main__":
    unittest.main()
