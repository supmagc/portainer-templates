"""Scene-set sources. Each one recognises its scene URLs (match → canonical URL) and fetches the public photo
set behind one (fetch → SetInfo, or None when the scene is gone from the site)."""
from dataclasses import dataclass, field


@dataclass
class ImageRef:
    url: str
    width: int = 0
    height: int = 0


@dataclass
class Archive:
    """A members' zip of the set (signed, short-lived URL)."""
    url: str
    name: str
    grabs: bool = False  # video frame grabs rather than the photo set


@dataclass
class SetInfo:
    source: str  # plugin source: kink | vixen
    site: str  # the actual site (kink, kinkmen, blacked, tushy, …); with source_id it names the folder
    source_id: str
    url: str
    images: list = field(default_factory=list)  # public photos
    title: str = None
    tags: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # source-specific facts stored in set.json as-is
    archives: list = field(default_factory=list)  # members' zips, when logged in
    logged_in: bool = None  # None = no member session was offered

    @property
    def key(self):
        return f"{self.site}-{self.source_id}"


@dataclass
class Context:
    fetcher: object
    cdp: object = None
    kink_session: tuple = None


def get(name):
    from . import kink, vixen
    return {"kink": kink, "vixen": vixen}[name]


def match(urls, sources):
    """(source module, canonical URL) of the first scene URL a source recognises, else None."""
    for url in urls or []:
        for src in sources:
            canon = src.match(url)
            if canon:
                return src, canon
    return None
