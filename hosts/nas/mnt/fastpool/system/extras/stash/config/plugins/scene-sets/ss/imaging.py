"""Image check: readable, which format, how large."""
import io

from PIL import Image

EXTS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}


def inspect(data):
    """(extension, width, height) of image bytes; ValueError when it isn't a readable image."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            ext = EXTS.get(img.format)
            if not ext:
                raise ValueError(f"unsupported format {img.format}")
            return ext, img.width, img.height
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(str(exc)) from exc
