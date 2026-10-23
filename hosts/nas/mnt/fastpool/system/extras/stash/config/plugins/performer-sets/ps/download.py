"""Selected performers → sources → new sets → deduplicated images in one folder per set."""
import collections
import math
import os
import posixpath
import traceback

from . import apply, imaging, layout
from .fetch import Blocked
from .index import FINAL
from .report import log
from .sources import get as get_source
from .sources import stashbox

SAMPLE = 3  # images fetched first to recognise a set we already have from another source


class Downloader:
    def __init__(self, stash, cfg, fetcher, idx, report, dry):
        self.stash, self.cfg, self.fetcher, self.idx, self.report, self.dry = stash, cfg, fetcher, idx, report, dry
        self.sources = [get_source(name) for name in cfg.sources]
        self.blocked = set()
        self.scan_dirs = set()
        self.refresh = set()

    def run(self, performers):
        boxes = self.stash.stash_boxes() if self.cfg.promo_images else {}
        total = len(performers)
        for i, p in enumerate(performers, 1):
            log("progress", (i - 1) / max(total, 1))
            who = f"{p['name']} [{p['id']}]"
            pdir = layout.performer_dir(self.cfg.root, p)
            try:
                if boxes:
                    self.promo(p, pdir, boxes, who)
                for src in self.sources:
                    if src.NAME not in self.blocked:
                        self.sets(p, pdir, src, who)
            except Exception as exc:  # noqa: BLE001
                self.report.emit("ERROR", who, f"{exc}\n{traceback.format_exc()}", "error")
        for folder in sorted(self.refresh):
            gallery = self.stash.gallery_for_folder(folder)
            if gallery:
                apply.apply_gallery(self.stash, self.cfg, gallery, self.report, self.dry)
        if self.scan_dirs and not self.dry:
            job = self.stash.scan(sorted(self.scan_dirs))
            self.report.emit("SCAN", None, f"queued scan job {job} for {len(self.scan_dirs)} performer folder(s)")

    # images

    def save_image(self, set_id, folder, stem, im, referer, data=None, meta=None, exclude_set=None):
        """'done' | 'duplicate' | 'small' | 'bad'."""
        if data is None:
            data = self.fetcher.get(im.url, referer=referer).content
        if meta is None:
            try:
                meta = imaging.inspect(data)
            except ValueError as exc:
                self.report.emit("BAD-IMAGE", None, f"{im.url}: {exc}", "warning")
                return "bad"
        if meta.long_edge < self.cfg.min_size:
            self.report.counts["small-image"] += 1
            return "small"
        if self.idx.has_sha(meta.sha256) or (self.cfg.distance > 0 and self.idx.near(meta, self.cfg.distance, exclude_set)):
            self.report.counts["duplicate-image"] += 1
            return "duplicate"
        path = posixpath.join(folder, stem + meta.ext)
        layout.atomic_write(path, data)
        self.idx.add_image(set_id, path, meta)
        self.report.counts["image"] += 1
        return "done"

    def duplicate_of(self, sample, own_set=None):
        """Existing set id (not own_set: a resumed download) that ≥ 2/3 of the sampled images belong to, else None."""
        votes = collections.Counter()
        for _im, _data, meta in sample:
            hits = self.idx.sets_with_sha(meta.sha256)
            if self.cfg.distance > 0:
                hits |= self.idx.near(meta, self.cfg.distance)
            votes.update(hits - {own_set})
        if not votes:
            return None
        set_id, n = votes.most_common(1)[0]
        return set_id if n >= math.ceil(len(sample) * 2 / 3) else None

    # promo images

    def promo(self, p, pdir, boxes, who):
        folder = posixpath.join(pdir, layout.PROMO)
        set_id = sidecar = None
        for stem, im in stashbox.promo_images(p, boxes, self.fetcher):
            if self.idx.status(im.url) in FINAL:
                continue
            if self.dry:
                self.report.emit("NEW-PROMO", who, im.url)
                continue
            if im.width and im.height and max(im.width, im.height) < self.cfg.min_size:
                self.idx.mark(im.url, "small")
                self.report.counts["small-image"] += 1
                continue
            if set_id is None:
                layout.ensure_dir(folder, self.cfg.root)
                set_id = self.idx.set_id(folder)
                sidecar = layout.read_sidecar(folder) or {
                    "kind": "promo", "title": f"{p['name']} - Promo", "performer_id": p["id"], "performer": p["name"], "images": {}}
            status = self.save_image(set_id, folder, stem, im, None)
            self.idx.mark(im.url, status, set_id)
            if status == "done":
                sidecar["images"][stem] = im.url
                layout.write_sidecar(folder, sidecar)
                self.scan_dirs.add(pdir)
                self.report.emit("PROMO", who, f"{stem} ← {im.url}")

    # sets

    def sets(self, p, pdir, src, who):
        keys = layout.performer_keys(p)
        new = 0
        try:
            for ref in src.list_sets(p, self.fetcher):
                if self.idx.status(ref.url) in FINAL:
                    continue
                if self.cfg.max_sets and new >= self.cfg.max_sets:
                    self.report.emit("LIMIT", who, f"{src.NAME}: stopped at {new} new sets (max_sets_per_source)")
                    return
                new += 1
                if self.dry:
                    self.report.emit("NEW-SET", who, ref.url)
                    continue
                try:
                    self.one_set(p, pdir, src, ref, keys, who)
                except Blocked:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.report.emit("ERROR", who, f"{ref.url}: {exc}\n{traceback.format_exc()}", "error")
        except Blocked as exc:
            self.blocked.add(src.NAME)
            self.report.emit("BLOCKED", who, f"{src.NAME} disabled for this run: {exc}", "warning")

    def one_set(self, p, pdir, src, ref, keys, who):
        info = src.get_set(ref, self.fetcher)
        others = [m for m in info.models if layout.norm(m) not in keys]
        if len(others) == len(info.models):
            self.idx.mark(ref.url, "rejected")
            self.report.emit("NOT-PERFORMER", who, f"{ref.url} models: {', '.join(info.models) or 'none found'}", "debug")
            return
        images = [im for im in info.images if not (im.width and im.height and max(im.width, im.height) < self.cfg.min_size)]
        if not images:
            self.idx.mark(ref.url, "small")
            self.report.emit("SMALL-SET", who, f"{ref.url}: {len(info.images)} images, none ≥ {self.cfg.min_size}px")
            return

        referer = src.referer(info)
        folder = posixpath.join(pdir, layout.set_dir_name(info))
        sample = []
        for im in images[:SAMPLE]:
            data = self.fetcher.get(im.url, referer=referer).content
            try:
                sample.append((im, data, imaging.inspect(data)))
            except ValueError:
                continue
        dup = self.duplicate_of(sample, self.idx.find_set(folder)) if sample else None
        if dup is not None:
            folder = self.idx.folder(dup)
            if folder and layout.add_sidecar_url(folder, ref.url):
                self.refresh.add(folder)
            self.idx.mark(ref.url, "duplicate", dup)
            self.report.emit("DUPLICATE-SET", who, f"{ref.url} = {folder}")
            return

        layout.ensure_dir(folder, self.cfg.root)
        set_id = self.idx.set_id(folder)
        if layout.read_sidecar(folder) is None:
            layout.write_sidecar(folder, {
                "kind": "set", "source": info.source, "source_id": info.source_id, "urls": [info.url],
                "title": info.title, "studio": info.studio, "date": info.date, "posted": info.posted,
                "performer_id": p["id"], "performer": p["name"], "other_performers": others,
                "source_tags": info.tags, "source_image_count": len(info.images)})
        self.idx.mark(ref.url, "partial", set_id)

        cached = {im.url: (data, meta) for im, data, meta in sample}
        saved = 0
        for n, im in enumerate(images, 1):
            stem = f"{n:03d}"
            if any(os.path.exists(posixpath.join(folder, stem + ext)) for ext in imaging.EXTS.values()):
                saved += 1
                continue
            data, meta = cached.get(im.url, (None, None))
            if self.save_image(set_id, folder, stem, im, referer, data, meta, exclude_set=set_id) == "done":
                saved += 1
        if saved == 0:
            os.unlink(posixpath.join(folder, layout.SIDECAR))
            os.rmdir(folder)
            self.idx.mark(ref.url, "duplicate")
            self.report.emit("DUPLICATE-SET", who, f"{ref.url}: every image already downloaded elsewhere")
            return
        self.idx.mark(ref.url, "done", set_id)
        self.scan_dirs.add(pdir)
        self.report.emit("SET", who, f"{saved}/{len(images)} images → {folder}")


def reindex(cfg, idx, report):
    """Rebuild the index from disk: every folder with a set.json is a set, its URLs count as done."""
    idx.reset()
    for dirpath, dirnames, filenames in os.walk(cfg.root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        sidecar = layout.read_sidecar(dirpath) if layout.SIDECAR in filenames else None
        if sidecar is None:
            continue
        set_id = idx.set_id(dirpath)
        for url in [*(sidecar.get("urls") or []), *(sidecar.get("images") or {}).values()]:
            idx.mark(url, "done", set_id)
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in set(imaging.EXTS.values()) | {".jpeg"}:
                continue
            path = posixpath.join(dirpath, name)
            with open(path, "rb") as fh:
                data = fh.read()
            try:
                idx.add_image(set_id, path, imaging.inspect(data))
                report.counts["image"] += 1
            except ValueError:
                report.emit("BAD-IMAGE", None, path, "warning")
        report.counts["set"] += 1
