import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lo import naming  # noqa: E402


class Sanitize(unittest.TestCase):
    def test_forbidden_chars(self):
        self.assertEqual(naming.sanitize('A: B/C|D "E" ?*<>'), "A B-C-D E")

    def test_trailing_dots_spaces_and_whitespace(self):
        self.assertEqual(naming.sanitize("  Foo   bar. . "), "Foo bar")

    def test_control_chars_and_surrogates(self):
        self.assertEqual(naming.sanitize("a\x00b\ud800c"), "abc")

    def test_reserved_names(self):
        self.assertEqual(naming.folder_name("CON"), "CON_")
        self.assertEqual(naming.folder_name("nul.txt"), "nul.txt_")
        self.assertEqual(naming.folder_name("Console"), "Console")

    def test_folder_name_empty(self):
        self.assertIsNone(naming.folder_name("..."))


class Quality(unittest.TestCase):
    def test_labels(self):
        cases = {(1920, 1080): "1080p", (1920, 800): "1080p", (1280, 720): "720p", (960, 540): "540p",
                 (854, 480): "480p", (720, 576): "576p", (640, 360): "360p", (3840, 2160): "2160p",
                 (1080, 1920): "1080p", (320, 180): "180p"}
        for (w, h), want in cases.items():
            self.assertEqual(naming.resolution_label(w, h), want, (w, h))

    def test_unknown(self):
        self.assertIsNone(naming.resolution_label(0, 0))
        self.assertEqual(naming.quality_token("x", None, None, "WEBDL"), "WEBDL-unknown")

    def test_keeps_existing_source(self):
        self.assertEqual(naming.quality_token("A - B [HDTV-720p]", 1280, 720, "WEBDL"), "HDTV-720p")
        self.assertEqual(naming.quality_token("A - B [WEBDL-540]", 960, 540, "WEBDL"), "WEBDL-540p")


class Compose(unittest.TestCase):
    def fields(self, **kw):
        base = {"studio": "Kink", "date": "2023-04-25", "title": "Title", "performers": ["Zed", "alice"]}
        base.update(kw)
        return base

    def build(self, fields, quality="WEBDL-480p", d="/data/Named/Kink", limit=200):
        return naming.build_basename(fields, quality, d, limit)

    def test_full(self):
        self.assertEqual(self.build(self.fields()), ("Kink - 2023-04-25 - Title - alice, Zed [WEBDL-480p]", False))

    def test_missing_segments_leave_no_separators(self):
        self.assertEqual(self.build(self.fields(studio=None))[0], "2023-04-25 - Title - alice, Zed [WEBDL-480p]")
        self.assertEqual(self.build(self.fields(date=None))[0], "Kink - Title - alice, Zed [WEBDL-480p]")
        self.assertEqual(self.build(self.fields(studio=None, date=None, performers=[]))[0], "Title [WEBDL-480p]")
        self.assertEqual(self.build(self.fields(performers=[]))[0], "Kink - 2023-04-25 - Title [WEBDL-480p]")

    def test_byte_limit_shortens_performers_first(self):
        perfs = [f"Performer Number {i:02d}" for i in range(30)]
        name, truncated = self.build(self.fields(performers=perfs, title="T" * 60))
        self.assertTrue(truncated)
        self.assertLessEqual(naming.utf8_len(name) + naming.SIDECAR_RESERVE, 255)
        self.assertIn("Title".replace("Title", "T" * 60), name)
        self.assertRegex(name, r" \+\d+ \[")

    def test_path_limit_shortens_title(self):
        name, truncated = self.build(self.fields(title="Long title " * 20, performers=["A"]), limit=120)
        self.assertTrue(truncated)
        self.assertLessEqual(len("/data/Named/Kink") + 1 + len(name) + naming.SIDECAR_RESERVE, 120)
        self.assertTrue(name.endswith("[WEBDL-480p]"))

    def test_multibyte_counts_bytes(self):
        name, truncated = self.build(self.fields(title="é" * 140, performers=[]))
        self.assertTrue(truncated)
        self.assertLessEqual(naming.utf8_len(name) + naming.SIDECAR_RESERVE, 255)

    def test_impossible(self):
        self.assertEqual(self.build(self.fields(), d="/" + "d" * 190, limit=200), (None, True))

    def test_suffix_number(self):
        self.assertEqual(naming.with_suffix_number("A", 1), "A")
        self.assertEqual(naming.with_suffix_number("A", 3), "A (3)")


if __name__ == "__main__":
    unittest.main()


class PerformerOrder(unittest.TestCase):
    def test_existing_order_kept_when_same_set(self):
        f = {"studio": "K", "date": "2020-01-01", "title": "T", "performers": ["Anton", "Eve"]}
        name, _ = naming.build_basename(f, "WEBDL-720p", "/d", 200, old_base="K - 2020-01-01 - T - Eve, Anton [WEBDL-720p]")
        self.assertEqual(name, "K - 2020-01-01 - T - Eve, Anton [WEBDL-720p]")

    def test_alphabetical_when_set_changed(self):
        f = {"studio": "K", "date": "2020-01-01", "title": "T", "performers": ["Anton", "Eve", "Cy"]}
        name, _ = naming.build_basename(f, "WEBDL-720p", "/d", 200, old_base="K - 2020-01-01 - T - Eve, Anton [WEBDL-720p]")
        self.assertEqual(name, "K - 2020-01-01 - T - Anton, Cy, Eve [WEBDL-720p]")
