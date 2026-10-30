"""SQLite index: what happened to each scene's source URL, so set pages aren't fetched again every run."""
import sqlite3
import time

# done = downloaded; exists = a hand-made set folder already has this key; gone = removed from the site;
# empty = no public photos; small = every photo below min_image_size. "partial" = resume on the next run.
FINAL = {"done", "exists", "gone", "empty", "small"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY, status TEXT NOT NULL, scene_id TEXT, folder TEXT,
  seen_at INTEGER);
"""


class Index:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=60)
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.commit()
        self.db.close()

    def status(self, url):
        row = self.db.execute("SELECT status FROM sources WHERE url=?", (url,)).fetchone()
        return row[0] if row else None

    def mark(self, url, status, scene_id=None, folder=None):
        self.db.execute("INSERT INTO sources(url, status, scene_id, folder, seen_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(url) DO UPDATE SET status=excluded.status, scene_id=excluded.scene_id, "
                        "folder=excluded.folder, seen_at=excluded.seen_at",
                        (url, status, scene_id, folder, int(time.time())))
        self.db.commit()
