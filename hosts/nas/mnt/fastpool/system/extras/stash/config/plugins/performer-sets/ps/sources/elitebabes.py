"""EliteBabes: model page → grid API (dated listing), set page → studio/models/tags/images and the real set title."""
import re

from bs4 import BeautifulSoup

from . import ImageRef, SetInfo, SetRef, slug_from_urls, slugify, text

NAME = "elitebabes"
BASE = "https://www.elitebabes.com"
MAX_PAGES = 100
_API_RE = re.compile(r"apiUrl\s*=\s*'([^']+)'")
_SET_RE = re.compile(r"^https://www\.elitebabes\.com/[a-z0-9-]+-(\d+)/$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def model_slug(performer):
    return slug_from_urls(performer, r"elitebabes\.com/model/([^/?#]+)") or slugify(performer["name"])


def parse_listing(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.find_all("li"):
        a = li.select_one("figure > a[href]")
        m = _SET_RE.match(a["href"]) if a else None
        if not m:
            continue
        label = text(li.select_one(".img-label span"))
        out.append(SetRef(NAME, a["href"], m.group(1), label if _DATE_RE.match(label) else None))
    return out


def list_sets(performer, fetcher):
    r = fetcher.get(f"{BASE}/model/{model_slug(performer)}/", referer=f"{BASE}/", missing_ok=True)
    m = _API_RE.search(r.text) if r is not None else None
    if not m:
        return
    api = m.group(1).replace("sort=popular", "sort=latest")
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        refs = [ref for ref in parse_listing(fetcher.get(f"{api}&mpage={page}", referer=r.url).text) if ref.url not in seen]
        if not refs:
            return
        for ref in refs:
            seen.add(ref.url)
            yield ref


def parse_set(ref, html):
    soup = BeautifulSoup(html, "html.parser")
    h1 = text(soup.find("h1"))
    title = h1.split(" set ", 1)[1] if " set " in h1 else h1
    info = SetInfo(NAME, ref.url, ref.source_id, title, posted=ref.posted)
    for a in soup.select("a.overlay-b[href]"):
        if "/model/" in a["href"]:
            info.models.append(text(a))
        elif info.studio is None and "/tag/" not in a["href"]:
            info.studio = text(a) or None
    info.tags = [text(a) for a in soup.select("a.visible-tag")]
    for a in soup.select("ul.list-gallery a[href^='https://cdn.elitebabes.com/content/']"):
        try:
            w, h = int(a.get("data-width") or 0), int(a.get("data-height") or 0)
        except ValueError:
            w = h = 0
        info.images.append(ImageRef(a["href"], w, h))
    return info


def get_set(ref, fetcher):
    return parse_set(ref, fetcher.get(ref.url, referer=f"{BASE}/").text)


def referer(info):
    return info.url
