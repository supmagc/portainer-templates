"""libraries.yaml loader."""
import posixpath

import yaml

DEFAULTS = {"studio_mode": "follow", "studio_folders": False, "default_source": "WEBDL", "max_path_len": 225,
            "rename": True, "studios": None, "studio": None, "whisparr": False}
STUDIO_MODES = ("follow", "fixed")
_MODE_ALIASES = {"free": "follow", "folder": "fixed"}


def _studio_map(raw):
    """folder name -> studio name; accepts a mapping or a list of single-entry mappings."""
    items = raw if isinstance(raw, list) else [raw or {}]
    out = {}
    for item in items:
        for key, value in (item or {}).items():
            out[str(key).casefold()] = value
    return out


class Library:
    def __init__(self, name, root, **opts):
        self.name = name
        self.root = root
        mode = opts["studio_mode"]
        self.studio_mode = _MODE_ALIASES.get(mode, mode)
        if self.studio_mode not in STUDIO_MODES:
            raise ValueError(f"library {name}: studio_mode must be one of {STUDIO_MODES}")
        folders = bool(opts["studio_folders"])
        if self.studio_mode == "follow":
            if not folders:
                raise ValueError(f"library {name}: studio_mode follow requires studio_folders: true")
            if opts["studio"] or opts["studios"]:
                raise ValueError(f"library {name}: studio_mode follow cannot have studio/studios")
        elif folders:
            if opts["studio"]:
                raise ValueError(f"library {name}: fixed with studio_folders: true cannot have studio (use studios)")
        else:
            if not opts["studio"]:
                raise ValueError(f"library {name}: fixed with studio_folders: false requires studio")
            if opts["studios"]:
                raise ValueError(f"library {name}: fixed with studio_folders: false cannot have studios (use studio)")
        self.studio_folders = folders and self.studio_mode == "follow"
        self.default_source = opts["default_source"]
        self.max_path_len = int(opts["max_path_len"])
        self.rename = bool(opts["rename"])
        self.whisparr = bool(opts["whisparr"])
        self.fixed_studio = opts["studio"] if self.studio_mode == "fixed" else None
        self.studios = _studio_map(opts["studios"])

    def forced_studio(self, directory):
        """Studio name Stash must carry for a file in `directory`, or None when Stash decides.

        fixed + `studio`: the whole library has that studio; fixed alone: the first-level
        folder below the library root is the studio (optionally renamed via `studios`).
        """
        if self.studio_mode != "fixed":
            return None
        if self.fixed_studio:
            return self.fixed_studio
        rel = posixpath.relpath(directory, self.root)
        if rel == ".":
            return None
        first = rel.split("/")[0]
        return self.studios.get(first.casefold(), first)


class Config:
    def __init__(self, raw):
        self.base_path = posixpath.normpath(raw.get("base_path") or "/")
        self.poster_crop_x = float(raw.get("poster_crop_x", 0.5))
        self.whisparr = raw.get("whisparr") or {}
        self.libraries = []
        for item in raw.get("libraries") or []:
            opts = {**DEFAULTS, **{k: v for k, v in item.items() if k in DEFAULTS}}
            root = posixpath.normpath(posixpath.join(self.base_path, item["path"]))
            self.libraries.append(Library(item["name"], root, **opts))

    def library_for(self, path):
        best = None
        for lib in self.libraries:
            if path.startswith(lib.root + "/") and (best is None or len(lib.root) > len(best.root)):
                best = lib
        return best

    def select(self, names):
        wanted = {n.strip().casefold() for n in (names or "").split(",") if n.strip()}
        return [lib for lib in self.libraries if not wanted or lib.name.casefold() in wanted]


def load(path):
    with open(path, encoding="utf-8") as fh:
        return Config(yaml.safe_load(fh) or {})
