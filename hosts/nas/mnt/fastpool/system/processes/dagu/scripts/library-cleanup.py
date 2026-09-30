#!/usr/bin/env python3
"""
library-cleanup - find (and optionally remove) cruft across the movies/series/music
libraries. See docs/multimedia-library-layout.md for the canonical layout and the
category table this script implements.

Rules:
  * Only the well-defined categories below are ever deleted/moved, and only when
    APPLY=true. Everything else is report-only, forever - missing/multiple videos,
    oversized backdrops, and missing nfos all need a human judgment call this script
    can't safely make.
  * OS artifacts, re-scrape backups, partial downloads, and one specific broken-download
    artifact predating the backdrop-generate DAG are cheap to regenerate or were never
    part of the media itself, so they're deleted outright.
  * Numbered fanartN.jpg/png files loose at a movie/series root are moved into
    extrafanart/, and exact-duplicate images (same content hash) are removed - whether
    duplicated within extrafanart/, between a loose file and extrafanart/, or between
    music's legacy extrathumbs/ and extrafanart/. A handful of Emby-written nfo files
    (local-trailer extras, music artist.nfo) store a literal path to a specific
    extrafanart/extrathumbs image; every rename/removal here is reflected back into any
    such nfo in the same folder so those references never go stale.
  * A `*-trailer.*` video next to a movie's/series' main files is moved into trailers/
    together with its same-named nfo (an exact duplicate of one already there is
    dropped). trailer-fetch only ever looks inside trailers/, and replaces what it finds
    there with its own downloads.
  * A <name>.nfo with no <name>.<media> beside it (an upgraded/renamed video's old nfo,
    a replaced trailer's Emby nfo) is deleted. Folder-level movie/tvshow/season.nfo are
    kept, as is every nfo in a folder with no media at all (report-only "no video" case),
    except trailers/.
  * Folders whose name differs only by case (e.g. "Mika" vs "MIKA", seen at both artist
    and album level in this library - always one fully-populated folder plus an empty
    leftover stub) are merged into whichever one holds more content. Exact-duplicate
    files are dropped; a same-name file that actually differs is kept (never silently
    lost) for audio/video, suffixed to avoid overwriting, and dropped only for
    metadata/image files, which are cheap to re-derive.
  * A backdrops/theme.mp4|mkv that turns out to be a frozen/static image throughout
    (sampled frame-to-frame pixel diff near zero) is report-only, like oversized
    backdrops - could be a legacy pre-automation file, a source video too dark/still
    for backdrop-generate's own motion check to have rejected it, or a corrupt encode;
    all three need a human look, not an automatic delete.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("library-cleanup")

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts"}
TRAILER_EXTS = VIDEO_EXTS | {".mov", ".webm"}  # keep in sync with trailer-fetch's VIDEO_EXTS
# deliberately wide: a missing ext here gets that video's nfo deleted as orphaned
MEDIA_EXTS = TRAILER_EXTS | {".wmv", ".mpg", ".mpeg", ".divx", ".flv", ".m2ts", ".vob", ".ifo", ".iso", ".ogv", ".3gp"}
FOLDER_NFOS = {"movie.nfo", "tvshow.nfo", "season.nfo"}
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".wma", ".aac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
CRUFT_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "_imgdb.nfo"}
CRUFT_SUFFIXES = (".nfo-orig", ".bak", ".orig", ".part", ".crdownload", ".!qb")
OVERSIZED_BACKDROP_BYTES = 150 * 1024 * 1024
STATIC_BACKDROP_SAMPLES = 4
STATIC_BACKDROP_THRESHOLD = 2.0  # mean pixel-diff (0-255); backdrop-generate's own
                                  # MIN_MOTION=4.0 gates *source window selection*, this
                                  # checks an already-written clip for being frozen
                                  # throughout, so it can (and should) run stricter
LOOSE_FANART_RE = re.compile(r"(?i)^fanart\d+\.(jpg|jpeg|png)$")

# Every <fanart> reference seen in this library's nfos already points one level down
# into extrafanart/ or extrathumbs/ - never at a loose root file - so the rewrite only
# needs to retarget that one path component plus the filename.
FANART_REF_RE = re.compile(
    r"(<fanart>[^<]*?)(extrafanart|extrathumbs)([/\\])([^/\\<]+?)(</fanart>)",
    re.IGNORECASE,
)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    v = env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


MOVIES_DIR = Path(env("MOVIES_DIR", "/media/movies"))
SERIES_DIR = Path(env("SERIES_DIR", "/media/tv"))  # /media/tv to match themerr-fetch/backdrop-generate's mount name
MUSIC_DIR = Path(env("MUSIC_DIR", "/media/music"))
DATA_DIR = Path(env("DATA_DIR", "/data"))
APPLY = env_bool("APPLY")
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_keys(files: list[Path]) -> dict[Path, str]:
    """Exact-duplicate key per file. Only files sharing a byte size with another
    candidate are hashed - a unique size can't have an exact duplicate, so it's keyed by
    size alone and never read. Keeps the weekly run from re-reading every extrafanart
    image in the library just to find the handful that actually collide."""
    sizes = {f: f.stat().st_size for f in files}
    counts = Counter(sizes.values())
    return {f: sha256_file(f) if counts[s] > 1 else f"size:{s}" for f, s in sizes.items()}


# --------------------------------------------------------------------------- cruft scan

def is_cruft_file(f: Path) -> bool:
    if f.name.lower() in CRUFT_NAMES:
        return True
    return f.name.lower().endswith(CRUFT_SUFFIXES)


def scan_cruft(root: Path, findings: dict, deleted: list[str]) -> None:
    """OS artifacts, re-scrape backups, and partial downloads - shared across all
    three libraries. Broken backdrops/theme.html stubs are handled separately since
    they're specific to movies/series. Runs before fanart consolidation so a stray
    _imgdb.nfo doesn't stop an emptied extrathumbs/ folder from being removed."""
    if not root.is_dir():
        return
    # os.walk rather than rglob+is_file: scandir's d_type already says file vs dir, so
    # this skips a stat() per entry across the whole library
    t0 = time.monotonic()
    scanned = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        scanned += len(filenames)
        for name in filenames:
            f = Path(dirpath, name)
            if not is_cruft_file(f):
                continue
            findings["cruft_files"].append(str(f))
            if APPLY:
                try:
                    f.unlink()
                    deleted.append(str(f))
                except OSError as e:
                    log.warning("delete %s: %s", f, e)
    log.info("cruft scan: %d files in %.0fs (%s)", scanned, time.monotonic() - t0, root)


