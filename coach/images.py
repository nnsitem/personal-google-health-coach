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


def save_temp_image(image_bytes: bytes, mime_type: str) -> str:
    """Persist image bytes under a random token. Returns the token."""
    token = secrets.token_urlsafe(16)
    ext = _EXT_BY_MIME.get(mime_type, "jpg")
    (IMAGE_DIR / f"{token}.{ext}").write_bytes(image_bytes)
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
