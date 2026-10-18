#!/usr/bin/env python3
"""
backdrop-generate — build Emby/Jellyfin "theme videos" (backdrops/theme.mp4) for
Radarr/Sonarr libraries. Source order: the community theme-video index (the catalogue
behind the Emby Movie/TV Theme Video plugins), then ThemerrDB's theme-song video, then a
montage of short, cut-free, motion-containing clips sampled directly out of the
movie/episode files (see docs/dagu.md). Nothing is ever uploaded.

Rules:
  * A file in backdrops/ that this tool did not write is treated as user-provided
    and never touched.
  * A folder containing a `.nobackdrop` file is skipped entirely.
  * A community video replaces anything this tool wrote itself and is never downgraded.
  * A ThemerrDB/community URL that turns out static is remembered and never downloaded
    again; a new ThemerrDB URL for the same item is tried on a later run and replaces a
    local montage.
  * A local montage is regenerated only if its source file(s) changed (size/mtime).
  * Movies sample from the one file. Series sample from SERIES_EPISODES
    representative episodes spread across the show, skipping the pilot.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("backdrop-generate")


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

DATA_DIR = Path(env("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "state.json"
BACKDROP_NAME = env("BACKDROP_FILENAME", "theme.mp4")
BACKDROP_MAX_HEIGHT = int(env_num("BACKDROP_MAX_HEIGHT", 1080))

N_SAMPLES = int(env_num("N_SAMPLES", 10))
SAMPLE_DURATION = env_num("SAMPLE_DURATION", 3.0)          # seconds per clip
SKIP_START_PCT = env_num("SKIP_START_PCT", 5)              # avoid logos/intro
SKIP_END_PCT = env_num("SKIP_END_PCT", 10)                 # avoid credits
SEARCH_RADIUS = env_num("SEARCH_RADIUS", 30)               # seconds, outward search
SEARCH_STEP = env_num("SEARCH_STEP", 2)                    # seconds, search increment
SCENE_THRESHOLD = env("SCENE_THRESHOLD", "0.3")             # ffmpeg scene-score cut point
MIN_MOTION = env_num("MIN_MOTION", 4.0)                    # min mean pixel-diff to accept
SERIES_EPISODES = int(env_num("SERIES_EPISODES", 3))       # representative episodes/series

THEMERR_VIDEOS = env_bool("THEMERR_VIDEOS", True)          # false = local montages only
THEMERRDB_BASE = env("THEMERRDB_BASE", "https://app.lizardbyte.dev/ThemerrDB").rstrip("/")
THEMERRDB_DELAY = env_num("THEMERRDB_DELAY", 0.3)          # seconds between live lookups
RECHECK_DAYS = env_num("RECHECK_DAYS", 7)                  # re-query items not in ThemerrDB
REJECT_RETRY_DAYS = env_num("REJECT_RETRY_DAYS", 90)       # re-try unavailable (never static) URLs
YTDLP = Path(env("YTDLP_PATH", str(DATA_DIR / "yt-dlp")))
YTDLP_AUTOUPDATE = env_bool("YTDLP_AUTOUPDATE", True)
YTDLP_COOKIES = env("YTDLP_COOKIES")                       # optional Netscape cookies.txt
MIN_DURATION = int(env_num("MIN_DURATION", 20))            # seconds, ThemerrDB's link limits
MAX_DURATION = int(env_num("MAX_DURATION", 300))
DOWNLOAD_DELAY = env_num("DOWNLOAD_DELAY", 5)
MAX_DOWNLOADS = int(env_num("MAX_DOWNLOADS", 50))          # YouTube attempts per run, 0 = unlimited
STATIC_SAMPLES = int(env_num("STATIC_SAMPLES", 8))         # points checked for motion
STATIC_GAP = env_num("STATIC_GAP", 1.0)                    # seconds between the two frames per point
STATIC_THRESHOLD = env_num("STATIC_THRESHOLD", 2.0)        # mean pixel-diff counted as motion
STATIC_MIN_MOVING_PCT = env_num("STATIC_MIN_MOVING_PCT", 50)  # below this share of points = static

NETWORK_VIDEOS = env_bool("NETWORK_VIDEOS", True)          # false = skip the community theme-video index
NETWORK_BASE = env("NETWORK_BASE", "http://www.ballingtons.com/s/mf").rstrip("/")
NETWORK_API_KEY = env("NETWORK_API_KEY", "SPEBGSPSEP2KA4D2NTSB")  # shared constant from the Emby plugin
NETWORK_API_UN = env("NETWORK_API_UN", "MBJBAPI")
MAX_NETWORK_DOWNLOADS = int(env_num("MAX_NETWORK_DOWNLOADS", 50))  # MediaFire downloads per run, 0 = unlimited
NETWORK_DELAY = env_num("NETWORK_DELAY", 3)
NETWORK_MAX_MB = env_num("NETWORK_MAX_MB", 300)
NORMALISE_AUDIO = env_bool("NORMALISE_AUDIO", True)
NORMALISE_MAX_DB = env_num("NORMALISE_MAX_DB", -15)        # target peak, like the Emby plugin

NVENC_PRESET = env("NVENC_PRESET", "p4")
NVENC_CQ = env("NVENC_CQ", "23")
GPU_PROBE_TIMEOUT = env_num("GPU_PROBE_TIMEOUT", 60)       # seconds; a wedged driver hangs, not errors
FFMPEG_TIMEOUT = env_num("FFMPEG_TIMEOUT", 300)            # seconds per ffmpeg/ffprobe call; a corrupt file can hang one
FAIL_RETRY_DAYS = env_num("FAIL_RETRY_DAYS", 7)            # skip an item whose montage failed until then

RUN_BUDGET_MIN = env_num("RUN_BUDGET_MIN", 100)            # stop starting work after this; keep below the DAG's timeout_sec
DEADLINE = time.monotonic() + RUN_BUDGET_MIN * 60


ITEM_BUDGET_MIN = env_num("ITEM_BUDGET_MIN", 10)          # per-item cap; a file this slow is marked failed
SLOW_CALL_SEC = env_num("SLOW_CALL_SEC", 30)               # log any ffmpeg call slower than this
item_deadline = float("inf")


class ItemTooSlow(RuntimeError):
    pass


class OutOfTime(RuntimeError):
    """RUN_BUDGET_MIN spent - end the run cleanly so state is saved and the DAG succeeds."""


def check_budget() -> None:
    if RUN_BUDGET_MIN and time.monotonic() > DEADLINE:
        raise OutOfTime
    if time.monotonic() > item_deadline:
        raise ItemTooSlow

MAX_ITEMS_PER_RUN = int(env_num("MAX_ITEMS_PER_RUN", 20))  # GPU budget, 0 = unlimited
INTERVAL_HOURS = env_num("INTERVAL_HOURS", 0)              # 0 = run once and exit

PROCESS_UID = int(env_num("PROCESS_UID", 920))  # nas_processes - owns everything under DATA_DIR
PROCESS_GID = int(env_num("PROCESS_GID", 920))
MEDIA_UID = int(env_num("MEDIA_UID", 910))      # nas_multimedia - owns written backdrops
MEDIA_GID = int(env_num("MEDIA_GID", 910))

MEDIASERVER_TYPE = env("MEDIASERVER_TYPE").lower()   # emby | jellyfin | empty
MEDIASERVER_URL = env("MEDIASERVER_URL").rstrip("/")
MEDIASERVER_API_KEY = env("MEDIASERVER_API_KEY")
DRY_RUN = env_bool("DRY_RUN")

UA = "backdrop-generate/1.0"

# checked before UNUSABLE_RE: a rate-limited session can get "Video unavailable ... try again later"
RATE_LIMIT_RE = re.compile(r"not a bot|HTTP Error 429|Too Many Requests|try again later", re.I)
UNUSABLE_RE = re.compile(
    r"unavailable|has been removed|private video|confirm your age|age-restricted|your country|"
    r"your location|geo.?restrict|terminated|copyright|members-only|join this channel", re.I)


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
    # folder -> {source: local|themerr|network, sources: [{path,size,mtime}] | url | mf_key, size, mtime, written}
    s.setdefault("owned", {})
    s.setdefault("missing", {})   # ThemerrDB key -> iso timestamp of last negative lookup
    s.setdefault("rejected", {})  # YouTube URL or "network/<mediafire key>" -> {reason, checked, static}
    s.setdefault("failed", {})    # folder -> iso timestamp of last failed local montage
    return s


def recent(ts: str | None, days: float) -> bool:
    return bool(ts) and now() - datetime.fromisoformat(ts) < timedelta(days=days)


def save_state(state: dict) -> None:
    if DRY_RUN:
        return
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE_FILE)


def chown_data_dir() -> None:
    """Hand DATA_DIR's contents to PROCESS_UID:PROCESS_GID."""
    for p in [DATA_DIR, *DATA_DIR.iterdir()]:
        try:
            os.chown(p, PROCESS_UID, PROCESS_GID)
        except OSError as e:
            log.warning("chown %s: %s", p, e)


