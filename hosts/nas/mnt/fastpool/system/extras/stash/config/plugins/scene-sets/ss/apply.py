"""set.json → Stash gallery. Fills empty title/date/details/studio, adds performers/URLs and the scene; never
overwrites manual edits."""
from . import layout

REVIEW_TAG = "Scene Sets: needs review"


def plan(stash, gallery, sidecar, dry=True):
    """(GalleryUpdateInput values or None when nothing changes, list of problems)."""
    problems = []
    values = {}

    scenes = {s["id"] for s in gallery.get("scenes") or []}
    sid = sidecar.get("scene_id")
    if sid:
        if stash.find_scene(str(sid)):
            scenes.add(str(sid))
        else:
            problems.append(f"scene {sid} not in Stash")

    performers = {p["id"] for p in gallery["performers"]}
    for name in sidecar.get("performers") or []:
        pid = stash.lookup("performer", name)
        if pid:
            performers.add(pid)
        else:
            problems.append(f"performer '{name}' not in Stash")

    studio = sidecar.get("studio")
    if studio and not gallery.get("studio"):
        st = stash.lookup("studio", studio)
        if st:
            values["studio_id"] = st
        else:
            problems.append(f"studio '{studio}' not in Stash")
    for key in ("title", "date", "details"):
        if sidecar.get(key) and not gallery.get(key):
            values[key] = sidecar[key]
    urls = list(gallery.get("urls") or [])
    urls += [u for u in sidecar.get("urls") or [] if u not in urls]
    if urls != (gallery.get("urls") or []):
        values["urls"] = urls

    tags = {t["id"] for t in gallery["tags"]}
    review = stash.lookup("tag", REVIEW_TAG)
    if problems and review is None and not dry:
        review = stash.ensure_tag(REVIEW_TAG)
    if review:
        (tags.add if problems else tags.discard)(review)
    if scenes != {s["id"] for s in gallery.get("scenes") or []}:
        values["scene_ids"] = sorted(scenes, key=int)
    if performers != {p["id"] for p in gallery["performers"]}:
        values["performer_ids"] = sorted(performers, key=int)
    if tags != {t["id"] for t in gallery["tags"]}:
        values["tag_ids"] = sorted(tags, key=int)
    return ({"id": gallery["id"], **values} if values else None), problems


def apply_gallery(stash, gallery, report, dry):
    folder = gallery["folder"]["path"]
    sidecar = layout.read_sidecar(folder)
    if sidecar is None or sidecar.get("kind") != "scene":
        report.counts["no-sidecar"] += 1
        return
    values, problems = plan(stash, gallery, sidecar, dry)
    for p in problems:
        report.emit("REVIEW", f"gallery={gallery['id']}", f"{p} ({folder})", "warning")
    if not values:
        report.counts["gallery-ok"] += 1
        return
    report.emit("APPLY", f"gallery={gallery['id']}", f"{', '.join(k for k in values if k != 'id')} ← {folder}")
    if not dry:
        stash.update_gallery(values)
