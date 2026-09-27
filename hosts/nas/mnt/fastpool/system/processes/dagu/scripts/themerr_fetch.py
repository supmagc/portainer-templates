#!/usr/bin/env python3
"""
themerr-fetch — download theme songs from ThemerrDB for Radarr/Sonarr libraries.

Server-agnostic: writes theme.mp3 into each movie/series folder, which Emby,
Jellyfin (and Plex with local-assets) pick up natively.

Rules:
  * A theme.* file that this tool did not write is treated as user-provided
    and never touched.
  * A folder containing a `.nothemerr` file is skipped entirely.
  * Themes this tool wrote are replaced when ThemerrDB points to a new video
    (disable with UPDATE_CHANGED=false).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("themerr-fetch")


# --------------------------------------------------------------------------- config

def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    v = env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


def env_num(name: str, default: float) -> float:
    v = env(name)
    return float(v) if v else default


RADARR_URL = env("RADARR_URL").rstrip("/")
RADARR_API_KEY = env("RADARR_API_KEY")
SONARR_URL = env("SONARR_URL").rstrip("/")
SONARR_API_KEY = env("SONARR_API_KEY")

# "arr_prefix=local_prefix;arr_prefix2=local_prefix2"
PATH_MAP = [
    tuple(p.split("=", 1)) for p in env("PATH_MAP").split(";") if "=" in p
]

THEMERRDB_BASE = env("THEMERRDB_BASE", "https://app.lizardbyte.dev/ThemerrDB").rstrip("/")
DATA_DIR = Path(env("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "state.json"
YTDLP = Path(env("YTDLP_PATH", str(DATA_DIR / "yt-dlp")))
YTDLP_AUTOUPDATE = env_bool("YTDLP_AUTOUPDATE", True)
YTDLP_COOKIES = env("YTDLP_COOKIES")          # optional Netscape cookies.txt
THEME_NAME = env("THEME_FILENAME", "theme.mp3")
LOUDNESS_LUFS = env("LOUDNESS_LUFS", "-16")   # empty = no normalisation
MAX_DURATION = int(env_num("MAX_DURATION", 0))  # seconds, 0 = keep full length
FADE_OUT = float(env_num("FADE_OUT", 3))        # seconds, only when trimming
MP3_QUALITY = env("MP3_QUALITY", "2")           # LAME VBR -q:a (0 best .. 9)
RECHECK_DAYS = env_num("RECHECK_DAYS", 7)       # re-query items not in DB after N days
UPDATE_CHANGED = env_bool("UPDATE_CHANGED", True)
DOWNLOAD_DELAY = env_num("DOWNLOAD_DELAY", 5)   # seconds between YouTube downloads
MAX_DOWNLOADS = int(env_num("MAX_DOWNLOADS", 0))  # per run, 0 = unlimited
THEMERRDB_DELAY = env_num("THEMERRDB_DELAY", 0.3)  # seconds between live ThemerrDB lookups
INTERVAL_HOURS = env_num("INTERVAL_HOURS", 0)   # 0 = run once and exit (use with Dagu/cron)

# optional: trigger a library scan when themes were added/updated
MEDIASERVER_TYPE = env("MEDIASERVER_TYPE").lower()   # emby | jellyfin | empty
MEDIASERVER_URL = env("MEDIASERVER_URL").rstrip("/")
MEDIASERVER_API_KEY = env("MEDIASERVER_API_KEY")
DRY_RUN = env_bool("DRY_RUN")

UA = "themerr-fetch/1.0 (+https://github.com/LizardByte/ThemerrDB)"


# --------------------------------------------------------------------------- helpers

def http_json(url: str, headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def map_path(arr_path: str) -> Path:
    for src, dst in PATH_MAP:
        src = src.rstrip("/")
        if arr_path == src or arr_path.startswith(src + "/"):
            return Path(dst.rstrip("/") + arr_path[len(src):])
    return Path(arr_path)


def now() -> datetime:
    return datetime.now(timezone.utc)


def load_state() -> dict:
    try:
        s = json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        s = {}
    s.setdefault("owned", {})    # folder -> {url, size, mtime, written}
    s.setdefault("missing", {})  # db key -> iso timestamp of last negative lookup
    return s


def save_state(state: dict) -> None:
    if DRY_RUN:
        return
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- sources

def arr_items() -> list[dict]:
    items: list[dict] = []
    if RADARR_URL and RADARR_API_KEY:
        movies = http_json(f"{RADARR_URL}/api/v3/movie", {"X-Api-Key": RADARR_API_KEY}, 120)
        for m in movies:
            if not m.get("path"):
                continue
            items.append({
                "kind": "movies",
                "title": f"{m.get('title')} ({m.get('year')})",
                "path": map_path(m["path"]),
                "ids": [("themoviedb", m.get("tmdbId")), ("imdb", m.get("imdbId"))],
            })
        log.info("Radarr: %d movies", len(movies))
    if SONARR_URL and SONARR_API_KEY:
        series = http_json(f"{SONARR_URL}/api/v3/series", {"X-Api-Key": SONARR_API_KEY}, 120)
        for s in series:
            if not s.get("path"):
                continue
            # ThemerrDB indexes TV shows by TMDb id only
            items.append({
                "kind": "tv_shows",
                "title": f"{s.get('title')} ({s.get('year')})",
                "path": map_path(s["path"]),
                "ids": [("themoviedb", s.get("tmdbId"))],
            })
        log.info("Sonarr: %d series", len(series))
    return items


# --------------------------------------------------------------------------- themerrdb

def lookup_theme(item: dict, state: dict) -> str | None:
    """Return the YouTube URL for an item, or None. Caches negative results."""
    for id_type, id_val in item["ids"]:
        if not id_val:
            continue
        key = f"{item['kind']}/{id_type}/{id_val}"
        checked = state["missing"].get(key)
        if checked and now() - datetime.fromisoformat(checked) < timedelta(days=RECHECK_DAYS):
            continue
        try:
            data = http_json(f"{THEMERRDB_BASE}/{key}.json")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                state["missing"][key] = now().isoformat()
                continue
            log.warning("ThemerrDB %s: HTTP %s", key, e.code)
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            log.warning("ThemerrDB %s: %s", key, e)
            continue
        finally:
            if THEMERRDB_DELAY:
                time.sleep(THEMERRDB_DELAY)
        state["missing"].pop(key, None)
        url = data.get("youtube_theme_url")
        if url:
            return url
    return None


# --------------------------------------------------------------------------- yt-dlp / ffmpeg

def ensure_ytdlp() -> None:
    if not YTDLP.exists():
        log.info("Downloading yt-dlp to %s", YTDLP)
        YTDLP.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp",
            headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r, open(YTDLP, "wb") as f:
            shutil.copyfileobj(r, f)
        YTDLP.chmod(0o755)
    elif YTDLP_AUTOUPDATE:
        subprocess.run([str(YTDLP), "-U"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ver = subprocess.run([str(YTDLP), "--version"], capture_output=True, text=True)
    log.info("yt-dlp %s", ver.stdout.strip() or "?")


def fetch_theme(url: str, dest: Path) -> bool:
    """Download audio for `url` and write a normalised MP3 atomically to `dest`."""
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp:
        cmd = [str(YTDLP), "-f", "bestaudio/best", "--no-playlist", "--no-progress",
               "--quiet", "--no-warnings", "-o", f"{tmp}/src.%(ext)s", url]
        if YTDLP_COOKIES:
            cmd[1:1] = ["--cookies", YTDLP_COOKIES]
        r = subprocess.run(cmd, capture_output=True, text=True)
        src = next(Path(tmp).glob("src.*"), None)
        if r.returncode != 0 or src is None:
            log.error("  yt-dlp failed: %s", (r.stderr or r.stdout).strip()[-400:])
            return False

        filters = []
        if MAX_DURATION > 0 and FADE_OUT > 0:
            filters.append(f"afade=t=out:st={max(MAX_DURATION - FADE_OUT, 0)}:d={FADE_OUT}")
        if LOUDNESS_LUFS:
            filters.append(f"loudnorm=I={LOUDNESS_LUFS}:TP=-1.5:LRA=11")
        out = Path(tmp) / "theme.mp3"
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(src),
               "-vn", "-map_metadata", "-1"]
        if MAX_DURATION > 0:
            cmd += ["-t", str(MAX_DURATION)]
        if filters:
            cmd += ["-af", ",".join(filters)]
        cmd += ["-ar", "44100", "-c:a", "libmp3lame", "-q:a", MP3_QUALITY, str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            log.error("  ffmpeg failed: %s", r.stderr.strip()[-400:])
            return False

        # copy next to the target first, then rename: never leaves a half-written theme
        staging = dest.with_name(f".{dest.name}.partial")
        shutil.copyfile(out, staging)
        staging.replace(dest)
    return True


# --------------------------------------------------------------------------- media server

def refresh_library() -> None:
    if not (MEDIASERVER_TYPE and MEDIASERVER_URL and MEDIASERVER_API_KEY):
        return
    if MEDIASERVER_TYPE == "emby":
        url, headers = f"{MEDIASERVER_URL}/emby/Library/Refresh", {"X-Emby-Token": MEDIASERVER_API_KEY}
    elif MEDIASERVER_TYPE == "jellyfin":
        url = f"{MEDIASERVER_URL}/Library/Refresh"
        headers = {"Authorization": f'MediaBrowser Token="{MEDIASERVER_API_KEY}"'}
    else:
        log.warning("Unknown MEDIASERVER_TYPE %r", MEDIASERVER_TYPE)
        return
    req = urllib.request.Request(url, data=b"", method="POST",
                                 headers={"User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
        log.info("Triggered %s library scan", MEDIASERVER_TYPE)
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("Library scan trigger failed: %s", e)


# --------------------------------------------------------------------------- main loop

def user_theme(folder: Path, owned: dict | None) -> Path | None:
    """Return a theme file that we did not write ourselves, if any."""
    for f in folder.glob("theme.*"):
        if f.name.startswith("."):
            continue
        if f.name == THEME_NAME and owned:
            st = f.stat()
            if st.st_size == owned.get("size") and int(st.st_mtime) == owned.get("mtime"):
                continue  # ours, unchanged
        return f
    return None


def run_once() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    ensure_ytdlp()

    items = arr_items()
    stats = dict(added=0, updated=0, user=0, skipped=0, missing=0, failed=0, nofolder=0)
    downloads = 0

    for item in items:
        folder: Path = item["path"]
        if not folder.is_dir():
            stats["nofolder"] += 1
            log.debug("No folder: %s", folder)
            continue
        if (folder / ".nothemerr").exists():
            stats["skipped"] += 1
            continue

        key = str(folder)
        owned = state["owned"].get(key)
        if user_theme(folder, owned):
            stats["user"] += 1
            state["owned"].pop(key, None)  # user took over this folder
            continue

        dest = folder / THEME_NAME
        have_ours = owned is not None and dest.exists()
        if have_ours and not UPDATE_CHANGED:
            continue

        url = lookup_theme(item, state)
        if not url:
            if not have_ours:
                stats["missing"] += 1
            continue
        if have_ours and owned.get("url") == url:
            continue

        if MAX_DOWNLOADS and downloads >= MAX_DOWNLOADS:
            log.info("MAX_DOWNLOADS reached, continuing next run")
            break

        action = "Updating" if have_ours else "Adding"
        log.info("%s theme: %s -> %s", action, item["title"], url)
        if DRY_RUN:
            stats["updated" if have_ours else "added"] += 1
            continue
        if downloads:
            time.sleep(DOWNLOAD_DELAY)
        downloads += 1
        if fetch_theme(url, dest):
            st = dest.stat()
            state["owned"][key] = {"url": url, "size": st.st_size,
                                   "mtime": int(st.st_mtime), "written": now().isoformat()}
            stats["updated" if have_ours else "added"] += 1
            save_state(state)  # save as we go, so an interrupted run loses nothing
        else:
            stats["failed"] += 1

    save_state(state)
    log.info("Done: %s", ", ".join(f"{k}={v}" for k, v in stats.items()))
    if (stats["added"] or stats["updated"]) and not DRY_RUN:
        refresh_library()
    return stats


def main() -> int:
    logging.basicConfig(level=env("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    if not (RADARR_URL or SONARR_URL):
        log.error("Set RADARR_URL/RADARR_API_KEY and/or SONARR_URL/SONARR_API_KEY")
        return 2
    # ffmpeg/ffprobe/deno are vendored into DATA_DIR by the DAG's build step, not installed
    # into the image - PATH must include it before the ffmpeg presence check below.
    os.environ["PATH"] = f"{DATA_DIR}{os.pathsep}{os.environ.get('PATH', '')}"
    if shutil.which("ffmpeg") is None:
        log.error("ffmpeg not found in PATH")
        return 2
    os.umask(int(env("UMASK", "002"), 8))

    while True:
        try:
            stats = run_once()
        except Exception:
            log.exception("Run failed")
            if INTERVAL_HOURS <= 0:
                return 1
        else:
            # logs go to stderr; stdout carries one JSON summary line for schedulers
            print(json.dumps(stats), flush=True)
            if INTERVAL_HOURS <= 0:
                return 3 if stats["failed"] and env_bool("FAIL_ON_ERRORS") else 0
        log.info("Sleeping %.1f h", INTERVAL_HOURS)
        time.sleep(INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    sys.exit(main())