FRAME_SIZE = 32 * 32 * 3  # rgb24 @ 32x32
FFMPEG_TIMEOUT_SEC = 30
STATIC_CHECK_WORKERS = min(8, os.cpu_count() or 4)


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                       timeout=FFMPEG_TIMEOUT_SEC)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def frame_pixels(path: Path, t: float) -> bytes | None:
    """`-ss` *before* `-i` seeks to the keyframe before t, then decodes forward to t -
    the original, numerically-validated-correct method (confirmed live against a known
    static backdrop's actual mean pixel-diff). Cost scales with GOP length, not clip
    length: on a long-GOP clip every sample decodes up to a whole GOP at native
    resolution, which is why this is now only the fallback/confirmation path behind
    keyframe_pixels rather than the per-file default."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-ss", str(max(t, 0)), "-i", str(path), "-frames:v", "1",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "32x32", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout if r.returncode == 0 and r.stdout else None


def keyframe_pixels(path: Path) -> list[bytes] | None:
    """Every keyframe in one ffmpeg pass, decoding nothing else - ~0.1s/file vs. ~1-3s
    for a probe + 4 seeks. An earlier `-skip_frame nokey` attempt that went through an
    `fps` filter flagged 185/187 test clips static; never root-caused, but an `fps`
    filter fills its fixed-rate slots by repeating the last (sparse) keyframe, which
    alone would do that. `-fps_mode passthrough` emits exactly one frame per decoded
    keyframe, and is_static_video re-checks any static verdict with the seek method
    regardless."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-skip_frame", "nokey", "-i", str(path), "-map", "0:v:0",
           "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "32x32", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or not r.stdout or len(r.stdout) % FRAME_SIZE:
        return None
    return [r.stdout[i:i + FRAME_SIZE] for i in range(0, len(r.stdout), FRAME_SIZE)]


