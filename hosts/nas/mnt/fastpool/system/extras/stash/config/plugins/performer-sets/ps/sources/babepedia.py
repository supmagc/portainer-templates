"""Babepedia: the model page lists every gallery (newest first) and holds the profile pics and user uploads (promo).
Gallery page → models, Source = studio, tags, images; "Published on" is Babepedia's own date, not the release."""
import re
from datetime import datetime
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup

from . import ImageRef, SetInfo, SetRef, slug_from_urls, slugify, text

NAME = "babepedia"
BASE = "https://www.babepedia.com"
_GALLERY_RE = re.compile(r"^/gallery/[^/]+/(\d+)$")
_PUBLISHED_RE = re.compile(r"Published on:\s*(\d{1,2} [A-Za-z]+ \d{4})")
_SKIP_TAG_GROUPS = {"gallery-tag-group-sponsor", "gallery-tag-group-model"}
_page = {}  # the last model page, shared by the set listing and the promo images of one performer


def model_slug(performer):
    return slug_from_urls(performer, r"babepedia\.com/babe/([^/?#]+)") or performer["name"].strip().replace(" ", "_")


def _url(href):
    return BASE + quote(unquote(href), safe="/,()'!&")


def model_page(performer, fetcher):
    """(url, html) of the model page; html is None when the name isn't known (the site redirects to a search)."""
    url = _url(f"/babe/{model_slug(performer)}")
    if url not in _page:
        _page.clear()
        r = fetcher.get(url, referer=f"{BASE}/", missing_ok=True)
        ok = r is not None and "/search/" not in r.url and 'id="babename"' in r.text
        _page[url] = r.text if ok else None
    return url, _page[url]


def parse_listing(html):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select(".thumbshot a[href^='/gallery/']"):
        m = _GALLERY_RE.match(a["href"])
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(SetRef(NAME, _url(a["href"]), m.group(1)))
    return out


def list_sets(performer, fetcher):
    _, html = model_page(performer, fetcher)
    yield from parse_listing(html) if html else []


def parse_set(ref, html):
    soup = BeautifulSoup(html, "html.parser")
    info = SetInfo(NAME, ref.url, ref.source_id, text(soup.find("h1")))
    info.models = [text(a) for a in soup.select("h2.featuring a[href^='/babe/']")]
    box = soup.find(id="galleryinfo")
    for p in box.find_all("p") if box else []:
        line = text(p)
        if line.startswith("Source:"):
            info.studio = line[len("Source:"):].split(" (", 1)[0].strip() or None
        m = _PUBLISHED_RE.search(line)
        if m:
            try:
                info.posted = datetime.strptime(m.group(1), "%d %B %Y").strftime("%Y-%m-%d")
            except ValueError:
                pass
    info.tags = [text(t) for g in soup.select(".gallery-tag-group") if not set(g.get("class") or []) & _SKIP_TAG_GROUPS
                 for t in g.select(".gallery-tag")]
    info.images = [ImageRef(_url(a["href"])) for a in soup.select("a.img[href^='/galleries/']")]
    return info


def get_set(ref, fetcher):
    return parse_set(ref, fetcher.get(ref.url, referer=f"{BASE}/").text)


def referer(info):
    return info.url


def parse_promo(html):
    """(file stem, ImageRef) for the profile pics and user uploads; stems come from the site's file names."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.select("a[href^='/pics/'], a[href^='/user-uploads/']"):
        path = unquote(a["href"])
        name = path.rsplit("/", 1)[1].rsplit(".", 1)[0]
        if "_thumb" in name:
            continue
        kind = "pics" if path.startswith("/pics/") else "upload"
        out.setdefault(f"{NAME}-{kind}-{slugify(name)}", ImageRef(_url(a["href"])))
    return list(out.items())


def promo_images(performer, fetcher):
    """(file stem, ImageRef, referer); sizes are unknown until downloaded."""
    url, html = model_page(performer, fetcher)
    for stem, im in parse_promo(html) if html else []:
        yield stem, im, url
