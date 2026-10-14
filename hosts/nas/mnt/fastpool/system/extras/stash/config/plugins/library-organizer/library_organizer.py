#!/usr/bin/env python3
"""Stash raw plugin: keeps video/NFO/poster/fanart named, filed and described per library.

See docs/extras.md. Run by Stash as a hook/task; `--standalone` is for manual testing.
"""
import argparse
import json
import os
import sys
import traceback

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PLUGIN_DIR)

from lo import config, executor, orphans, planner  # noqa: E402
from lo.lock import scene_lock  # noqa: E402
from lo.report import Report, log  # noqa: E402
from lo.stash import Stash, StudioIndex  # noqa: E402
from lo.whisparr import Whisparr  # noqa: E402

PLUGIN_ID = "library-organizer"
HOOK_MODES = {
    "Scene.Update.Post": "scene",
    "Scene.Create.Post": "scene",
    "Studio.Update.Post": "studio",
    "Performer.Update.Post": "performer",
}
DEFAULT_REPORT = os.path.join(os.path.dirname(os.path.dirname(PLUGIN_DIR)), "library-organizer-dry-run-report.txt")  # /config; the plugin dir is root-owned after deploy


def process_scene(scene, cfg, stash, whisparr, report, organized_only=True):
    with scene_lock():
        # re-read inside the lock: a hook fired by our own previous edit must see the finished state
        scene = stash.find_scene(scene["id"]) or scene
        if organized_only and not scene.get("organized"):
            report.counts["not-organized"] += 1
            return
        files = scene.get("files") or []
        lib = cfg.library_for(files[0]["path"]) if files else None
        studios = getattr(stash, "studios", None)
        actions = planner.plan_scene(scene, cfg, whisparr, studios)
        if lib and not any(a.kind not in planner.INFO_KINDS for a in actions):
            report.counts["OK"] += 1
        executor.execute(actions, scene["id"], stash, cfg, lib.root if lib else "/", files[0]["path"] if files else "", report, studios)


def scenes_for_mode(stash, mode, hook_id):
    if mode == "scene":
        scene = stash.find_scene(hook_id)
        return [scene] if scene else []
    if mode == "studio":
        sf = {"studios": {"value": [hook_id], "modifier": "INCLUDES", "depth": 0}}
    else:
        sf = {"performers": {"value": [hook_id], "modifier": "INCLUDES"}}
    return stash.iter_scenes(sf)


def run_scenes(scenes, total, cfg, libs, stash, whisparr, report, organized_only=True):
    for i, scene in enumerate(scenes, 1):
        files = scene.get("files") or []
        lib = cfg.library_for(files[0]["path"]) if files else None
        if total and i % 25 == 0:
            log("progress", i / total)
        if organized_only and not scene.get("organized"):
            report.counts["not-organized"] += 1
            continue
        if files and (lib is None or lib not in libs):
            report.counts["outside-selection"] += 1
            continue
        try:
            process_scene(scene, cfg, stash, whisparr, report, organized_only)
        except Exception as exc:  # noqa: BLE001
            report.emit("ERROR", scene.get("id"), f"{exc}\n{traceback.format_exc()}", "error")


def run(stash, settings, task_args):
    cfg = config.load(os.path.join(PLUGIN_DIR, "libraries.yaml"))
    stash.studios = StudioIndex(stash)
    hook = task_args.get("hookContext")
    mode = HOOK_MODES.get(hook["type"]) if hook else task_args.get("mode")
    if hook and settings.get("enable_hook") is False:
        return "hook disabled"

    force_dry = mode == "dry_run_all"
    dry = True if force_dry else settings.get("dry_run", True) is not False
    path = settings.get("dry_run_report_path") or (DEFAULT_REPORT if force_dry else None)
    report = Report(path or None, truncate=not hook, dry=dry)
    whisparr = Whisparr(cfg.whisparr)
    libs = cfg.select(settings.get("library_filter"))
    organized_only = settings.get("organized_only") is not False

    if hook:
        log("debug", f"hook {hook['type']} id={hook['id']} dry={dry}")
        scenes = scenes_for_mode(stash, mode, hook["id"])
        run_scenes(scenes, 0, cfg, libs, stash, whisparr, report, organized_only)
    elif mode in ("dry_run_all", "reconcile_all"):
        total = stash.gql("{ findScenes(filter:{per_page:1}){ count } }")["findScenes"]["count"]
        log("info", f"{'dry run' if dry else 'reconcile'}: {total} scenes, libraries: {', '.join(l.name for l in libs)}")
        run_scenes(stash.iter_scenes(), total, cfg, libs, stash, whisparr, report, organized_only)
        if mode == "dry_run_all":
            orphans.scan(cfg, libs, settings, report)
    elif mode == "clean_orphans":
        orphans.scan(cfg, libs, settings, report)
    else:
        raise SystemExit(f"unknown mode: {mode}")

    if not dry:
        whisparr.flush()
    summary = report.summary()
    log("info", summary)
    if path and not hook:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(summary + "\n")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--standalone", action="store_true", help="test run without Stash's stdin protocol")
    ap.add_argument("--url", default="http://localhost:9999")
    ap.add_argument("--mode", default="dry_run_all")
    ap.add_argument("--live", action="store_true", help="standalone: really change files")
    ap.add_argument("--report", default=None)
    ap.add_argument("--libraries", default=None)
    ap.add_argument("--scene", default=None, help="standalone: only this scene id")
    ap.add_argument("--include-unorganized", action="store_true", help="standalone: also scenes not flagged Organized")
    ns = ap.parse_args()
    try:
        if ns.standalone:
            stash = Stash(ns.url, headers={"ApiKey": os.environ["STASH_API_KEY"]})
            settings = {"dry_run": not ns.live, "dry_run_report_path": ns.report, "library_filter": ns.libraries,
                        "organized_only": not ns.include_unorganized}
            task_args = {"hookContext": {"type": "Scene.Update.Post", "id": ns.scene}} if ns.scene else {"mode": ns.mode}
        else:
            payload = json.load(sys.stdin)
            stash = Stash.from_connection(payload["server_connection"])
            task_args = payload.get("args") or {}
            settings = stash.plugin_settings(PLUGIN_ID)
        out = {"output": run(stash, settings, task_args)}
    except Exception as exc:  # noqa: BLE001
        log("error", traceback.format_exc())
        out = {"error": str(exc)}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
