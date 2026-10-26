"""Image fingerprints (sha256 for exact copies, 64-bit dHash + aspect ratio for re-encoded/resized copies) and a
focus measure for soft/blurred/upscaled images."""
import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageStat

EXTS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}
FILE_EXTS = frozenset(EXTS.values()) | {".jpeg"}
ASPECT_TOLERANCE = 0.03
SHARP_EDGE = 1024
_LAPLACE = ImageFilter.Kernel((3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), scale=1, offset=128)


@dataclass
class Meta:
    sha256: str
    dhash: int
    width: int
    height: int
    ext: str
    sharpness: float = 0.0

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


def sharpness(img):
    """Laplacian variance at ≤ 1024 px, 75th percentile of a 4×4 tile grid: a sharp subject in front of a blurred
    background still counts as sharp. Sharp photos measured 110–360, blurred or 3× upscaled copies 17–82."""
    g = img.convert("L")
    g.thumbnail((SHARP_EDGE, SHARP_EDGE), Image.Resampling.LANCZOS)
    lap = g.filter(_LAPLACE)
    w, h = lap.size
    tiles = sorted(ImageStat.Stat(lap.crop((i * w // 4, j * h // 4, (i + 1) * w // 4, (j + 1) * h // 4))).var[0]
                   for i in range(4) for j in range(4))
    return round(tiles[12], 1)


def inspect(data):
    """Meta for image bytes; ValueError when it isn't a readable image."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            ext = EXTS.get(img.format)
            if not ext:
                raise ValueError(f"unsupported format {img.format}")
            return Meta(hashlib.sha256(data).hexdigest(), dhash(img), img.width, img.height, ext, sharpness(img))
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(str(exc)) from exc


def distance(a, b):
    return (a ^ b).bit_count()


def same_shape(w1, h1, w2, h2):
    if not (w1 and h1 and w2 and h2):
        return False
    r1, r2 = w1 / h1, w2 / h2
    return abs(r1 - r2) <= ASPECT_TOLERANCE * max(r1, r2)
