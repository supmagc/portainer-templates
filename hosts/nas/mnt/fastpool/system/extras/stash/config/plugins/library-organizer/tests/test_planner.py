import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lo import config, executor, fsutil, nfo, orphans, planner  # noqa: E402
from lo.report import Report  # noqa: E402

WHISPARR_NFO = b"""<episodedetails>
  <title>Old</title><season>2023</season><aired>2023-04-25</aired><studio>Network</studio>
  <uniqueid type="tvdb" default="true">111</uniqueid>
  <uniqueid type="whisparr">584</uniqueid>
  <watched>true</watched>
  <customthing>keep me</customthing>
</episodedetails>"""


class FakeWhisparr:
    enabled = True

    def __init__(self):
        self.ids = []

    def queue(self, i):
        self.ids.append(i)

    def describe(self, i):
        return f"id={i}"


class FakeStash:
    def __init__(self):
        self.moves = []

    def move_file(self, file_id, folder, basename):
        src = self.scene_path
        dst = os.path.join(folder, basename)
        os.makedirs(folder, exist_ok=True)
        os.rename(src, dst)
        self.moves.append((file_id, folder, basename))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        os.makedirs(os.path.join(self.base, "Named", "Old Studio"))
        os.makedirs(os.path.join(self.base, "Plain"))
        self.cfg = config.Config({"base_path": self.base, "libraries": [
            {"name": "Named", "path": "Named", "studio_folders": True, "whisparr": True},
            {"name": "Plain", "path": "Plain", "studio_mode": "fixed", "studio_folders": True}]})
        self.whisparr = FakeWhisparr()

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, *parts, data=b"x"):
        path = os.path.join(self.base, *parts)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def scene(self, path, **kw):
        s = {"id": "7", "title": "Great Title", "date": "2023-04-25", "details": "Plot", "studio": {"id": "1", "name": "Kink", "parent_studio": None},
             "performers": [{"id": "1", "name": "Bob"}, {"id": "2", "name": "Alice", "urls": ["http://x"]}], "tags": [{"name": "t1"}],
             "files": [{"id": "9", "path": path, "width": 1920, "height": 1080, "duration": 600, "video_codec": "h264", "audio_codec": "aac",
                        "frame_rate": 30.0, "bit_rate": 5000}],
             "paths": {"screenshot": "http://h/scene/7/screenshot?t=1"}, "stash_ids": [{"endpoint": "https://stashdb.org/graphql", "stash_id": "abc"}],
             "urls": ["http://u"], "created_at": "2023-01-01T10:00:00Z", "updated_at": "2023-01-02T10:00:00Z", "scene_markers": [], "galleries": []}
        s.update(kw)
        return s

    def kinds(self, actions):
        return [a.kind for a in actions]


