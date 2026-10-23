"""Folder naming, name matching, ownership-preserving writes and the set.json sidecar."""
import json
import os
import posixpath
import re
import stat
import tempfile
import unicodedata

PROMO = "_promo"
SIDECAR = "set.json"
MAX_COMPONENT_BYTES = 180
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_ID_RE = re.compile(r"\[(\d+)\]$")


def sanitize(text):
    """One path component: no Windows-forbidden chars, no trailing dots/spaces, NFC."""
    s = unicodedata.normalize("NFC", text or "")
    s = s.encode("utf-8", "ignore").decode("utf-8")
    s = s.replace(":", " ").replace('"', "")
    s = re.sub(r"[\\/|]", "-", s)
    s = re.sub(r"[?*<>]", "", s)
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(". ")
    if s.split(".")[0].rstrip().upper() in RESERVED:
        s += "_"
    return s


def _fit(text, limit):
    while len(text.encode("utf-8")) > limit:
        text = text[:-1]
    return text.rstrip(". -")


def norm(name):
    """Comparison key: accents, case, spacing and punctuation ignored."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^0-9a-z]", "", s.casefold())


def performer_keys(performer):
    return {k for k in (norm(n) for n in [performer["name"], *(performer.get("alias_list") or [])]) if k}


def performer_dir(root, performer):
    """Existing '<anything> [<id>]' folder (a later rename in Stash keeps it), else '<name> [<id>]'."""
    pid = str(performer["id"])
    try:
        for entry in sorted(os.listdir(root)):
            m = _ID_RE.search(entry)
            if m and m.group(1) == pid and os.path.isdir(posixpath.join(root, entry)):
                return posixpath.join(root, entry)
    except FileNotFoundError:
        pass
    name = sanitize(performer["name"]) or "performer"
    return posixpath.join(root, f"{_fit(name, MAX_COMPONENT_BYTES - 12)} [{pid}]")


def set_dir_name(info):
    """'<date> <studio> - <title> [<source>-<id>]'; the bracket keeps names unique per source set."""
    tail = f" [{info.source}-{info.source_id}]"
    head = " ".join(p for p in (info.date, sanitize(info.studio)) if p)
    title = sanitize(info.title) or "untitled"
    body = f"{head} - {title}" if head else title
    return _fit(body, MAX_COMPONENT_BYTES - len(tail.encode("utf-8"))) + tail


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
        _match_owner(d, parent)
        os.chmod(d, stat.S_IMODE(os.stat(parent).st_mode))


def _match_owner(path, like):
    st = os.stat(like)
    try:
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


def atomic_write(path, data):
    """Write via a temp file in the same folder (no image extension, so a concurrent scan ignores it)."""
    directory = posixpath.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".ps-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        _match_owner(tmp, directory)
        os.chmod(tmp, stat.S_IMODE(os.stat(directory).st_mode) & 0o666)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_sidecar(folder):
    try:
        with open(posixpath.join(folder, SIDECAR), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_sidecar(folder, data):
    atomic_write(posixpath.join(folder, SIDECAR), (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def add_sidecar_url(folder, url):
    data = read_sidecar(folder)
    if data is None or url in data.get("urls", []):
        return False
    data.setdefault("urls", []).append(url)
    write_sidecar(folder, data)
    return True
