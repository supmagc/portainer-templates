import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ps.sources import SetRef, babepedia, elitebabes, pornpics, slug_from_urls  # noqa: E402

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

BP_MODEL = """
<h1 id="babename">Jane Doe</h1>
<div id="mobile-scroll-gallery"><div id="profimg"><a href="/pics/Jane%20Doe.jpg"><img src="/pics/Jane%20Doe_thumb3.jpg"></a></div>
 <div id="profselect"><a href="/pics/Jane%20Doe2.jpg"><img src="/pics/Jane%20Doe2_thumb3.jpg"></a></div></div>
<div class="separate"><h2>Jane Doe Galleries</h2><div id="thumbs" class="thumbsblock">
 <div class="thumbshot"><a href="/gallery/Jane_Doe/380682"><img data-src="/galleries-thumbs/Acme-X/01.jpg"></a></div>
 <div class="thumbshot"><a href="/gallery/Jane_Doe/374179"><img></a></div>
 <div class="thumbshot"><a href="/gallery/Jane_Doe/374179"><img></a></div></div></div>
<h2>Jane Doe Photos <small>(Uploaded By Our Users)</small></h2><div class="gallery useruploads2">
 <a href="/user-uploads/Jane Doe.jpg"><img data-src="/user-uploads-thumbs/Jane%20Doe_thumb.jpg"></a>
 <a href="/user-uploads/Jane Doe10.jpg"><img></a></div>
<div class="thumbshot thumbshotsmall menuthumb"><a href="/babe/Other_Babe"><img src="/pics/Other%20Babe_thumb.jpg"></a></div>
"""

BP_GALLERY = """
<div class='gallerynavigation'><a href='/gallery/Jane_Doe/380365'>older</a></div><h1>Jane Doe and Mary Major by the pool</h1>
<h2 class='featuring'>featuring <a href='/babe/Jane_Doe' title='Jane Doe'>Jane Doe</a> &amp; <a href='/babe/Mary_Major' title='Mary Major'class='tooltiplink'>Mary Major</a></h2>
<a href='/galleries/Acme-JaneDoeAndMaryMajor,ByThePool/01.jpg' class='img' rel='gallery'><img src='/galleries-thumbs/Acme-JaneDoeAndMaryMajor,ByThePool/01.jpg'></a>
<a href='/galleries/Acme-JaneDoeAndMaryMajor,ByThePool/02.jpg' class='img' rel='gallery'><img></a>
<div id='galleryinfo'><h2>Gallery information</h2><p>Model: <a href='/babe/Jane_Doe'>Jane Doe</a></p>
<p>Source: Fitting Room (<a href='/source/fittingroom/recent'>More Fitting Room galleries</a>)</p>
<p id='gallerytags'><span class='gallery-tags-label'>Tags:</span><span class='gallery-tag-groups'>
 <span class='gallery-tag-group gallery-tag-group-scene'><span class='gallery-tag-type'>Scene</span><span class='gallery-tag-items'><span class='gallery-tag'>Lingerie</span><span class='gallery-tag'>Pool</span></span></span>
 <span class='gallery-tag-group gallery-tag-group-sponsor'><span class='gallery-tag-type'>Source</span><span class='gallery-tag-items'><span class='gallery-tag'>Fittingroom</span></span></span>
 <span class='gallery-tag-group gallery-tag-group-model'><span class='gallery-tag-type'>Model</span><span class='gallery-tag-items'><span class='gallery-tag'>Jane Doe</span></span></span>
</span></p><p>Published on: 29 August 2021</p><p>Views: 39,507</p></div>
"""


class Babepedia(unittest.TestCase):
    def test_listing(self):
        refs = babepedia.parse_listing(BP_MODEL)
        self.assertEqual([(r.source_id, r.url) for r in refs], [("380682", "https://www.babepedia.com/gallery/Jane_Doe/380682"),
                                                                ("374179", "https://www.babepedia.com/gallery/Jane_Doe/374179")])

    def test_parse_set(self):
        info = babepedia.parse_set(SetRef("babepedia", "https://www.babepedia.com/gallery/Jane_Doe/380682", "380682"), BP_GALLERY)
        self.assertEqual(info.title, "Jane Doe and Mary Major by the pool")
        self.assertEqual(info.models, ["Jane Doe", "Mary Major"])
        self.assertEqual((info.studio, info.date, info.posted), ("Fitting Room", None, "2021-08-29"))
        self.assertEqual(info.tags, ["Lingerie", "Pool"])
        self.assertEqual([i.url for i in info.images],
                         ["https://www.babepedia.com/galleries/Acme-JaneDoeAndMaryMajor,ByThePool/01.jpg",
                          "https://www.babepedia.com/galleries/Acme-JaneDoeAndMaryMajor,ByThePool/02.jpg"])

    def test_promo(self):
        self.assertEqual([(stem, im.url) for stem, im in babepedia.parse_promo(BP_MODEL)], [
            ("babepedia-pics-jane-doe", "https://www.babepedia.com/pics/Jane%20Doe.jpg"),
            ("babepedia-pics-jane-doe2", "https://www.babepedia.com/pics/Jane%20Doe2.jpg"),
            ("babepedia-upload-jane-doe", "https://www.babepedia.com/user-uploads/Jane%20Doe.jpg"),
            ("babepedia-upload-jane-doe10", "https://www.babepedia.com/user-uploads/Jane%20Doe10.jpg")])

    def test_model_slug(self):
        self.assertEqual(babepedia.model_slug({"name": "Jane Doe", "urls": []}), "Jane_Doe")
        self.assertEqual(babepedia.model_slug({"name": "J", "urls": ["https://www.babepedia.com/babe/Jane_Doe_2"]}), "Jane_Doe_2")

    def test_unknown_model_redirects_to_search(self):
        babepedia._page.clear()
        r = type("R", (), {"url": "https://www.babepedia.com/search/Nobody", "text": "<h1>Results for 'Nobody'</h1>"})()
        fetcher = type("F", (), {"get": lambda self, url, **kw: r})()
        self.assertEqual(list(babepedia.list_sets({"name": "Nobody", "urls": []}, fetcher)), [])
        self.assertEqual(list(babepedia.promo_images({"name": "Nobody", "urls": []}, fetcher)), [])


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