class Planner(Base):
    def test_studio_change_moves_everything_and_converts_nfo(self):
        video = self.touch("Named", "Old Studio", "Old Name [WEBDL-720p].mp4")
        self.touch("Named", "Old Studio", "Old Name [WEBDL-720p].nfo", data=WHISPARR_NFO)
        self.touch("Named", "Old Studio", "Old Name [WEBDL-720p]-poster.png")
        self.touch("Named", "Old Studio", "Old Name [WEBDL-720p]-background.png")
        actions = planner.plan_scene(self.scene(video), self.cfg, self.whisparr)
        kinds = self.kinds(actions)
        self.assertEqual(kinds[:2], ["MKDIR", "MOVE"])
        self.assertIn("RENAME-BG", kinds)
        self.assertIn("WRITE-NFO", kinds)
        self.assertIn("RMDIR", kinds)
        self.assertNotIn("CREATE-POSTER", kinds)
        self.assertNotIn("CREATE-FANART", kinds)
        self.assertEqual(self.whisparr.ids, [584])
        move = next(a for a in actions if a.kind == "MOVE")
        self.assertEqual(move.data["basename"], "Kink - 2023-04-25 - Great Title - Alice, Bob [WEBDL-1080p].mp4")
        self.assertTrue(move.data["folder"].endswith("/Named/Kink"))

    def test_execute_then_replan_is_noop(self):
        video = self.touch("Named", "Old Studio", "Old Name.mp4")
        self.touch("Named", "Old Studio", "Old Name.nfo", data=WHISPARR_NFO)
        self.touch("Named", "Old Studio", "Old Name-background.png")
        scene = self.scene(video, paths={"screenshot": "http://h/x?default=true"})
        stash = FakeStash()
        stash.scene_path = video
        actions = planner.plan_scene(scene, self.cfg, self.whisparr)
        report = Report(None, dry=False)
        executor.execute(actions, "7", stash, self.cfg, self.cfg.libraries[0].root, video, report)
        self.assertNotIn("ERROR", report.counts)
        self.assertFalse(os.path.exists(os.path.join(self.base, "Named", "Old Studio")))
        new_dir = os.path.join(self.base, "Named", "Kink")
        names = sorted(os.listdir(new_dir))
        stem = "Kink - 2023-04-25 - Great Title - Alice, Bob [WEBDL-1080p]"
        self.assertEqual(names, [stem + "-fanart.png", stem + ".mp4", stem + ".nfo"])
        # second pass over the moved scene: nothing but missing art that has no screenshot source
        scene2 = self.scene(os.path.join(new_dir, stem + ".mp4"), paths={"screenshot": "http://h/scene/7/screenshot?default=true"})
        again = planner.plan_scene(scene2, self.cfg, FakeWhisparr())
        self.assertEqual([a.kind for a in again if a.kind not in planner.INFO_KINDS], [])
        root = ET.parse(os.path.join(new_dir, stem + ".nfo")).getroot()
        self.assertEqual(root.tag, "movie")
        self.assertEqual([u.get("type") for u in root.findall("uniqueid")], ["tvdb", "whisparr", "stash", "stashdb"])
        self.assertEqual(root.findtext("watched"), "true")
        self.assertEqual(root.findtext("customthing"), "keep me")
        self.assertEqual([a.findtext("name") for a in root.findall("actor")], ["Bob", "Alice"])
        self.assertIsNone(root.find("season"))

    def test_no_title_keeps_name_and_no_studio_keeps_folder(self):
        video = self.touch("Plain", "Bimbo Dream.mp4")
        actions = planner.plan_scene(self.scene(video, title="", studio=None), self.cfg, self.whisparr)
        kinds = self.kinds(actions)
        self.assertNotIn("MOVE", kinds)
        self.assertIn("WRITE-NFO", kinds)
        self.assertIn("CREATE-POSTER", kinds)
        self.assertIn("CREATE-FANART", kinds)

    def test_studio_less_scene_in_studio_library_stays_put(self):
        video = self.touch("Named", "Old Studio", "x.mp4")
        actions = planner.plan_scene(self.scene(video, studio=None), self.cfg, self.whisparr)
        move = next(a for a in actions if a.kind == "MOVE")
        self.assertTrue(move.data["folder"].endswith("/Named/Old Studio"))
        self.assertEqual(move.data["basename"], "2023-04-25 - Great Title - Alice, Bob [WEBDL-1080p].mp4")

    def test_case_insensitive_studio_folder_reused(self):
        os.makedirs(os.path.join(self.base, "Named", "kink"))
        video = self.touch("Named", "Old Studio", "x.mp4")
        actions = planner.plan_scene(self.scene(video), self.cfg, self.whisparr)
        move = next(a for a in actions if a.kind == "MOVE")
        self.assertTrue(move.data["folder"].endswith("/Named/kink"))
        self.assertNotIn("MKDIR", self.kinds(actions))

    def test_collision_gets_number(self):
        os.makedirs(os.path.join(self.base, "Named", "Kink"))
        self.touch("Named", "Kink", "Kink - 2023-04-25 - Great Title - Alice, Bob [WEBDL-1080p].mp4")
        video = self.touch("Named", "Old Studio", "x.mp4")
        actions = planner.plan_scene(self.scene(video), self.cfg, self.whisparr)
        move = next(a for a in actions if a.kind == "MOVE")
        self.assertTrue(move.data["basename"].endswith("[WEBDL-1080p] (2).mp4"))
        self.assertIn("WARN", self.kinds(actions))

    def test_outside_library_skipped_and_multifile_skipped(self):
        other = os.path.join(self.base, "Elsewhere", "a.mp4")
        self.assertEqual(self.kinds(planner.plan_scene(self.scene(other), self.cfg, self.whisparr)), ["SKIP"])
        video = self.touch("Plain", "a.mp4")
        two = self.scene(video)
        two["files"] = two["files"] * 2
        self.assertEqual(self.kinds(planner.plan_scene(two, self.cfg, self.whisparr)), ["SKIP"])

    def test_other_video_with_prefix_name_is_not_a_sidecar(self):
        self.touch("Plain", "a.mp4")
        self.touch("Plain", "a.part2.mp4")
        self.touch("Plain", "a(1).nfo-orig")
        self.assertEqual(fsutil.find_sidecars(os.path.join(self.base, "Plain"), "a"), [])

    def test_dry_run_changes_nothing(self):
        video = self.touch("Named", "Old Studio", "x.mp4")
        before = sorted(os.listdir(os.path.join(self.base, "Named", "Old Studio")))
        actions = planner.plan_scene(self.scene(video), self.cfg, self.whisparr)
        report = Report(None, dry=True)
        executor.execute(actions, "7", FakeStash(), self.cfg, self.cfg.libraries[0].root, video, report)
        self.assertEqual(before, sorted(os.listdir(os.path.join(self.base, "Named", "Old Studio"))))
        self.assertFalse(os.path.exists(os.path.join(self.base, "Named", "Kink")))


