"""Minimal Stash GraphQL client (requests) plus cached name → id lookups."""
import time

import requests

from .layout import norm

KEEPALIVE = 600  # seconds; the session cookie expires after Stash's max_session_age (default 1 h) without requests

PERFORMER_FIELDS = "id name disambiguation alias_list urls favorite rating100 stash_ids { endpoint stash_id }"
GALLERY_FIELDS = "id title date details urls rating100 organized studio { id } performers { id } tags { id } folder { path }"


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
        self._studios = {}
        self._lineage = {}
        self._favorite_tags = None
        self._genders = None

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

    def stash_boxes(self):
        data = self.gql("{ configuration { general { stashBoxes { endpoint api_key name } } } }")
        return {b["endpoint"]: b for b in data["configuration"]["general"]["stashBoxes"]}

    # performers

    def find_performer(self, pid):
        return self.gql("query($id: ID!){ findPerformer(id:$id){ %s } }" % PERFORMER_FIELDS, {"id": pid})["findPerformer"]

    def find_performers(self, performer_filter):
        q = ("query($f: FindFilterType, $pf: PerformerFilterType){ findPerformers(filter:$f, performer_filter:$pf)"
             "{ performers { %s } } }" % PERFORMER_FIELDS)
        data = self.gql(q, {"f": {"per_page": -1, "sort": "name"}, "pf": performer_filter})
        return data["findPerformers"]["performers"]

    def select_performers(self, cfg, tag_ids):
        """Union of the enabled criteria (favorites / minimum rating / tags), sorted by name."""
        filters = []
        if cfg.include_favorites:
            filters.append({"filter_favorites": True})
        if cfg.min_rating100:
            filters.append({"rating100": {"value": cfg.min_rating100 - 1, "modifier": "GREATER_THAN"}})
        if tag_ids:
            filters.append({"tags": {"value": tag_ids, "modifier": "INCLUDES", "depth": -1}})
        chosen = {}
        for pf in filters:
            for p in self.find_performers(pf):
                chosen[p["id"]] = p
        return sorted(chosen.values(), key=lambda p: (p["name"].casefold(), int(p["id"])))

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

    def studio_info(self, name):
        """{favorite, rating100} of the studio this name resolves to (a hearted/rated parent studio counts for its
        sub-studios), None when it isn't in Stash."""
        sid = self.lookup("studio", name)
        if sid is None:
            return None
        if sid not in self._studios:
            q = "query($id: ID!){ findStudio(id:$id){ favorite rating100 parent_studio { favorite rating100 } } }"
            st = self.gql(q, {"id": sid})["findStudio"] or {}
            parent = st.get("parent_studio") or {}
            self._studios[sid] = {"favorite": bool(st.get("favorite") or parent.get("favorite")),
                                  "rating100": st.get("rating100") or parent.get("rating100")}
        return self._studios[sid]

    def tag_lineage(self, tid):
        """The tag and all its parent tags (ids)."""
        if tid not in self._lineage:
            self._lineage[tid] = {tid}
            parents = (self.gql("query($id: ID!){ findTag(id:$id){ parents { id } } }", {"id": tid})["findTag"] or {}).get("parents") or []
            for p in parents:
                self._lineage[tid] |= self.tag_lineage(p["id"])
        return self._lineage[tid]

    def is_female(self, name):
        """Whether the name/alias belongs to exactly one Stash gender, FEMALE (one query for all performers)."""
        if self._genders is None:
            q = "query{ findPerformers(filter:{per_page:-1}){ performers { name alias_list gender } } }"
            self._genders = {}
            for p in self.gql(q)["findPerformers"]["performers"]:
                for n in [p["name"], *(p["alias_list"] or [])]:
                    self._genders.setdefault(norm(n), set()).add(p.get("gender"))
        return self._genders.get(norm(name)) == {"FEMALE"}

    def favorite_tags(self):
        """Ids of the hearted tags."""
        if self._favorite_tags is None:
            q = "query{ findTags(filter:{per_page:-1}, tag_filter:{favorite:true}){ tags { id } } }"
            self._favorite_tags = {t["id"] for t in self.gql(q)["findTags"]["tags"]}
        return self._favorite_tags

    def ensure_tag(self, name):
        tid = self.lookup("tag", name)
        if tid is None:
            tid = self.gql("mutation($i: TagCreateInput!){ tagCreate(input:$i){ id } }", {"i": {"name": name}})["tagCreate"]["id"]
            self._names[("tag", name.casefold())] = tid
        return tid

    # galleries

    def find_gallery(self, gid):
        return self.gql("query($id: ID!){ findGallery(id:$id){ %s } }" % GALLERY_FIELDS, {"id": gid})["findGallery"]

    def _galleries_matching(self, path):
        q = ("query($f: FindFilterType, $gf: GalleryFilterType){ findGalleries(filter:$f, gallery_filter:$gf)"
             "{ galleries { %s } } }" % GALLERY_FIELDS)
        data = self.gql(q, {"f": {"per_page": -1, "sort": "id"}, "gf": {"path": {"value": path, "modifier": "INCLUDES"}}})
        return [g for g in data["findGalleries"]["galleries"] if (g.get("folder") or {}).get("path")]

    def galleries_under(self, root):
        prefix = root.rstrip("/") + "/"
        return [g for g in self._galleries_matching(root) if g["folder"]["path"].startswith(prefix)]

    def gallery_for_folder(self, folder):
        return next((g for g in self._galleries_matching(folder) if g["folder"]["path"] == folder), None)

    def update_gallery(self, values):
        self.gql("mutation($i: GalleryUpdateInput!){ galleryUpdate(input:$i){ id } }", {"i": values})

    def clean(self, paths):
        return self.gql("mutation($i: CleanMetadataInput!){ metadataClean(input:$i) }",
                        {"i": {"paths": list(paths), "dryRun": False}})["metadataClean"]

    def scan(self, paths):
        return self.gql("mutation($i: ScanMetadataInput!){ metadataScan(input:$i) }", {"i": {"paths": list(paths)}})["metadataScan"]
