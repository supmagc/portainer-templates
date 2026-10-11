"""File/folder naming: Windows-safe sanitising, quality label, length fitting."""
import re
import unicodedata

RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
MAX_COMPONENT_BYTES = 255
# Longest sidecar suffix we may add next to a basename (e.g. "-fanart.jpeg", ".nfo-orig").
SIDECAR_RESERVE = len("-background.jpeg")
ELLIPSIS = "…"

# (height, width) pairs; first match wins.
_STANDARD = [(2160, 3840), (1440, 2560), (1080, 1920), (720, 1280), (576, 1024),
             (540, 960), (480, 854), (360, 640), (240, 426)]
_SOURCE_RE = re.compile(r"\[([A-Za-z0-9._]+)-(?:\d+p?|unknown)\]")


def utf8_len(text):
    return len(text.encode("utf-8"))


def truncate_bytes(text, limit):
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def sanitize(text):
    """One path component: no Windows-forbidden chars, no trailing dots/spaces, NFC."""
    s = unicodedata.normalize("NFC", text or "")
    s = s.encode("utf-8", "ignore").decode("utf-8")
    s = s.replace(":", " ").replace('"', "")
    s = re.sub(r"[\\/|]", "-", s)
    s = re.sub(r"[?*<>]", "", s)
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.rstrip(". ")


def fix_reserved(name):
    if name.split(".")[0].rstrip().upper() in RESERVED:
        return name + "_"
    return name


def folder_name(studio):
    """Studio folder component; None when the studio can't make a usable folder name."""
    s = fix_reserved(sanitize(studio))
    return truncate_bytes(s, MAX_COMPONENT_BYTES).rstrip(". ") or None


def resolution_label(width, height):
    w, h = max(width or 0, height or 0), min(width or 0, height or 0)
    if not w:
        return None
    for std_h, std_w in _STANDARD:
        if h >= std_h * 0.97 or w >= std_w * 0.95:
            return f"{std_h}p"
    return f"{h}p" if h else None


def parse_source(basename, default):
    m = _SOURCE_RE.search(basename or "")
    return m.group(1) if m else default


def quality_token(basename, width, height, default_source):
    res = resolution_label(width, height) or "unknown"
    return f"{parse_source(basename, default_source)}-{res}"


def compose(studio, date, title, performers, quality):
    head = " - ".join(p for p in (studio, date, title, performers) if p)
    return f"{head} [{quality}]" if head else f"[{quality}]"


def _order(names, old_base):
    """Alphabetical, unless the existing name already lists exactly these performers (then keep its order)."""
    names = sorted({sanitize(n) for n in names if sanitize(n)}, key=str.casefold)
    if old_base and names:
        tail = old_base.rsplit(" [", 1)[0].rsplit(" - ", 1)[-1]
        parts = tail.split(", ")
        if len(parts) == len(names) and set(parts) == set(names):
            return parts
    return names


def performer_text(names, shown=None, old_base=None):
    names = _order(names, old_base)
    if shown is None or shown >= len(names):
        return ", ".join(names)
    return ", ".join(names[:shown]) + f" +{len(names) - shown}"


def build_basename(scene_fields, quality, dir_path, max_path_len, old_base=None):
    """Fit the name into the byte and total-path budgets.

    Returns (basename, truncated_flag), or (None, True) when even the minimum doesn't fit.
    scene_fields: dict(studio, date, title, performers[list of str]).
    """
    studio = sanitize(scene_fields.get("studio") or "")
    date = sanitize(scene_fields.get("date") or "")
    title = sanitize(scene_fields.get("title") or "")
    names = scene_fields.get("performers") or []
    total = len({sanitize(n) for n in names if sanitize(n)})

    def fits(name):
        # +4 covers ".mp4"; reserve covers the longest sidecar suffix; +1 for the separator
        ext_budget = max(SIDECAR_RESERVE, 5)
        if utf8_len(name) + ext_budget > MAX_COMPONENT_BYTES:
            return False
        return len(dir_path) + 1 + len(name) + ext_budget <= max_path_len

    shown = None
    cur_title = title
    name = compose(studio, date, cur_title, performer_text(names, shown, old_base), quality)
    if fits(name):
        return name, False
    for shown in range(total - 1, 0, -1):
        name = compose(studio, date, cur_title, performer_text(names, shown, old_base), quality)
        if fits(name):
            return name, True
    shown = 1 if total else None
    perf = performer_text(names, shown, old_base)
    while len(cur_title) > 10:
        cur_title = cur_title[: max(10, len(cur_title) - 8)].rstrip(" .,-") + ELLIPSIS
        name = compose(studio, date, cur_title, perf, quality)
        if fits(name):
            return name, True
        cur_title = cur_title.rstrip(ELLIPSIS)
    name = compose(studio, date, cur_title, "", quality)
    if fits(name):
        return name, True
    return None, True


def with_suffix_number(basename, n):
    return basename if n <= 1 else f"{basename} ({n})"