class Orphans(Base):
    def test_quarantine_only_matching_sidecars(self):
        d = os.path.join(self.base, "Named", "S")
        os.makedirs(d)
        self.touch("Named", "S", "keep.mp4")
        self.touch("Named", "S", "keep.nfo")
        self.touch("Named", "S", "gone.nfo")
        self.touch("Named", "S", "gone-poster.png")
        self.touch("Named", "S", "Thumbs.db")
        self.touch("Named", "S", "other.jpg")
        for n in os.listdir(d):
            os.utime(os.path.join(d, n), (1, 1))
        report = Report(None, dry=False)
        orphans.scan(self.cfg, self.cfg.libraries, {}, report)
        self.assertEqual(sorted(os.listdir(d)), ["Thumbs.db", "keep.mp4", "keep.nfo", "other.jpg"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.base, ".orphans", "Named", "S"))), ["gone-poster.png", "gone.nfo"])

    def test_fresh_files_are_kept(self):
        self.touch("Named", "Old Studio", "gone.nfo")
        report = Report(None, dry=False)
        orphans.scan(self.cfg, self.cfg.libraries, {}, report)
        self.assertEqual(report.counts, {})


class NfoMerge(unittest.TestCase):
    def test_unreadable_returns_none(self):
        self.assertIsNone(nfo.parse_existing(b"<movie>"))


if __name__ == "__main__":
    unittest.main()


class FakeStudios:
    def __init__(self, existing=(), unused=True):
        self.existing = {s["name"].casefold(): s for s in existing}
        self.unused = unused
        self.calls = []

    def find(self, name):
        return self.existing.get(name.casefold())

    def create(self, name):
        s = {"id": "99", "name": name, "parent_studio": None}
        self.existing[name.casefold()] = s
        self.calls.append(("create", name))
        return s

    def set_scene_studio(self, scene_id, studio_id):
        self.calls.append(("set", scene_id, studio_id))

    def is_unused(self, studio_id):
        return self.unused

    def destroy(self, studio_id):
        self.calls.append(("destroy", studio_id))


