"""Find (and quarantine/delete) sidecars whose video file is gone."""
import os
import posixpath
import re
import time

from . import fsutil

_SIDECAR_RE = re.compile(r"^(?P<base>.+?)(?P<suffix>\.nfo-orig|\.nfo|-(?:poster|fanart|background|thumb)\.[A-Za-z0-9]+)$")


def scan(cfg, libraries, settings, report):
    action = settings.get("orphan_action") or "quarantine"
    days = settings.get("orphan_min_age_days")
    min_age = float(1 if days is None else days) * 86400
    now = time.time()
    for lib in libraries:
        for directory, dirs, names in os.walk(lib.root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            videos = {os.path.splitext(n)[0] for n in names if fsutil.is_video(n)}
            for name in sorted(names):
                m = _SIDECAR_RE.match(name)
                if not m or m.group("base") in videos:
                    continue
                path = posixpath.join(directory, name)
                if now - os.path.getmtime(path) < min_age:
                    continue
                reason = f"(no video '{m.group('base')}.*')"
                if action == "delete":
                    report.emit("ORPHAN-DELETE", None, f"{path} {reason}")
                    if not report.dry:
                        os.unlink(path)
                else:
                    dest = posixpath.join(cfg.base_path, ".orphans", lib.name, posixpath.relpath(path, lib.root))
                    report.emit("ORPHAN-QUARANTINE", None, f"{path} -> {dest} {reason}")
                    if not report.dry:
                        fsutil.ensure_dir(posixpath.dirname(dest), cfg.base_path)
                        fsutil.rename_nooverwrite(path, dest)
