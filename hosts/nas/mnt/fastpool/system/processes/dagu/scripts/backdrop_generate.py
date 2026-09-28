#!/usr/bin/env python3
"""
backdrop-generate — build Emby/Jellyfin "theme videos" (backdrops/theme.mp4) for
Radarr/Sonarr libraries by sampling short, cut-free, motion-containing clips
directly out of the movie/episode files themselves. There is no external database
for backdrop videos the way ThemerrDB covers theme songs (see docs/monitoring.md) -
every real precedent (emby-theme-maker, backdrop-generator) works this way.

Rules:
  * A file in backdrops/ that this tool did not write is treated as user-provided
    and never touched.
  * A folder containing a `.nobackdrop` file is skipped entirely.
  * A backdrop is regenerated only if its source file(s) changed (size/mtime) -
    there's no upstream URL to compare against like themerr-fetch has.
  * Movies sample from the one file. Series sample from SERIES_EPISODES
    representative episodes spread across the show, skipping the pilot.
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
from datetime import datetime, timezone
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

NVENC_PRESET = env("NVENC_PRESET", "p4")
NVENC_CQ = env("NVENC_CQ", "23")

MAX_ITEMS_PER_RUN = int(env_num("MAX_ITEMS_PER_RUN", 20))  # GPU/CPU budget, 0 = unlimited
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
    s.setdefault("owned", {})  # folder -> {sources: [{path,size,mtime}], size, mtime, written}
    return s


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
    """Each item: {kind, title, folder, videos: [Path, ...]} - one path for a movie,
    up to SERIES_EPISODES representative episode paths for a series."""
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

def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, errors="replace")
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def frame_pixels(path: Path, t: float, hwaccel: bool = True) -> bytes | None:
    base = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if hwaccel:
        base += ["-hwaccel", "cuda"]
    cmd = base + ["-ss", str(max(t, 0)), "-i", str(path), "-frames:v", "1",
                  "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "32x32", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode == 0 and r.stdout:
        return r.stdout
    return frame_pixels(path, t, hwaccel=False) if hwaccel else None


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


def cuts_in_window(path: Path, window_start: float, window_len: float, hwaccel: bool = True) -> list[float]:
    """Scene-cut timestamps (absolute) inside [window_start, window_start+window_len].
    Scoped to a small local window rather than the whole file - much cheaper."""
    base = ["ffmpeg", "-nostdin", "-hide_banner"]
    if hwaccel:
        base += ["-hwaccel", "cuda"]
    cmd = base + ["-ss", str(max(window_start, 0)), "-i", str(path), "-t", str(window_len),
                  "-vf", f"scale=320:-1,select='gt(scene,{SCENE_THRESHOLD})',showinfo",
                  "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        if hwaccel:
            return cuts_in_window(path, window_start, window_len, hwaccel=False)
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
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode == 0 and out.exists():
        return True
    log.warning("  NVENC clip extract failed, falling back to CPU: %s", r.stderr.strip()[-300:])
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-ss", str(start), "-i", str(path), "-t", str(SAMPLE_DURATION), "-an",
           "-vf", vf, "-c:v", "libx264", "-crf", "23", "-preset", "veryfast", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0 or not out.exists():
        log.error("  ffmpeg clip extract failed: %s", r.stderr.strip()[-400:])
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
               "-c:v", "libx264", "-crf", "23", "-preset", "veryfast", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
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


def run_once() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    items = arr_items()
    stats = dict(added=0, updated=0, user=0, skipped=0, nosource=0, failed=0, nofolder=0)
    processed = 0
    budget_warned = False

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

        current_sources = source_fingerprint(videos)
        have_ours = owned is not None and dest.exists()
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
            os.chown(dest, MEDIA_UID, MEDIA_GID)
            st = dest.stat()
            state["owned"][key] = {"sources": current_sources, "size": st.st_size,
                                   "mtime": int(st.st_mtime), "written": now().isoformat()}
            stats["updated" if have_ours else "added"] += 1
            save_state(state)
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
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        log.error("ffmpeg/ffprobe not found in PATH")
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
