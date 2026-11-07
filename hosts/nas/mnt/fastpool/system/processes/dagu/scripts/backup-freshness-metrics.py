#!/usr/bin/env python3
# Writes the newest file's mtime/size per rightpool/backup/automated/<name>
# folder as node-exporter textfile metrics, for the BackupStale alert. Run
# hourly by the Dagu backup-freshness DAG. See docs/backups.md
# "Stale backup detection".

import os
import time

ROOT = "/backups"
OUT_FILE = "/textfile/backups.prom"
OUTPUT_UID = int(os.environ.get("OUTPUT_UID", 920))
OUTPUT_GID = int(os.environ.get("OUTPUT_GID", 920))
HOUR = 3600
DAY = 24 * HOUR
DEFAULT_MAX_AGE = 8 * DAY
# folder -> max age of its newest file before BackupStale fires (schedule + slack), None = not checked.
# A listed folder that doesn't exist is reported as never backed up.
MAX_AGE = {
    "mariadb": 30 * HOUR,
    "postgres": 30 * HOUR,
    "openwrt": 30 * HOUR,
    "truenas": 30 * HOUR,
    "openhabian": 30 * HOUR,
    "tunarr": 30 * HOUR,
    "sonarr": 6 * DAY,
    "radarr": 6 * DAY,
    "lidarr": 6 * DAY,
    "jellyfin": 6 * DAY,
    "seerr": 6 * DAY,
    "navidrome": 6 * DAY,
    "whisparr": 6 * DAY,
    "stash": 6 * DAY,
    "bazarr": 8 * DAY,
    "emby": None,  # retired
}
SKIP_SUFFIXES = (".partial", ".tmp")


def newest_file(folder):
    count, newest_mtime, newest_size = 0, 0, 0
    for dirpath, _, filenames in os.walk(folder):
        for fn in filenames:
            if fn.endswith(SKIP_SUFFIXES) or fn.startswith("."):
                continue
            try:
                st = os.stat(os.path.join(dirpath, fn))
            except OSError:
                continue
            count += 1
            if st.st_mtime > newest_mtime:
                newest_mtime, newest_size = st.st_mtime, st.st_size
    return count, int(newest_mtime), newest_size


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


names = {e.name for e in os.scandir(ROOT) if e.is_dir()} | set(MAX_AGE)
lines = [
    "# HELP backup_newest_file_timestamp_seconds mtime of the newest file in the backup folder, 0 if it has none.",
    "# TYPE backup_newest_file_timestamp_seconds gauge",
    "# HELP backup_newest_file_size_bytes Size of the newest file in the backup folder.",
    "# TYPE backup_newest_file_size_bytes gauge",
    "# HELP backup_files Number of files in the backup folder.",
    "# TYPE backup_files gauge",
    "# HELP backup_max_age_seconds Age the newest file may reach before BackupStale fires.",
    "# TYPE backup_max_age_seconds gauge",
]
for name in sorted(names):
    max_age = MAX_AGE.get(name, DEFAULT_MAX_AGE)
    if max_age is None:
        continue
    count, mtime, size = newest_file(os.path.join(ROOT, name))
    label = f'{{name="{esc(name)}"}}'
    lines += [
        f"backup_newest_file_timestamp_seconds{label} {mtime}",
        f"backup_newest_file_size_bytes{label} {size}",
        f"backup_files{label} {count}",
        f"backup_max_age_seconds{label} {max_age}",
    ]
    age = f"{(time.time() - mtime) / DAY:.1f}d" if mtime else "never"
    print(f"{name}: {count} files, newest {age} old, max {max_age / DAY:.1f}d")

tmp = OUT_FILE + ".tmp"
with open(tmp, "w") as f:
    f.write("\n".join(lines) + "\n")
os.chown(tmp, OUTPUT_UID, OUTPUT_GID)
os.chmod(tmp, 0o644)
os.replace(tmp, OUT_FILE)
