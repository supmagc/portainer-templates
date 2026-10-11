"""Apply a scene's actions (or only report them in dry-run)."""
import os
import posixpath
import shutil
import subprocess
import tempfile

from . import fsutil
from .planner import INFO_KINDS


def _write_art(stash, cfg, action, like):
    image, ctype = stash.fetch_image(action.src)
    ffmpeg = shutil.which("ffmpeg")
    with tempfile.TemporaryDirectory() as tmp:
        src = posixpath.join(tmp, "in.img")
        out = posixpath.join(tmp, "out.jpg")
        with open(src, "wb") as fh:
            fh.write(image)
        if action.data == "poster":
            vf = f"crop=ih*2/3:ih:(iw-ow)*{cfg.poster_crop_x}:0"
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", src, "-vf", vf, "-q:v", "2", out]
        elif ctype == "image/jpeg":
            shutil.copyfile(src, out)
            cmd = None
        else:
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", src, "-q:v", "2", out]
        if cmd:
            if not ffmpeg:
                raise RuntimeError("ffmpeg not found")
            subprocess.run(cmd, check=True, capture_output=True)
        with open(out, "rb") as fh:
            fsutil.atomic_write(action.dst, fh.read(), like)


def execute(actions, scene_id, stash, cfg, lib_root, video_path, report, studios=None):
    """Run actions in order; a failed MOVE aborts the rest of this scene."""
    like = video_path
    created = {}
    for a in actions:
        if a.kind in INFO_KINDS or report.dry:
            report.emit(a.kind, scene_id, a.text, a.level)
            continue
        try:
            if a.kind == "CREATE-STUDIO":
                created[a.data.casefold()] = studios.create(a.data)
            elif a.kind == "SET-STUDIO":
                target = a.data["studio"]
                sid = target["id"] or created[target["name"].casefold()]["id"]
                studios.set_scene_studio(a.data["scene_id"], sid)
            elif a.kind == "DELETE-STUDIO":
                if not studios.is_unused(a.data):
                    report.emit("SKIP", scene_id, f"studio {a.text.split(' (')[0]} still in use; kept")
                    continue
                studios.destroy(a.data)
            elif a.kind == "MKDIR":
                fsutil.ensure_dir(a.dst, lib_root)
            elif a.kind == "MOVE":
                stash.move_file(a.data["file_id"], a.data["folder"], a.data["basename"])
                if not os.path.exists(a.dst):
                    raise RuntimeError("moveFiles reported success but the target is missing")
                like = a.dst
            elif a.kind in ("RENAME", "RENAME-BG"):
                fsutil.rename_nooverwrite(a.src, a.dst)
            elif a.kind == "WRITE-NFO":
                fsutil.atomic_write(a.dst, a.data, like)
            elif a.kind.startswith("CREATE-"):
                _write_art(stash, cfg, a, like)
            elif a.kind == "RMDIR":
                if not os.listdir(a.dst):
                    os.rmdir(a.dst)
                continue
            report.emit(a.kind, scene_id, a.text, a.level)
        except Exception as exc:  # noqa: BLE001
            report.emit("ERROR", scene_id, f"{a.kind} {a.text}: {exc}", "error")
            if a.kind in ("MOVE", "CREATE-STUDIO", "SET-STUDIO"):
                return False
    return True
