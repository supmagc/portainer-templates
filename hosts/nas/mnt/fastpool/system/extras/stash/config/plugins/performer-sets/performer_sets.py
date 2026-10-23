#!/usr/bin/env python3
"""Stash raw plugin: photosets and promo images for selected performers, one gallery folder per set.

See docs/stash-performer-sets.md. Run by Stash as a hook/task; `--standalone` is for manual testing.
"""
import argparse
import json
import os
import sys
import traceback

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PLUGIN_DIR)

from ps import apply, download  # noqa: E402
from ps.config import Config  # noqa: E402
from ps.fetch import Fetcher  # noqa: E402
from ps.index import Index  # noqa: E402
from ps.report import Report, log  # noqa: E402
from ps.stash import Stash  # noqa: E402

PLUGIN_ID = "performer-sets"
CONFIG_DIR = os.path.dirname(os.path.dirname(PLUGIN_DIR))  # /config; the plugin dir is root-owned after deploy
INDEX_PATH = os.path.join(CONFIG_DIR, "performer-sets.db")
DEFAULT_REPORT = os.path.join(CONFIG_DIR, "performer-sets-report.txt")


def selected(stash, cfg, performer_id=None):
    if performer_id:
        p = stash.find_performer(performer_id)
        return [p] if p else []
    tag_ids = []
    for name in cfg.tags:
        tid = stash.lookup("tag", name)
        if tid:
            tag_ids.append(tid)
        else:
            log("warning", f"tag '{name}' not found (or ambiguous), ignored")
    return stash.select_performers(cfg, tag_ids)


def run(stash, settings, task_args, performer_id=None):
    cfg = Config.from_settings(settings)
    hook = task_args.get("hookContext")
    mode = "hook" if hook else task_args.get("mode")
    if hook:
        if not cfg.enable_hook:
            return "hook disabled"
        gallery = stash.find_gallery(hook["id"])
        folder = ((gallery or {}).get("folder") or {}).get("path") or ""
        if folder.startswith(cfg.root + "/"):
            apply.apply_gallery(stash, cfg, gallery, Report(dry=False), dry=False)
        return "ok"

    dry = True if mode == "dry_run" else cfg.dry_run
    report = Report(DEFAULT_REPORT, truncate=True, dry=dry)
    if (mode == "reindex" or not dry) and not os.path.isdir(cfg.root):
        raise SystemExit(f"download folder {cfg.root} does not exist (create it and add it as a Stash library)")

    if mode in ("dry_run", "download"):
        performers = selected(stash, cfg, performer_id)
        crit = [c for c, on in (("favorites", cfg.include_favorites), (f"rating ≥ {cfg.min_rating100 / 20:g}★", cfg.min_rating100),
                                (f"tags {', '.join(cfg.tags)}", cfg.tags)) if on]
        log("info", f"{'dry run' if dry else 'download'}: {len(performers)} performers ({' or '.join(crit) or 'no selection set'}), "
                    f"sources: {', '.join(cfg.sources)}{', promo' if cfg.promo_images else ''}")
        for p in performers:
            report.emit("PERFORMER", f"{p['name']} [{p['id']}]", "selected", "debug")
        idx = Index(INDEX_PATH)
        try:
            download.Downloader(stash, cfg, Fetcher(cfg.delay), idx, report, dry).run(performers)
        finally:
            idx.close()
    elif mode == "apply":
        galleries = stash.galleries_under(cfg.root)
        log("info", f"apply: {len(galleries)} galleries under {cfg.root}")
        for i, g in enumerate(galleries, 1):
            if i % 25 == 0:
                log("progress", i / len(galleries))
            apply.apply_gallery(stash, cfg, g, report, dry)
    elif mode == "reindex":
        idx = Index(INDEX_PATH)
        try:
            download.reindex(cfg, idx, report)
        finally:
            idx.close()
    else:
        raise SystemExit(f"unknown mode: {mode}")

    summary = report.summary()
    log("info", summary)
    with open(DEFAULT_REPORT, "a", encoding="utf-8") as fh:
        fh.write(summary + "\n")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--standalone", action="store_true", help="test run without Stash's stdin protocol")
    ap.add_argument("--url", default="http://localhost:9999")
    ap.add_argument("--mode", default="dry_run", choices=["dry_run", "download", "apply", "reindex"])
    ap.add_argument("--live", action="store_true", help="standalone: really download / update galleries")
    ap.add_argument("--performer", default=None, help="standalone: only this performer id (ignores the selection settings)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="standalone: override a plugin setting")
    ap.add_argument("--no-scan", action="store_true", help="standalone: don't queue a Stash scan afterwards")
    ns = ap.parse_args()
    try:
        if ns.standalone:
            stash = Stash(ns.url, headers={"ApiKey": os.environ["STASH_API_KEY"]})
            settings = dict(stash.plugin_settings(PLUGIN_ID), dry_run=not ns.live)
            settings.update(kv.split("=", 1) for kv in ns.set)
            if ns.no_scan:
                stash.scan = lambda paths: "skipped (--no-scan)"
            out = {"output": run(stash, settings, {"mode": ns.mode}, ns.performer)}
        else:
            payload = json.load(sys.stdin)
            stash = Stash.from_connection(payload["server_connection"])
            out = {"output": run(stash, stash.plugin_settings(PLUGIN_ID), payload.get("args") or {})}
    except Exception as exc:  # noqa: BLE001
        log("error", traceback.format_exc())
        out = {"error": str(exc)}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
