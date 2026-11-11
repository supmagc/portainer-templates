import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lo import executor  # noqa: E402


class Poster(unittest.TestCase):
    def test_landscape_crops_width(self):
        self.assertEqual(executor.poster_filter(1920, 1080, 0.5), "crop=720:1080:600:0")

    def test_crop_x_offsets_landscape(self):
        self.assertEqual(executor.poster_filter(1920, 1080, 0.0), "crop=720:1080:0:0")

    def test_portrait_crops_height_centred(self):
        self.assertEqual(executor.poster_filter(1080, 1920, 0.5), "crop=1080:1620:0:150")

    def test_exact_two_by_three_is_full_frame(self):
        self.assertEqual(executor.poster_filter(720, 1080, 0.5), "crop=720:1080:0:0")


class Fanart(unittest.TestCase):
    def test_landscape_used_as_is(self):
        self.assertIsNone(executor.fanart_filter(1920, 1080))
        self.assertIsNone(executor.fanart_filter(640, 480))

    def test_portrait_gets_16x9_fill_capped_at_1080(self):
        vf = executor.fanart_filter(1080, 1920)
        self.assertIn("scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080", vf)
        self.assertIn("[fg]scale=-2:1080", vf)

    def test_small_portrait_keeps_its_height(self):
        self.assertIn("crop=1518:854", executor.fanart_filter(480, 854))

    def test_square_gets_fill(self):
        self.assertIsNotNone(executor.fanart_filter(720, 720))


if __name__ == "__main__":
    unittest.main()
