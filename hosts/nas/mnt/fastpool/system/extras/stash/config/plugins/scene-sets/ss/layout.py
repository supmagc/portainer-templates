"""Folder naming, ownership-preserving writes and the set.json sidecar.

<root>/<Studio>/<Studio> - <date> - <title> [<site>-<id>]/  — the "[<site>-<id>]" tail is the key, so a folder
renamed by hand (or made by hand, like the first Kink sets) is still found."""
import json
import os
import posixpath
import re
import stat
import tempfile
import unicodedata

SIDECAR = "set.json"
MAX_COMPONENT_BYTES = 180
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_KEY_RE = re.compile(r"\[([a-z0-9]+-[^\]\s]+)\]$")


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


def existing_sets(root):
    """{key: folder} for every '<anything> [<key>]' folder one level below a studio folder."""
    out = {}
    for studio in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        sdir = posixpath.join(root, studio)
        if studio.startswith(".") or not os.path.isdir(sdir):
            continue
        for entry in sorted(os.listdir(sdir)):
            m = _KEY_RE.search(entry)
            if m and os.path.isdir(posixpath.join(sdir, entry)):
                out.setdefault(m.group(1), posixpath.join(sdir, entry))
    return out


def set_folder(root, scene, info):
    studio = sanitize(((scene.get("studio") or {}).get("name")) or "") or "Unknown Studio"
    tail = f" [{info.key}]"
    title = sanitize(scene.get("title") or info.title) or "untitled"
    body = " - ".join(p for p in (studio, scene.get("date"), title) if p)
    return posixpath.join(root, _fit(studio, MAX_COMPONENT_BYTES), _fit(body, MAX_COMPONENT_BYTES - len(tail.encode("utf-8"))) + tail)


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
    fd, tmp = tempfile.mkstemp(prefix=".ss-", dir=directory)
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
