"""Decide what a scene needs (move/rename/NFO/art); no side effects."""
import os
import posixpath

from . import fsutil, naming, nfo

INFO_KINDS = {"SKIP", "WARN", "ERROR"}


class Action:
    def __init__(self, kind, text, src=None, dst=None, data=None, level="info"):
        self.kind, self.text, self.src, self.dst, self.data, self.level = kind, text, src, dst, data, level


def _skip(text, level="info"):
    return [Action("SKIP", text, level=level)]


def _free_basename(new_dir, old_dir, new_base, old_base, ext, suffixes, listing_old):
    """First numbered basename whose target names are unused in new_dir (case-insensitive)."""
    listing_new = listing_old if new_dir == old_dir else fsutil.listdir(new_dir)
    taken = {n.casefold() for n in listing_new}
    sources = set()
    if new_dir == old_dir:
        sources = {old_base + ext} | {old_base + s for s in suffixes}
    for n in range(1, 30):
        cand = naming.with_suffix_number(new_base, n)
        targets = [cand + ext] + [cand + fsutil.map_suffix(s) for s in suffixes]
        if all(t.casefold() not in taken or t in sources for t in targets):
            return cand, n
    return None, 30


def plan_scene(scene, cfg, whisparr, studios=None):
    files = scene.get("files") or []
    if not files:
        return _skip("no video file")
    if len(files) > 1:
        return _skip(f"{len(files)} files in one scene (multi-file not handled)", "warning")
    vf = files[0]
    path = vf["path"]
    lib = cfg.library_for(path)
    if not lib:
        return _skip(f"outside libraries: {path}")

    actions = []
    old_dir, old_name = posixpath.split(path)
    old_base, ext = os.path.splitext(old_name)
    listing = fsutil.listdir(old_dir)
    suffixes = fsutil.find_sidecars(old_dir, old_base, listing)

    old_studio = scene.get("studio")
    forced = lib.forced_studio(old_dir)
    if forced and (old_studio or {}).get("name", "").casefold() != forced.casefold():
        found = studios.find(forced) if studios else None
        pending = getattr(studios, "pending", None)
        if not found and pending is not None:
            found = pending.get(forced.casefold())
        if not found:
            actions.append(Action("CREATE-STUDIO", forced, data=forced))
            found = {"id": None, "name": forced, "parent_studio": None}
            if pending is not None:
                pending[forced.casefold()] = found
        actions.append(Action("SET-STUDIO", f"{(old_studio or {}).get('name') or '(none)'} -> {forced}",
                              data={"scene_id": scene["id"], "studio": found}))
        scene = dict(scene, studio=found)
    studio = (scene.get("studio") or {}).get("name")
    new_dir = old_dir
    if lib.studio_folders and studio:
        comp = naming.folder_name(studio)
        if comp:
            new_dir = fsutil.resolve_dir(lib.root, comp)

    new_base = old_base
    title = (scene.get("title") or "").strip()
    if lib.rename and title:
        fields = {"studio": studio, "date": scene.get("date"), "title": title,
                  "performers": [p["name"] for p in scene.get("performers") or []]}
        quality = naming.quality_token(old_base, vf.get("width"), vf.get("height"), lib.default_source)
        built, truncated = naming.build_basename(fields, quality, new_dir, lib.max_path_len, old_base)
        if built is None:
            actions.append(Action("WARN", f"name too long even when shortened; not renamed: {old_name}", level="warning"))
        else:
            new_base = built
            if truncated:
                actions.append(Action("WARN", f"name shortened to fit length limits: {built}", level="warning"))
    elif lib.rename:
        actions.append(Action("SKIP", f"no title; keeping name {old_name}"))

    moving = new_dir != old_dir or new_base != old_base
    if moving:
        free, n = _free_basename(new_dir, old_dir, new_base, old_base, ext, suffixes, listing)
        if free is None:
            return actions + _skip(f"no free target name for {new_base}", "warning")
        if n > 1:
            actions.append(Action("WARN", f"target exists; using '{free}'", level="warning"))
        new_base = free

        if new_dir != old_dir and not os.path.isdir(new_dir):
            actions.append(Action("MKDIR", new_dir, dst=new_dir))
        dst = posixpath.join(new_dir, new_base + ext)
        actions.append(Action("MOVE", f"{path} -> {dst}", src=path, dst=dst,
                              data={"file_id": vf["id"], "folder": new_dir, "basename": new_base + ext}))

    final_suffixes = []
    old_names = {(old_base + s).casefold() for s in suffixes}
    for s in suffixes:
        mapped = fsutil.map_suffix(s)
        src = posixpath.join(old_dir, old_base + s)
        dst = posixpath.join(new_dir, new_base + mapped)
        in_place = new_dir == old_dir and new_base == old_base
        if mapped != s and in_place and (new_base + mapped).casefold() in old_names:
            actions.append(Action("WARN", f"{old_base + s}: fanart already exists; legacy file left", level="warning"))
            final_suffixes.append(s)
            continue
        if src != dst:
            actions.append(Action("RENAME-BG" if mapped != s else "RENAME", f"{src} -> {dst}", src=src, dst=dst))
        final_suffixes.append(mapped)

    existing_bytes = None
    existing = None
    nfo_ok = True
    nfo_old = posixpath.join(old_dir, old_base + ".nfo")
    if ".nfo" in suffixes:
        with open(nfo_old, "rb") as fh:
            existing_bytes = fh.read()
        existing = nfo.parse_existing(existing_bytes)
        if existing is None:
            nfo_ok = False
            actions.append(Action("WARN", f"{nfo_old}: unreadable XML; NFO left untouched", level="warning"))

    def art_name(kind):
        for s in final_suffixes:
            if fsutil.art_suffix_kind(s) == kind and posixpath.splitext(s)[1].lower() in fsutil.IMAGE_EXTS:
                return new_base + s
        return None

    screenshot = (scene.get("paths") or {}).get("screenshot") or ""
    can_art = bool(screenshot) and "default=true" not in screenshot
    poster = art_name("poster") or (f"{new_base}-poster.jpg" if can_art else None)
    fanart = art_name("fanart") or (f"{new_base}-fanart.jpg" if can_art else None)

    if nfo_ok:
        data = nfo.build(scene, title or old_base, existing, poster, fanart)
        nfo_new = posixpath.join(new_dir, new_base + ".nfo")
        if existing_bytes != data:
            how = "convert" if existing and existing["root"] != "movie" else ("update" if existing else "new")
            actions.append(Action("WRITE-NFO", f"{nfo_new} ({how})", dst=nfo_new, data=data))

    if can_art:
        for kind, name in (("poster", poster), ("fanart", fanart)):
            if not art_name(kind):
                dst = posixpath.join(new_dir, name)
                actions.append(Action(f"CREATE-{kind.upper()}", dst, src=screenshot, dst=dst, data=kind))
    elif not art_name("poster") or not art_name("fanart"):
        actions.append(Action("SKIP", "no screenshot to build missing poster/fanart from"))

    if forced and old_studio and old_studio.get("id") and old_studio["name"].casefold() != forced.casefold():
        actions.append(Action("DELETE-STUDIO", f"{old_studio['name']} (only if it has no scenes left)", data=old_studio["id"]))

    if moving:
        wid = nfo.whisparr_id(existing) if lib.whisparr else None
        if wid:
            whisparr.queue(wid)
            if whisparr.enabled:
                actions.append(Action("WHISPARR-RESCAN", whisparr.describe(wid), data=wid))
        if new_dir != old_dir and old_dir != lib.root:
            actions.append(Action("RMDIR", old_dir, dst=old_dir))
    return actions
