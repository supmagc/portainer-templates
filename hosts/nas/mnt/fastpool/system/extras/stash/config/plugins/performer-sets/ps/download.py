"""Selected performers → survey the sources → best candidates into the budget (evicting what they push out) →
deduplicated images."""
import collections
import math
import os
import posixpath
import statistics
import traceback

from . import apply, curate, imaging, layout
from .fetch import Blocked
from .index import FINAL
from .report import log
from .sources import get as get_source
from .sources import stashbox

SAMPLE = 3  # images fetched first: same set from another source? good enough to download?
UPGRADE_EDGE = 1.2  # a duplicate replaces the copy on disk with ≥ 20 % more resolution (and ≥ 80 % of the images)…
UPGRADE_COUNT = 1.5  # …or ≥ 50 % more images (and ≥ 90 % of the resolution)


class Downloader:
    def __init__(self, stash, cfg, fetcher, idx, report, dry):
        self.stash, self.cfg, self.fetcher, self.idx, self.report, self.dry = stash, cfg, fetcher, idx, report, dry
        self.sources = [get_source(name) for name in cfg.sources]
        self.blocked = set()
        self.scan_dirs = set()
        self.refresh = set()
        self.scorer = curate.Scorer(cfg, stash)
        self.soft_below = None
        self.upgraded = 0
        self.evicted = 0

    def run(self, performers):
        boxes = self.stash.stash_boxes() if self.cfg.promo_images and "stashbox" in self.cfg.promo_sources else {}
        self.soft_below = self.idx.soft_threshold(self.cfg.blur_percentile)
        total = len(performers)
        for i, p in enumerate(performers, 1):
            log("progress", (i - 1) / max(total, 1))
            who = f"{p['name']} [{p['id']}]"
            pdir = layout.performer_dir(self.cfg.root, p)
            try:
                if self.cfg.promo_images:
                    self.promo(p, pdir, boxes, who)
                for src in self.sources:
                    if src.NAME not in self.blocked:
                        self.survey(p, src, who)
                self.fill(p, pdir, who)
            except Exception as exc:  # noqa: BLE001
                self.report.emit("ERROR", who, f"{exc}\n{traceback.format_exc()}", "error")
        for folder in sorted(f for f in self.refresh if os.path.isdir(f)):  # not evicted since
            gallery = self.stash.gallery_for_folder(folder)
            if gallery:
                apply.apply_gallery(self.stash, gallery, self.report, self.dry)
        if not self.dry and (pruned := layout.prune_empty_dirs(self.cfg.root)):
            self.report.emit("PRUNED", None, f"{pruned} empty folder(s)")
        if self.scan_dirs and not self.dry:
            job = self.stash.scan(sorted(self.scan_dirs))
            self.report.emit("SCAN", None, f"queued scan job {job} for {len(self.scan_dirs)} performer folder(s)")
        if (self.upgraded or self.evicted) and not self.dry:
            self.report.emit("CLEAN", None, f"queued clean job {self.stash.clean([self.cfg.root])} for replaced/evicted sets")

    # images

    def save_image(self, set_id, folder, stem, im, referer, data=None, meta=None, exclude_set=None, ignore=frozenset()):
        """'done' | 'duplicate' | 'small' | 'bad'. exclude_set: only exact copies count within it; ignore: sets
        that don't count at all (the copy being replaced)."""
        if data is None:
            data = self.fetcher.get(im.url, referer=referer, image=True).content
        if meta is None:
            try:
                meta = imaging.inspect(data)
            except ValueError as exc:
                self.report.emit("BAD-IMAGE", None, f"{im.url}: {exc}", "warning")
                return "bad"
        if meta.long_edge < self.cfg.min_size:
            self.report.counts["small-image"] += 1
            return "small"
        if (self.idx.sets_with_sha(meta.sha256) - ignore
                or (self.cfg.distance > 0 and self.idx.near(meta, self.cfg.distance, exclude_set) - ignore)):
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

    def promo_refs(self, p, boxes):
        """(source name, iterator of (file stem, ImageRef, referer)) per enabled promo source."""
        for name in self.cfg.promo_sources:
            if name in self.blocked:
                continue
            if name == "stashbox":
                if boxes:
                    yield name, stashbox.promo_images(p, boxes, self.fetcher)
            else:
                yield name, get_source(name).promo_images(p, self.fetcher)

    def promo(self, p, pdir, boxes, who):
        """All promo sources share one folder: stems carry the source, the index tracks every image URL."""
        folder = posixpath.join(pdir, layout.PROMO)
        set_id = sidecar = None
        for name, refs in self.promo_refs(p, boxes):
            try:
                for stem, im, referer in refs:
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
                            "kind": "promo", "title": f"{p['name']} - Promo", "performer_id": p["id"], "performer": p["name"],
                            "images": {}}
                    status = self.save_image(set_id, folder, stem, im, referer)
                    self.idx.mark(im.url, status, set_id)
                    if status == "done":
                        sidecar["images"][stem] = im.url
                        layout.write_sidecar(folder, sidecar)
                        self.scan_dirs.add(pdir)
                        self.report.emit("PROMO", who, f"{stem} ← {im.url}")
            except Blocked as exc:
                self.blocked.add(name)
                self.report.emit("BLOCKED", who, f"{name} disabled for this run: {exc}", "warning")

    # sets: survey (set pages only, cached as candidates) → fill free budget slots with the best candidates

    def survey(self, p, src, who):
        """Fetch the set pages of up to max_sets new listing entries; once a listing was walked to the end it is
        skipped for recheck_days."""
        if self.idx.listing_fresh(p["id"], src.NAME, self.cfg.recheck_days):
            self.report.counts["listing-fresh"] += 1
            return
        keys = layout.performer_keys(p)
        new = 0
        try:
            for ref in src.list_sets(p, self.fetcher):
                if self.idx.status(ref.url) in FINAL or self.idx.has_candidate(ref.url):
                    continue
                if self.cfg.max_sets and new >= self.cfg.max_sets:
                    self.report.emit("LIMIT", who, f"{src.NAME}: surveyed {new} new sets, the rest next run (max_sets_per_source)")
                    return
                new += 1
                try:
                    self.survey_one(p, src, ref, keys, who)
                except Blocked:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.report.emit("ERROR", who, f"{ref.url}: {exc}\n{traceback.format_exc()}", "error")
            self.idx.mark_listing(p["id"], src.NAME)
        except Blocked as exc:
            self.blocked.add(src.NAME)
            self.report.emit("BLOCKED", who, f"{src.NAME} disabled for this run: {exc}", "warning")

    def survey_one(self, p, src, ref, keys, who):
        if self.stash:
            self.stash.keepalive()
        info = src.get_set(ref, self.fetcher)
        if not any(layout.norm(m) in keys for m in info.models):
            self.idx.mark(ref.url, "rejected")
            self.report.emit("NOT-PERFORMER", who, f"{ref.url} models: {', '.join(info.models) or 'none found'}", "debug")
            return
        images = [im for im in info.images if not (im.width and im.height and max(im.width, im.height) < self.cfg.min_size)]
        if not images:
            self.idx.mark(ref.url, "small")
            self.report.emit("SMALL-SET", who, f"{ref.url}: {len(info.images)} images, none ≥ {self.cfg.min_size}px")
            return
        info.images = images
        self.idx.add_candidate(p["id"], info)
        self.report.counts["candidate"] += 1

    def fill(self, p, pdir, who):
        """Candidates and downloaded sets compete for the budget: the best candidate inside it is downloaded, and
        only once it is on disk does the lowest downloaded set fall out (evicted). A candidate that turns out a
        duplicate or low quality costs nothing."""
        keys = layout.performer_keys(p)
        cands = self.idx.candidates(p["id"])
        for status, info in cands:
            if status == "partial" and not self.dry:  # its folder already counts as a downloaded set
                self.download_set(p, pdir, info, keys, who)
        galleries = {g["folder"]["path"]: g for g in self.stash.galleries_under(pdir)} if self.stash else {}
        open_ = [curate.candidate_item(info) for status, info in cands if status is None]
        if self.dry:
            kept = curate.kept_items(pdir, self.idx, galleries, self.soft_below)
            _chosen, out, queued = curate.plan(kept + open_, self.cfg, self.scorer)
            for item in queued:
                self.report.emit("WOULD-GET", who, f"score {item.score:g}, {item.images} images: {item.key}")
            for item in sorted(out, key=lambda i: i.key):
                curate.evict(item, self.idx, self.report, True, who)
            return
        self.trim(pdir, galleries, who)  # excluded, rated out or over a lowered budget: no replacement needed
        while open_:
            kept = curate.kept_items(pdir, self.idx, galleries, self.soft_below)
            _chosen, _out, queued = curate.plan(kept + open_, self.cfg, self.scorer)
            if not queued:
                break
            open_.remove(queued[0])
            if self.download_set(p, pdir, queued[0].info, keys, who) in ("done", "upgraded"):
                self.trim(pdir, galleries, who)

    def trim(self, pdir, galleries, who):
        """Evict the downloaded sets outside the budget (lowest score first out)."""
        kept = curate.kept_items(pdir, self.idx, galleries, self.soft_below)
        _chosen, out, _queued = curate.plan(kept, self.cfg, self.scorer)
        for item in sorted(out, key=lambda i: i.key):
            curate.evict(item, self.idx, self.report, self.dry, who)
        self.evicted += len(out)

    def download_set(self, p, pdir, info, keys, who):
        """'done' | 'upgraded' | 'duplicate' | 'low-quality' | 'error' (only 'done' takes a free slot)."""
        if info.source in self.blocked:
            return "error"
        if self.stash:
            self.stash.keepalive()
        try:
            return self.one_set(p, pdir, info, keys, who)
        except Blocked as exc:
            self.blocked.add(info.source)
            self.report.emit("BLOCKED", who, f"{info.source} disabled for this run: {exc}", "warning")
        except Exception as exc:  # noqa: BLE001
            self.report.emit("ERROR", who, f"{info.url}: {exc}\n{traceback.format_exc()}", "error")
        return "error"

    def one_set(self, p, pdir, info, keys, who):
        """'done' | 'upgraded' | 'duplicate' | 'low-quality'."""
        others = [m for m in info.models if layout.norm(m) not in keys]
        images = info.images
        referer = get_source(info.source).referer(info)
        resumed = self.idx.folder(self.idx.set_of(info.url)) if self.idx.status(info.url) == "partial" else None
        folder = resumed if resumed and os.path.isdir(resumed) else posixpath.join(pdir, layout.set_dir_name(info))
        sample = []
        for im in images[:SAMPLE]:
            data = self.fetcher.get(im.url, referer=referer, image=True).content
            try:
                sample.append((im, data, imaging.inspect(data)))
            except ValueError:
                continue
        own = self.idx.find_set(folder)
        dup = self.duplicate_of(sample, own) if sample else None
        replaced = self.upgrade_of(dup, info, sample) if dup is not None else None
        if dup is not None and replaced is None:
            old = self.idx.folder(dup)
            if old and layout.add_sidecar_url(old, info.url):
                self.refresh.add(old)
            self.idx.mark(info.url, "duplicate", dup)
            self.report.emit("DUPLICATE-SET", who, f"{info.url} = {old}")
            return "duplicate"
        if own is None and replaced is None and sample and self.low_quality(sample):
            edge, sharp = _medians([m for _im, _d, m in sample])
            self.idx.mark(info.url, "low-quality")
            self.report.emit("LOW-QUALITY", who, f"{info.url}: sample {edge}px, sharpness {sharp:g}")
            return "low-quality"

        old_sc = layout.read_sidecar(replaced) if replaced else None
        if replaced and not resumed:
            folder = posixpath.join(posixpath.dirname(replaced), layout.set_dir_name(info))
        owner = old_sc or {"performer_id": p["id"], "performer": p["name"]}
        if str(owner["performer_id"]) != str(p["id"]):  # the copy being replaced belongs to another performer's folder
            others = [m for m in info.models if layout.norm(m) != layout.norm(owner["performer"])]
        layout.ensure_dir(folder, self.cfg.root)
        set_id = self.idx.set_id(folder)
        sidecar = layout.read_sidecar(folder) or {
            "kind": "set", "source": info.source, "source_id": info.source_id,
            "urls": [info.url, *[u for u in (old_sc or {}).get("urls", []) if u != info.url]],
            "title": info.title, "studio": info.studio, "date": info.date, "posted": info.posted,
            "performer_id": owner["performer_id"], "performer": owner["performer"], "other_performers": others,
            "source_tags": info.tags, "source_image_count": len(info.images)}
        layout.write_sidecar(folder, sidecar)
        self.idx.mark(info.url, "partial", set_id)

        ignore = frozenset([dup]) if replaced else frozenset()
        cached = {im.url: (data, meta) for im, data, meta in sample}
        saved = 0
        for n, im in enumerate(images, 1):
            stem = f"{n:03d}"
            if any(os.path.exists(posixpath.join(folder, stem + ext)) for ext in imaging.EXTS.values()):
                saved += 1
                continue
            data, meta = cached.get(im.url, (None, None))
            if self.save_image(set_id, folder, stem, im, referer, data, meta, exclude_set=set_id, ignore=ignore) == "done":
                saved += 1
        if saved == 0:
            os.unlink(posixpath.join(folder, layout.SIDECAR))
            os.rmdir(folder)
            self.idx.mark(info.url, "duplicate", dup)
            self.report.emit("DUPLICATE-SET", who, f"{info.url}: every image already downloaded elsewhere")
            return "duplicate"
        sidecar["quality"] = self.quality(set_id)
        layout.write_sidecar(folder, sidecar)
        self.idx.mark(info.url, "done", set_id)
        self.scan_dirs.add(posixpath.dirname(folder))
        if replaced:
            curate.delete_set_folder(replaced, self.report, who)
            self.idx.drop_set(dup)
            for url in (old_sc or {}).get("urls", []):
                self.idx.mark(url, "duplicate", set_id)
            self.upgraded += 1
            self.report.emit("UPGRADE", who, f"{saved}/{len(images)} images → {folder} (replaces {posixpath.basename(replaced)})")
            return "upgraded"
        self.report.emit("SET", who, f"{saved}/{len(images)} images → {folder}")
        return "done"

    def upgrade_of(self, dup, info, sample):
        """Folder of the duplicate to replace when this copy is clearly better, else None. Evicted sets and
        galleries you rated or marked organized are never replaced."""
        old = self.idx.folder(dup)
        if not old or not os.path.isdir(old) or self.idx.is_evicted(dup) or not sample:
            return None
        old_edge, _ = curate.medians(self.idx.stats(dup))
        new_edge, _ = _medians([m for _im, _d, m in sample])
        old_n, new_n = len(curate.image_files(old)), len(info.images)
        better = ((old_edge and new_edge >= old_edge * UPGRADE_EDGE and new_n >= old_n * 0.8)
                  or (new_n >= old_n * UPGRADE_COUNT and new_edge >= old_edge * 0.9))
        if not better:
            return None
        g = self.stash.gallery_for_folder(old) if self.stash else None
        if g and (g.get("organized") or g.get("rating100")):
            return None
        return old

    def low_quality(self, sample):
        edge, sharp = _medians([m for _im, _d, m in sample])
        return edge < self.cfg.min_size or (self.soft_below is not None and sharp < self.soft_below)

    def quality(self, set_id):
        edge, sharp = curate.medians(self.idx.stats(set_id))
        return {"edge": edge, "sharpness": sharp, "soft": curate.is_soft(sharp, self.soft_below)}


def _medians(metas):
    """(median long edge, median sharpness) of image Metas."""
    if not metas:
        return 0, 0.0
    return int(statistics.median(m.long_edge for m in metas)), float(statistics.median(m.sharpness for m in metas))

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
            if os.path.splitext(name)[1].lower() not in imaging.FILE_EXTS:
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