def pixel_diff(a: bytes, b: bytes) -> float:
    if not a or len(a) != len(b):
        return 0.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def all_near_identical(frames: list[bytes]) -> bool:
    return all(pixel_diff(frames[i], frames[i + 1]) < STATIC_BACKDROP_THRESHOLD
               for i in range(len(frames) - 1))


def is_static_by_seek(path: Path) -> bool:
    duration = probe_duration(path)
    if duration <= 0:
        return False
    times = [duration * (i + 1) / (STATIC_BACKDROP_SAMPLES + 1) for i in range(STATIC_BACKDROP_SAMPLES)]
    frames = [f for f in (frame_pixels(path, t) for t in times) if f is not None]
    return len(frames) >= 2 and all_near_identical(frames)


def is_static_video(path: Path) -> bool:
    """Sample a few evenly-spaced frames and check consecutive pixel-diff - True only
    if every sample pair is near-identical, i.e. the clip never actually moves. Returns
    False (not flagged) if the file can't be probed/decoded at all, since that's a
    different, separately-obvious problem (corrupt file) rather than a static one.

    The keyframe pass settles the common case (the clip moves) on its own. It never
    gets the last word on "static": a clip with fewer than 2 keyframes can't be judged
    from keyframes at all, and a static verdict is re-checked by the seek method, so
    the fast path can only ever clear a file, never flag one the validated method
    wouldn't."""
    keyframes = keyframe_pixels(path)
    if keyframes and len(keyframes) >= 2:
        n = len(keyframes)
        # same evenly-spaced, endpoints-excluded spread as the seek method's timestamps
        idx = sorted({min(n - 1, round(n * (i + 1) / (STATIC_BACKDROP_SAMPLES + 1)))
                      for i in range(STATIC_BACKDROP_SAMPLES)})
        if len(idx) < 2:
            idx = [0, n - 1]
        if not all_near_identical([keyframes[i] for i in idx]):
            return False
    return is_static_by_seek(path)


def scan_backdrops(root: Path, findings: dict, deleted: list[str]) -> None:
    if not root.is_dir():
        return
    for f in root.glob("*/backdrops/theme.html"):
        findings["broken_backdrops"].append(str(f))
        if APPLY:
            try:
                f.unlink()
                deleted.append(str(f))
            except OSError as e:
                log.warning("delete %s: %s", f, e)

    candidates: list[Path] = []
    for f in root.glob("*/backdrops/theme.*"):
        if f.suffix.lower() == ".html":
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        oversized = size > OVERSIZED_BACKDROP_BYTES
        if oversized:
            findings["oversized_backdrops"].append(f"{f} ({size / 1024 / 1024:.0f}M)")
        # skip the static-image decode on an already-flagged oversized file - it's
        # already going to a human for review either way, no need to pay for a much
        # bigger-than-normal decode just to add a second label
        if FFMPEG_AVAILABLE and not oversized and f.suffix.lower() in VIDEO_EXTS:
            candidates.append(f)

    if not candidates:
        return
    t0 = time.monotonic()
    checked = 0
    with ThreadPoolExecutor(max_workers=STATIC_CHECK_WORKERS) as pool:
        for f, static in zip(candidates, pool.map(is_static_video, candidates)):
            checked += 1
            if static:
                findings["static_backdrops"].append(str(f))
            if checked % 100 == 0:
                log.info("static-backdrop check: %d/%d files in %.0fs (%s)",
                         checked, len(candidates), time.monotonic() - t0, root)
    log.info("static-backdrop check done: %d files in %.0fs (%s)",
             len(candidates), time.monotonic() - t0, root)


# --------------------------------------------------------------------------- fanart

def next_fanart_name(existing: set[str], ext: str) -> str:
    n = 1
    while f"fanart{n}{ext}" in existing:
        n += 1
    return f"fanart{n}{ext}"


