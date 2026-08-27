"""Temporary public hosting for inbound photos.

LINE Flex hero images must reference a URL LINE's own servers can fetch, but
inbound photo bytes only ever exist in-memory for the Gemini vision call.
This module persists a copy under DATA_DIR just long enough to be served back
through main.py's /images/{token} route, and prunes anything older than
IMAGE_TTL_HOURS via a scheduled job.
"""

import logging
import os
import secrets
import time
from pathlib import Path

from coach.config import DATA_DIR

log = logging.getLogger(__name__)

IMAGE_DIR = DATA_DIR / "tmp_images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_TTL_HOURS = 24
_EXT_BY_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}

# Longest edge for the copy sent to Gemini. Deliberately generous: measured
# 2026-08-23, the model still read both packaging labels exactly right
# (186 kcal / 40g protein, and 330 kcal) all the way down to 768px — but two
# photos are not enough evidence to risk a dense fine-print nutrition table for
# a saving that is meaningless at ~15-30 calls a day. This is a BOUND for
# pathological input (a full-resolution photo through the CLI path), not an
# optimisation: LINE already delivers at most ~1.64 MP, so 1108x1477 passes
# through untouched and the tallest observed photo (960x1706) is trimmed 8% to
# 882x1568.
VISION_MAX_EDGE = 1568

# The hero copy is displayed small inside a LINE Flex card and fetched by LINE's
# servers over the public tunnel, so there is nothing to lose by shrinking it.
HERO_MAX_EDGE = 1024
JPEG_QUALITY = 88


def downscale(image_bytes: bytes, max_edge: int, mime_type: str = "image/jpeg") -> bytes:
    """Shrink so the longest edge is at most `max_edge`. Never upscales.

    Returns the ORIGINAL bytes when the image already fits, when the format
    isn't raster-resizable, or when anything at all goes wrong — a photo that
    can't be resized must still be logged.
    """
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow unavailable — sending image at original size")
        return image_bytes

    try:
        import io
        with Image.open(io.BytesIO(image_bytes)) as im:
            if max(im.size) <= max_edge:
                return image_bytes
            before = im.size
            im = im.convert("RGB")
            im.thumbnail((max_edge, max_edge), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
        out = buf.getvalue()
        if len(out) >= len(image_bytes):
            return image_bytes   # re-encoding made it bigger; keep the original
        log.info("downscaled image %dx%d -> %dx%d (%d KB -> %d KB)",
                 before[0], before[1], im.size[0], im.size[1],
                 len(image_bytes) // 1024, len(out) // 1024)
        return out
    except Exception:
        log.warning("could not downscale image — sending original", exc_info=True)
        return image_bytes


def save_temp_image(image_bytes: bytes, mime_type: str) -> str:
    """Persist image bytes under a random token. Returns the token.

    Stored at HERO_MAX_EDGE: the card shows it small, LINE fetches it over the
    tunnel, and the file sits on disk until the hourly prune.
    """
    token = secrets.token_urlsafe(16)
    hero = downscale(image_bytes, HERO_MAX_EDGE, mime_type)
    ext = "jpg" if hero is not image_bytes else _EXT_BY_MIME.get(mime_type, "jpg")
    (IMAGE_DIR / f"{token}.{ext}").write_bytes(hero)
    return token


def resolve_temp_image(token: str) -> Path | None:
    """Map a token from the URL back to its file.

    Rejects anything that isn't exactly the charset secrets.token_urlsafe()
    produces — the serving endpoint passes `token` straight from the request
    path, so this is what stops it being used to read arbitrary files.
    """
    if not token or not all(c.isalnum() or c in "-_" for c in token):
        return None
    for ext in _EXT_BY_MIME.values():
        path = IMAGE_DIR / f"{token}.{ext}"
        if path.is_file():
            return path
    return None


def temp_image_url(token: str) -> str:
    host = os.environ.get("PUBLIC_HOST", "coach.signagegold.co")
    return f"https://{host}/images/{token}"


def cleanup_old_images(max_age_hours: int = IMAGE_TTL_HOURS) -> None:
    """Delete temp images older than max_age_hours. Scheduled hourly."""
    cutoff = time.time() - max_age_hours * 3600
    for f in IMAGE_DIR.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            log.exception("failed to prune temp image %s", f)
