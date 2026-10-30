"""Plugin settings → typed config (Stash returns unset settings as missing keys)."""
from dataclasses import dataclass, field

DEFAULT_ROOT = "/data/SceneSets"
ALL_SOURCES = ("kink", "vixen")


def _num(value, default):
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _choice(value, known, what):
    picked = [x.strip().lower() for x in (value or "").split(",") if x.strip()] or list(known)
    unknown = [x for x in picked if x not in known]
    if unknown:
        raise SystemExit(f"unknown {what}(s): {', '.join(unknown)} (known: {', '.join(known)})")
    return picked


def _cookie(value, default_name):
    """'name=value', a copied Set-Cookie line ('"kinky.sess=…; Path=/; Expires=…"') or a bare value →
    (name, value), or None when empty."""
    first = (value or "").strip().strip('"').split(";", 1)[0].strip()
    if not first:
        return None
    name, sep, val = first.partition("=")
    return (name.strip(), val.strip()) if sep else (default_name, first)


@dataclass
class Config:
    dry_run: bool = True
    root: str = DEFAULT_ROOT
    min_rating100: int = 0
    include_favorite_performers: bool = True
    sources: list = field(default_factory=lambda: list(ALL_SOURCES))
    min_size: int = 800
    delay: float = 2.0
    cdp_url: str = ""
    kink_session: tuple = None  # (cookie name, value) of a logged-in kink.com session
    enable_hook: bool = True

    @classmethod
    def from_settings(cls, s):
        return cls(
            dry_run=_bool(s.get("dry_run"), True),
            root=(s.get("download_root") or DEFAULT_ROOT).rstrip("/"),
            min_rating100=max(0, min(100, round(_num(s.get("min_rating"), 0) * 20))),
            include_favorite_performers=_bool(s.get("include_favorite_performers"), True),
            sources=_choice(s.get("sources"), ALL_SOURCES, "source"),
            min_size=int(_num(s.get("min_image_size"), 800)),
            delay=_num(s.get("request_delay"), 2.0),
            cdp_url=(s.get("cdp_url") or "").strip(),
            kink_session=_cookie(s.get("kink_cookie"), "kinky.sess"),
            enable_hook=_bool(s.get("enable_hook"), True),
        )
