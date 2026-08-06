"""LINE Flex Message bubble builders.

Bubbles are built as plain JSON dicts (the shape LINE's API expects) and
handed to coach.line.flex_message(), which wraps them via
linebot.v3.messaging.FlexContainer.from_dict(). Reference:
https://developers.line.biz/en/docs/messaging-api/using-flex-messages/
"""

COLOR_DAILY = "#2E7D5B"
COLOR_WEEKLY = "#3D5A80"
COLOR_SYNCED = "#2E7D5B"
COLOR_NOT_SYNCED = "#C0392B"
TEXT_MUTED = "#8A8A8A"
TEXT_DARK = "#1A1A1A"


class FlexReply:
    """Wraps a Flex Message payload so a reply channel can carry either plain
    text or a Flex bubble through the same tuple/list contract."""

    __slots__ = ("alt_text", "bubble")

    def __init__(self, alt_text: str, bubble: dict):
        self.alt_text = alt_text[:400]  # LINE altText hard limit
        self.bubble = bubble


def build_report_bubble(title: str, emoji: str, color: str, body_text: str) -> dict:
    """A single-column report card: colored header + wrapped body paragraphs.

    `body_text` is the free-form Gemini-generated brief/report; paragraphs
    (split on blank lines) become separate wrapped text blocks so long
    reports don't render as one unbroken run of text.
    """
    paragraphs = [p.strip() for p in body_text.split("\n\n") if p.strip()] or [body_text.strip()]
    body_contents = [
        {
            "type": "text",
            "text": para,
            "wrap": True,
            "size": "sm",
            "color": TEXT_DARK,
            "margin": "md" if i else "none",
        }
        for i, para in enumerate(paragraphs)
    ]
    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": color,
            "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": f"{emoji} {title}", "color": "#FFFFFF",
                 "weight": "bold", "size": "lg"},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "contents": body_contents,
        },
    }


def build_log_bubble(*, name: str, emoji: str, rows: list[tuple[str, str]],
                     notes: str | None, synced: bool, sync_label: str,
                     low_conf_label: str | None = None,
                     image_url: str | None = None) -> dict:
    """A food/drink log confirmation card: optional hero photo (the analyzed
    image, when the log came from a photo) + name + macro rows + sync status
    footer (green when saved to Google Health, red when analysis succeeded
    but the write failed)."""
    body_contents = [
        {"type": "text", "text": f"{emoji} {name}", "weight": "bold", "size": "lg",
         "wrap": True, "color": TEXT_DARK},
        {"type": "separator", "margin": "md"},
        {
            "type": "box",
            "layout": "vertical",
            "margin": "md",
            "spacing": "sm",
            "contents": [
                {
                    "type": "box",
                    "layout": "baseline",
                    "contents": [
                        {"type": "text", "text": label, "size": "sm", "color": TEXT_MUTED, "flex": 3},
                        {"type": "text", "text": value, "size": "sm", "color": TEXT_DARK,
                         "align": "end", "flex": 2, "weight": "bold"},
                    ],
                }
                for label, value in rows
            ],
        },
    ]
    if notes:
        body_contents.append({"type": "separator", "margin": "md"})
        body_contents.append({
            "type": "text", "text": f"📝 {notes}", "size": "xs", "color": TEXT_MUTED,
            "wrap": True, "margin": "md",
        })

    footer_contents = [{
        "type": "text", "text": sync_label, "size": "xs", "wrap": True, "weight": "bold",
        "color": COLOR_SYNCED if synced else COLOR_NOT_SYNCED,
    }]
    if low_conf_label:
        footer_contents.append({"type": "text", "text": low_conf_label, "size": "xs",
                                "color": TEXT_MUTED, "wrap": True})

    bubble = {
        "type": "bubble",
        "size": "kilo",
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body_contents},
        "footer": {"type": "box", "layout": "vertical", "paddingAll": "12px",
                  "spacing": "xs", "contents": footer_contents},
    }
    if image_url:
        bubble["hero"] = {"type": "image", "url": image_url, "size": "full",
                          "aspectRatio": "20:13", "aspectMode": "cover"}
    return bubble
