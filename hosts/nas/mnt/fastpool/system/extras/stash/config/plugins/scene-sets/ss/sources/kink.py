"""Kink (kink.com, kinkmen.com, kinktrans.com; verified on kink.com 2026-10-30).

The shoot page is behind Cloudflare, so it is loaded in Stash's chromedp (with the age-gate cookie, as the Kink
scraper does). `#galleryImagesContainer[data-images]` holds the public photos as JSON
({fullPath, sequence, alt, …}); fullPath (`cdnp.kink.com/imagedb/<id>/i/h/830/<n>.jpg`) is served at 1400×934
and the CDN answers plain requests. A removed shoot redirects away from /shoot/<id>.

Members (the `kinky.sess` session cookie of a logged-in browser, kink.com only): the page also lists
`a.download-zip-images-file` — signed cdn.kink.com zips. 2014–2020 shoots: one zip = the same 15 photos +
2 small performer-N.jpg; 2026: 16 PNGs at 3840×2160; 2013: "<site>_<id>_hi_res_pictures_1 Glamour.zip" (the posed
set) plus "…_Scene N.zip" (video frame grabs). Logged in = the page has a logout link."""
import json
import re
from urllib.parse import urlsplit

from ..fetch import Blocked
from . import Archive, ImageRef, SetInfo

NAME = "kink"
MEMBER_HOST = "www.kink.com"
_URL = re.compile(r"https?://(?:www\.)?(kink|kinkmen|kinktrans)\.com/shoot/(\d+)", re.I)
_GRABS = re.compile(r"_pictures_Scene \d+\.zip$", re.I)

READY = "!!document.querySelector('#galleryImagesContainer') || !location.pathname.startsWith('/shoot/')"
PAGE = ("JSON.stringify({title: document.title, href: location.href, "
        "images: (document.querySelector('#galleryImagesContainer') || {dataset: {}}).dataset.images || null, "
        "loggedIn: !!document.querySelector('a[href*=\"logout\"]'), "
        "zips: [...document.querySelectorAll('a.download-zip-images-file')].map(a => [a.href, a.textContent.trim()])})")


def match(url):
    m = _URL.match(url or "")
    return f"https://www.{m.group(1).lower()}.com/shoot/{m.group(2)}" if m else None


def key(url):
    """The folder key, known before fetching the page."""
    m = _URL.match(url)
    return f"{m.group(1).lower()}-{m.group(2)}"


def fetch(url, ctx):
    host = urlsplit(url).hostname
    cookies = [{"name": "age_gate_accepted", "value": "1", "domain": host, "path": "/"}]
    member = bool(ctx.kink_session) and host == MEMBER_HOST
    if member:
        name, value = ctx.kink_session
        cookies.append({"name": name, "value": value, "domain": host, "path": "/", "secure": True, "httpOnly": True})
    ctx.fetcher.wait(host)
    page = ctx.cdp.evaluate(url, PAGE, ready=READY, cookies=cookies)
    return parse(url, json.loads(page), member)


def parse(url, page, member=False):
    title = page.get("title") or ""
    if "just a moment" in title.lower():
        raise Blocked(f"{urlsplit(url).hostname}: bot challenge in Chrome")
    m = _URL.match(page.get("href") or "")
    if not m or m.group(2) != _URL.match(url).group(2):
        return None
    raw = json.loads(page["images"]) if page.get("images") else []
    images = [ImageRef(i["fullPath"]) for i in sorted(raw, key=lambda i: i.get("sequence", 0)) if i.get("fullPath")]
    archives, seen = [], set()
    for href, text in (page.get("zips") or []) if member else []:
        if href and href not in seen:  # the dropdown is rendered twice (desktop + mobile)
            seen.add(href)
            name = text or urlsplit(href).path.rsplit("/", 1)[-1]
            archives.append(Archive(href, name, bool(_GRABS.search(name))))
    archives.sort(key=lambda a: (a.grabs, a.name))  # the photo set first, then frame grabs
    return SetInfo(NAME, m.group(1).lower(), m.group(2), url, images,
                   title=re.sub(r"\s*\|\s*Kink\.com\s*$", "", title, flags=re.I),
                   archives=archives, logged_in=bool(page.get("loggedIn")) if member else None)
