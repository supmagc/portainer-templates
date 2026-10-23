"""Plugin settings → typed config (Stash returns unset settings as missing keys)."""
from dataclasses import dataclass, field

DEFAULT_ROOT = "/data/PerformerSets"
ALL_SOURCES = ("pornpics", "elitebabes")


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


def _csv(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass
class Config:
    dry_run: bool = True
    root: str = DEFAULT_ROOT
    include_favorites: bool = True
    min_rating100: int = 0
    tags: list = field(default_factory=list)
    sources: list = field(default_factory=lambda: list(ALL_SOURCES))
    promo_images: bool = True
    max_sets: int = 50
    min_size: int = 800
    delay: float = 2.0
    distance: int = 4
    map_source_tags: bool = False
    enable_hook: bool = True

    @classmethod
    def from_settings(cls, s):
        stars = _num(s.get("min_rating"), 0)
        sources = [x.lower() for x in _csv(s.get("sources"))] or list(ALL_SOURCES)
        unknown = [x for x in sources if x not in ALL_SOURCES]
        if unknown:
            raise SystemExit(f"unknown source(s): {', '.join(unknown)} (known: {', '.join(ALL_SOURCES)})")
        return cls(
            dry_run=_bool(s.get("dry_run"), True),
            root=(s.get("download_root") or DEFAULT_ROOT).rstrip("/"),
            include_favorites=_bool(s.get("include_favorites"), True),
            min_rating100=max(0, min(100, round(stars * 20))),
            tags=_csv(s.get("tags")),
            sources=sources,
            promo_images=_bool(s.get("promo_images"), True),
            max_sets=int(_num(s.get("max_sets_per_source"), 50)),
            min_size=int(_num(s.get("min_image_size"), 800)),
            delay=_num(s.get("request_delay"), 2.0),
            distance=int(_num(s.get("dedupe_distance"), 4)),
            map_source_tags=_bool(s.get("map_source_tags"), False),
            enable_hook=_bool(s.get("enable_hook"), True),
        )
