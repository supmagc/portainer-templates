"""SQLite index: which source URLs were handled, a fingerprint per downloaded image, surveyed candidate sets."""
import collections
import dataclasses
import json
import sqlite3
import statistics
import time

from . import imaging
from .sources import ImageRef, SetInfo

FINAL = {"done", "duplicate", "rejected", "small", "evicted", "low-quality"}  # "partial" = resume on the next run

SCHEMA = """
CREATE TABLE IF NOT EXISTS sets (id INTEGER PRIMARY KEY, folder TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY, status TEXT NOT NULL, set_id INTEGER, seen_at INTEGER);
CREATE TABLE IF NOT EXISTS images (id INTEGER PRIMARY KEY, set_id INTEGER NOT NULL, path TEXT UNIQUE NOT NULL,
  sha256 TEXT NOT NULL, dhash INTEGER NOT NULL, width INTEGER, height INTEGER);
CREATE INDEX IF NOT EXISTS images_sha ON images(sha256);
CREATE TABLE IF NOT EXISTS evicted_sets (set_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS candidates (url TEXT PRIMARY KEY, performer_id TEXT NOT NULL, info TEXT NOT NULL, seen_at INTEGER);
CREATE INDEX IF NOT EXISTS candidates_performer ON candidates(performer_id);
CREATE TABLE IF NOT EXISTS listings (performer_id TEXT NOT NULL, source TEXT NOT NULL, checked_at INTEGER NOT NULL,
  PRIMARY KEY (performer_id, source));
"""


def _signed(h):
    return h - (1 << 64) if h >= 1 << 63 else h


def _unsigned(h):
    return h + (1 << 64) if h < 0 else h


