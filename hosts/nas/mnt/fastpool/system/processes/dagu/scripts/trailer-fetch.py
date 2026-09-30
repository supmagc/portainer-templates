#!/usr/bin/env python3
"""
trailer-fetch — download original-language trailers for Radarr/Sonarr libraries into
each item's trailers/ folder, with baked-in letterbox/pillarbox bars cropped out.

Trailer choice comes from TMDB's per-video language tags (Sonarr writes no trailer to
its NFOs; Radarr's NFO <trailer> is just its API youTubeTrailerId, used here as the
preferred pick when it's in the original language). See docs/monitoring.md.

Rules:
  * Up to MAX_TRAILERS per item, each written as `<Title (Year)> - <TMDB name> [<id>].mp4`
    and tracked per YouTube id in state.json.
  * Any other video in trailers/ (Trailarr's or hand-placed) is removed once the item has
    at least one of ours. Only trailers/ is looked at; a `*-trailer.*` next to the main
    video is library-cleanup's to move in here.
  * A folder containing a `.notrailer` file is skipped entirely.
  * With ORIGINAL_LANGUAGE_ONLY (default), an item with no trailer tagged in its
    original language gets none rather than a dub.
  * Videos must pass ThemerrDB's link rules (public, not age-restricted, playable from
    here, MIN_DURATION..MAX_DURATION); failing ones are excluded for BAD_VIDEO_DAYS and
    the next candidate is tried.
  * Transient download failures back off exponentially per item; a YouTube rate-limit /
    bot-check stops all further YouTube requests for the run.
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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("trailer-fetch")


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
TMDB_API_KEY = env("TMDB_API_KEY")            # v3 API key or v4 read access token
TMDB_BASE = "https://api.themoviedb.org/3"

# "arr_prefix=local_prefix;arr_prefix2=local_prefix2"
PATH_MAP = [
    tuple(p.split("=", 1)) for p in env("PATH_MAP").split(";") if "=" in p
]

DATA_DIR = Path(env("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "state.json"
YTDLP = Path(env("YTDLP_PATH", str(DATA_DIR / "yt-dlp")))
YTDLP_AUTOUPDATE = env_bool("YTDLP_AUTOUPDATE", True)
YTDLP_COOKIES = env("YTDLP_COOKIES")          # optional Netscape cookies.txt
TRAILER_DIR = env("TRAILER_DIR", "trailers")
MAX_TRAILERS = int(env_num("MAX_TRAILERS", 5))   # per item, 0 = unlimited
MAX_HEIGHT = int(env_num("MAX_HEIGHT", 1080))
VIDEO_TYPES = [t.strip() for t in env("VIDEO_TYPES", "Trailer,Teaser").split(",") if t.strip()]
ORIGINAL_LANGUAGE_ONLY = env_bool("ORIGINAL_LANGUAGE_ONLY", True)
RECHECK_DAYS = env_num("RECHECK_DAYS", 14)       # re-query items below MAX_TRAILERS
BAD_VIDEO_DAYS = env_num("BAD_VIDEO_DAYS", 90)   # re-allow a rejected video after N days
MAX_BACKOFF_DAYS = env_num("MAX_BACKOFF_DAYS", 30)
MIN_DURATION = int(env_num("MIN_DURATION", 20))  # seconds, ThemerrDB's link limits
MAX_DURATION = int(env_num("MAX_DURATION", 300))
TMDB_DELAY = env_num("TMDB_DELAY", 0.1)
DOWNLOAD_DELAY = env_num("DOWNLOAD_DELAY", 5)
MAX_DOWNLOADS = int(env_num("MAX_DOWNLOADS", 0))  # YouTube attempts per run, 0 = unlimited

CROP_BARS = env_bool("CROP_BARS", True)
CROP_SAMPLES = int(env_num("CROP_SAMPLES", 8))
CROP_MIN_PCT = env_num("CROP_MIN_PCT", 2)        # ignore bars thinner than this % of w/h
CROP_LIMIT = int(env_num("CROP_LIMIT", 24))      # luma at or below this counts as black (black ~16)
CROP_OVERLAY_PCT = env_num("CROP_OVERLAY_PCT", 25)  # bright share of a bar row still treated as bar (captions/logos)
NVENC_PRESET = env("NVENC_PRESET", "p5")
NVENC_CQ = env("NVENC_CQ", "24")

PROCESS_UID = int(env_num("PROCESS_UID", 920))  # nas_processes - owns everything under DATA_DIR
PROCESS_GID = int(env_num("PROCESS_GID", 920))
MEDIA_UID = int(env_num("MEDIA_UID", 910))      # nas_multimedia - owns written trailers
MEDIA_GID = int(env_num("MEDIA_GID", 910))

# the media server must see the library at the same paths this container does
MEDIASERVER_TYPE = env("MEDIASERVER_TYPE").lower()   # emby | jellyfin | empty
MEDIASERVER_URL = env("MEDIASERVER_URL").rstrip("/")
MEDIASERVER_API_KEY = env("MEDIASERVER_API_KEY")
DRY_RUN = env_bool("DRY_RUN")

UA = "trailer-fetch/1.0"
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts"}  # keep in sync with library-cleanup's TRAILER_EXTS

# checked before UNUSABLE_RE: a rate-limited session can get "Video unavailable ... try again later"
RATE_LIMIT_RE = re.compile(r"not a bot|HTTP Error 429|Too Many Requests|try again later", re.I)
TAG_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.mp4$")  # the id tag trailer_filename() writes
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


def recent(ts: str | None, days: float) -> bool:
    return bool(ts) and now() - datetime.fromisoformat(ts) < timedelta(days=days)


def load_state() -> dict:
    try:
        s = json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        s = {}
    s.setdefault("owned", {})    # folder -> {youtube key -> {file, lang, crop, written}}
    s.setdefault("checked", {})  # folder -> iso timestamp of the last complete candidate pass
    s.setdefault("tmdb", {})     # "movie/123" -> {original_language, tv_id}
    s.setdefault("bad", {})      # youtube key -> {reason, checked}
    s.setdefault("failed", {})   # folder -> {count, last} for transient-failure backoff
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


# --------------------------------------------------------------------------- sources

def arr_items() -> list[dict]:
    items: list[dict] = []
    if RADARR_URL and RADARR_API_KEY:
        movies = http_json(f"{RADARR_URL}/api/v3/movie", {"X-Api-Key": RADARR_API_KEY}, 120)
        for m in movies:
            if not m.get("path") or not m.get("hasFile"):
                continue
            items.append({
                "kind": "movie",
                "title": f"{m.get('title')} ({m.get('year')})",
                "folder": map_path(m["path"]),
                "tmdb_id": m.get("tmdbId") or 0,
                "arr_trailer": m.get("youTubeTrailerId") or "",
            })
        log.info("Radarr: %d movies with files", len(items))
    if SONARR_URL and SONARR_API_KEY:
        series = http_json(f"{SONARR_URL}/api/v3/series", {"X-Api-Key": SONARR_API_KEY}, 120)
        n = 0
        for s in series:
            if not s.get("path") or not (s.get("statistics") or {}).get("episodeFileCount"):
                continue
            items.append({
                "kind": "tv",
                "title": f"{s.get('title')} ({s.get('year')})",
                "folder": map_path(s["path"]),
                "tmdb_id": s.get("tmdbId") or 0,
                "tvdb_id": s.get("tvdbId") or 0,
                "arr_trailer": "",
            })
            n += 1
        log.info("Sonarr: %d series with files", n)
    return items


# --------------------------------------------------------------------------- tmdb

def tmdb(path: str, **params) -> dict:
    headers = {"Accept": "application/json"}
    if TMDB_API_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
    else:
        params["api_key"] = TMDB_API_KEY
    try:
        return http_json(f"{TMDB_BASE}{path}?{urllib.parse.urlencode(params)}", headers)
    finally:
        if TMDB_DELAY:
            time.sleep(TMDB_DELAY)


def tmdb_id_for(item: dict, state: dict) -> int:
    if item["tmdb_id"] or item["kind"] != "tv" or not item.get("tvdb_id"):
        return item["tmdb_id"]
    key = f"tvdb/{item['tvdb_id']}"
    if key not in state["tmdb"]:
        res = tmdb(f"/find/{item['tvdb_id']}", external_source="tvdb_id").get("tv_results") or []
        state["tmdb"][key] = {"tv_id": res[0]["id"] if res else 0}
    return state["tmdb"][key]["tv_id"]


def is_bad(yt_key: str, state: dict) -> bool:
    """Rejected by the link rules within BAD_VIDEO_DAYS, or voted bad by the user (permanent)."""
    bad = state["bad"].get(yt_key) or {}
    return bool(bad.get("user")) or recent(bad.get("checked"), BAD_VIDEO_DAYS)


def trailer_candidates(item: dict, state: dict) -> list[dict]:
    """Ranked {key, lang, name} trailers in the item's original language, minus videos
    already rejected; Radarr's own trailer as the only fallback when
    ORIGINAL_LANGUAGE_ONLY is off and none is tagged."""
    tmdb_id = tmdb_id_for(item, state)
    if not tmdb_id:
        return []
    base = f"/{item['kind']}/{tmdb_id}"
    meta = state["tmdb"].setdefault(base.lstrip("/"), {})
    if "original_language" not in meta:
        meta["original_language"] = tmdb(base).get("original_language") or ""
    lang = meta["original_language"]
    videos = tmdb(f"{base}/videos", language=lang, include_video_language=lang).get("results", [])
    candidates = [v for v in videos
                  if v.get("site") == "YouTube" and v.get("key")
                  and v.get("type") in VIDEO_TYPES and v.get("iso_639_1") == lang]
    if candidates:
        candidates.sort(key=lambda v: (v["key"] != item["arr_trailer"], not v.get("official"),
                                       VIDEO_TYPES.index(v["type"]), -(v.get("size") or 0),
                                       v.get("published_at") or ""))
        return [{"key": v["key"], "lang": lang, "name": v.get("name") or v["type"]}
                for v in candidates if not is_bad(v["key"], state)]
    if item["arr_trailer"] and not ORIGINAL_LANGUAGE_ONLY and not is_bad(item["arr_trailer"], state):
        return [{"key": item["arr_trailer"], "lang": "unverified", "name": "Trailer"}]
    return []


def trailer_filename(title: str, name: str, yt_key: str) -> str:
    """`<Title (Year)> - <TMDB name> [<id>].mp4`, the id in brackets since YouTube ids
    themselves contain - and _. Capped in bytes so the `.partial` staging name still fits."""
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", f"{title} - {name}")
    base = re.sub(r"\s+", " ", base).strip(" .")
    base = base.encode()[:200].decode(errors="ignore").rstrip(" .")
    return f"{base} [{yt_key}].mp4"


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
    ver = subprocess.run([str(YTDLP), "--version"], capture_output=True, text=True, errors="replace")
    log.info("yt-dlp %s", ver.stdout.strip() or "?")


def download(key: str, tmp: Path) -> tuple[str, Path | str]:
    """("ok", path) or (status, reason) with status unusable | ratelimited | error.
    The match filter applies ThemerrDB's link rules on the same request as the download;
    "playable from here" is covered by yt-dlp's own geo/private/removed errors."""
    # lang first: YouTube's auto-dubbed audio tracks rank below the original track
    # no --quiet: it would also hide the "does not pass filter" line
    cmd = [str(YTDLP), "--no-playlist", "--no-progress", "--no-warnings",
           "--match-filters", f"availability=?public & age_limit<?18 & "
                              f"duration>=?{MIN_DURATION} & duration<=?{MAX_DURATION}",
           "-S", f"lang,res:{MAX_HEIGHT},vcodec:h264,ext:mp4:m4a",
           "--merge-output-format", "mp4", "-o", f"{tmp}/src.%(ext)s",
           f"https://www.youtube.com/watch?v={key}"]
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