def owned_file(search_dir: Path, name: str, owned: dict | None) -> Path | None:
    """Return a file matching `name`'s stem in search_dir that this tool did not write, if any."""
    if not search_dir.is_dir():
        return None
    for f in search_dir.glob(f"{Path(name).stem}.*"):
        if f.name.startswith("."):
            continue
        if f.name == name and owned:
            st = f.stat()
            if st.st_size == owned.get("size") and int(st.st_mtime) == owned.get("mtime"):
                continue  # ours, unchanged
        return f
    return None


# --------------------------------------------------------------------------- sources

def arr_items() -> list[dict]:
    """Each item: {kind, title, folder, ids, videos: [Path, ...]} - one path for a movie,
    up to SERIES_EPISODES representative episode paths for a series. `ids` are the
    ThemerrDB lookup keys."""
    items: list[dict] = []
    if RADARR_URL and RADARR_API_KEY:
        movies = http_json(f"{RADARR_URL}/api/v3/movie", {"X-Api-Key": RADARR_API_KEY}, 120)
        for m in movies:
            if not m.get("path") or not m.get("hasFile") or not m.get("movieFile"):
                continue
            items.append({
                "kind": "movie",
                "title": f"{m.get('title')} ({m.get('year')})",
                "folder": map_path(m["path"]),
                "ids": [("movies/themoviedb", m.get("tmdbId")), ("movies/imdb", m.get("imdbId"))],
                "net_id": str(m.get("tmdbId") or ""),
                "videos": [map_path(m["movieFile"]["path"])],
            })
        log.info("Radarr: %d movies with files", len(items))
    if SONARR_URL and SONARR_API_KEY:
        series = http_json(f"{SONARR_URL}/api/v3/series", {"X-Api-Key": SONARR_API_KEY}, 120)
        n_series = 0
        for s in series:
            if not s.get("path"):
                continue
            videos = pick_representative_episodes(s["id"])
            if not videos:
                continue
            items.append({
                "kind": "series",
                "title": f"{s.get('title')} ({s.get('year')})",
                "folder": map_path(s["path"]),
                "ids": [("tv_shows/themoviedb", s.get("tmdbId"))],  # ThemerrDB indexes TV by TMDb only
                "net_id": str(s.get("tvdbId") or ""),               # the network indexes TV by TVDB
                "videos": videos,
            })
            n_series += 1
        log.info("Sonarr: %d series with usable episodes", n_series)
    return items


