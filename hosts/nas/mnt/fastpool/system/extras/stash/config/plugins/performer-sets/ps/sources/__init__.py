"""Set sources. Each yields SetRefs for a performer (listing only) and turns one into a SetInfo (set page)."""
import re
from dataclasses import dataclass, field


@dataclass
class SetRef:
    source: str
    url: str
    source_id: str
    posted: str = None  # the aggregator's own upload date, not the studio release date


@dataclass
class ImageRef:
    url: str
    width: int = 0
    height: int = 0


@dataclass
class SetInfo:
    source: str
    url: str
    source_id: str
    title: str
    studio: str = None
    date: str = None  # release date; no source provides one yet
    posted: str = None
    models: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    images: list = field(default_factory=list)


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def slug_from_urls(performer, pattern):
    """First performer URL matching pattern (one capture group = the site's slug)."""
    for url in performer.get("urls") or []:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def text(node):
    return " ".join(node.get_text(" ").split()) if node is not None else ""


def get(name):
    from . import elitebabes, pornpics
    return {"pornpics": pornpics, "elitebabes": elitebabes}[name]