class ForcedStudio(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.base, "Unnamed", "HotKinkyJo"))
        self.cfg = config.Config({"base_path": self.base, "libraries": [
            {"name": "Unnamed", "path": "Unnamed", "studio_mode": "fixed", "studio_folders": True,
             "studios": {"HotKinkyJo": "Hot Kinky Jo"}},
            {"name": "Hyp", "path": "Hyp", "studio_mode": "fixed", "studio": "HypnoTube"}]})
        os.makedirs(os.path.join(self.base, "Hyp"))

    def test_folder_mode_resets_studio_creates_it_and_removes_old(self):
        video = self.touch("Unnamed", "HotKinkyJo", "clip.mp4")
        old = {"id": "5", "name": "Uploader123", "parent_studio": None}
        studios = FakeStudios()
        actions = planner.plan_scene(self.scene(video, studio=old, paths={"screenshot": "http://h/x?default=true"}), self.cfg, self.whisparr, studios)
        kinds = self.kinds(actions)
        self.assertEqual(kinds[:2], ["CREATE-STUDIO", "SET-STUDIO"])
        self.assertEqual(kinds[-1] if "WHISPARR-RESCAN" not in kinds else "DELETE-STUDIO", "DELETE-STUDIO")
        move = next(a for a in actions if a.kind == "MOVE")
        self.assertTrue(move.data["basename"].startswith("Hot Kinky Jo - 2023-04-25 - "))
        self.assertTrue(move.data["folder"].endswith("/Unnamed/HotKinkyJo"))
        report = Report(None, dry=False)
        stash = FakeStash()
        stash.scene_path = video
        executor.execute(actions, "7", stash, self.cfg, self.cfg.libraries[0].root, video, report, studios)
        self.assertNotIn("ERROR", report.counts)
        self.assertEqual(studios.calls, [("create", "Hot Kinky Jo"), ("set", "7", "99"), ("destroy", "5")])

    def test_studio_already_right_is_untouched(self):
        video = self.touch("Unnamed", "HotKinkyJo", "clip.mp4")
        right = {"id": "5", "name": "hot kinky jo", "parent_studio": None}
        actions = planner.plan_scene(self.scene(video, studio=right), self.cfg, self.whisparr, FakeStudios())
        self.assertFalse({"CREATE-STUDIO", "SET-STUDIO", "DELETE-STUDIO"} & set(self.kinds(actions)))

    def test_old_studio_in_use_is_kept(self):
        video = self.touch("Hyp", "clip.mp4")
        old = {"id": "5", "name": "Uploader", "parent_studio": None}
        studios = FakeStudios(existing=[{"id": "32", "name": "HypnoTube", "parent_studio": None}], unused=False)
        actions = planner.plan_scene(self.scene(video, studio=old, paths={"screenshot": "http://h/x?default=true"}), self.cfg, self.whisparr, studios)
        self.assertEqual(self.kinds(actions)[0], "SET-STUDIO")
        report = Report(None, dry=False)
        stash = FakeStash()
        stash.scene_path = video
        executor.execute(actions, "7", stash, self.cfg, self.cfg.libraries[1].root, video, report, studios)
        self.assertEqual(studios.calls, [("set", "7", "32")])

    def test_scene_without_studio_gets_one_and_no_delete(self):
        video = self.touch("Hyp", "clip.mp4")
        studios = FakeStudios(existing=[{"id": "32", "name": "HypnoTube", "parent_studio": None}])
        actions = planner.plan_scene(self.scene(video, studio=None), self.cfg, self.whisparr, studios)
        self.assertIn("SET-STUDIO", self.kinds(actions))
        self.assertNotIn("DELETE-STUDIO", self.kinds(actions))

    def test_unmapped_folder_uses_its_name_and_root_files_stay_free(self):
        os.makedirs(os.path.join(self.base, "Unnamed", "MilfHumiliation"))
        v1 = self.touch("Unnamed", "MilfHumiliation", "a.mp4")
        self.assertEqual(self.cfg.libraries[0].forced_studio(os.path.dirname(v1)), "MilfHumiliation")
        self.assertIsNone(self.cfg.libraries[0].forced_studio(os.path.join(self.base, "Unnamed")))

    def test_no_studio_folders_moves_in_forced_libraries(self):
        video = self.touch("Unnamed", "HotKinkyJo", "clip.mp4")
        actions = planner.plan_scene(self.scene(video), self.cfg, self.whisparr, FakeStudios())
        self.assertNotIn("MKDIR", self.kinds(actions))


class ConfigShapes(unittest.TestCase):
    def test_users_libraries_yml_shape_loads(self):
        cfg = config.Config({"base_path": "/data", "libraries": [
            {"name": "Named", "path": "Named", "studio_folders": True, "studio_mode": "follow"},
            {"name": "Tubes", "path": "Tubes", "studio_folders": True, "studio_mode": "fixed",
             "studios": [{"HypnoTube": "Hypno Tube"}]}]})
        named, tubes = cfg.libraries
        self.assertTrue(named.studio_folders)
        self.assertEqual(tubes.studio_mode, "fixed")
        self.assertEqual(tubes.forced_studio("/data/Tubes/HypnoTube"), "Hypno Tube")
        self.assertEqual(tubes.forced_studio("/data/Tubes/Other"), "Other")
        self.assertIsNone(tubes.forced_studio("/data/Tubes"))

    def test_old_mode_names_still_load(self):
        cfg = config.Config({"base_path": "/d", "libraries": [
            {"name": "a", "path": "a", "studio_mode": "free", "studio_folders": True},
            {"name": "b", "path": "b", "studio_mode": "folder", "studio_folders": True}]})
        self.assertEqual([l.studio_mode for l in cfg.libraries], ["follow", "fixed"])

    def test_invalid_combinations_are_rejected(self):
        bad = [
            {"studio_mode": "follow"},
            {"studio_mode": "follow", "studio_folders": True, "studio": "X"},
            {"studio_mode": "follow", "studio_folders": True, "studios": {"a": "b"}},
            {"studio_mode": "fixed", "studio_folders": True, "studio": "X"},
            {"studio_mode": "fixed", "studio_folders": False},
            {"studio_mode": "fixed", "studio_folders": False, "studio": "X", "studios": {"a": "b"}},
        ]
        for opts in bad:
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                config.Config({"base_path": "/d", "libraries": [{"name": "a", "path": "a", **opts}]})

    def test_bad_mode_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            config.Config({"base_path": "/d", "libraries": [{"name": "a", "path": "a", "studio_mode": "bogus"}]})