def pick_representative_episodes(series_id: int) -> list[Path]:
    """Pick up to SERIES_EPISODES episode file paths spread across the show
    (skipping specials and the pilot), by joining /episode and /episodefile."""
    episodes = http_json(f"{SONARR_URL}/api/v3/episode?seriesId={series_id}",
                         {"X-Api-Key": SONARR_API_KEY}, 60)
    files = http_json(f"{SONARR_URL}/api/v3/episodefile?seriesId={series_id}",
                      {"X-Api-Key": SONARR_API_KEY}, 60)
    paths_by_file_id = {f["id"]: f.get("path") for f in files if f.get("path")}

    candidates = [
        e for e in episodes
        if e.get("hasFile") and e.get("seasonNumber", 0) > 0
        and e.get("episodeFileId") in paths_by_file_id
    ]
    candidates.sort(key=lambda e: (e["seasonNumber"], e["episodeNumber"]))
    if not candidates:
        return []
    # skip the pilot (index 0) unless it's the only episode available
    pool = candidates[1:] or candidates
    n = min(SERIES_EPISODES, len(pool))
    picks = [pool[round(i * (len(pool) - 1) / max(n - 1, 1))] for i in range(n)]
    seen: set[int] = set()
    result = []
    for e in picks:
        if e["episodeFileId"] in seen:
            continue
        seen.add(e["episodeFileId"])
        result.append(map_path(paths_by_file_id[e["episodeFileId"]]))
    return result


# --------------------------------------------------------------------------- ffmpeg / ffprobe

class GpuUnavailable(RuntimeError):
    """CUDA/NVENC is gone - abort the whole run instead of failing every item one by one."""


GPU_PROBE_CMD = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-init_hw_device", "cuda", "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
                 "-c:v", "h264_nvenc", "-f", "null", "-"]


