"""Per-performer set budget: score downloaded sets and surveyed candidates the same way, keep the best.

Downloads only fill free slots; the Curate task measures sharpness, evicts downloaded sets that fall outside the
budget and re-tags the rest."""
import collections
import io
import os
import posixpath
import statistics
from dataclasses import dataclass, field

from PIL import Image

from . import apply, imaging, layout
from .report import log
from .sources import get as get_source

TAG_SCORE_CAP = 6  # tag weights add up, but never beyond ±6 (the other factors still matter)


@dataclass
class Item:
    kind: str  # "kept" (folder on disk) | "candidate" (surveyed set page)
    key: str  # folder or source URL
    studio: str = None
    models: int = 1
    images: int = 0
    long_edge: int = 0  # median, 0 = unknown
    tags: list = field(default_factory=list)  # source categories
    tag_ids: set = field(default_factory=set)  # Stash tags on the gallery (kept)
    sharpness: float = 0.0  # median, 0 = unknown
    soft: bool = False
    rating100: int = None  # Stash gallery rating (kept)
    organized: bool = False
    size: int = 0  # bytes on disk (kept)
    set_id: int = None
    info: object = None  # SetInfo (candidate)
    score: float = None


def image_files(folder):
    return [posixpath.join(folder, n) for n in sorted(os.listdir(folder))
            if os.path.splitext(n)[1].lower() in imaging.FILE_EXTS]


def set_folders(pdir):
    """(folder, set.json) of the photo sets (not _promo) in a performer folder."""
    out = []
    for name in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        folder = posixpath.join(pdir, name)
        sc = layout.read_sidecar(folder) if os.path.isdir(folder) else None
        if sc and sc.get("kind") == "set":
            out.append((folder, sc))
    return out


def medians(rows):
    """(median long edge, median sharpness) of index rows (width, height, sharpness or None)."""
    edges = [max(w, h) for w, h, _s in rows if w and h]
    sharp = [x for _w, _h, x in rows if x is not None]
    return (int(statistics.median(edges)) if edges else 0), (float(statistics.median(sharp)) if sharp else 0.0)


def is_soft(sharpness, soft_below):
    return bool(sharpness and soft_below is not None and sharpness < soft_below)


def kept_items(pdir, idx, galleries=None, soft_below=None):
    """Set folders of one performer; galleries (folder → Stash gallery) adds ratings and tags for Curate."""
    out = []
    for folder, sc in set_folders(pdir):
        files = image_files(folder)
        set_id = idx.find_set(folder)
        edge, sharp = medians(idx.stats(set_id)) if set_id else (0, 0.0)
        g = (galleries or {}).get(folder) or {}
        out.append(Item("kept", folder, sc.get("studio"), 1 + len(sc.get("other_performers") or []), len(files), edge,
                        sc.get("source_tags") or [], {t["id"] for t in g.get("tags") or []}, sharp,
                        is_soft(sharp, soft_below), g.get("rating100"), bool(g.get("organized")),
                        sum(os.path.getsize(f) for f in files), set_id))
    return out


def candidate_item(info):
    sizes = getattr(get_source(info.source), "SIZES_RELIABLE", True)
    edges = [max(i.width, i.height) for i in info.images if i.width and i.height] if sizes else []
    return Item("candidate", info.url, info.studio, len(info.models) or 1, len(info.images),
                int(statistics.median(edges)) if edges else 0, info.tags or [], info=info)


def analyze(pdir, idx, report):
    """Measure sharpness for images indexed before v0.4 (one read per image, once)."""
    for folder, _sc in set_folders(pdir):
        set_id = idx.find_set(folder)
        for image_id, path in idx.unmeasured(set_id) if set_id else []:
            try:
                with open(path, "rb") as fh, Image.open(io.BytesIO(fh.read())) as img:
                    img.load()
                    idx.set_sharpness(image_id, imaging.sharpness(img))
                report.counts["measured"] += 1
            except (OSError, Image.DecompressionBombError):
                report.counts["unreadable"] += 1
        idx.commit()


