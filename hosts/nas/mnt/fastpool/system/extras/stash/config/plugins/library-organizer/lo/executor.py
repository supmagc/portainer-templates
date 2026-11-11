"""Apply a scene's actions (or only report them in dry-run)."""
import os
import posixpath
import shutil
import subprocess
import tempfile

from . import fsutil
from .planner import INFO_KINDS


FANART_MAX_HEIGHT = 1080


def _even(n):
    return max(2, int(n) // 2 * 2)


def poster_filter(w, h, crop_x):
    """2:3 crop: full height on frames wider than 2:3, else full width centred vertically."""
    if w * 3 >= h * 2:
        cw = _even(h * 2 / 3)
        return f"crop={cw}:{h}:{int((w - cw) * crop_x)}:0"
    ch = _even(w * 3 / 2)
    return f"crop={w}:{ch}:0:{(h - ch) // 2}"


def fanart_filter(w, h):
    """None for frames 4:3 or wider (used as-is); narrower ones go centred on a blurred 16:9 fill."""
    if w * 3 >= h * 4:
        return None
    oh = _even(min(h, FANART_MAX_HEIGHT))
    ow = _even(oh * 16 / 9)
    return (
        f"split=2[bg][fg];"
        f"[bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,crop={ow}:{oh},boxblur=40:2[b];"
        f"[fg]scale=-2:{oh}[f];"
        f"[b][f]overlay=(W-w)/2:(H-h)/2"
    )


def _dimensions(ffprobe, src):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", src],
        check=True, capture_output=True, text=True, errors="replace",
    ).stdout.strip()
    w, h = (int(x) for x in out.split("x")[:2])
    return w, h


def _write_art(stash, cfg, action, like):
    image, ctype = stash.fetch_image(action.src)
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg/ffprobe not found")
    with tempfile.TemporaryDirectory() as tmp:
        src = posixpath.join(tmp, "in.img")
        out = posixpath.join(tmp, "out.jpg")
        with open(src, "wb") as fh:
            fh.write(image)
        w, h = _dimensions(ffprobe, src)
        if action.data == "poster":
            vf = poster_filter(w, h, cfg.poster_crop_x)
        else:
            vf = fanart_filter(w, h)
        if vf is None and ctype == "image/jpeg":
            shutil.copyfile(src, out)
        else:
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", src] + (["-vf", vf] if vf else [])
            subprocess.run(cmd + ["-q:v", "2", out], check=True, capture_output=True)
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
