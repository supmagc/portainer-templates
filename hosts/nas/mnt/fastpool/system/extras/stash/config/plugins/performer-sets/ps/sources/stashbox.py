"""Promo images: the performer's images on every configured stash-box it is linked to (stash_ids)."""
import re

from . import ImageRef

QUERY = "query($id: ID!){ findPerformer(id:$id){ images { id url width height } } }"


def box_key(box):
    return re.sub(r"[^a-z0-9]+", "", box["name"].casefold()) or "stashbox"


def promo_images(performer, boxes, fetcher):
    """(file stem, ImageRef) per image; stems are '<box>-<image id>' so re-runs map to the same file."""
    for sid in performer.get("stash_ids") or []:
        box = boxes.get(sid["endpoint"])
        if not box:
            continue
        r = fetcher.post(box["endpoint"], json={"query": QUERY, "variables": {"id": sid["stash_id"]}},
                         headers={"ApiKey": box["api_key"]})
        found = ((r.json().get("data") or {}).get("findPerformer") or {}).get("images") or []
        for img in found:
            yield f"{box_key(box)}-{img['id']}", ImageRef(img["url"], img.get("width") or 0, img.get("height") or 0)
