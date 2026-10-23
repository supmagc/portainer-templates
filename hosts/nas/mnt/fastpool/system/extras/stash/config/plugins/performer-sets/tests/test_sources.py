import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ps.sources import SetRef, elitebabes, pornpics, slug_from_urls  # noqa: E402

PP_GALLERY = """
<h1>Blonde Jane Doe poses by the pool</h1>
<div class="gallery-info to-gall-info">
 <div class='gallery-info__item'><span class='gallery-info__title'>Channel:&nbsp;</span><a href='/channels/acme/' title="Acme Pics">Acme</a></div>
 <div class="gallery-info__item"><span class="gallery-info__title">Models:</span>
  <div class="gallery-info__content"><a href="/pornstars/jane-doe/"><span>Jane Doe</span></a>
   <a href="/pornstars/john-roe/"><span>John Roe</span></a><span class="suggest-2-button">Suggest</span></div></div>
 <div class="gallery-info__item tags"><span class="gallery-info__title">Categories:</span>
  <div class="gallery-info__content"><a href='/blonde/'><span>Blonde</span></a><a href='/pool/'><span>Pool</span></a></div></div>
</div>
<ul id="tiles">
 <li class='thumbwook'><a class='rel-link' href='https://cdni.pornpics.com/1280/1/2/123/123_001_aa.jpg' data-size="1000x1500"><img></a></li>
 <li class='thumbwook'><a class='rel-link' href='https://cdni.pornpics.com/1280/1/2/123/123_002_bb.jpg'><img></a></li>
 <li class='thumbwook'><a class='rel-link' href='https://www.pornpics.com/galleries/other-999/'><img></a></li>
</ul>
"""

EB_LISTING = """
<li style="--ratio: 0.6"><figure><span class="img-label"><span>2018-12-24</span></span>
 <a href="https://www.elitebabes.com/jane-doe-in-call-girl-5-19431/" title="x"><img></a></figure></li>
<li><figure><a href="https://www.elitebabes.com/model/someone/"><img></a></figure></li>
<li><figure><a href="https://www.elitebabes.com/undated-set-20000/"><img></a></figure></li>
"""

EB_GALLERY = """
<h1>Jane Doe &amp; Mary Major in Penthouse set Call Girl 5</h1>
<p><a class="overlay-b" href="https://www.elitebabes.com/penthouse/">Penthouse</a><a class="overlay-b" href="https://www.elitebabes.com/model/jane-doe/">Jane Doe</a>
<a class="overlay-b" href="https://www.elitebabes.com/model/mary-major/">Mary Major</a>
<a href="https://www.elitebabes.com/tag/babe/" class="visible-tag">Babe</a></p>
<ul class="list-gallery static css">
 <li><a href="https://cdn.elitebabes.com/content/1/0002-01.jpg" data-width="1066" data-height="1600"><img></a></li>
 <li><a href="https://cdn.elitebabes.com/content/1/0002-02.jpg"><img></a></li>
</ul>
<ul class="list-gallery"><li><a href="https://www.elitebabes.com/related-set-55/"><img></a></li></ul>
"""


class PornPics(unittest.TestCase):
    def test_parse_set(self):
        info = pornpics.parse_set(SetRef("pornpics", "https://www.pornpics.com/galleries/x-123/", "123"), PP_GALLERY)
        self.assertEqual(info.title, "Blonde Jane Doe poses by the pool")
        self.assertEqual(info.studio, "Acme")
        self.assertEqual(info.models, ["Jane Doe", "John Roe"])
        self.assertEqual(info.tags, ["Blonde", "Pool"])
        self.assertEqual([(i.url[-10:], i.width, i.height) for i in info.images],
                         [("001_aa.jpg", 1000, 1500), ("002_bb.jpg", 0, 0)])

    def test_query_prefers_url_slug(self):
        p = {"name": "Jane", "urls": ["https://www.pornpics.com/pornstars/jane-doe/"]}
        self.assertEqual(pornpics.query(p), "jane doe")
        self.assertEqual(pornpics.query({"name": "Jane Doe", "urls": []}), "Jane Doe")


class EliteBabes(unittest.TestCase):
    def test_listing(self):
        refs = elitebabes.parse_listing(EB_LISTING)
        self.assertEqual([(r.source_id, r.posted) for r in refs], [("19431", "2018-12-24"), ("20000", None)])

    def test_parse_set(self):
        ref = SetRef("elitebabes", "https://www.elitebabes.com/jane-doe-in-call-girl-5-19431/", "19431", "2018-12-24")
        info = elitebabes.parse_set(ref, EB_GALLERY)
        self.assertEqual(info.title, "Call Girl 5")
        self.assertEqual(info.studio, "Penthouse")
        self.assertEqual(info.models, ["Jane Doe", "Mary Major"])
        self.assertEqual(info.tags, ["Babe"])
        self.assertEqual((info.date, info.posted), (None, "2018-12-24"))
        self.assertEqual([(i.width, i.height) for i in info.images], [(1066, 1600), (0, 0)])

    def test_model_slug(self):
        self.assertEqual(elitebabes.model_slug({"name": "Ella", "urls": ["https://www.elitebabes.com/model/ella-7/"]}), "ella-7")
        self.assertEqual(elitebabes.model_slug({"name": "Zoë D'Arc", "urls": []}), "zo-d-arc")


class Slug(unittest.TestCase):
    def test_no_match(self):
        self.assertIsNone(slug_from_urls({"urls": ["https://example.com/x"]}, r"pornpics\.com/pornstars/([^/]+)"))


if __name__ == "__main__":
    unittest.main()
