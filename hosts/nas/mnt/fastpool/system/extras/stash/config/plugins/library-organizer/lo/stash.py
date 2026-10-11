"""Minimal Stash GraphQL client (requests)."""
from urllib.parse import urlsplit

import requests

SCENE_FIELDS = """
id title code details director urls date rating100 organized o_counter interactive custom_fields
created_at updated_at last_played_at resume_time play_count
files { id path basename width height duration video_codec audio_codec frame_rate bit_rate }
paths { screenshot }
studio { id name parent_studio { id name parent_studio { id name parent_studio { id name } } } }
performers { id name disambiguation urls }
tags { name }
groups { group { name } }
stash_ids { endpoint stash_id }
scene_markers { title seconds end_seconds primary_tag { name } tags { name } }
galleries { id }
"""


class StashError(Exception):
    pass


class Stash:
    def __init__(self, base_url, headers=None, cookies=None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(headers or {})
        for k, v in (cookies or {}).items():
            self.session.cookies.set(k, v)

    @classmethod
    def from_connection(cls, conn):
        host = conn.get("Host") or "localhost"
        if host in ("0.0.0.0", "::", ""):
            host = "localhost"
        cookie = conn.get("SessionCookie") or {}
        cookies = {cookie["Name"]: cookie["Value"]} if cookie.get("Name") else {}
        return cls(f"{conn.get('Scheme', 'http')}://{host}:{conn.get('Port', 9999)}", cookies=cookies)

    def gql(self, query, variables=None):
        r = self.session.post(f"{self.base_url}/graphql", json={"query": query, "variables": variables or {}}, timeout=60)
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise StashError("; ".join(e.get("message", "?") for e in body["errors"]))
        return body["data"]

    def plugin_settings(self, plugin_id):
        data = self.gql("{ configuration { plugins } }")
        return (data["configuration"]["plugins"] or {}).get(plugin_id) or {}

    def find_scene(self, scene_id):
        data = self.gql("query($id: ID!){ findScene(id:$id){ %s } }" % SCENE_FIELDS, {"id": scene_id})
        return data["findScene"]

    def iter_scenes(self, scene_filter=None, per_page=100):
        page = 1
        q = ("query($f: FindFilterType, $sf: SceneFilterType){ findScenes(filter:$f, scene_filter:$sf)"
             "{ count scenes { %s } } }" % SCENE_FIELDS)
        while True:
            data = self.gql(q, {"f": {"page": page, "per_page": per_page, "sort": "id", "direction": "ASC"},
                                "sf": scene_filter})["findScenes"]
            yield from data["scenes"]
            if page * per_page >= data["count"]:
                return
            page += 1

    def scene_ids_for(self, scene_filter):
        q = "query($f: FindFilterType, $sf: SceneFilterType){ findScenes(filter:$f, scene_filter:$sf){ scenes { id } } }"
        data = self.gql(q, {"f": {"per_page": -1, "sort": "id"}, "sf": scene_filter})
        return [s["id"] for s in data["findScenes"]["scenes"]]

    def move_file(self, file_id, folder, basename):
        self.gql("mutation($i: MoveFilesInput!){ moveFiles(input:$i) }",
                 {"i": {"ids": [file_id], "destination_folder": folder, "destination_basename": basename}})

    def fetch_image(self, url):
        """Fetch a Stash-served image; the URL's own host is replaced with our connection's."""
        parts = urlsplit(url)
        local = self.base_url + parts.path + (f"?{parts.query}" if parts.query else "")
        r = self.session.get(local, timeout=60)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if not ctype.startswith("image/") or len(r.content) < 1024:
            raise StashError(f"no usable image at {parts.path} ({ctype}, {len(r.content)} bytes)")
        return r.content, ctype


_STUDIO_FIELDS = "id name parent_studio { id name parent_studio { id name parent_studio { id name } } }"


class StudioIndex:
    """Cached studio lookups/creation for forced-studio libraries."""

    def __init__(self, stash):
        self.stash = stash
        self.cache = {}
        self.pending = {}  # studios planned for creation (dry run reports each once)

    def find(self, name):
        key = name.casefold()
        if key not in self.cache:
            data = self.stash.gql(
                "query($n: String!){ findStudios(studio_filter:{name:{value:$n, modifier:EQUALS}}){ studios { %s } } }" % _STUDIO_FIELDS,
                {"n": name})
            studios = data["findStudios"]["studios"]
            self.cache[key] = studios[0] if studios else None
        return self.cache[key]

    def create(self, name):
        data = self.stash.gql("mutation($i: StudioCreateInput!){ studioCreate(input:$i){ %s } }" % _STUDIO_FIELDS, {"i": {"name": name}})
        self.cache[name.casefold()] = data["studioCreate"]
        return data["studioCreate"]

    def set_scene_studio(self, scene_id, studio_id):
        self.stash.gql("mutation($i: SceneUpdateInput!){ sceneUpdate(input:$i){ id } }", {"i": {"id": scene_id, "studio_id": studio_id}})

    def is_unused(self, studio_id):
        s = self.stash.gql("query($id: ID!){ findStudio(id:$id){ scene_count image_count gallery_count group_count child_studios { id } } }",
                           {"id": studio_id})["findStudio"]
        return bool(s) and not (s["scene_count"] or s["image_count"] or s["gallery_count"] or s["group_count"] or s["child_studios"])

    def destroy(self, studio_id):
        self.stash.gql("mutation($i: StudioDestroyInput!){ studioDestroy(input:$i) }", {"i": {"id": studio_id}})
