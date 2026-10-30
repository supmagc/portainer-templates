"""Selected scenes → their source's photo set (members' zips when logged in, else the public photos) → one folder
per scene with set.json."""
import os
import posixpath
import re
import tempfile
import traceback
import zipfile

from . import imaging, layout
from .fetch import Blocked
from .index import FINAL
from .report import log
from .sources import Context
from .sources import get as get_source
from .sources import match as match_source

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_SKIP_ENTRY = re.compile(r"(^|/)(performer-\d+\.\w+|__MACOSX/.*|\..*)$", re.I)  # model headshots, archive junk
CHUNK = 1 << 20


def redact(text):
    """Signed URLs (zips, VMG photos) must not end up in the log or report."""
    return re.sub(r"\?\S*", "?…", str(text))


def _natural(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


class Downloader:
    def __init__(self, stash, cfg, fetcher, cdp, idx, report, dry):
        self.stash, self.cfg, self.fetcher, self.idx, self.report, self.dry = stash, cfg, fetcher, idx, report, dry
        self.ctx = Context(fetcher, cdp, cfg.kink_session)
        self.sources = [get_source(name) for name in cfg.sources]
        self.blocked = set()
        self.scan_dirs = set()
        # a public-photo Kink set is a "preview": upgraded to the members' set once a session is configured
        self.final = FINAL if cfg.kink_session else FINAL | {"preview"}

    def run(self, scenes):
        existing = layout.existing_sets(self.cfg.root)
        total = len(scenes)
        for i, scene in enumerate(scenes, 1):
            log("progress", (i - 1) / max(total, 1))
            who = f"{scene.get('title') or 'untitled'} [scene {scene['id']}]"
            hit = match_source(scene.get("urls"), self.sources)
            if not hit:
                self.report.counts["no-source"] += 1
                continue
            src, url = hit
            status = self.idx.status(url)
            if status in self.final:
                self.report.counts[f"skip-{status}"] += 1
                continue
            if src.NAME in self.blocked:
                self.report.counts["skip-blocked"] += 1
                continue
            if scene.get("galleries") and status not in ("partial", "preview"):
                self.report.emit("HAS-GALLERY", who, f"{url}: scene already has a gallery", "debug")
                continue
            try:
                self.one(scene, src, url, existing, who)
            except Blocked as exc:
                self.blocked.add(src.NAME)
                self.report.emit("BLOCKED", who, f"{src.NAME} disabled for this run: {redact(exc)}", "warning")
            except Exception as exc:  # noqa: BLE001
                self.report.emit("ERROR", who, redact(f"{url}: {exc}\n{traceback.format_exc()}"), "error")
        if self.scan_dirs and not self.dry:
            job = self.stash.scan(sorted(self.scan_dirs))
            self.report.emit("SCAN", None, f"queued scan job {job} for {len(self.scan_dirs)} studio folder(s)")

    def one(self, scene, src, url, existing, who):
        self.stash.keepalive()
        known = src.key(url)
        if known and self._hand_made(existing.get(known), url, scene, who):
            return
        info = src.fetch(url, self.ctx)
        if info is None:
            self.idx.mark(url, "gone", scene["id"])
            self.report.emit("GONE", who, f"{url}: no longer on the site")
            return
        if info.logged_in is False:
            self.ctx.kink_session = None
            self.report.emit("NOT-LOGGED-IN", who, "Kink session cookie rejected (expired?): public photos only for the "
                             "rest of this run; update the kink_cookie setting", "warning")
        archives = info.archives if info.logged_in else []
        if len(archives) == 1 and archives[0].grabs:  # a lone frame-grab zip isn't a photo set: public photos instead
            archives = []
        images = [im for im in info.images if not (im.width and im.height and max(im.width, im.height) < self.cfg.min_size)]
        if not archives and not images:
            self.idx.mark(url, "small" if info.images else "empty", scene["id"])
            self.report.emit("NO-PHOTOS", who, f"{url}: {len(info.images)} public photos, none ≥ {self.cfg.min_size}px")
            return
        folder = existing.get(info.key)
        if self._hand_made(folder, url, scene, who):
            return
        folder = folder or layout.set_folder(self.cfg.root, scene, info)
        if self.dry:
            what = f"members' {', '.join(a.name for a in archives)}" if archives else f"{len(images)} public photos"
            self.report.emit("WOULD-GET", who, f"{what} → {folder}")
            return
        if archives:
            self.download_member(scene, info, archives, folder, who)
        else:
            # a Kink set from the public photos can still become a members' set; "logged in, no zips" can't
            preview = info.source == "kink" and info.site == "kink" and not info.logged_in
            self.download_public(scene, info, images, folder, who, "preview" if preview else "done")
        existing[info.key] = folder

    def _hand_made(self, folder, url, scene, who):
        """A set folder for this key that this plugin didn't make (no "images" map in set.json): leave it alone."""
        if not folder or isinstance((layout.read_sidecar(folder) or {}).get("images"), dict):
            return False
        self.idx.mark(url, "exists", scene["id"], folder)
        self.report.emit("EXISTS", who, f"{url}: set already on disk ({folder})")
        return True

    # writing

    def _start(self, scene, info, folder):
        layout.ensure_dir(folder, self.cfg.root)
        sidecar = layout.read_sidecar(folder) or sidecar_for(scene, info)
        layout.write_sidecar(folder, sidecar)
        self.idx.mark(info.url, "partial", scene["id"], folder)
        return sidecar

    def _exists(self, folder, stem):
        return any(os.path.exists(posixpath.join(folder, stem + ext)) for ext in imaging.EXTS.values())

    def _save(self, folder, stem, data, origin, sidecar, who):
        try:
            ext, w, h = imaging.inspect(data)
        except ValueError as exc:
            self.report.emit("BAD-IMAGE", who, f"{redact(origin)}: {exc}", "warning")
            return False
        if max(w, h) < self.cfg.min_size:
            self.report.counts["small-image"] += 1
            return False
        layout.atomic_write(posixpath.join(folder, stem + ext), data)
        sidecar["images"][stem] = origin.split("?")[0]  # signed URLs expire; the path identifies the photo
        layout.write_sidecar(folder, sidecar)
        self.report.counts["image"] += 1
        return True

    def _finish(self, scene, info, folder, saved, total, status, who, what):
        if saved == 0:
            for name in os.listdir(folder):
                os.unlink(posixpath.join(folder, name))
            os.rmdir(folder)
            if not os.listdir(posixpath.dirname(folder)):
                os.rmdir(posixpath.dirname(folder))
            self.idx.mark(info.url, "small", scene["id"])
            self.report.emit("NO-PHOTOS", who, f"{info.url}: no photo ≥ {self.cfg.min_size}px after download")
            return
        self.idx.mark(info.url, status, scene["id"], folder)
        self.scan_dirs.add(posixpath.dirname(folder))
        self.report.emit("SET", who, f"{what}: {saved}/{total} photos → {folder}")

    def download_public(self, scene, info, images, folder, who, status):
        sidecar = self._start(scene, info, folder)
        sidecar["source_image_count"] = len(info.images)
        saved = 0
        for n, im in enumerate(images, 1):
            stem = f"{n:03d}"
            if self._exists(folder, stem):
                saved += 1
                continue
            data = self.fetcher.get(im.url, referer=info.url).content
            saved += self._save(folder, stem, data, im.url, sidecar, who)
        self._finish(scene, info, folder, saved, len(images), status, who, "public")

    def download_member(self, scene, info, archives, folder, who):
        sidecar = self._start(scene, info, folder)
        if not sidecar.get("member"):  # replaces a preview set: its public photos go
            for stem in list(sidecar["images"]):
                for ext in imaging.EXTS.values():
                    path = posixpath.join(folder, stem + ext)
                    if os.path.exists(path):
                        os.unlink(path)
            sidecar.update(images={}, member=True)
        sidecar["archives"] = [a.name for a in archives]
        layout.write_sidecar(folder, sidecar)
        n = saved = 0
        for a in archives:
            with tempfile.TemporaryFile() as tmp:
                with self.fetcher.get(a.url, referer=info.url, stream=True) as r:
                    for chunk in r.iter_content(CHUNK):
                        tmp.write(chunk)
                with zipfile.ZipFile(tmp) as z:
                    entries = sorted((e.filename for e in z.infolist() if not e.is_dir()
                                      and e.filename.lower().endswith(IMAGE_EXTS) and not _SKIP_ENTRY.search(e.filename)),
                                     key=_natural)
                    for entry in entries:
                        n += 1
                        stem = f"{n:03d}"
                        if self._exists(folder, stem):
                            saved += 1
                            continue
                        saved += self._save(folder, stem, z.read(entry), f"{a.name}/{entry}", sidecar, who)
                self.stash.keepalive()
        sidecar["source_image_count"] = n
        layout.write_sidecar(folder, sidecar)
        self._finish(scene, info, folder, saved, n, "done", who, "members' set")


def sidecar_for(scene, info):
    """set.json of a new scene set; same shape as the hand-made Kink sets (plus site, images, source extras)."""
    return {
        "kind": "scene", "source": info.source, "site": info.site, "source_id": info.source_id,
        "urls": [info.url], "title": scene.get("title") or info.title,
        "studio": (scene.get("studio") or {}).get("name"), "date": scene.get("date"), "posted": None,
        "scene_id": scene["id"], "performers": [p["name"] for p in scene.get("performers") or []],
        "director": scene.get("director") or None, "details": scene.get("details") or None,
        "source_tags": info.tags, "source_image_count": len(info.images), **info.extra, "member": False, "images": {},
    }