def require_gpu() -> None:
    try:
        r = subprocess.run(GPU_PROBE_CMD, capture_output=True, text=True, errors="replace",
                           timeout=GPU_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise GpuUnavailable(f"GPU probe timed out after {GPU_PROBE_TIMEOUT:.0f}s") from None
    if r.returncode != 0:
        raise GpuUnavailable(f"GPU probe failed: {r.stderr.strip()[-300:]}")


def run_gpu(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a CUDA/NVENC ffmpeg command. On failure, re-probe the GPU: if it's gone,
    raise GpuUnavailable; otherwise the failure is this file's problem."""
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT, **kwargs)
        if time.monotonic() - t0 > SLOW_CALL_SEC:
            log.warning("  slow ffmpeg call (%.0fs): %s", time.monotonic() - t0, " ".join(cmd[-8:]))
    except subprocess.TimeoutExpired:
        log.warning("  ffmpeg timed out after %.0fs: %s", FFMPEG_TIMEOUT, " ".join(cmd[-3:]))
        require_gpu()  # raises if the GPU is what hung; otherwise it's this file's problem
        empty = "" if kwargs.get("text") else b""
        return subprocess.CompletedProcess(cmd, 124, empty, empty)
    if r.returncode != 0:
        require_gpu()
    return r


def probe_duration(path: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, errors="replace",
                           timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 0.0
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


THUMB_BYTES = 32 * 32 * 3
THUMB_FPS = 2  # thumbnails per second of the window; starts are snapped to this grid


def window_thumbs(path: Path, window_start: float, window_len: float) -> list[bytes]:
    """32x32 thumbnails of [window_start, window_start+window_len] at THUMB_FPS, from ONE
    ffmpeg call. Probing motion per candidate start cost 3 seeks+CUDA inits each (up to
    ~90 per sample), which blew the DAG timeout on 4K remuxes."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-hwaccel", "cuda",
           "-ss", str(max(window_start, 0)), "-i", str(path), "-t", str(window_len), "-an",
           "-vf", f"fps={THUMB_FPS},scale=32:32", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = run_gpu(cmd)
    if r.returncode != 0:
        return []
    return [r.stdout[i:i + THUMB_BYTES] for i in range(0, len(r.stdout) - THUMB_BYTES + 1, THUMB_BYTES)]


def frame_pixels(path: Path, t: float) -> bytes | None:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-hwaccel", "cuda",
           "-ss", str(max(t, 0)), "-i", str(path), "-frames:v", "1",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "32x32", "-"]
    r = run_gpu(cmd)
    return r.stdout if r.returncode == 0 and r.stdout else None


def pixel_diff(a: bytes, b: bytes) -> float:
    if len(a) != len(b):
        return 0.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def has_motion(thumbs: list[bytes], window_start: float, start: float) -> bool:
    def at(t: float) -> bytes | None:
        i = round((t - window_start) * THUMB_FPS)
        return thumbs[i] if 0 <= i < len(thumbs) else None

    a, b, c = at(start), at(start + SAMPLE_DURATION / 2), at(start + SAMPLE_DURATION)
    if not a or not b or not c:
        return False
    return (pixel_diff(a, b) + pixel_diff(b, c)) / 2 >= MIN_MOTION


def cuts_in_window(path: Path, window_start: float, window_len: float) -> list[float]:
    """Scene-cut timestamps (absolute) inside [window_start, window_start+window_len].
    Scoped to a small local window rather than the whole file - much cheaper."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-hwaccel", "cuda",
           "-ss", str(max(window_start, 0)), "-i", str(path), "-t", str(window_len),
           "-vf", f"scale=320:-1,select='gt(scene,{SCENE_THRESHOLD})',showinfo",
           "-f", "null", "-"]
    r = run_gpu(cmd, text=True, errors="replace")
    if r.returncode != 0:
        return []
    cuts = []
    for line in r.stderr.splitlines():
        if "pts_time:" not in line:
            continue
        try:
            cuts.append(window_start + float(line.split("pts_time:")[1].split()[0]))
        except (IndexError, ValueError):
            continue
    return cuts


def pick_window(path: Path, target: float, lo: float, hi: float) -> float | None:
    """Find a SAMPLE_DURATION-long window near `target` (within [lo, hi]) with no
    scene cut and enough motion, searching outward. None if nothing qualifies."""
    window_lo = max(lo, target - SEARCH_RADIUS)
    window_hi = min(hi - SAMPLE_DURATION, target + SEARCH_RADIUS)
    if window_hi <= window_lo:
        return None
    span = (window_hi - window_lo) + SAMPLE_DURATION
    cuts = cuts_in_window(path, window_lo, span)
    thumbs = window_thumbs(path, window_lo, span)

    def clean(start: float) -> bool:
        return not any(start < c < start + SAMPLE_DURATION for c in cuts)

    offsets = [0.0]
    step = SEARCH_STEP
    while step <= SEARCH_RADIUS:
        offsets += [step, -step]
        step += SEARCH_STEP
    for off in offsets:
        start = target + off
        if not (window_lo <= start <= window_hi):
            continue
        if clean(start) and has_motion(thumbs, window_lo, start):
            return start
    return None


def extract_clip(path: Path, start: float, out: Path) -> bool:
    vf = f"scale=-2:min({BACKDROP_MAX_HEIGHT}\\,ih),format=yuv420p"  # Pascal NVENC can't encode 10-bit
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-hwaccel", "cuda",
           "-ss", str(start), "-i", str(path), "-t", str(SAMPLE_DURATION), "-an",
           "-vf", vf, "-c:v", "h264_nvenc", "-preset", NVENC_PRESET, "-rc", "vbr",
           "-cq", NVENC_CQ, "-b:v", "0", str(out)]
    r = run_gpu(cmd, text=True, errors="replace")
    if r.returncode != 0 or not out.exists():
        log.error("  NVENC clip extract failed: %s", r.stderr.strip()[-400:])
        return False
    return True