def fix_nfo_fanart_refs(item_dir: Path, rename_map: dict[str, str]) -> int:
    """Rewrite <fanart>...extrafanart|extrathumbs/<name></fanart> references in any
    nfo directly under item_dir or its trailers/ (Emby's local-trailer/artist nfos - see
    module docstring) so a renamed or merged-away file's old name doesn't go stale.
    Collapses a line to nothing if the rewrite would leave two <fanart> entries pointing
    at the same file."""
    fixed = 0
    for nfo in sorted([*item_dir.glob("*.nfo"), *item_dir.glob("trailers/*.nfo")]):
        try:
            text = nfo.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "<fanart>" not in text:
            continue

        def repl(m: re.Match) -> str:
            prefix, _marker, sep, name, suffix = m.groups()
            new_name = rename_map.get(name)
            if new_name is None:
                return m.group(0)
            return f"{prefix}extrafanart{sep}{new_name}{suffix}"

        new_text = FANART_REF_RE.sub(repl, text)
        if new_text == text:
            continue

        seen: set[str] = set()
        out_lines = []
        for line in new_text.splitlines(keepends=True):
            m = re.search(r"<fanart>(.*?)</fanart>", line)
            if m:
                if m.group(1) in seen:
                    continue
                seen.add(m.group(1))
            out_lines.append(line)
        nfo.write_text("".join(out_lines), encoding="utf-8")
        fixed += 1
    return fixed


def consolidate_fanart(item_dir: Path, findings: dict, moved: list[str], deleted: list[str]) -> None:
    """Move loose numbered fanartN.* files at item_dir's root into extrafanart/, and
    remove any exact-duplicate image (by content hash) found along the way - within
    extrafanart/ itself, or between a loose file and one already there."""
    extrafanart_dir = item_dir / "extrafanart"
    hash_to_name: dict[str, str] = {}
    names: set[str] = set()
    rename_map: dict[str, str] = {}

    extrafanart_files = sorted(
        f for f in extrafanart_dir.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS
    ) if extrafanart_dir.is_dir() else []
    loose_files = sorted(p for p in item_dir.iterdir() if p.is_file() and LOOSE_FANART_RE.match(p.name))
    keys = content_keys(extrafanart_files + loose_files)

    for f in extrafanart_files:
        names.add(f.name)
        h = keys[f]
        if h in hash_to_name:
            findings["duplicate_fanart"].append(str(f))
            rename_map[f.name] = hash_to_name[h]
            if APPLY:
                f.unlink()
                deleted.append(str(f))
        else:
            hash_to_name[h] = f.name

    for f in loose_files:
        findings["loose_fanart"].append(str(f))
        h = keys[f]
        if h in hash_to_name:
            findings["duplicate_fanart"].append(str(f))
            rename_map[f.name] = hash_to_name[h]
            if APPLY:
                f.unlink()
                deleted.append(str(f))
        else:
            new_name = next_fanart_name(names, f.suffix.lower())
            names.add(new_name)
            hash_to_name[h] = new_name
            rename_map[f.name] = new_name
            if APPLY:
                extrafanart_dir.mkdir(exist_ok=True)
                dest = extrafanart_dir / new_name
                f.rename(dest)
                moved.append(f"{f} -> {dest}")

    if APPLY and rename_map and fix_nfo_fanart_refs(item_dir, rename_map):
        findings["nfo_fanart_fixed"].append(str(item_dir))


