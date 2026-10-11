"""Filesystem helpers: ownership-preserving mkdir/write/rename, sidecar discovery."""
import os
import posixpath
import re
import shutil
import stat
import tempfile

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".flv", ".webm", ".mpg", ".mpeg", ".ts", ".m2ts"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_ART_RE = re.compile(r"^-(poster|fanart|background|thumb)(\.[A-Za-z0-9]+)$")


def listdir(directory):
    try:
        return os.listdir(directory)
    except OSError:
        return []


def find_casefold(directory, name):
    """Actual on-disk spelling of name in directory (case-insensitive), or None."""
    want = name.casefold()
    for entry in listdir(directory):
        if entry.casefold() == want:
            return entry
    return None


def resolve_dir(parent, name):
    return posixpath.join(parent, find_casefold(parent, name) or name)


def match_owner(path, like):
    st = os.stat(like)
    try:
        os.chown(path, st.st_uid, st.st_gid)
    except (PermissionError, OSError):
        pass


def ensure_dir(path, stop_at):
    """mkdir -p below stop_at; new dirs copy owner and permission bits from their parent."""
    missing = []
    cur = path
    while not os.path.isdir(cur) and cur != stop_at and cur not in ("/", ""):
        missing.append(cur)
        cur = posixpath.dirname(cur)
    for d in reversed(missing):
        parent = posixpath.dirname(d)
        os.mkdir(d)
        match_owner(d, parent)
        os.chmod(d, stat.S_IMODE(os.stat(parent).st_mode))
    return list(reversed(missing))


def atomic_write(path, data, like):
    directory = posixpath.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".lo-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        match_owner(tmp, like)
        os.chmod(tmp, stat.S_IMODE(os.stat(like).st_mode) & 0o666)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def rename_nooverwrite(src, dst):
    if os.path.lexists(dst):
        raise FileExistsError(dst)
    try:
        os.rename(src, dst)
    except OSError:
        shutil.move(src, dst)


def find_sidecars(directory, base, listing=None):
    """Suffixes of files that belong to video `base` (e.g. '.nfo', '-poster.png', '.en.srt')."""
    out = []
    for name in listing if listing is not None else listdir(directory):
        if not name.startswith(base) or name == base:
            continue
        suffix = name[len(base):]
        if suffix[0] == "-":
            if not _ART_RE.match(suffix):
                continue
        elif suffix[0] == ".":
            if any(suffix.lower().endswith(e) for e in VIDEO_EXTS):
                continue
        else:
            continue
        out.append(suffix)
    return sorted(out)


def map_suffix(suffix):
    """Legacy -background art becomes -fanart (extension kept)."""
    m = _ART_RE.match(suffix)
    if m and m.group(1) == "background":
        return "-fanart" + m.group(2)
    return suffix


def has_art(suffixes, kind):
    return any((m := _ART_RE.match(s)) and m.group(1) == kind and m.group(2).lower() in IMAGE_EXTS for s in suffixes)


def is_video(name):
    return os.path.splitext(name)[1].lower() in VIDEO_EXTS


def art_suffix_kind(suffix):
    m = _ART_RE.match(suffix)
    return m.group(1) if m else None