def concat_clips(clips: list[Path], tmp: Path, dest: Path) -> bool:
    listfile = tmp / "concat.txt"
    listfile.write_text("".join(f"file '{c}'\n" for c in clips))
    out = tmp / "backdrop.mp4"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        r = subprocess.CompletedProcess(cmd, 124, "", "timed out")
    if r.returncode != 0 or not out.exists():
        log.warning("  concat -c copy failed, re-encoding: %s", r.stderr.strip()[-300:])
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", str(listfile),
               "-c:v", "h264_nvenc", "-preset", NVENC_PRESET, "-rc", "vbr",
               "-cq", NVENC_CQ, "-b:v", "0", str(out)]
        r = run_gpu(cmd, text=True, errors="replace")
        if r.returncode != 0 or not out.exists():
            log.error("  concat failed: %s", r.stderr.strip()[-400:])
            return False
    staging = dest.with_name(f".{dest.name}.partial")
    shutil.copyfile(out, staging)
    staging.replace(dest)
    return True


def generate_backdrop(videos: list[Path], dest: Path) -> bool:
    global item_deadline
    item_deadline = time.monotonic() + ITEM_BUDGET_MIN * 60 if ITEM_BUDGET_MIN else float("inf")
    try:
        return _generate_backdrop(videos, dest)
    except ItemTooSlow:
        log.error("  gave up after ITEM_BUDGET_MIN=%.0f min: %s", ITEM_BUDGET_MIN, videos)
        return False
    finally:
        item_deadline = float("inf")


