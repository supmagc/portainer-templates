"""Stash script scraper: fill a scene from the Kodi/Jellyfin <movie> NFO next to its video file.

stdin: Stash scene fragment JSON (has files[].path). stdout: scraped scene JSON, or null when no usable NFO.
"""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

IGNORED_TAGS = {"adult"}
QUALITY_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
# Whisparr naming: "<Studio> - <yyyy-mm-dd> - <Title>[ - <performers>] [quality]"
NAME_RE = re.compile(r"^(?P<studio>.+?) - (?P<date>\d{4}-\d{2}-\d{2}) - (?P<rest>.+)$")


def text(root, tag):
    value = (root.findtext(tag) or "").strip()
    return value or None


def find_nfo(files):
    for f in files or []:
        base, _ = os.path.splitext(f.get("path") or "")
        if base and os.path.isfile(base + ".nfo"):
            return base + ".nfo"
    return None


def clean_title(raw, actors):
    """Strip quality suffix, and the Whisparr 'studio - date -' prefix and ' - performers' suffix."""
    studio = date = None
    title = QUALITY_RE.sub("", raw).strip()
    m = NAME_RE.match(title)
    if m:
        studio, date, title = m["studio"], m["date"], m["rest"].strip()
    head, sep, tail = title.rpartition(" - ")
    if actors and sep and {n.strip().lower() for n in re.split(r",|&", tail)} == {a.lower() for a in actors}:
        title = head.strip()
    return title or raw, studio, date


def meaningful_url(url):
    p = urlparse(url)
    return bool(p.scheme and p.netloc and p.path.strip("/"))


def scrape(fragment):
    path = find_nfo(fragment.get("files"))
    if not path:
        return None
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    if root.tag != "movie":
        return None

    actors = [n for a in root.findall("actor") if (n := (a.findtext("name") or "").strip())]
    title, name_studio, name_date = clean_title(text(root, "title") or text(root, "originaltitle") or "", actors)
    date = text(root, "premiered") or text(root, "releasedate") or name_date
    studio = text(root, "studio") or name_studio

    tags, seen = [], set()
    for el in root.findall("tag") + root.findall("genre"):
        name = (el.text or "").strip()
        if name and name.lower() not in IGNORED_TAGS and name.lower() not in seen:
            seen.add(name.lower())
            tags.append({"name": name})

    urls = []
    for url in [e.text.strip() for e in root.findall("website") + root.findall("sourceid") if e.text]:
        if meaningful_url(url) and url not in urls:
            urls.append(url)

    code = None
    for u in root.findall("uniqueid"):
        if u.get("type") == "code" and (u.text or "").strip():
            code = u.text.strip()

    out = {
        "title": title,
        "code": code,
        "details": text(root, "plot"),
        "date": date,
        "urls": urls or None,
        "studio": {"name": studio} if studio else None,
        "performers": [{"name": n} for n in actors] or None,
        "tags": tags or None,
    }
    return {k: v for k, v in out.items() if v}


def main():
    try:
        fragment = json.load(sys.stdin)
    except json.JSONDecodeError:
        fragment = {}
    print(json.dumps(scrape(fragment), ensure_ascii=False))


if __name__ == "__main__":
    main()