def consolidate_music_extrathumbs(artist_dir: Path, findings: dict, moved: list[str], deleted: list[str]) -> None:
    """extrathumbs/ is a leftover from the Kodi-era scraper that also wrote
    extrafanart/ - fold any image unique to it into extrafanart/, drop exact
    duplicates, and remove the folder once it's empty."""
    extrathumbs_dir = artist_dir / "extrathumbs"
    if not extrathumbs_dir.is_dir():
        return
    extrafanart_dir = artist_dir / "extrafanart"
    hash_to_name: dict[str, str] = {}
    names: set[str] = set()
    extrafanart_files = [
        f for f in extrafanart_dir.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS
    ] if extrafanart_dir.is_dir() else []
    extrathumbs_files = sorted(
        f for f in extrathumbs_dir.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS
    )
    keys = content_keys(extrafanart_files + extrathumbs_files)
    for f in extrafanart_files:
        names.add(f.name)
        hash_to_name.setdefault(keys[f], f.name)

    findings["extrathumbs_overlap"].append(str(artist_dir))
    rename_map: dict[str, str] = {}
    for f in extrathumbs_files:
        h = keys[f]
        if h in hash_to_name:
            findings["duplicate_fanart"].append(str(f))
            rename_map[f.name] = hash_to_name[h]
            if APPLY:
                f.unlink()
                deleted.append(str(f))
        else:
            new_name = next_fanart_name(names, f.suffix.lower())
            names.add(new_name)
            hash_to_name[h] = new_name
            rename_map[f.name] = new_name
            if APPLY:
                extrafanart_dir.mkdir(exist_ok=True)
                dest = extrafanart_dir / new_name
                f.rename(dest)
                moved.append(f"{f} -> {dest}")

    if not APPLY:
        return
    if rename_map:
        fix_nfo_fanart_refs(artist_dir, rename_map)
    try:
        if not any(extrathumbs_dir.iterdir()):
            extrathumbs_dir.rmdir()
    except OSError as e:
        log.warning("rmdir %s: %s", extrathumbs_dir, e)


# --------------------------------------------------------------------------- trailers

def is_loose_trailer(f: Path) -> bool:
    return f.stem.lower().endswith("-trailer") and f.suffix.lower() in TRAILER_EXTS


def consolidate_trailers(item_dir: Path, findings: dict, moved: list[str], deleted: list[str]) -> None:
    """Move loose trailers into trailers/, taking a same-named .nfo along. When the video
    is an exact duplicate of one already there, its nfo is moved only if that one has none
    (nfos are cheap to re-derive, so an existing one wins)."""
    trailers_dir = item_dir / "trailers"
    for f in sorted(p for p in item_dir.iterdir() if p.is_file() and is_loose_trailer(p)):
        findings["loose_trailers"].append(str(f))
        if not APPLY:
            continue
        nfo = f.with_suffix(".nfo")
        dest = trailers_dir / f.name
        if dest.exists() and dest.stat().st_size == f.stat().st_size and sha256_file(dest) == sha256_file(f):
            f.unlink()
            deleted.append(str(f))
        else:
            n = 1
            while dest.exists():
                dest = trailers_dir / f"{f.stem} ({n}){f.suffix}"
                n += 1
            trailers_dir.mkdir(exist_ok=True)
            f.rename(dest)
            moved.append(f"{f} -> {dest}")
        if not nfo.is_file():
            continue
        dest_nfo = dest.with_suffix(".nfo")
        if dest_nfo.exists():
            nfo.unlink()
            deleted.append(str(nfo))
        else:
            nfo.rename(dest_nfo)
            moved.append(f"{nfo} -> {dest_nfo}")


# --------------------------------------------------------------------------- orphaned nfos

def clean_orphan_nfos(item_dir: Path, findings: dict, deleted: list[str]) -> None:
    """Delete <name>.nfo files with no <name>.<media> next to them (an upgraded/renamed
    video's old nfo, a replaced trailer's Emby nfo), anywhere under item_dir. Folder-level
    nfos are kept, and so is every nfo in a folder holding no media at all - that's the
    report-only "no video" case - except trailers/, which trailer-fetch may empty."""
    for dirpath, _dirnames, filenames in os.walk(item_dir):
        d = Path(dirpath)
        media = {Path(n).stem for n in filenames if Path(n).suffix.lower() in MEDIA_EXTS}
        if not media and d.name.lower() != "trailers":
            continue
        for name in sorted(filenames):
            low = name.lower()
            if not low.endswith(".nfo") or low in FOLDER_NFOS or low in CRUFT_NAMES:
                continue
            if Path(name).stem in media:
                continue
            f = d / name
            findings["orphan_nfos"].append(str(f))
            if APPLY:
                f.unlink(missing_ok=True)
                deleted.append(str(f))


