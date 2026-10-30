"""Vixen Media Group (verified on blacked.com and deeper.com 2026-10-30).

Each site has a public GraphQL API at https://www.<site>.com/graphql (what the community vixenNetwork scraper
uses; introspection is off). findOneVideo.carousel[].main[] are the public tour photos — 8 per scene, 1200×800,
CloudFront-signed URLs that plain requests can fetch; picturesInSet is the size of the members' set."""
import re

from . import ImageRef, SetInfo

NAME = "vixen"
SITES = ("vixen", "blacked", "blackedraw", "tushy", "tushyraw", "deeper", "slayed", "milfy", "wifey")
_URL = re.compile(r"https?://(?:www\.)?(%s)\.com/videos/([^/?#]+)" % "|".join(SITES), re.I)

QUERY = """query getVideo($videoSlug: String, $site: Site) {
  findOneVideo(input: {slug: $videoSlug, site: $site}) {
    videoId title picturesInSet categories { name } carousel { main { src width height } }
  }
}"""


def match(url):
    m = _URL.match(url or "")
    return f"https://www.{m.group(1).lower()}.com/videos/{m.group(2)}" if m else None


def key(url):
    """The folder key needs the videoId, which only the API knows."""
    return None


def fetch(url, ctx):
    m = _URL.match(url)
    site, slug = m.group(1).lower(), m.group(2)
    r = ctx.fetcher.post(f"https://www.{site}.com/graphql", referer=url, headers={"Accept": "application/json"},
                         json={"operationName": "getVideo", "variables": {"site": site.upper(), "videoSlug": slug},
                               "query": QUERY})
    return parse(url, site, r.json())


def parse(url, site, body):
    if body.get("errors"):
        raise ValueError("; ".join(e.get("message", "?") for e in body["errors"]))
    v = (body.get("data") or {}).get("findOneVideo")
    if not v:
        return None
    images = []
    for item in v.get("carousel") or []:
        best = max(item.get("main") or [], key=lambda i: i.get("width") or 0, default=None)
        if best and best.get("src"):
            images.append(ImageRef(best["src"], best.get("width") or 0, best.get("height") or 0))
    return SetInfo(NAME, site, str(v["videoId"]), url, images, title=v.get("title"),
                   tags=[c["name"] for c in v.get("categories") or [] if c.get("name")],
                   extra={"pictures_in_set": v.get("picturesInSet")})
