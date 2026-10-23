"""PornPics: name search JSON for the listing, gallery page for channel/models/categories/images. No dates."""
import re

from bs4 import BeautifulSoup

from . import ImageRef, SetInfo, SetRef, slug_from_urls, text

NAME = "pornpics"
BASE = "https://www.pornpics.com"
PAGE = 20
MAX_OFFSET = 1000  # the site's own pager stops at 25 pages of 20 (P_MAX)
_SIZE_RE = re.compile(r"^(\d+)x(\d+)$")


def query(performer):
    slug = slug_from_urls(performer, r"pornpics\.com/(?:\w\w/)?pornstars/([^/?#]+)")
    return slug.replace("-", " ") if slug else performer["name"]


def list_sets(performer, fetcher):
    q = query(performer)
    for offset in range(0, MAX_OFFSET, PAGE):
        r = fetcher.get(f"{BASE}/search/srch.php", params={"q": q, "lang": "en", "limit": PAGE, "offset": offset},
                        headers={"X-Requested-With": "XMLHttpRequest"}, referer=f"{BASE}/")
        items = r.json() if r.text.strip() else []
        if not items:
            return
        for it in items:
            if it.get("g_url") and it.get("gid") and not it.get("outLink"):
                yield SetRef(NAME, it["g_url"], str(it["gid"]))
        if len(items) < PAGE:
            return


def parse_set(ref, html):
    soup = BeautifulSoup(html, "html.parser")
    info = SetInfo(NAME, ref.url, ref.source_id, text(soup.find("h1")))
    for item in soup.select(".gallery-info__item"):
        label = text(item.select_one(".gallery-info__title")).rstrip(": ").casefold()
        if label == "channel":
            info.studio = text(item.find("a")) or None
        elif label == "models":
            info.models = [text(a) for a in item.select(".gallery-info__content a[href*='/pornstars/']")]
        elif label == "categories":
            info.tags = [text(a) for a in item.select(".gallery-info__content a")]
    for a in soup.select("li.thumbwook a.rel-link[href]"):
        href = a["href"]
        if "cdni.pornpics.com" not in href or f"/{ref.source_id}/" not in href:
            continue
        m = _SIZE_RE.match(a.get("data-size") or "")
        info.images.append(ImageRef(href, int(m.group(1)), int(m.group(2))) if m else ImageRef(href))
    return info


def get_set(ref, fetcher):
    return parse_set(ref, fetcher.get(ref.url, referer=f"{BASE}/").text)


def referer(_info):
    return f"{BASE}/"
