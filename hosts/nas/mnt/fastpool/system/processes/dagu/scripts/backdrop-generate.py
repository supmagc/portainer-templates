#!/usr/bin/env python3
"""
backdrop-generate — build Emby/Jellyfin "theme videos" (backdrops/theme.mp4) for
Radarr/Sonarr libraries. ThemerrDB's theme-song video is tried first; when it is a
still image (or missing/unusable), a montage of short, cut-free, motion-containing
clips is sampled directly out of the movie/episode files instead (see docs/monitoring.md).

Rules:
  * A file in backdrops/ that this tool did not write is treated as user-provided
    and never touched.
  * A folder containing a `.nobackdrop` file is skipped entirely.
  * A ThemerrDB URL that turns out static is remembered and never downloaded again;
    a new URL for the same item is tried on a later run and replaces a local montage.
  * A local montage is regenerated only if its source file(s) changed (size/mtime).
  * Movies sample from the one file. Series sample from SERIES_EPISODES
    representative episodes spread across the show, skipping the pilot.
"""

from __future__ import annotations

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

NVENC_PRESET = env("NVENC_PRESET", "p4")
NVENC_CQ = env("NVENC_CQ", "23")
GPU_PROBE_TIMEOUT = env_num("GPU_PROBE_TIMEOUT", 60)       # seconds; a wedged driver hangs, not errors

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
    # folder -> {source: local|themerr, sources: [{path,size,mtime}] | url, size, mtime, written}
    s.setdefault("owned", {})
    s.setdefault("missing", {})   # ThemerrDB key -> iso timestamp of last negative lookup
    s.setdefault("rejected", {})  # YouTube URL -> {reason, checked, static}
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
    r = subprocess.run(cmd, capture_output=True, **kwargs)
    if r.returncode != 0:
        require_gpu()
    return r


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, errors="replace")
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


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


def has_motion(path: Path, start: float) -> bool:
    mid, end = start + SAMPLE_DURATION / 2, start + SAMPLE_DURATION
    a, b, c = frame_pixels(path, start), frame_pixels(path, mid), frame_pixels(path, end)
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
    cuts = cuts_in_window(path, window_lo, (window_hi - window_lo) + SAMPLE_DURATION)

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
        if clean(start) and has_motion(path, start):
            return start
    return None


def extract_clip(path: Path, start: float, out: Path) -> bool:
    vf = f"scale=-2:min({BACKDROP_MAX_HEIGHT}\\,ih)"
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
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
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
                start = pick_window(video, target, lo, hi)
                if start is None:
                    log.debug("  no clean window near %.1fs in %s", target, video)
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
    """Per-run YouTube budget: MAX_DOWNLOADS, DOWNLOAD_DELAY, and a stop on rate limiting."""

    def __init__(self):
        self.count = 0
        self.stopped = False
        self.budget_warned = False

    def available(self) -> bool:
        if self.stopped:
            return False
        if MAX_DOWNLOADS and self.count >= MAX_DOWNLOADS:
            if not self.budget_warned:
                log.info("MAX_DOWNLOADS reached, skipping further ThemerrDB downloads this run")
                self.budget_warned = True
            return False
        return True

    def start(self) -> None:
        if self.count:
            time.sleep(DOWNLOAD_DELAY)
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
    stats = dict(added=0, updated=0, themerr=0, rejected=0, user=0, skipped=0,
                 nosource=0, failed=0, nofolder=0)
    processed = 0
    budget_warned = False
    dl = Downloads()

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

        if MAX_ITEMS_PER_RUN and processed >= MAX_ITEMS_PER_RUN:
            if not budget_warned:
                log.info("MAX_ITEMS_PER_RUN reached, skipping further backdrops this run")
                budget_warned = True
            continue

        action = "Updating" if have_ours else "Adding"
        log.info("%s backdrop: %s", action, item["title"])
        if DRY_RUN:
            stats["updated" if have_ours else "added"] += 1
            continue

        processed += 1
        backdrop_dir.mkdir(parents=True, exist_ok=True)
        if generate_backdrop(videos, dest):
            record_owned(state, key, dest, source="local", sources=current_sources)
            stats["updated" if have_ours else "added"] += 1
        else:
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
