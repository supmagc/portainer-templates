"""Image fingerprints: sha256 for exact copies, 64-bit dHash + aspect ratio for re-encoded/resized copies."""
import hashlib
import io
from dataclasses import dataclass

from PIL import Image

EXTS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}
ASPECT_TOLERANCE = 0.03


@dataclass
class Meta:
    sha256: str
    dhash: int
    width: int
    height: int
    ext: str

    @property
    def long_edge(self):
        return max(self.width, self.height)


def dhash(img):
    small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = small.tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def inspect(data):
    """Meta for image bytes; ValueError when it isn't a readable image."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            ext = EXTS.get(img.format)
            if not ext:
                raise ValueError(f"unsupported format {img.format}")
            return Meta(hashlib.sha256(data).hexdigest(), dhash(img), img.width, img.height, ext)
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(str(exc)) from exc


def distance(a, b):
    return (a ^ b).bit_count()


def same_shape(w1, h1, w2, h2):
    if not (w1 and h1 and w2 and h2):
        return False
    r1, r2 = w1 / h1, w2 / h2
    return abs(r1 - r2) <= ASPECT_TOLERANCE * max(r1, r2)