class TagWeights:
    """The tag_weights setting. An entry matches a source category of the same name, or a Stash tag that is the
    entry's tag or one of its sub-tags: a category resolved by name/alias, or (downloaded sets) a gallery tag.
    Hearted Stash tags (and their sub-tags) count with favorite_weight, unless tag_weights names them."""

    def __init__(self, entries, stash=None, favorite_weight=0.0):
        self.entries = entries
        self.stash = stash
        self.ids = {i: stash.lookup("tag", name) for i, (name, _w) in enumerate(entries)} if stash else {}
        self.favorites = set()
        if stash and favorite_weight:
            self.favorites = stash.favorite_tags() - set(self.ids.values())
        self.favorite_weight = favorite_weight

    def match(self, names, tag_ids=()):
        """(excluded, summed weight)."""
        if not self.entries and not self.favorites:
            return False, 0.0
        keys = {layout.norm(n) for n in names}
        lineage = set()
        if self.stash:
            ids = set(tag_ids) | {self.stash.lookup("tag", n) for n in names}
            for tid in ids - {None}:
                lineage |= self.stash.tag_lineage(tid)
        hits = [w for i, (name, w) in enumerate(self.entries)
                if layout.norm(name) in keys or (self.ids.get(i) and self.ids[i] in lineage)]
        hits += [self.favorite_weight] * len(self.favorites & lineage)
        if any(w is None for w in hits):
            return True, 0.0
        return False, max(-TAG_SCORE_CAP, min(TAG_SCORE_CAP, sum(hits)))


class Scorer:
    def __init__(self, cfg, stash=None, studio_info=None):
        self.cfg = cfg
        self.studio_info = studio_info or (stash.studio_info if stash else (lambda _name: None))
        self.tags = TagWeights(cfg.tag_weights, stash, cfg.favorite_tag_weight)

    def score(self, item):
        """None = excluded (studio or tag), else higher is better."""
        studio = layout.norm(item.studio or "")
        if studio and studio in {layout.norm(s) for s in self.cfg.exclude_studios}:
            return None
        excluded, tag_score = self.tags.match(item.tags, item.tag_ids)
        if excluded:
            return None
        s = {1: 3.0, 2: 1.0, 3: 0.0}.get(item.models, -2.0)
        s += -3.0 if item.images < 8 else 1.0 if item.images >= 12 else 0.0
        if item.long_edge:
            s += next(v for edge, v in ((2000, 3.0), (1600, 2.0), (1200, 0.0), (1000, -1.0), (0, -3.0)) if item.long_edge >= edge)
        if item.soft:
            s -= 4.0
        st = self.studio_info(item.studio) if item.studio else None
        if st:
            s += 4.0 if st.get("favorite") else 0.0
            s += st["rating100"] / 20 - 3 if st.get("rating100") else 0.0
        s += tag_score
        if item.kind == "kept":
            s += 0.5  # ties keep what is already there
        return s


def protected(item, cfg):
    return item.kind == "kept" and (item.organized or (item.rating100 or 0) > cfg.evict_rating100)


def rejected_by_rating(item, cfg):
    return (item.kind == "kept" and not item.organized and cfg.evict_rating100 > 0
            and item.rating100 is not None and item.rating100 <= cfg.evict_rating100)


def rank(items, cfg, scorer):
    """Items in the order they fill the budget: protected first, then by score with at most max_per_studio per
    studio, then the studio overflow (a free slot is never left empty). Excluded and low-rated items are left out."""
    for i in items:
        i.score = scorer.score(i)
    first = [i for i in items if protected(i, cfg)]
    pool = sorted((i for i in items if not protected(i, cfg) and not rejected_by_rating(i, cfg) and i.score is not None),
                  key=lambda i: (-i.score, i.kind != "kept", -i.images, i.key))
    per_studio = collections.Counter(layout.norm(i.studio or "") for i in first)
    capped, overflow = [], []
    for i in pool:
        k = layout.norm(i.studio or "")
        if k and cfg.max_per_studio and per_studio[k] >= cfg.max_per_studio:
            overflow.append(i)
        else:
            capped.append(i)
            per_studio[k] += 1
    return first + capped + overflow