def _generate_backdrop(videos: list[Path], dest: Path) -> bool:
    per_video = max(N_SAMPLES // len(videos), 1)
    clips: list[Path] = []
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp_s:
        tmp = Path(tmp_s)
        for video in videos:
            duration = probe_duration(video)
            if duration <= 0:
                log.warning("  no duration for %s, skipping", video)
                continue
            lo = duration * SKIP_START_PCT / 100
            hi = duration * (1 - SKIP_END_PCT / 100)
            if hi <= lo:
                continue
            targets = [lo + (hi - lo) * (i + 0.5) / per_video for i in range(per_video)]
            for i, target in enumerate(targets):
                check_budget()
                start = pick_window(video, target, lo, hi)
                if start is None:
                    log.info("  no clean window near %.0fs", target)
                    continue
                clip = tmp / f"clip_{len(clips):03d}.mp4"
                if extract_clip(video, start, clip):
                    clips.append(clip)
        if not clips:
            log.error("  no usable clips extracted from %s", videos)
            return False
        return concat_clips(clips, tmp, dest)


# --------------------------------------------------------------------------- themerrdb / yt-dlp

def lookup_theme(item: dict, state: dict) -> str | None:
    """Return the item's ThemerrDB YouTube URL, or None. Caches negative results."""
    for id_type, id_val in item["ids"]:
        if not id_val:
            continue
        key = f"{id_type}/{id_val}"
        if recent(state["missing"].get(key), RECHECK_DAYS):
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
        if url := data.get("youtube_theme_url"):
            return url
    return None


def is_rejected(url: str, state: dict) -> bool:
    """Static is permanent; unavailable is re-tried after REJECT_RETRY_DAYS."""
    rej = state["rejected"].get(url) or {}
    return bool(rej.get("static")) or recent(rej.get("checked"), REJECT_RETRY_DAYS)


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
    ver = subprocess.run([str(YTDLP), "--version"], capture_output=True, text=True, errors="replace")
    log.info("yt-dlp %s", ver.stdout.strip() or "?")


def download(url: str, tmp: Path) -> tuple[str, Path | str]:
    """("ok", path) or (status, reason) with status unusable | ratelimited | error."""
    cmd = [str(YTDLP), "--no-playlist", "--no-progress", "--no-warnings",
           "--match-filters", f"availability=?public & age_limit<?18 & "
                              f"duration>=?{MIN_DURATION} & duration<=?{MAX_DURATION}",
           "-S", f"res:{BACKDROP_MAX_HEIGHT},vcodec:h264,ext:mp4:m4a",
           "--merge-output-format", "mp4", "-o", f"{tmp}/src.%(ext)s", url]
    if YTDLP_COOKIES:
        cmd[1:1] = ["--cookies", YTDLP_COOKIES]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    out = f"{r.stdout}\n{r.stderr}"
    if "does not pass filter" in out:
        return "unusable", f"outside link rules (public, <18, {MIN_DURATION}-{MAX_DURATION}s)"
    src = next((p for p in tmp.glob("src.*") if not p.name.endswith(".part")), None)
    if r.returncode == 0 and src is not None:
        return "ok", src
    errors = [l for l in out.splitlines() if l.startswith("ERROR:")]
    reason = (errors[-1] if errors else out.strip()[-300:])[:300]
    if RATE_LIMIT_RE.search(out):
        return "ratelimited", reason
    if UNUSABLE_RE.search(" ".join(errors)):
        return "unusable", reason
    return "error", reason


def is_static_video(path: Path, duration: float) -> bool | None:
    """A still image (or slideshow) with music - the common ThemerrDB upload. At each of
    STATIC_SAMPLES points, two frames STATIC_GAP apart are compared; static when fewer
    than STATIC_MIN_MOVING_PCT of the points show motion. None = too few frames decoded."""
    span = duration - STATIC_GAP
    moving = checked = 0
    for i in range(STATIC_SAMPLES):
        t = span * (i + 1) / (STATIC_SAMPLES + 1)
        a, b = frame_pixels(path, t), frame_pixels(path, t + STATIC_GAP)
        if not a or not b:
            continue
        checked += 1
        moving += pixel_diff(a, b) >= STATIC_THRESHOLD
    if checked < 2:
        return None
    return moving * 100 < checked * STATIC_MIN_MOVING_PCT


def fetch_themerr(url: str, dest: Path) -> tuple[str, str]:
    """Download, reject a static video, remux (audio kept) and atomically write `dest`.
    Returns ("ok", "") or (static | unusable | ratelimited | error, reason)."""
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp_s:
        tmp = Path(tmp_s)
        status, src = download(url, tmp)
        if status != "ok":
            return status, src
        duration = probe_duration(src)
        if duration <= STATIC_GAP:
            return "error", "unreadable download"
        static = is_static_video(src, duration)
        if static is None:
            return "error", "too few frames decoded for the motion check"
        if static:
            return "static", "no motion (still image)"
        out = tmp / "out.mp4"
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
               "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-sn",
               "-c", "copy", "-movflags", "+faststart", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0 or not out.exists():
            return "error", f"remux failed: {r.stderr.strip()[-300:]}"
        staging = dest.with_name(f".{dest.name}.partial")
        shutil.copyfile(out, staging)
        staging.replace(dest)
        return "ok", ""


# --------------------------------------------------------------------------- community network

NET_ENTRY_RE = re.compile(r"<(-?\d+)>([A-Za-z0-9]+)</\1>")
NET_URL_RE = re.compile(r"https?://download[^\"'\s<>]+", re.I)
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")


def load_network_index(kind: str) -> dict[str, str]:
    """TMDB id (movie) / TVDB id (series) -> MediaFire key, from the Emby theme-video
    plugins' community index. Falls back to the last good copy when the fetch fails."""
    name = "movies.php" if kind == "movie" else "tv.php"
    cache = DATA_DIR / f"network-index-{kind}.json"
    req = urllib.request.Request(f"{NETWORK_BASE}/{name}", headers={
        "User-Agent": UA, "ApiKey": NETWORK_API_KEY, "ApiUN": NETWORK_API_UN})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read().decode("utf-8", errors="replace")
        index = {i: k for i, k in NET_ENTRY_RE.findall(body) if k.lower() != "false" and int(i) > 0}
        if index:
            cache.write_text(json.dumps(index))
            return index
        log.warning("Network index %s came back empty", name)
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("Network index %s: %s", name, e)
    try:
        index = json.loads(cache.read_text())
        log.warning("Using cached network index for %ss (%d entries)", kind, len(index))
        return index
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def resolve_network(key: str) -> tuple[str, str]:
    """MediaFire key -> direct download URL. ("ok", url) or (status, reason)."""
    req = urllib.request.Request(f"https://www.mediafire.com/file/{key}", headers={"User-Agent": CHROME_UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            page = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):
            return "ratelimited", f"MediaFire page HTTP {e.code}"
        return ("unusable" if e.code == 404 else "error"), f"MediaFire page HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return "error", f"MediaFire page: {e}"
    urls = [html.unescape(u) for u in NET_URL_RE.findall(page)]
    url = next((u for u in urls if f"/{key}/" in u), urls[0] if urls else None)
    return ("ok", url) if url else ("unusable", "no download link on the MediaFire page")


def download_network(url: str, dest: Path) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": CHROME_UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if "text/html" in r.headers.get("Content-Type", "").lower():
                return "ratelimited", "got an HTML page instead of a video (server load limit?)"
            size = int(r.headers.get("Content-Length") or 0)
            if size > NETWORK_MAX_MB * 1024 * 1024:
                return "unusable", f"{size / 1024 / 1024:.0f} MB exceeds NETWORK_MAX_MB"
            with open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
    except urllib.error.HTTPError as e:
        return ("ratelimited" if e.code in (403, 429, 503) else "error"), f"download HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return "error", f"download: {e}"
    return "ok", ""


def stream_codec(path: Path, select: str) -> str:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", select, "-show_entries",
                        "stream=codec_name", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, errors="replace")
    return r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""


def remux_network(src: Path, out: Path) -> str:
    """Remux to mp4 (video copied), normalising the audio peak to NORMALISE_MAX_DB like the
    Emby plugin did. "" on success, else the error."""
    base = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-sn", "-c:v", "copy"]
    if stream_codec(src, "v:0") in ("hevc", "h265"):
        base += ["-tag:v", "hvc1"]
    audio = ["-c:a", "copy"]
    if stream_codec(src, "a:0"):
        audio = ["-c:a", "aac", "-ac", "2", "-ar", "44100", "-b:a", "128k"]
        if NORMALISE_AUDIO:
            r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(src), "-t", "60",
                                "-vn", "-af", "volumedetect", "-f", "null", "-"],
                               capture_output=True, text=True, errors="replace")
            m = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
            if m:
                audio += ["-af", f"volume={NORMALISE_MAX_DB - float(m.group(1)):.1f}dB"]
            else:
                log.warning("  volumedetect found no audio level, leaving the volume as is")
    tail = ["-movflags", "+faststart", str(out)]
    # copying the source audio into mp4 can fail (e.g. mkv/opus): retry once as aac
    for a in ([audio] if audio[1] == "aac" else [audio, ["-c:a", "aac", "-ac", "2", "-b:a", "128k"]]):
        r = subprocess.run(base + a + tail, capture_output=True, text=True, errors="replace")
        if r.returncode == 0 and out.exists():
            return ""
    return r.stderr.strip()[-300:]


