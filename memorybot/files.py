"""Processing of uploaded files before they are stored or sent to Gemini."""
import io
import logging
import os

from PIL import Image, ImageOps

from .config import MAX_IMAGE_PX

log = logging.getLogger("memorybot")


def is_photo(mime: str) -> bool:
    return mime.startswith("image/") and mime != "image/gif"


def prepare_file(data: bytes, filename: str, mime: str, max_px: int = MAX_IMAGE_PX) -> tuple[bytes, str]:
    """Shrink photos to max `max_px` JPEG; keep everything else as-is. Returns (data, extension)."""
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    if is_photo(mime):
        try:
            with Image.open(io.BytesIO(data)) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail((max_px, max_px))
                out = io.BytesIO()
                img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
                return out.getvalue(), ".jpg"
        except Exception:
            log.warning("Could not re-encode %s, storing original", filename)
    return data, ext
