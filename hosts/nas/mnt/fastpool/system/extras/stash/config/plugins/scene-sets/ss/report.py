"""Action log: Stash log protocol on stderr, optional report file, summary counters."""
import collections
import datetime
import sys

_LEVELS = {"trace": "t", "debug": "d", "info": "i", "warning": "w", "error": "e", "progress": "p"}


def log(level, msg):
    sys.stderr.write(f"\x01{_LEVELS[level]}\x02{msg}\n")
    sys.stderr.flush()


class Report:
    def __init__(self, path=None, truncate=False, dry=True):
        self.path = path
        self.dry = dry
        self.counts = collections.Counter()
        if path and truncate:
            with open(path, "w", encoding="utf-8"):
                pass

    def emit(self, kind, who, text, level="info"):
        self.counts[kind] += 1
        tag = "[DRY] " if self.dry else ""
        who = f"{who} " if who else ""
        line = f"{tag}{kind:<14} {who}{text}"
        log(level, line)
        if self.path:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(f"{ts} {line}\n")

    def summary(self):
        parts = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        return f"summary: {parts or 'nothing to do'}"