class Index:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=60)
        self.db.executescript(SCHEMA)
        if "sharpness" not in {r[1] for r in self.db.execute("PRAGMA table_info(images)")}:
            self.db.execute("ALTER TABLE images ADD COLUMN sharpness REAL")  # v0.4; NULL = not measured yet
            self.db.commit()
        self._hashes = None

    def close(self):
        self.db.commit()
        self.db.close()

    def reset(self):
        """Forget everything that can be rebuilt from disk; evicted sets (URLs + fingerprints) and candidates stay."""
        self.db.executescript("DELETE FROM images WHERE set_id NOT IN (SELECT set_id FROM evicted_sets);"
                              "DELETE FROM sets WHERE id NOT IN (SELECT set_id FROM evicted_sets);"
                              "DELETE FROM sources WHERE status != 'evicted'; DELETE FROM listings;")
        self._hashes = None

    def status(self, url):
        row = self.db.execute("SELECT status FROM sources WHERE url=?", (url,)).fetchone()
        return row[0] if row else None

    def mark(self, url, status, set_id=None):
        self.db.execute("INSERT INTO sources(url, status, set_id, seen_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(url) DO UPDATE SET status=excluded.status, set_id=excluded.set_id, seen_at=excluded.seen_at",
                        (url, status, set_id, int(time.time())))
        self.db.commit()

    def set_id(self, folder):
        self.db.execute("INSERT OR IGNORE INTO sets(folder) VALUES(?)", (folder,))
        return self.db.execute("SELECT id FROM sets WHERE folder=?", (folder,)).fetchone()[0]

    def find_set(self, folder):
        row = self.db.execute("SELECT id FROM sets WHERE folder=?", (folder,)).fetchone()
        return row[0] if row else None

    def set_of(self, url):
        row = self.db.execute("SELECT set_id FROM sources WHERE url=?", (url,)).fetchone()
        return row[0] if row else None

    def folder(self, set_id):
        row = self.db.execute("SELECT folder FROM sets WHERE id=?", (set_id,)).fetchone()
        return row[0] if row else None

    def has_sha(self, sha):
        return self.db.execute("SELECT 1 FROM images WHERE sha256=?", (sha,)).fetchone() is not None

    def sets_with_sha(self, sha):
        return {r[0] for r in self.db.execute("SELECT set_id FROM images WHERE sha256=?", (sha,))}

    def _all(self):
        if self._hashes is None:
            self._hashes = [(_unsigned(d), w, h, s) for d, w, h, s in
                            self.db.execute("SELECT dhash, width, height, set_id FROM images")]
        return self._hashes

    def near(self, meta, max_distance, exclude_set=None):
        """set_ids holding a near-identical image (same aspect ratio, dHash within max_distance)."""
        hits = set()
        for d, w, h, s in self._all():
            if s != exclude_set and imaging.distance(d, meta.dhash) <= max_distance and imaging.same_shape(w, h, meta.width, meta.height):
                hits.add(s)
        return hits

    def stats(self, set_id):
        """(width, height, sharpness or None) per image of a set."""
        return self.db.execute("SELECT width, height, sharpness FROM images WHERE set_id=?", (set_id,)).fetchall()

    def unmeasured(self, set_id):
        return self.db.execute("SELECT id, path FROM images WHERE set_id=? AND sharpness IS NULL", (set_id,)).fetchall()

    def set_sharpness(self, image_id, value):
        self.db.execute("UPDATE images SET sharpness=? WHERE id=?", (value, image_id))

    def commit(self):
        self.db.commit()

    def soft_threshold(self, percentile, min_sets=50):
        """Set-median sharpness below which a set counts as soft: the given percentile of the library's sets
        (relative, because the measure depends on content); None while too few sets are measured."""
        if not percentile:
            return None
        per_set = collections.defaultdict(list)
        for set_id, value in self.db.execute("SELECT set_id, sharpness FROM images WHERE sharpness IS NOT NULL "
                                             "AND set_id NOT IN (SELECT set_id FROM evicted_sets)"):
            per_set[set_id].append(value)
        medians = sorted(statistics.median(v) for v in per_set.values())
        if len(medians) < min_sets:
            return None
        return medians[min(len(medians) - 1, int(len(medians) * percentile / 100))]

    def drop_set(self, set_id):
        """Forget a set entirely (replaced by a better copy)."""
        self.db.execute("DELETE FROM images WHERE set_id=?", (set_id,))
        self.db.execute("DELETE FROM sets WHERE id=?", (set_id,))
        self.db.commit()
        self._hashes = None

    def is_evicted(self, set_id):
        return self.db.execute("SELECT 1 FROM evicted_sets WHERE set_id=?", (set_id,)).fetchone() is not None

    def evict_set(self, set_id):
        """The folder is gone; its fingerprints stay so the same set from another source counts as a duplicate."""
        self.db.execute("INSERT OR IGNORE INTO evicted_sets(set_id) VALUES(?)", (set_id,))
        self.db.commit()

    # candidates: surveyed set pages waiting for a free slot

    def add_candidate(self, performer_id, info):
        self.db.execute("INSERT OR REPLACE INTO candidates(url, performer_id, info, seen_at) VALUES(?,?,?,?)",
                        (info.url, str(performer_id), json.dumps(dataclasses.asdict(info)), int(time.time())))
        self.db.commit()

    def has_candidate(self, url):
        return self.db.execute("SELECT 1 FROM candidates WHERE url=?", (url,)).fetchone() is not None

    def candidates(self, performer_id):
        """(status, SetInfo) for candidates not handled yet (status None) or half downloaded ("partial")."""
        rows = self.db.execute("SELECT c.info, s.status FROM candidates c LEFT JOIN sources s ON s.url = c.url "
                               "WHERE c.performer_id=? AND (s.status IS NULL OR s.status='partial') ORDER BY c.seen_at, c.url",
                               (str(performer_id),))
        out = []
        for info, status in rows:
            d = json.loads(info)
            d["images"] = [ImageRef(**i) for i in d.get("images") or []]
            out.append((status, SetInfo(**d)))
        return out

    def listing_fresh(self, performer_id, source, days):
        row = self.db.execute("SELECT checked_at FROM listings WHERE performer_id=? AND source=?",
                              (str(performer_id), source)).fetchone()
        return bool(days) and row is not None and time.time() - row[0] < days * 86400

    def mark_listing(self, performer_id, source):
        self.db.execute("INSERT OR REPLACE INTO listings(performer_id, source, checked_at) VALUES(?,?,?)",
                        (str(performer_id), source, int(time.time())))
        self.db.commit()

    def add_image(self, set_id, path, meta):
        self.db.execute("INSERT OR REPLACE INTO images(set_id, path, sha256, dhash, width, height, sharpness) "
                        "VALUES(?,?,?,?,?,?,?)", (set_id, path, meta.sha256, _signed(meta.dhash), meta.width, meta.height,
                                                  meta.sharpness))
        self.db.commit()
        self._all().append((meta.dhash, meta.width, meta.height, set_id))