def fetch_network(key: str, dest: Path) -> tuple[str, str]:
    """Resolve, download, validate (video stream, not a still image), remux/normalise and
    atomically write `dest`. ("ok", "") or (static | unusable | ratelimited | error, reason)."""
    status, url = resolve_network(key)
    if status != "ok":
        return status, url
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp_s:
        tmp = Path(tmp_s)
        src = tmp / "src"
        status, reason = download_network(url, src)
        if status != "ok":
            return status, reason
        if not stream_codec(src, "v:0"):
            return "unusable", "downloaded file has no video stream"
        duration = probe_duration(src)
        if duration <= STATIC_GAP:
            return "unusable", "unreadable or too short"
        static = is_static_video(src, duration)
        if static is None:
            return "error", "too few frames decoded for the motion check"
        if static:
            return "static", "no motion (still image)"
        out = tmp / "out.mp4"
        if err := remux_network(src, out):
            return "error", f"remux failed: {err}"
        staging = dest.with_name(f".{dest.name}.partial")
        shutil.copyfile(out, staging)
        staging.replace(dest)
        return "ok", ""


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

def source_fingerprint(paths: list[Path]) -> list[dict]:
    out = []
    for p in paths:
        st = p.stat()
        out.append({"path": str(p), "size": st.st_size, "mtime": int(st.st_mtime)})
    return out


def needs_regen(owned: dict | None, current: list[dict]) -> bool:
    if owned is None:
        return True
    return owned.get("sources") != current


class Downloads:
    """Per-run download budget for one remote: a cap, a delay, and a stop on rate limiting."""

    def __init__(self, label: str, limit: int, delay: float):
        self.label, self.limit, self.delay = label, limit, delay
        self.count = 0
        self.stopped = False
        self.budget_warned = False

    def available(self) -> bool:
        if self.stopped:
            return False
        if self.limit and self.count >= self.limit:
            if not self.budget_warned:
                log.info("Download budget reached, skipping further %s downloads this run", self.label)
                self.budget_warned = True
            return False
        return True

    def start(self) -> None:
        if self.count:
            time.sleep(self.delay)
        self.count += 1


def try_themerr(item: dict, url: str, dest: Path, state: dict, dl: Downloads) -> str:
    """fetch_themerr()'s status ("ok" = `dest` now holds the ThemerrDB video). A static
    or unusable URL is recorded in state["rejected"]; anything else is retried next run."""
    log.info("Trying ThemerrDB video: %s -> %s", item["title"], url)
    if DRY_RUN:
        return "dry-run"
    dl.start()
    dest.parent.mkdir(parents=True, exist_ok=True)
    status, reason = fetch_themerr(url, dest)
    if status in ("static", "unusable"):
        log.info("  rejected (%s): %s", status, reason)
        state["rejected"][url] = {"reason": reason, "checked": now().isoformat(),
                                  "static": status == "static"}
    elif status == "ratelimited":
        log.error("YouTube rate limit / bot check, stopping downloads for this run: %s", reason)
        dl.stopped = True
    elif status != "ok":
        log.error("  ThemerrDB download failed: %s", reason)
    return status


def try_network(item: dict, key: str, dest: Path, state: dict, dl: Downloads) -> str:
    """fetch_network()'s status ("ok" = `dest` now holds the community video). Static and
    unusable are recorded in state["rejected"]; anything else is retried next run."""
    log.info("Trying community video: %s -> mediafire %s", item["title"], key)
    if DRY_RUN:
        return "dry-run"
    dl.start()
    dest.parent.mkdir(parents=True, exist_ok=True)
    status, reason = fetch_network(key, dest)
    if status in ("static", "unusable"):
        log.info("  rejected (%s): %s", status, reason)
        state["rejected"][f"network/{key}"] = {"reason": reason, "checked": now().isoformat(),
                                               "static": status == "static"}
    elif status == "ratelimited":
        log.error("Community host is throttling, stopping network downloads for this run: %s", reason)
        dl.stopped = True
    elif status != "ok":
        log.error("  community download failed: %s", reason)
    else:
        log.info("  community video saved")
    return status