# --------------------------------------------------------------------------- case-duplicate folders

def merge_dir_tree(src: Path, dst: Path, findings: dict, moved: list[str], deleted: list[str]) -> None:
    """Recursively fold src into dst (matching existing dst entries case-insensitively
    at every level), then remove src once emptied. Only ever called under APPLY."""
    dst.mkdir(parents=True, exist_ok=True)
    dst_entries = {p.name.lower(): p for p in dst.iterdir()}
    for item in sorted(src.iterdir()):
        key = item.name.lower()
        target = dst_entries.get(key)
        if item.is_dir():
            if target is not None and target.is_dir():
                merge_dir_tree(item, target, findings, moved, deleted)
            else:
                dest = dst / item.name
                item.rename(dest)
                moved.append(f"{item} -> {dest}")
                dst_entries[key] = dest
            continue
        if target is None:
            dest = dst / item.name
            item.rename(dest)
            moved.append(f"{item} -> {dest}")
            dst_entries[key] = dest
        elif item.stat().st_size == target.stat().st_size and sha256_file(item) == sha256_file(target):
            findings["case_merge_exact_duplicates"].append(str(item))
            item.unlink()
            deleted.append(str(item))
        elif item.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS:
            dest = dst / f"{item.stem} (case-merge){item.suffix}"
            item.rename(dest)
            moved.append(f"{item} -> {dest}")
            findings["case_merge_conflicts"].append(f"{item} differs from {target}, kept as {dest}")
        else:
            findings["case_merge_conflicts"].append(f"{item} differs from {target}, dropped (metadata/image)")
            item.unlink()
            deleted.append(str(item))
    try:
        if not any(src.iterdir()):
            src.rmdir()
    except OSError as e:
        log.warning("rmdir %s: %s", src, e)


def find_and_merge_case_duplicates(parent: Path, findings: dict, moved: list[str], deleted: list[str]) -> None:
    """Group parent's immediate subfolders by lowercased name; for any group with more
    than one, merge every folder into whichever has the most files (recursively)."""
    if not parent.is_dir():
        return
    groups: dict[str, list[Path]] = {}
    for p in parent.iterdir():
        if p.is_dir():
            groups.setdefault(p.name.lower(), []).append(p)
    for paths in groups.values():
        if len(paths) < 2:
            continue

        def total_files(p: Path) -> int:
            return sum(1 for f in p.rglob("*") if f.is_file())

        winner, *losers = sorted(paths, key=lambda p: (total_files(p), p.name), reverse=True)
        for loser in losers:
            findings["case_duplicate_folders"].append(f"{loser} -> {winner}")
            if APPLY:
                merge_dir_tree(loser, winner, findings, moved, deleted)


# --------------------------------------------------------------------------- movies

def scan_movies(findings: dict, moved: list[str], deleted: list[str]) -> None:
    if not MOVIES_DIR.is_dir():
        log.warning("MOVIES_DIR %s not found, skipping", MOVIES_DIR)
        return
    find_and_merge_case_duplicates(MOVIES_DIR, findings, moved, deleted)
    movie_dirs = sorted(p for p in MOVIES_DIR.iterdir() if p.is_dir())
    for i, folder in enumerate(movie_dirs, 1):
        videos = [f for f in folder.iterdir()
                  if f.is_file() and f.suffix.lower() in VIDEO_EXTS and not is_loose_trailer(f)]
        if not videos:
            findings["movies_no_video"].append(str(folder))
        elif len(videos) > 1:
            findings["movies_multiple_videos"].append(f"{folder} ({len(videos)} files)")
        consolidate_trailers(folder, findings, moved, deleted)
        clean_orphan_nfos(folder, findings, deleted)
        consolidate_fanart(folder, findings, moved, deleted)
        if i % 200 == 0:
            log.info("movies: %d/%d scanned", i, len(movie_dirs))


# --------------------------------------------------------------------------- series

