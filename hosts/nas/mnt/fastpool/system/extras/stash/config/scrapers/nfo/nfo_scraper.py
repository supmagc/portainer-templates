"""Stash script scraper: fill a scene from the Kodi/Jellyfin <movie> or <episodedetails> NFO next to its video file.

stdin: Stash scene fragment JSON (has files[].path). stdout: scraped scene JSON, or null when no usable NFO.
"""
import base64
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

IGNORED_TAGS = {"adult"}
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
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


def image_value(root, nfo_dir):
    """Cover for the scene: remote URL as-is, local file inlined as a data URI. Prefers landscape fanart."""
    cands = [e.text for e in root.findall("fanart/thumb")] + [e.text for e in root.findall("thumb")]
    cands += [e.text for e in root.findall("art/background") + root.findall("art/poster")]
    for c in cands:
        c = (c or "").strip()
        if not c:
            continue
        if c.startswith(("http://", "https://", "data:")):
            return c
        path = os.path.join(nfo_dir, c)
        mime = MIME.get(os.path.splitext(path)[1].lower())
        if mime and os.path.isfile(path) and os.path.getsize(path) <= MAX_IMAGE_BYTES:
            with open(path, "rb") as fh:
                return f"data:{mime};base64,{base64.b64encode(fh.read()).decode()}"
    return None


def performer(actor):
    name = (actor.findtext("name") or "").strip()
    if not name:
        return None
    out = {"name": name}
    role = (actor.findtext("role") or "").strip()
    if role:
        out["disambiguation"] = role
    for tag in ("gender", "birthdate", "height", "country", "ethnicity", "eye_color", "hair_color"):
        val = (actor.findtext(tag) or "").strip()
        if val:
            out[tag] = val
    profile = (actor.findtext("profile") or "").strip()
    if profile:
        out["urls"] = [profile]
    thumb = (actor.findtext("thumb") or "").strip()
    if thumb.startswith(("http://", "https://")):
        out["images"] = [thumb]
    return out


def studio_chain(names):
    """<studio> elements are child first, then parents (as the organizer writes them)."""
    node = None
    for name in reversed(names):
        node = {"name": name, **({"parent": node} if node else {})}
    return node


def scrape(fragment):
    path = find_nfo(fragment.get("files"))
    if not path:
        return None
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    if root.tag not in ("movie", "episodedetails"):
        return None

    actor_els = root.findall("actor")
    actors = [n for a in actor_els if (n := (a.findtext("name") or "").strip())]
    title, name_studio, name_date = clean_title(text(root, "title") or text(root, "originaltitle") or "", actors)
    date = text(root, "premiered") or text(root, "releasedate") or text(root, "aired") or name_date
    studio_names = [(e.text or "").strip() for e in root.findall("studio") if (e.text or "").strip()]
    studio = studio_chain(studio_names) if studio_names else ({"name": name_studio} if name_studio else None)

    tags, seen = [], set()
    for el in root.findall("tag") + root.findall("genre"):
        name = (el.text or "").strip()
        if name and name.lower() not in IGNORED_TAGS and name.lower() not in seen:
            seen.add(name.lower())
            tags.append({"name": name})

    urls = []
    for url in [e.text.strip() for e in root.findall("website") + root.findall("url") + root.findall("sourceid") if e.text]:
        if meaningful_url(url) and url not in urls:
            urls.append(url)

    code = None
    for u in root.findall("uniqueid"):
        if u.get("type") == "code" and (u.text or "").strip():
            code = u.text.strip()

    groups = [{"name": n} for s in root.findall("set") if (n := (s.findtext("name") or s.text or "").strip())]

    out = {
        "title": title,
        "code": code,
        "details": text(root, "plot") or text(root, "outline"),
        "director": text(root, "director"),
        "date": date,
        "urls": urls,
        "image": image_value(root, os.path.dirname(path)),
        "studio": studio,
        "performers": [p for a in actor_els if (p := performer(a))],
        "tags": tags,
        "groups": groups,
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
