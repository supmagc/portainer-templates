"""Plugin settings → typed config (Stash returns unset settings as missing keys)."""
from dataclasses import dataclass, field

DEFAULT_ROOT = "/data/PerformerSets"
ALL_SOURCES = ("pornpics", "elitebabes", "babepedia")
ALL_PROMO = ("stashbox", "babepedia")


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


def _weights(value):
    """'Lingerie=3, Outdoor, Hardcore=-3, Gangbang=x' → [(name, weight or None = exclude)]; no weight = 2."""
    out = []
    for part in _csv(value):
        name, _, w = part.partition("=")
        w = w.strip().lower()
        if name.strip():
            out.append((name.strip(), None if w in ("x", "exclude") else max(-10.0, min(10.0, _num(w, 2.0)))))
    return out


def _choice(value, known, what):
    picked = [x.lower() for x in _csv(value)] or list(known)
    unknown = [x for x in picked if x not in known]
    if unknown:
        raise SystemExit(f"unknown {what}(s): {', '.join(unknown)} (known: {', '.join(known)})")
    return picked


@dataclass
class Config:
    dry_run: bool = True
    root: str = DEFAULT_ROOT
    include_favorites: bool = True
    min_rating100: int = 0
    tags: list = field(default_factory=list)
    sources: list = field(default_factory=lambda: list(ALL_SOURCES))
    promo_images: bool = True
    promo_sources: list = field(default_factory=lambda: list(ALL_PROMO))
    max_sets: int = 50
    max_per_performer: int = 50
    max_per_studio: int = 5
    exclude_studios: list = field(default_factory=list)
    tag_weights: list = field(default_factory=list)
    favorite_tag_weight: float = 2.0
    ideal_set_size: int = 40
    evict_rating100: int = 20
    recheck_days: float = 7
    blur_percentile: float = 10
    min_size: int = 800
    delay: float = 2.0
    distance: int = 4
    enable_hook: bool = True

    @classmethod
    def from_settings(cls, s):
        stars = _num(s.get("min_rating"), 0)
        return cls(
            dry_run=_bool(s.get("dry_run"), True),
            root=(s.get("download_root") or DEFAULT_ROOT).rstrip("/"),
            include_favorites=_bool(s.get("include_favorites"), True),
            min_rating100=max(0, min(100, round(stars * 20))),
            tags=_csv(s.get("tags")),
            sources=_choice(s.get("sources"), ALL_SOURCES, "source"),
            promo_images=_bool(s.get("promo_images"), True),
            promo_sources=_choice(s.get("promo_sources"), ALL_PROMO, "promo source"),
            max_sets=int(_num(s.get("max_sets_per_source"), 50)),
            max_per_performer=int(_num(s.get("max_sets_per_performer"), 50)),
            max_per_studio=int(_num(s.get("max_sets_per_studio"), 5)),
            exclude_studios=_csv(s.get("exclude_studios")),
            tag_weights=_weights(s.get("tag_weights")),
            favorite_tag_weight=max(-10.0, min(10.0, _num(s.get("favorite_tag_weight"), 2.0))),
            ideal_set_size=max(0, int(_num(s.get("ideal_set_size"), 40))),
            evict_rating100=max(0, min(100, round(_num(s.get("evict_rating"), 1) * 20))),
            recheck_days=_num(s.get("recheck_days"), 7),
            blur_percentile=_num(s.get("blur_percentile"), 10),
            min_size=int(_num(s.get("min_image_size"), 800)),
            delay=_num(s.get("request_delay"), 2.0),
            distance=int(_num(s.get("dedupe_distance"), 4)),
            enable_hook=_bool(s.get("enable_hook"), True),
        )
