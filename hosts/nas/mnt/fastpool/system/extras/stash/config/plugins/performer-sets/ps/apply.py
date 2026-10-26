"""set.json → Stash gallery. Fills empty title/date/studio, adds performers/URLs/tags; never overwrites manual edits.
Quality tags (solo/duo/group, high-res, soft) follow set.json and are the only tags this removes again."""
from . import layout

REVIEW_TAG = "Performer Sets: needs review"
QUALITY_TAGS = {"solo": "Performer Sets: Solo", "duo": "Performer Sets: Duo", "group": "Performer Sets: Group",
                "hires": "Performer Sets: High-res", "soft": "Performer Sets: Soft"}
HIRES_EDGE = 1600


def quality_tags(sidecar):
    """QUALITY_TAGS keys a set gallery should carry."""
    if sidecar.get("kind") != "set":
        return set()
    n = 1 + len(sidecar.get("other_performers") or [])
    out = {"solo" if n == 1 else "duo" if n == 2 else "group"}
    q = sidecar.get("quality") or {}
    if (q.get("edge") or 0) >= HIRES_EDGE:
        out.add("hires")
    if q.get("soft"):
        out.add("soft")
    return out


def plan(stash, cfg, gallery, sidecar, dry=True):
    """(GalleryUpdateInput values or None when nothing changes, list of problems)."""
    problems = []
    performers = {p["id"] for p in gallery["performers"]}
    if sidecar.get("performer_id"):
        performers.add(str(sidecar["performer_id"]))
    for name in sidecar.get("other_performers") or []:
        pid = stash.lookup("performer", name)
        if pid:
            performers.add(pid)
        else:
            problems.append(f"performer '{name}' not in Stash")

    tags = {t["id"] for t in gallery["tags"]}
    if cfg.map_source_tags:
        tags |= {tid for tid in (stash.lookup("tag", t) for t in sidecar.get("source_tags") or []) if tid}
    if cfg.quality_tags:
        wanted = quality_tags(sidecar)
        for key, name in QUALITY_TAGS.items():
            tid = stash.lookup("tag", name)
            if key in wanted and tid is None and not dry:
                tid = stash.ensure_tag(name)
            if tid:
                (tags.add if key in wanted else tags.discard)(tid)

    values = {}
    studio = sidecar.get("studio")
    if studio and not gallery.get("studio"):
        sid = stash.lookup("studio", studio)
        if sid:
            values["studio_id"] = sid
        else:
            problems.append(f"studio '{studio}' not in Stash")
    if sidecar.get("title") and not gallery.get("title"):
        values["title"] = sidecar["title"]
    if sidecar.get("date") and not gallery.get("date"):
        values["date"] = sidecar["date"]
    urls = list(gallery.get("urls") or [])
    urls += [u for u in sidecar.get("urls") or [] if u not in urls]
    if urls != (gallery.get("urls") or []):
        values["urls"] = urls

    review = stash.lookup("tag", REVIEW_TAG)
    if problems and review is None and not dry:
        review = stash.ensure_tag(REVIEW_TAG)
    if review:
        (tags.add if problems else tags.discard)(review)
    if performers != {p["id"] for p in gallery["performers"]}:
        values["performer_ids"] = sorted(performers, key=int)
    if tags != {t["id"] for t in gallery["tags"]}:
        values["tag_ids"] = sorted(tags, key=int)
    return ({"id": gallery["id"], **values} if values else None), problems


def apply_gallery(stash, cfg, gallery, report, dry, sidecar=None):
    folder = gallery["folder"]["path"]
    sidecar = sidecar or layout.read_sidecar(folder)
    if sidecar is None:
        report.counts["no-sidecar"] += 1
        return
    values, problems = plan(stash, cfg, gallery, sidecar, dry)
    for p in problems:
        report.emit("REVIEW", f"gallery={gallery['id']}", f"{p} ({folder})", "warning")
    if not values:
        report.counts["gallery-ok"] += 1
        return
    report.emit("APPLY", f"gallery={gallery['id']}", f"{', '.join(k for k in values if k != 'id')} ← {folder}")
    if not dry:
        stash.update_gallery(values)
