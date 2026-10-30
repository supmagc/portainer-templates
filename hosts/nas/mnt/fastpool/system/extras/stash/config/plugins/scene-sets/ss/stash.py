"""Minimal Stash GraphQL client (requests) plus cached name → id lookups."""
import time

import requests

from .layout import norm

KEEPALIVE = 600  # seconds; the session cookie expires after Stash's max_session_age (default 1 h) without requests

SCENE_FIELDS = ("id title code date details director rating100 urls studio { name } performers { name favorite } "
                "galleries { id }")
GALLERY_FIELDS = "id title date details urls studio { id } performers { id } scenes { id } tags { id } folder { path }"


class StashError(Exception):
    pass


class Stash:
    def __init__(self, base_url, headers=None, cookies=None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(headers or {})
        self.cookies = dict(cookies or {})
        self._last = time.monotonic()
        self._names = {}
        self._scenes = {}

    @classmethod
    def from_connection(cls, conn):
        host = conn.get("Host") or "localhost"
        if host in ("0.0.0.0", "::", ""):
            host = "localhost"
        cookie = conn.get("SessionCookie") or {}
        cookies = {cookie["Name"]: cookie["Value"]} if cookie.get("Name") else {}
        return cls(f"{conn.get('Scheme', 'http')}://{host}:{conn.get('Port', 9999)}", cookies=cookies)

    def gql(self, query, variables=None):
        r = self.session.post(f"{self.base_url}/graphql", json={"query": query, "variables": variables or {}},
                              cookies=self.cookies, timeout=60)
        # Stash re-issues the session cookie on every request: carry the fresh one, never the stale original
        self.session.cookies.clear()
        self.cookies.update(r.cookies.get_dict())
        self._last = time.monotonic()
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise StashError("; ".join(e.get("message", "?") for e in body["errors"]))
        return body["data"]

    def keepalive(self):
        """Call from long loops that don't talk to Stash, so the session doesn't expire."""
        if time.monotonic() - self._last > KEEPALIVE:
            self.gql("{ version { version } }")

    def plugin_settings(self, plugin_id):
        data = self.gql("{ configuration { plugins } }")
        return (data["configuration"]["plugins"] or {}).get(plugin_id) or {}

    def cdp_path(self):
        """Stash's own scraper Chrome CDP setting, e.g. http://chromedp:9222/json/version."""
        return self.gql("{ configuration { scraping { scraperCDPPath } } }")["configuration"]["scraping"]["scraperCDPPath"]

    # scenes

    def find_scenes(self, scene_filter):
        q = ("query($f: FindFilterType, $sf: SceneFilterType){ findScenes(filter:$f, scene_filter:$sf)"
             "{ scenes { %s } } }" % SCENE_FIELDS)
        return self.gql(q, {"f": {"per_page": -1, "sort": "date", "direction": "DESC"}, "sf": scene_filter})["findScenes"]["scenes"]

    def select_scenes(self, cfg):
        """Union of the enabled criteria (minimum rating / a favorite performer), newest first."""
        filters = []
        if cfg.min_rating100:
            filters.append({"rating100": {"value": cfg.min_rating100 - 1, "modifier": "GREATER_THAN"}})
        if cfg.include_favorite_performers:
            filters.append({"performer_favorite": True})
        chosen = {}
        for sf in filters:
            for s in self.find_scenes(sf):
                chosen[s["id"]] = s
        return sorted(chosen.values(), key=lambda s: (s.get("date") or "", int(s["id"])), reverse=True)

    def find_scene(self, sid):
        if sid not in self._scenes:
            self._scenes[sid] = self.gql("query($id: ID!){ findScene(id:$id){ %s } }" % SCENE_FIELDS, {"id": sid})["findScene"]
        return self._scenes[sid]

    # name → id lookups: exact name/alias first, then a text search compared with layout.norm
    # ("X Art" finds "X-Art"); more than one hit = ambiguous = None

    _KINDS = {"performer": ("Performers", "PerformerFilterType", "alias_list"),
              "studio": ("Studios", "StudioFilterType", "aliases"),
              "tag": ("Tags", "TagFilterType", "aliases")}

    def lookup(self, kind, name):
        key = (kind, name.casefold())
        if key not in self._names:
            plural, ftype, alias_field = self._KINDS[kind]
            q = ("query($f: FindFilterType, $x: %s){ find%s(filter:$f, %s_filter:$x){ %s { id name %s } } }"
                 % (ftype, plural, kind, plural.lower(), alias_field))
            crit = {"value": name, "modifier": "EQUALS"}
            hits = self.gql(q, {"f": {"per_page": 2}, "x": {"name": crit, "OR": {"aliases": crit}}})[f"find{plural}"][plural.lower()]
            if not hits:
                want = norm(name)
                found = self.gql(q, {"f": {"q": name, "per_page": 40}, "x": None})[f"find{plural}"][plural.lower()]
                hits = [h for h in found if want and want in {norm(n) for n in [h["name"], *(h[alias_field] or [])]}]
            self._names[key] = hits[0]["id"] if len(hits) == 1 else None
        return self._names[key]

    def ensure_tag(self, name):
        tid = self.lookup("tag", name)
        if tid is None:
            tid = self.gql("mutation($i: TagCreateInput!){ tagCreate(input:$i){ id } }", {"i": {"name": name}})["tagCreate"]["id"]
            self._names[("tag", name.casefold())] = tid
        return tid

    # galleries

    def find_gallery(self, gid):
        return self.gql("query($id: ID!){ findGallery(id:$id){ %s } }" % GALLERY_FIELDS, {"id": gid})["findGallery"]

    def galleries_under(self, root):
        prefix = root.rstrip("/") + "/"
        q = ("query($f: FindFilterType, $gf: GalleryFilterType){ findGalleries(filter:$f, gallery_filter:$gf)"
             "{ galleries { %s } } }" % GALLERY_FIELDS)
        data = self.gql(q, {"f": {"per_page": -1, "sort": "id"}, "gf": {"path": {"value": root, "modifier": "INCLUDES"}}})
        return [g for g in data["findGalleries"]["galleries"] if ((g.get("folder") or {}).get("path") or "").startswith(prefix)]

    def update_gallery(self, values):
        self.gql("mutation($i: GalleryUpdateInput!){ galleryUpdate(input:$i){ id } }", {"i": values})

    def scan(self, paths):
        return self.gql("mutation($i: ScanMetadataInput!){ metadataScan(input:$i) }", {"i": {"paths": list(paths)}})["metadataScan"]