def plan(items, cfg, scorer):
    """(chosen, evict, queued): the best budget's worth, downloaded sets outside it, candidates inside it."""
    ranked = rank(items, cfg, scorer)
    n = max(cfg.max_per_performer or len(ranked), sum(protected(i, cfg) for i in items))
    chosen = ranked[:n]
    ids = {id(i) for i in chosen}
    return (chosen, [i for i in items if i.kind == "kept" and id(i) not in ids],
            [i for i in chosen if i.kind == "candidate"])


def delete_set_folder(folder, report, who):
    for f in image_files(folder):
        os.unlink(f)
    sidecar = posixpath.join(folder, layout.SIDECAR)
    if os.path.exists(sidecar):
        os.unlink(sidecar)
    try:
        os.rmdir(folder)
    except OSError:
        report.emit("EVICT-LEFTOVER", who, f"{folder}: other files left in place", "warning")


def evict(item, idx, report, dry, who):
    """Delete a set folder; its URLs become 'evicted' so no source fetches it again."""
    report.emit("EVICT", who, f"score {item.score if item.score is not None else 'excluded'}, "
                              f"{item.size / 1e6:.0f} MB: {posixpath.basename(item.key)}")
    if dry:
        return
    sc = layout.read_sidecar(item.key) or {}
    delete_set_folder(item.key, report, who)
    for url in sc.get("urls") or []:
        idx.mark(url, "evicted", item.set_id)
    if item.set_id:
        idx.evict_set(item.set_id)


def curate_all(stash, cfg, idx, report, dry):
    """Apply the budget to every performer folder on disk (selected or not), then re-tag the sets that stay."""
    galleries = {g["folder"]["path"]: g for g in stash.galleries_under(cfg.root)}
    dirs = layout.performer_dirs(cfg.root)
    for n, (_pid, pdir) in enumerate(dirs, 1):
        log("progress", (n - 1) / max(len(dirs), 1) / 2)
        stash.keepalive()
        analyze(pdir, idx, report)
    soft_below = idx.soft_threshold(cfg.blur_percentile)
    report.emit("SOFT-BELOW", None, f"sharpness {soft_below:g} ({cfg.blur_percentile:g}th percentile of set medians)"
                if soft_below is not None else "not enough measured sets yet: blur check off")
    scorer = Scorer(cfg, stash)
    evicted = freed = 0
    for n, (pid, pdir) in enumerate(dirs, 1):
        log("progress", 0.5 + (n - 1) / max(len(dirs), 1) / 2)
        who = posixpath.basename(pdir)
        kept = kept_items(pdir, idx, galleries, soft_below)
        cands = [candidate_item(info) for status, info in idx.candidates(pid) if status is None]
        _chosen, out, queued = plan(kept + cands, cfg, scorer)
        for item in sorted(out, key=lambda i: i.key):
            evict(item, idx, report, dry, who)
        evicted += len(out)
        freed += sum(i.size for i in out)
        report.emit("CURATE", who, f"keep {len(kept) - len(out)} of {len(kept)} sets, evict {len(out)} "
                                   f"({sum(i.size for i in out) / 1e6:.0f} MB), {len(queued)} better candidates queued")
        gone = {id(i) for i in out}
        for item in kept:
            if id(item) not in gone:
                retag(stash, cfg, item, galleries.get(item.key), report, dry)
    report.emit("FREED", None, f"{evicted} sets, {freed / 1e9:.2f} GB{' (would be)' if dry else ''}")
    if evicted and not dry:
        report.emit("CLEAN", None, f"queued clean job {stash.clean([cfg.root])} for {cfg.root}")


def retag(stash, cfg, item, gallery, report, dry):
    """Store the measured quality in set.json and apply it (quality tags) to the gallery."""
    sc = layout.read_sidecar(item.key) or {}
    quality = {"edge": item.long_edge, "sharpness": item.sharpness, "soft": item.soft}
    if sc.get("quality") != quality:
        sc["quality"] = quality
        if not dry:
            layout.write_sidecar(item.key, sc)
    if gallery:
        apply.apply_gallery(stash, cfg, gallery, report, dry, sidecar=sc)
