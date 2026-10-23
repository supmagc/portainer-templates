"""SQLite dedupe index: which source URLs were handled, and a fingerprint per downloaded image."""
import sqlite3
import time

from . import imaging

FINAL = {"done", "duplicate", "rejected", "small"}  # "partial" = resume on the next run

SCHEMA = """
CREATE TABLE IF NOT EXISTS sets (id INTEGER PRIMARY KEY, folder TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY, status TEXT NOT NULL, set_id INTEGER, seen_at INTEGER);
CREATE TABLE IF NOT EXISTS images (id INTEGER PRIMARY KEY, set_id INTEGER NOT NULL, path TEXT UNIQUE NOT NULL,
  sha256 TEXT NOT NULL, dhash INTEGER NOT NULL, width INTEGER, height INTEGER);
CREATE INDEX IF NOT EXISTS images_sha ON images(sha256);
"""


def _signed(h):
    return h - (1 << 64) if h >= 1 << 63 else h


def _unsigned(h):
    return h + (1 << 64) if h < 0 else h


class Index:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=60)
        self.db.executescript(SCHEMA)
        self._hashes = None

    def close(self):
        self.db.commit()
        self.db.close()

    def reset(self):
        self.db.executescript("DELETE FROM images; DELETE FROM sources; DELETE FROM sets;")
        self._hashes = []

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

    def add_image(self, set_id, path, meta):
        self.db.execute("INSERT OR REPLACE INTO images(set_id, path, sha256, dhash, width, height) VALUES(?,?,?,?,?,?)",
                        (set_id, path, meta.sha256, _signed(meta.dhash), meta.width, meta.height))
        self.db.commit()
        self._all().append((meta.dhash, meta.width, meta.height, set_id))