def record_owned(state: dict, key: str, dest: Path, **extra) -> None:
    os.chown(dest, MEDIA_UID, MEDIA_GID)
    st = dest.stat()
    state["owned"][key] = {**extra, "size": st.st_size, "mtime": int(st.st_mtime),
                           "written": now().isoformat()}
    save_state(state)


def run_once() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DRY_RUN:
        require_gpu()
        if THEMERR_VIDEOS:
            ensure_ytdlp()
    state = load_state()

    items = arr_items()
    stats = dict(added=0, updated=0, network=0, themerr=0, rejected=0, user=0, skipped=0,
                 nosource=0, failed=0, nofolder=0)
    processed = 0
    budget_warned = False
    dl = Downloads("ThemerrDB", MAX_DOWNLOADS, DOWNLOAD_DELAY)
    net = Downloads("community", MAX_NETWORK_DOWNLOADS, NETWORK_DELAY)
    net_index = ({k: load_network_index(k) for k in {i["kind"] for i in items}}
                 if NETWORK_VIDEOS else {})

    for item in items:
        folder: Path = item["folder"]
        if not folder.is_dir():
            stats["nofolder"] += 1
            continue
        if (folder / ".nobackdrop").exists():
            stats["skipped"] += 1
            continue

        key = str(folder)
        backdrop_dir = folder / "backdrops"
        dest = backdrop_dir / BACKDROP_NAME
        owned = state["owned"].get(key)
        if owned_file(backdrop_dir, BACKDROP_NAME, owned):
            stats["user"] += 1
            state["owned"].pop(key, None)
            continue

        videos = [v for v in item["videos"] if v.is_file()]
        if not videos:
            stats["nosource"] += 1
            continue

        have_ours = owned is not None and dest.exists()
        source = owned.get("source", "local") if have_ours else None

        net_key = net_index.get(item["kind"], {}).get(item["net_id"])
        if (net_key and not (source == "network" and owned.get("mf_key") == net_key)
                and not is_rejected(f"network/{net_key}", state) and net.available()):
            status = try_network(item, net_key, dest, state, net)
            if status == "ok":
                record_owned(state, key, dest, source="network", mf_key=net_key)
                stats["updated" if have_ours else "added"] += 1
                stats["network"] += 1
                continue
            stats["rejected"] += status in ("static", "unusable")
        if source == "network":
            continue  # a community video beats everything else

        url = lookup_theme(item, state) if THEMERR_VIDEOS else None
        if (url and not (source == "themerr" and owned.get("url") == url)
                and not is_rejected(url, state) and dl.available()):
            status = try_themerr(item, url, dest, state, dl)
            if status == "ok":
                record_owned(state, key, dest, source="themerr", url=url)
                stats["updated" if have_ours else "added"] += 1
                stats["themerr"] += 1
                continue
            stats["rejected"] += status in ("static", "unusable")
        if source == "themerr":
            continue  # a working ThemerrDB video beats a local montage

        current_sources = source_fingerprint(videos)
        if have_ours and not needs_regen(owned, current_sources):
            continue

        if RUN_BUDGET_MIN and time.monotonic() > DEADLINE:
            log.warning("RUN_BUDGET_MIN reached, stopping; remaining items next run")
            break

        if MAX_ITEMS_PER_RUN and processed >= MAX_ITEMS_PER_RUN:
            if not budget_warned:
                log.info("MAX_ITEMS_PER_RUN reached, skipping further backdrops this run")
                budget_warned = True
            continue

        if recent(state["failed"].get(key), FAIL_RETRY_DAYS):
            stats["failed"] += 1
            continue

        action = "Updating" if have_ours else "Adding"
        log.info("%s backdrop: %s", action, item["title"])
        if DRY_RUN:
            stats["updated" if have_ours else "added"] += 1
            continue

        processed += 1
        backdrop_dir.mkdir(parents=True, exist_ok=True)
        try:
            generated = generate_backdrop(videos, dest)
        except OutOfTime:
            log.warning("RUN_BUDGET_MIN reached mid-item, stopping; %s is retried next run", item["title"])
            break
        if generated:
            record_owned(state, key, dest, source="local", sources=current_sources)
            state["failed"].pop(key, None)
            stats["updated" if have_ours else "added"] += 1
        else:
            state["failed"][key] = now().isoformat()
            save_state(state)
            stats["failed"] += 1

    save_state(state)
    chown_data_dir()
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
    # deno (yt-dlp's YouTube JS runtime) is vendored into DATA_DIR by the DAG's vendor step
    os.environ["PATH"] = f"{DATA_DIR}{os.pathsep}{os.environ.get('PATH', '')}"
    for tool in ("ffmpeg", "ffprobe", *(("deno",) if THEMERR_VIDEOS else ())):
        if shutil.which(tool) is None:
            log.error("%s not found in PATH", tool)
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