def scan_series(findings: dict, moved: list[str], deleted: list[str]) -> None:
    if not SERIES_DIR.is_dir():
        log.warning("SERIES_DIR %s not found, skipping", SERIES_DIR)
        return
    find_and_merge_case_duplicates(SERIES_DIR, findings, moved, deleted)
    for show in sorted(p for p in SERIES_DIR.iterdir() if p.is_dir()):
        for season in sorted(d for d in show.iterdir() if d.is_dir() and d.name.lower().startswith("season")):
            videos = [f for f in season.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
            if not videos:
                findings["seasons_no_video"].append(str(season))
                continue
            for v in videos:
                if not v.with_suffix(".nfo").exists():
                    findings["episodes_missing_nfo"].append(str(v))
        consolidate_trailers(show, findings, moved, deleted)
        clean_orphan_nfos(show, findings, deleted)
        consolidate_fanart(show, findings, moved, deleted)


# --------------------------------------------------------------------------- music

def scan_music(findings: dict, moved: list[str], deleted: list[str]) -> None:
    if not MUSIC_DIR.is_dir():
        log.warning("MUSIC_DIR %s not found, skipping", MUSIC_DIR)
        return
    find_and_merge_case_duplicates(MUSIC_DIR, findings, moved, deleted)
    skip_dirs = {"extrafanart", "extrathumbs"}
    for artist in sorted(p for p in MUSIC_DIR.iterdir() if p.is_dir()):
        consolidate_music_extrathumbs(artist, findings, moved, deleted)
        find_and_merge_case_duplicates(artist, findings, moved, deleted)
        for album in sorted(d for d in artist.iterdir() if d.is_dir() and d.name not in skip_dirs):
            tracks = [f for f in album.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS]
            if not tracks:
                findings["albums_no_audio"].append(str(album))


# --------------------------------------------------------------------------- main

def main() -> int:
    logging.basicConfig(level=env("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    findings: dict[str, list[str]] = {
        "cruft_files": [],
        "broken_backdrops": [],
        "oversized_backdrops": [],
        "loose_trailers": [],
        "orphan_nfos": [],
        "loose_fanart": [],
        "duplicate_fanart": [],
        "nfo_fanart_fixed": [],
        "case_duplicate_folders": [],
        "case_merge_exact_duplicates": [],
        "case_merge_conflicts": [],
        "static_backdrops": [],
        "movies_no_video": [],
        "movies_multiple_videos": [],
        "seasons_no_video": [],
        "episodes_missing_nfo": [],
        "albums_no_audio": [],
        "extrathumbs_overlap": [],
    }
    moved: list[str] = []
    deleted: list[str] = []
    if not FFMPEG_AVAILABLE:
        log.warning("ffmpeg/ffprobe not found - skipping static-backdrop detection")

    t0 = time.monotonic()
    scan_cruft(MOVIES_DIR, findings, deleted)
    scan_cruft(SERIES_DIR, findings, deleted)
    scan_cruft(MUSIC_DIR, findings, deleted)
    scan_backdrops(MOVIES_DIR, findings, deleted)
    scan_backdrops(SERIES_DIR, findings, deleted)
    scan_movies(findings, moved, deleted)
    scan_series(findings, moved, deleted)
    scan_music(findings, moved, deleted)
    elapsed = time.monotonic() - t0

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": now_iso(),
        "apply": APPLY,
        "elapsed_sec": round(elapsed, 1),
        "counts": {k: len(v) for k, v in findings.items()},
        "moved_count": len(moved),
        "deleted_count": len(deleted),
        "findings": findings,
        "moved": moved,
        "deleted": deleted,
    }
    report_path = DATA_DIR / f"report-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    report_path.write_text(json.dumps(report, indent=1, sort_keys=True))
    (DATA_DIR / "latest.json").write_text(json.dumps(report, indent=1, sort_keys=True))

    log.info("Mode: %s", "APPLY (cruft deleted, trailers/fanart moved/deduped, case-duplicates merged)"
              if APPLY else "report-only (nothing changed)")
    for k, v in findings.items():
        log.info("%s: %d", k, len(v))
    log.info("Report written to %s", report_path)

    # stdout carries one JSON summary line for schedulers/monitoring
    print(json.dumps({"apply": APPLY, "moved": len(moved), "deleted": len(deleted), **report["counts"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