def probe(path: Path) -> tuple[int, int, float] | None:
    """(width, height, duration) of the first video stream, or None."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-of", "json",
                        "-show_entries", "stream=width,height:format=duration", str(path)],
                       capture_output=True, text=True, errors="replace")
    try:
        j = json.loads(r.stdout)
        return int(j["streams"][0]["width"]), int(j["streams"][0]["height"]), float(j["format"]["duration"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def window_box(path: Path, start: float, iw: int, ih: int, hwaccel: bool = True) -> tuple[int, int, int, int] | None:
    """(x1, y1, x2, y2) of the picture area across a 2s window. A row/column counts as
    picture only when more than CROP_OVERLAY_PCT of its pixels are brighter than
    CROP_LIMIT - unlike cropdetect's row *average*, a caption or channel logo inside a bar
    can't pass that. Taken as the max over the window's frames, so dark frames can't shrink it."""
    base = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if hwaccel:
        base += ["-hwaccel", "cuda"]
    cmd = base + ["-ss", str(max(start, 0)), "-i", str(path), "-t", "2", "-an",
                  "-vf", "fps=2,format=gray", "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, capture_output=True)
    size = iw * ih
    if r.returncode != 0 or not r.stdout or len(r.stdout) % size:
        return window_box(path, start, iw, ih, hwaccel=False) if hwaccel else None
    q = 1 - CROP_OVERLAY_PCT / 100

    def level(samples: bytes) -> int:
        s = sorted(samples)
        return s[min(int(len(s) * q), len(s) - 1)]

    rows, cols = [0] * ih, [0] * iw
    for off in range(0, len(r.stdout), size):
        frame = r.stdout[off:off + size]
        for y in range(ih):
            rows[y] = max(rows[y], level(frame[y * iw:(y + 1) * iw:8]))
        for x in range(iw):
            cols[x] = max(cols[x], level(frame[x::iw * 8]))
    ys = [y for y, v in enumerate(rows) if v > CROP_LIMIT]
    xs = [x for x, v in enumerate(cols) if v > CROP_LIMIT]
    return (xs[0], ys[0], xs[-1] + 1, ys[-1] + 1) if xs and ys else None


def detect_crop(path: Path) -> str | None:
    """ffmpeg crop= argument (w:h:x:y) for the picture area most sampled windows agree on
    (within 8px), or None without a majority or when the bars are negligible. Majority, not
    union: one full-frame rating card or studio logo would otherwise cancel the crop outright."""
    if not (p := probe(path)):
        return None
    iw, ih, duration = p
    lo, hi = duration * 0.1, duration * 0.9
    starts = [lo + (hi - lo) * (i + 0.5) / CROP_SAMPLES for i in range(CROP_SAMPLES)]
    boxes = [b for s in starts if (b := window_box(path, s, iw, ih))]
    if not boxes:
        return None

    def close(a: tuple, b: tuple) -> bool:
        return all(abs(a[i] - b[i]) <= 8 for i in range(4))

    box = max(boxes, key=lambda b: sum(close(b, o) for o in boxes))
    near = [b for b in boxes if close(box, b)]
    if len(near) < (CROP_SAMPLES + 1) // 2:
        return None
    x1, y1 = min(b[0] for b in near), min(b[1] for b in near)
    x2, y2 = max(b[2] for b in near), max(b[3] for b in near)
    x1, y1 = x1 + x1 % 2, y1 + y1 % 2
    w, h = (x2 - x1) // 2 * 2, (y2 - y1) // 2 * 2
    if (iw - w) * 100 < iw * CROP_MIN_PCT and (ih - h) * 100 < ih * CROP_MIN_PCT:
        return None
    return f"{w}:{h}:{x1}:{y1}"


def finalize(src: Path, out: Path, crop: str | None) -> bool:
    """Crop + re-encode when there are bars, otherwise a straight remux."""
    maps = ["-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-sn"]
    tail = ["-c:a", "copy", "-movflags", "+faststart", str(out)]
    base = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if not crop:
        cmd = base + ["-i", str(src)] + maps + ["-c:v", "copy"] + tail
    else:
        cmd = base + ["-hwaccel", "cuda", "-i", str(src)] + maps + [
            "-vf", f"crop={crop}", "-c:v", "h264_nvenc", "-preset", NVENC_PRESET,
            "-rc", "vbr", "-cq", NVENC_CQ, "-b:v", "0"] + tail
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode == 0 and out.exists():
        return True
    if not crop:
        log.error("  ffmpeg remux failed: %s", r.stderr.strip()[-400:])
        return False
    log.warning("  NVENC encode failed, falling back to CPU: %s", r.stderr.strip()[-300:])
    cmd = base + ["-i", str(src)] + maps + [
        "-vf", f"crop={crop}", "-c:v", "libx264", "-crf", "21", "-preset", "medium"] + tail
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0 or not out.exists():
        log.error("  ffmpeg encode failed: %s", r.stderr.strip()[-400:])
        return False
    return True


def fetch_trailer(key: str, dest: Path) -> tuple[str, str | None]:
    """Download, crop and atomically write `dest`. Returns ("ok", crop or None) or
    one of download()'s failure statuses with its reason."""
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp_s:
        tmp = Path(tmp_s)
        status, src = download(key, tmp)
        if status != "ok":
            return status, src
        crop = detect_crop(src) if CROP_BARS else None
        if crop:
            log.info("  cropping black bars: crop=%s", crop)
        out = tmp / "out.mp4"
        if not finalize(src, out, crop):
            return "error", "ffmpeg failed"
        staging = dest.with_name(f".{dest.name}.partial")
        shutil.copyfile(out, staging)
        staging.replace(dest)
        return "ok", crop


# --------------------------------------------------------------------------- media server

def notify_media_server(folders: list[Path]) -> None:
    """Targeted per-folder update rather than the sibling jobs' full library refresh."""
    if not (folders and MEDIASERVER_TYPE and MEDIASERVER_URL and MEDIASERVER_API_KEY):
        return
    if MEDIASERVER_TYPE == "emby":
        url, auth = f"{MEDIASERVER_URL}/emby/Library/Media/Updated", {"X-Emby-Token": MEDIASERVER_API_KEY}
    elif MEDIASERVER_TYPE == "jellyfin":
        url = f"{MEDIASERVER_URL}/Library/Media/Updated"
        auth = {"Authorization": f'MediaBrowser Token="{MEDIASERVER_API_KEY}"'}
    else:
        log.warning("Unknown MEDIASERVER_TYPE %r", MEDIASERVER_TYPE)
        return
    body = json.dumps({"Updates": [{"Path": str(f), "UpdateType": "Modified"} for f in folders]}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/json", **auth})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
        log.info("Notified %s of %d updated folders", MEDIASERVER_TYPE, len(folders))
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("%s notify failed: %s", MEDIASERVER_TYPE, e)


# --------------------------------------------------------------------------- main loop

def scan_trailers(tdir: Path, owned: dict) -> tuple[dict, list[Path], list[str]]:
    """(ours, foreign, deleted). Ours is recognised by the state entry's file name or,
    failing that, by the `[<id>].mp4` tag - so a rename that keeps the tag, or a lost
    state.json, doesn't turn our trailers foreign. Deleted: ids in state with no file left
    under either rule (nothing in this job or library-cleanup deletes our files, so that
    was the user)."""
    files = [f for f in tdir.iterdir()
             if not f.name.startswith(".") and f.suffix.lower() in VIDEO_EXTS] if tdir.is_dir() else []
    tagged: dict[str, Path] = {}
    for f in files:
        if m := TAG_RE.search(f.name):
            tagged.setdefault(m.group(1), f)
    ours: dict[str, dict] = {}
    for k, v in owned.items():
        if (tdir / v["file"]).is_file():
            ours[k] = v
        elif k in tagged:
            ours[k] = {**v, "file": tagged[k].name}
    deleted = [k for k in owned if k not in ours]
    for k, f in tagged.items():
        if k not in ours:
            ours[k] = {"file": f.name, "lang": "unknown", "crop": None,
                       "written": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()}
    names = {v["file"] for v in ours.values()}
    return ours, [f for f in files if f.name not in names], deleted


class Run:
    def __init__(self, state: dict):
        self.state = state
        self.stats = dict(added=0, cropped=0, removed=0, voted=0, full=0, skipped=0, missing=0,
                          rejected=0, failed=0, backoff=0, nofolder=0)
        self.downloads = 0
        self.rate_limited = False
        self.budget_warned = False
        self.notify: list[Path] = []

    def budget_spent(self) -> bool:
        return bool(MAX_DOWNLOADS) and self.downloads >= MAX_DOWNLOADS

    def remove_foreign(self, item: dict, foreign: list[Path]) -> None:
        for f in foreign:
            log.info("%s pre-existing trailer: %s -> %s", "Would remove" if DRY_RUN else "Removing",
                     item["title"], f.name)
            self.stats["removed"] += 1
            if not DRY_RUN:
                f.unlink(missing_ok=True)
        if not DRY_RUN:
            self.notify.append(item["folder"])

    def candidates(self, item: dict) -> list[dict] | None:
        """trailer_candidates(), or None when TMDB couldn't be asked (retried next run)."""
        try:
            return trailer_candidates(item, self.state)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            log.warning("TMDB lookup failed for %s: HTTP %s", item["title"], e.code)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            # str(e) never includes the request URL, so the TMDB api_key can't leak here
            log.warning("TMDB lookup failed for %s: %s", item["title"], e)
        return None

    def process(self, item: dict) -> None:
        state, stats = self.state, self.stats
        folder: Path = item["folder"]
        key, tdir = str(folder), folder / TRAILER_DIR
        if not folder.is_dir():
            stats["nofolder"] += 1
            return
        if (folder / ".notrailer").exists():
            stats["skipped"] += 1
            return

        ours, foreign, deleted = scan_trailers(tdir, state["owned"].get(key, {}))
        state["owned"][key] = ours
        for k in deleted:
            log.info("Trailer deleted by user, voting it bad: %s -> %s", item["title"], k)
            state["bad"][k] = {"reason": "deleted by user", "checked": now().isoformat(), "user": True}
            stats["voted"] += 1
        if deleted:
            state["checked"].pop(key, None)  # fetch a replacement next run, not after RECHECK_DAYS
        if ours and foreign:
            self.remove_foreign(item, foreign)
            foreign = []
        need = MAX_TRAILERS - len(ours) if MAX_TRAILERS else sys.maxsize
        if need <= 0:
            stats["full"] += 1
            return
        if recent(state["checked"].get(key), RECHECK_DAYS):
            return
        failed = state["failed"].get(key)
        if failed and recent(failed["last"], min(2 ** (failed["count"] - 1), MAX_BACKOFF_DAYS)):
            stats["backoff"] += 1
            return
        # checked before the TMDB lookup, so a capped run doesn't query TMDB for items it won't download
        if self.budget_spent():
            if not self.budget_warned:
                log.info("MAX_DOWNLOADS reached, skipping further trailer downloads this run")
                self.budget_warned = True
            return
        if (candidates := self.candidates(item)) is None:
            return

        added, stop, detail = 0, None, None
        for c in (c for c in candidates if c["key"] not in ours):
            if added >= need:
                break
            if self.budget_spent():
                stop = "budget"  # untried candidates left - resume next run
                break
            log.info("Adding trailer: %s -> %s [%s] %s", item["title"], c["key"], c["lang"], c["name"])
            dest = tdir / trailer_filename(item["title"], c["name"], c["key"])
            if DRY_RUN:
                status, detail = "ok", None
            else:
                if self.downloads:
                    time.sleep(DOWNLOAD_DELAY)
                tdir.mkdir(exist_ok=True)
                os.chown(tdir, MEDIA_UID, MEDIA_GID)
                status, detail = fetch_trailer(c["key"], dest)
            self.downloads += 1
            if status == "ok":
                if not DRY_RUN:
                    os.chown(dest, MEDIA_UID, MEDIA_GID)
                ours[c["key"]] = {"file": dest.name, "lang": c["lang"], "crop": detail,
                                  "written": now().isoformat()}
                added += 1
                stats["added"] += 1
                stats["cropped"] += bool(detail)
            elif status == "unusable":
                log.info("  rejected %s: %s", c["key"], detail)
                state["bad"][c["key"]] = {"reason": detail, "checked": now().isoformat()}
                stats["rejected"] += 1
            else:
                stop = status
                break

        if added:
            if not DRY_RUN:
                self.notify.append(folder)
            names = {v["file"] for v in ours.values()}
            if foreign := [f for f in foreign if f.name not in names]:
                self.remove_foreign(item, foreign)
        if stop == "ratelimited":
            log.error("YouTube rate limit / bot check, stopping downloads for this run: %s", detail)
            self.rate_limited = True
            stats["failed"] += 1
        elif stop == "error":
            log.error("  download failed: %s", detail)
            state["failed"][key] = {"count": (failed or {}).get("count", 0) + 1, "last": now().isoformat()}
            stats["failed"] += 1
        elif stop is None:
            # every candidate tried (or the cap reached) - ask TMDB again after RECHECK_DAYS
            state["failed"].pop(key, None)
            state["checked"][key] = now().isoformat()
            stats["missing"] += not ours
        if not ours and not DRY_RUN:
            try:
                tdir.rmdir()  # only succeeds if it stayed empty
            except OSError:
                pass
        save_state(state)  # save as we go, so an interrupted run loses nothing


def run_once() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    ensure_ytdlp()
    items = arr_items()
    run = Run(state)
    for item in items:
        run.process(item)
        if run.rate_limited:
            break
    # forget emptied entries and folders gone from disk (renamed, case-merged, removed)
    for section in ("owned", "checked", "failed"):
        state[section] = {k: v for k, v in state[section].items() if v and Path(k).is_dir()}
    save_state(state)
    chown_data_dir()
    log.info("Done: %s", ", ".join(f"{k}={v}" for k, v in run.stats.items()))
    if not DRY_RUN:
        notify_media_server(list(dict.fromkeys(run.notify)))
    return run.stats


def main() -> int:
    logging.basicConfig(level=env("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    if not (RADARR_URL or SONARR_URL):
        log.error("Set RADARR_URL/RADARR_API_KEY and/or SONARR_URL/SONARR_API_KEY")
        return 2
    if not TMDB_API_KEY:
        log.error("Set TMDB_API_KEY (v3 API key or v4 read access token)")
        return 2
    # deno (yt-dlp's YouTube JS runtime) is vendored into DATA_DIR by the DAG's vendor step
    os.environ["PATH"] = f"{DATA_DIR}{os.pathsep}{os.environ.get('PATH', '')}"
    for tool in ("ffmpeg", "ffprobe", "deno"):
        if shutil.which(tool) is None:
            log.error("%s not found in PATH", tool)
            return 2
    os.umask(int(env("UMASK", "002"), 8))
    try:
        stats = run_once()
    except Exception:
        log.exception("Run failed")
        return 1
    # logs go to stderr; stdout carries one JSON summary line for schedulers
    print(json.dumps(stats), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
