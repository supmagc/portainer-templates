"""Kodi/Jellyfin <movie> NFO: build from a Stash scene, merging an existing NFO."""
import copy
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

OWNED = {
    "title", "originaltitle", "sorttitle", "plot", "outline", "premiered", "releasedate", "year", "studio",
    "director", "tag", "rating", "userrating", "runtime", "dateadded", "lastplayed", "playcount", "watched",
    "resume", "set", "actor", "uniqueid", "fileinfo", "thumb", "fanart", "website", "stash",
    # Whisparr <episodedetails> leftovers that the <movie> form replaces
    "season", "episode", "aired",
}
_ENDPOINT_TYPES = {"stashdb.org": "stashdb", "theporndb.net": "tpdb", "api.theporndb.net": "tpdb"}


def parse_existing(data):
    """-> dict(root=str, uniqueids=[(type, default, text)], watched=str|None, extras=[Element]) or None."""
    if not data:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    ids = [(u.get("type") or "", u.get("default"), (u.text or "").strip()) for u in root.findall("uniqueid")]
    watched = root.findtext("watched")
    extras = [copy.deepcopy(e) for e in root if e.tag not in OWNED]
    return {"root": root.tag, "uniqueids": ids, "watched": watched, "extras": extras}


def whisparr_id(existing):
    for typ, _, text in (existing or {}).get("uniqueids", []):
        if typ == "whisparr" and text.isdigit():
            return int(text)
    return None


def _sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items() if v is not None})
    if text is not None:
        el.text = str(text)
    return el


def _endpoint_type(endpoint):
    host = urlparse(endpoint).netloc or endpoint
    return _ENDPOINT_TYPES.get(host, host)


def _num(value, digits=7):
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def build(scene, title, existing, poster_name, fanart_name):
    root = ET.Element("movie")
    files = scene.get("files") or []
    vf = files[0] if files else {}
    date = scene.get("date")
    studio = scene.get("studio")

    _sub(root, "title", title)
    _sub(root, "originaltitle", title)
    _sub(root, "sorttitle", f"{date} {title}" if date else title)
    details = (scene.get("details") or "").strip()
    if details:
        _sub(root, "plot", details)
        _sub(root, "outline", details.split("\n\n")[0][:300])
    if date:
        _sub(root, "premiered", date)
        _sub(root, "releasedate", date)
        _sub(root, "year", date[:4])
    chain = []
    cur = studio
    while cur:
        chain.append(cur["name"])
        cur = cur.get("parent_studio")
    for name in chain:
        _sub(root, "studio", name)
    if scene.get("director"):
        _sub(root, "director", scene["director"])
    for tag in scene.get("tags") or []:
        _sub(root, "tag", tag["name"])
    if scene.get("rating100") is not None:
        rating = f"{scene['rating100'] / 10:.1f}"
        _sub(root, "rating", rating)
        _sub(root, "userrating", rating)
    if vf.get("duration"):
        _sub(root, "runtime", max(1, round(vf["duration"] / 60)))
    if scene.get("created_at"):
        _sub(root, "dateadded", scene["created_at"][:19].replace("T", " "))
    if scene.get("last_played_at"):
        _sub(root, "lastplayed", scene["last_played_at"][:19].replace("T", " "))
    plays = scene.get("play_count") or 0
    _sub(root, "playcount", plays)
    watched = "true" if plays > 0 else ((existing or {}).get("watched") or "false")
    _sub(root, "watched", watched)
    if scene.get("resume_time") and vf.get("duration"):
        res = _sub(root, "resume")
        _sub(res, "position", _num(scene["resume_time"], 3))
        _sub(res, "total", _num(vf["duration"], 3))
    for grp in scene.get("groups") or []:
        if (grp.get("group") or {}).get("name"):
            _sub(root, "set").append(_elem("name", grp["group"]["name"]))

    for idx, perf in enumerate(scene.get("performers") or [], 1):
        actor = _sub(root, "actor")
        _sub(actor, "name", perf["name"])
        if perf.get("disambiguation"):
            _sub(actor, "role", perf["disambiguation"])
        _sub(actor, "order", idx)
        if perf.get("urls"):
            _sub(actor, "profile", perf["urls"][0])

    seen = set()
    ids = []

    def add_id(typ, text, default=None):
        key = (typ, text)
        if text and key not in seen:
            seen.add(key)
            ids.append((typ, text, default))

    for typ, default, text in (existing or {}).get("uniqueids", []):
        add_id(typ, text, default)
    add_id("stash", scene["id"])
    for sid in scene.get("stash_ids") or []:
        add_id(_endpoint_type(sid["endpoint"]), sid["stash_id"])
    add_id("code", scene.get("code"))
    for typ, text, default in ids:
        _sub(root, "uniqueid", text, type=typ, default=default)

    if poster_name:
        _sub(root, "thumb", poster_name, aspect="poster")
    if fanart_name:
        fan = _sub(root, "fanart")
        _sub(fan, "thumb", fanart_name)
    for url in scene.get("urls") or []:
        _sub(root, "website", url)

    if vf:
        info = _sub(_sub(root, "fileinfo"), "streamdetails")
        for f in files:
            v = _sub(info, "video")
            if f.get("width") and f.get("height"):
                _sub(v, "aspect", _num(f["width"] / f["height"]))
            if f.get("bit_rate"):
                _sub(v, "bitrate", f["bit_rate"])
            if f.get("video_codec"):
                _sub(v, "codec", f["video_codec"])
            if f.get("frame_rate"):
                _sub(v, "framerate", _num(f["frame_rate"], 3))
            if f.get("height"):
                _sub(v, "height", f["height"])
            _sub(v, "scantype", "Progressive")
            if f.get("width"):
                _sub(v, "width", f["width"])
            if f.get("duration"):
                _sub(v, "duration", max(1, round(f["duration"] / 60)))
                _sub(v, "durationinseconds", round(f["duration"]))
            if f.get("audio_codec"):
                _sub(_sub(info, "audio"), "codec", f["audio_codec"])

    st = _sub(root, "stash")
    _sub(st, "scene_id", scene["id"])
    _sub(st, "organized", str(bool(scene.get("organized"))).lower())
    _sub(st, "o_counter", scene.get("o_counter") or 0)
    _sub(st, "interactive", str(bool(scene.get("interactive"))).lower())
    for key, val in sorted((scene.get("custom_fields") or {}).items()):
        _sub(st, "custom_field", val, name=key)
    for m in scene.get("scene_markers") or []:
        mk = _sub(st, "marker")
        _sub(mk, "title", m.get("title") or "")
        _sub(mk, "seconds", _num(m["seconds"], 3))
        if m.get("end_seconds") is not None:
            _sub(mk, "end_seconds", _num(m["end_seconds"], 3))
        if (m.get("primary_tag") or {}).get("name"):
            _sub(mk, "primary_tag", m["primary_tag"]["name"])
        for t in m.get("tags") or []:
            _sub(mk, "tag", t["name"])
    for g in scene.get("galleries") or []:
        _sub(st, "gallery", g["id"])
    if scene.get("updated_at"):
        _sub(st, "updated_at", scene["updated_at"])

    for extra in (existing or {}).get("extras", []):
        root.append(copy.deepcopy(extra))

    ET.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def _elem(tag, text):
    el = ET.Element(tag)
    el.text = text
    return el
