"""Create and set the LINE Rich Menu for the health coach bot.

Run once:  python -m coach.setup_rich_menu

Creates a 2x2 grid Rich Menu:
┌───────────────────┬───────────────────┐
│  🔗 Login         │  🔑 Set Key       │
│  Google Health    │  Gemini AI        │
├───────────────────┼───────────────────┤
│  💬 Chat          │  📊 My Summary    │
│                   │                   │
└───────────────────┴───────────────────┘
"""

import json
import logging
import requests

from coach.config import LINE_CHANNEL_ACCESS_TOKEN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

API_BASE = "https://api.line.me/v2/bot"

HEADERS = {
    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


def create_rich_menu() -> str | None:
    """Create the rich menu and return its ID."""
    menu = {
        "size": {"width": 2500, "height": 1686},
        "selected": True,
        "name": "Health Coach Menu",
        "chatBarText": "Menu",
        "areas": [
            {
                "bounds": {"x": 0, "y": 0, "width": 1250, "height": 843},
                "action": {"type": "message", "text": "login"},
            },
            {
                "bounds": {"x": 1250, "y": 0, "width": 1250, "height": 843},
                "action": {"type": "message", "text": "set key"},
            },
            {
                "bounds": {"x": 0, "y": 843, "width": 1250, "height": 843},
                "action": {"type": "message", "text": "สวัสดี"},
            },
            {
                "bounds": {"x": 1250, "y": 843, "width": 1250, "height": 843},
                "action": {"type": "message", "text": "ขอข้อมูลสุขภาพล่าสุด"},
            },
        ],
    }

    resp = requests.post(
        f"{API_BASE}/richmenu",
        headers=HEADERS,
        json=menu,
        timeout=30,
    )
    if resp.status_code != 200:
        log.error("failed to create rich menu: %s %s", resp.status_code, resp.text)
        return None

    rich_menu_id = resp.json().get("richMenuId")
    log.info("created rich menu: %s", rich_menu_id)
    return rich_menu_id


def upload_menu_image(rich_menu_id: str) -> bool:
    """Upload a menu image. We generate a simple colored-block PNG."""
    try:
        image_bytes = _generate_menu_image()
    except Exception as e:
        log.error("failed to generate menu image: %s", e)
        return False

    resp = requests.post(
        f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "image/png",
        },
        data=image_bytes,
        timeout=30,
    )
    if resp.status_code != 200:
        log.error("failed to upload menu image: %s %s", resp.status_code, resp.text)
        return False

    log.info("uploaded menu image for %s", rich_menu_id)
    return True


def set_default_menu(rich_menu_id: str) -> bool:
    """Set the rich menu as the default for all users."""
    resp = requests.post(
        f"{API_BASE}/user/all/richmenu/{rich_menu_id}",
        headers=HEADERS,
        timeout=30,
    )
    if resp.status_code != 200:
        log.error("failed to set default menu: %s %s", resp.status_code, resp.text)
        return False

    log.info("set default rich menu: %s", rich_menu_id)
    return True


def _generate_menu_image() -> bytes:
    """Generate a simple 2500x1686 PNG menu image with colored quadrants and text.

    Uses only standard library (no PIL required) — creates a minimal valid PNG.
    Actually, let's use a simple approach: create an SVG-like colored image.
    For simplicity, we'll create it with PIL if available, otherwise use a placeholder.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log.warning("Pillow not installed — generating a solid color placeholder image")
        return _generate_solid_png()

    img = Image.new("RGB", (2500, 1686))
    draw = ImageDraw.Draw(img)

    # Colors
    top_left = "#27AE60"      # green - login
    top_right = "#F39C12"     # orange - set key
    bottom_left = "#3498DB"   # blue - chat
    bottom_right = "#9B59B6"  # purple - summary

    # Draw quadrants
    draw.rectangle([0, 0, 1250, 843], fill=top_left)
    draw.rectangle([1250, 0, 2500, 843], fill=top_right)
    draw.rectangle([0, 843, 1250, 1686], fill=bottom_left)
    draw.rectangle([1250, 843, 2500, 1686], fill=bottom_right)

    # Draw divider lines
    draw.line([(1250, 0), (1250, 1686)], fill="white", width=4)
    draw.line([(0, 843), (2500, 843)], fill="white", width=4)

    # Add text labels (use default font, scaled up)
    try:
        font_large = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 72)
        font_small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 48)
    except (OSError, IOError):
        font_large = ImageFont.load_default()
        font_small = font_large

    labels = [
        (625, 350, "🔗 Login\nGoogle Health", font_large),
        (1875, 350, "🔑 Set Key\nGemini AI", font_large),
        (625, 1200, "💬 Chat", font_large),
        (1875, 1200, "📊 My Summary", font_large),
    ]

    for x, y, text, font in labels:
        draw.text((x, y), text, fill="white", font=font, anchor="mm")

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _generate_solid_png() -> bytes:
    """Generate a minimal 2500x1686 solid-green PNG without PIL."""
    import struct
    import zlib

    width, height = 2500, 1686
    # Create raw pixel data (RGB green)
    raw_rows = []
    for y in range(height):
        row = b"\x00"  # filter byte
        if y < 843:
            color = b"\x27\xae\x60" if (y % 1250 == 0 or True) else b"\xf3\x9c\x12"
            # Top half: left green, right orange
            row += (b"\x27\xae\x60" * 1250 + b"\xf3\x9c\x12" * 1250)
        else:
            # Bottom half: left blue, right purple
            row += (b"\x34\x98\xdb" * 1250 + b"\x9b\x59\xb6" * 1250)
        raw_rows.append(row)

    raw = b"".join(raw_rows)
    compressed = zlib.compress(raw)

    def chunk(ctype, data):
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", compressed)
    png += chunk(b"IEND", b"")
    return png


def main():
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("ERROR: LINE_CHANNEL_ACCESS_TOKEN not set")
        return

    # Delete existing default menu first
    resp = requests.delete(
        f"{API_BASE}/user/all/richmenu",
        headers=HEADERS,
        timeout=30,
    )
    log.info("cleared existing default menu (status %d)", resp.status_code)

    # Create new menu
    menu_id = create_rich_menu()
    if not menu_id:
        return

    # Upload image
    if not upload_menu_image(menu_id):
        log.warning("menu created but image upload failed — menu may appear blank")

    # Set as default
    if set_default_menu(menu_id):
        print(f"\n✅ Rich Menu created and set as default: {menu_id}")
        print("All users will now see the 4-button menu at the bottom of the chat.")
    else:
        print(f"\n⚠️ Menu created ({menu_id}) but couldn't set as default.")


if __name__ == "__main__":
    main()
