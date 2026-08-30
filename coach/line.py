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
    FlexContainer,
    FlexMessage,
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)

from coach.config import LINE_CHANNEL_ACCESS_TOKEN

log = logging.getLogger(__name__)

# Max messages LINE accepts per push/reply call.
MAX_MESSAGES_PER_REQUEST = 5


class LineError(RuntimeError):
    pass


def _get_api() -> MessagingApi:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise LineError("LINE_CHANNEL_ACCESS_TOKEN not set in .env")
    configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    api_client = ApiClient(configuration)
    return MessagingApi(api_client)


def flex_message(alt_text: str, bubble: dict) -> FlexMessage:
    """Build a FlexMessage from a plain bubble dict (see coach.flex)."""
    return FlexMessage(alt_text=alt_text[:400], contents=FlexContainer.from_dict(bubble))


def send_messages(messages: list, to: str) -> dict:
    """Push a list of already-built Message objects (TextMessage, FlexMessage,
    ...) to the user, chunked to LINE's per-request limit."""
    if not to:
        raise LineError("send_messages requires a 'to' user ID")
    if not messages:
        return {"ok": True, "message_ids": []}

    api = _get_api()
    all_ids: list[str] = []
    try:
        for i in range(0, len(messages), MAX_MESSAGES_PER_REQUEST):
            chunk = messages[i:i + MAX_MESSAGES_PER_REQUEST]
            resp = api.push_message(PushMessageRequest(to=to, messages=chunk))
            all_ids += _sent_ids(resp)
        log.info("LINE push sent (%d message(s)) to %s", len(messages), to)
        return {"ok": True, "message_ids": all_ids}
    except Exception as e:
        raise LineError(f"LINE push failed: {e}")


def reply_messages(reply_token: str, messages: list) -> dict:
    """Reply to a webhook event with a list of already-built Message objects.

    A reply token is single-use, so unlike send_messages this can't chunk
    across multiple calls — it silently truncates to the first
    MAX_MESSAGES_PER_REQUEST (5), which is far more than any current reply
    sends (text + a couple of Flex bubbles at most).
    """
    if not messages:
        return {"ok": True, "message_ids": []}
    if len(messages) > MAX_MESSAGES_PER_REQUEST:
        log.warning("reply_messages: %d messages exceeds LINE's per-reply limit, truncating",
                    len(messages))
        messages = messages[:MAX_MESSAGES_PER_REQUEST]

    api = _get_api()
    try:
        resp = api.reply_message(ReplyMessageRequest(reply_token=reply_token, messages=messages))
        return {"ok": True, "message_ids": _sent_ids(resp)}
    except Exception as e:
        raise LineError(f"LINE reply failed: {e}")


def send_text(text: str, to: str | None = None) -> dict:
    """Send a push message to the user. `to` (LINE userId) is required in v2."""
    if not to:
        raise LineError("send_text requires a 'to' user ID")

    messages = []
    while text:
        messages.append(TextMessage(text=text[:5000]))
        text = text[5000:]
    return send_messages(messages, to=to)


def _sent_ids(resp) -> list[str]:
    """LINE message ids of the messages just sent (for quote-reply tracking)."""
    sent = getattr(resp, "sent_messages", None) or []
    return [m.id for m in sent if getattr(m, "id", None)]


def get_message_content(message_id: str) -> bytes:
    """Download the binary content of an image, audio, video, or file message
    from LINE (whatever type — the endpoint itself is type-agnostic)."""
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
    messages = []
    while text:
        messages.append(TextMessage(text=text[:5000]))
        text = text[5000:]
    return reply_messages(reply_token, messages)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"
    message = sys.argv[1] if len(sys.argv) > 1 else "Hello from your health coach 🏃"
    send_text(message, to=DEFAULT_USER_ID)
    print(f"Sent: {message}")
