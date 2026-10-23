"""set.json → Stash gallery. Fills empty title/date/studio, adds performers/URLs/tags; never overwrites manual edits."""
from . import layout

REVIEW_TAG = "Performer Sets: needs review"


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


def apply_gallery(stash, cfg, gallery, report, dry):
    folder = gallery["folder"]["path"]
    sidecar = layout.read_sidecar(folder)
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
