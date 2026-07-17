"""LINE Messaging API sender.

Push message test:  python -m coach.line "Hello from your health coach"

LINE text messages support:
- Emoji (Unicode)
- Line breaks (\n)
- No bold/italic (unlike WhatsApp) — use emoji and spacing for emphasis
"""

import sys
import logging

from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)

from coach.config import LINE_CHANNEL_ACCESS_TOKEN

log = logging.getLogger(__name__)


class LineError(RuntimeError):
    pass


def _get_api() -> MessagingApi:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise LineError("LINE_CHANNEL_ACCESS_TOKEN not set in .env")
    configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    api_client = ApiClient(configuration)
    return MessagingApi(api_client)


def send_text(text: str, to: str | None = None) -> dict:
    """Send a push message to the user. `to` (LINE userId) is required in v2."""
    if not to:
        raise LineError("send_text requires a 'to' user ID")

    api = _get_api()
    messages = []
    while text:
        chunk = text[:5000]
        messages.append(TextMessage(text=chunk))
        text = text[5000:]

    try:
        resp = api.push_message(PushMessageRequest(to=to, messages=messages))
        log.info("LINE push message sent to %s", to)
        return {"ok": True, "message_ids": _sent_ids(resp)}
    except Exception as e:
        raise LineError(f"LINE push failed: {e}")


def _sent_ids(resp) -> list[str]:
    """LINE message ids of the messages just sent (for quote-reply tracking)."""
    sent = getattr(resp, "sent_messages", None) or []
    return [m.id for m in sent if getattr(m, "id", None)]


def get_image_content(message_id: str) -> bytes:
    """Download the binary content of an image message from LINE."""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise LineError("LINE_CHANNEL_ACCESS_TOKEN not set in .env")
    configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    api_client = ApiClient(configuration)
    blob_api = MessagingApiBlob(api_client)
    try:
        return blob_api.get_message_content(message_id)
    except Exception as e:
        raise LineError(f"LINE content download failed: {e}")


def reply_text(reply_token: str, text: str) -> dict:
    """Reply to a webhook event (free, no quota cost)."""
    api = _get_api()
    messages = []
    while text:
        chunk = text[:5000]
        messages.append(TextMessage(text=chunk))
        text = text[5000:]

    try:
        resp = api.reply_message(ReplyMessageRequest(reply_token=reply_token, messages=messages))
        return {"ok": True, "message_ids": _sent_ids(resp)}
    except Exception as e:
        raise LineError(f"LINE reply failed: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"
    message = sys.argv[1] if len(sys.argv) > 1 else "Hello from your health coach 🏃"
    send_text(message, to=DEFAULT_USER_ID)
    print(f"Sent: {message}")
