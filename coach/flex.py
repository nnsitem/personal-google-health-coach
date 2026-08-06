"""LINE Flex Message bubble builders.

Bubbles are built as plain JSON dicts (the shape LINE's API expects) and
handed to coach.line.flex_message(), which wraps them via
linebot.v3.messaging.FlexContainer.from_dict(). Reference:
https://developers.line.biz/en/docs/messaging-api/using-flex-messages/

Visual language borrows three familiar LINE card patterns: a receipt's small
uppercase kicker + itemized rows + muted footer key/value line, a menu
card's hero photo + bold title + colored highlight badge, and a news card's
colored header bar with a kicker above a bold headline.
"""

COLOR_DAILY = "#2E7D5B"
COLOR_WEEKLY = "#3D5A80"
COLOR_FOOD = "#B45309"
COLOR_DRINK = "#1D4ED8"
COLOR_SYNCED = "#2E7D5B"
COLOR_NOT_SYNCED = "#C0392B"
TEXT_MUTED = "#8A8A8A"
TEXT_DARK = "#1A1A1A"

# A header-derived headline longer than this reads as cramped/overwhelming
# painted across a colored bar, so it's left in the body as regular text instead.
_MAX_HEADLINE_CHARS = 160


class FlexReply:
    """Wraps a Flex Message payload so a reply channel can carry either plain
    text or a Flex bubble through the same tuple/list contract."""

    __slots__ = ("alt_text", "bubble")

    def __init__(self, alt_text: str, bubble: dict):
        self.alt_text = alt_text[:400]  # LINE altText hard limit
        self.bubble = bubble


def _kicker(text: str, color: str) -> dict:
    return {"type": "text", "text": text.upper(), "size": "xs", "weight": "bold",
            "color": color, "wrap": True}


def build_report_bubble(title: str, emoji: str, color: str, body_text: str) -> dict:
    """A report card: colored header (kicker + bold headline) + body paragraphs.

    `body_text` is the free-form Gemini-generated brief/report; paragraphs
    (split on blank lines) become separate wrapped text blocks. When the
    first paragraph is short enough to read as a headline, it moves into the
    header (news-card style) instead of starting the body.
    """
    paragraphs = [p.strip() for p in body_text.split("\n\n") if p.strip()] or [body_text.strip()]

    headline = None
    if paragraphs and len(paragraphs[0]) <= _MAX_HEADLINE_CHARS:
        headline = paragraphs.pop(0)

    header_contents = [_kicker(f"{emoji} {title}", "#FFFFFF")]
    if headline:
        header_contents.append({"type": "text", "text": headline, "wrap": True,
                                "size": "md", "weight": "bold", "color": "#FFFFFF",
                                "margin": "sm"})

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
    ] or [{"type": "text", "text": "—", "size": "sm", "color": TEXT_MUTED}]

    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": color,
            "paddingAll": "16px",
            "spacing": "xs",
            "contents": header_contents,
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "contents": body_contents,
        },
    }


def build_log_bubble(*, name: str, kicker: str, accent_color: str,
                     highlight: tuple[str, str], rows: list[tuple[str, str]],
                     notes: str | None, synced: bool, sync_label: str,
                     low_conf_label: str | None = None,
                     image_url: str | None = None) -> dict:
    """A food/drink log confirmation card.

    Layout (top to bottom): optional hero photo, a small uppercase kicker
    ("NUTRITION LOG" / "HYDRATION LOG") in the type's accent color, the
    logged item's name as a bold title, a colored highlight badge for the
    single most important stat (kcal for food, volume for drinks), the rest
    of the macros as receipt-style itemized rows, an optional muted notes
    line, and a footer STATUS row (green when saved to Google Health, red
    when analysis succeeded but the write failed).
    """
    highlight_icon, highlight_value = highlight

    body_contents = [
        _kicker(kicker, accent_color),
        {"type": "text", "text": name, "weight": "bold", "size": "xl",
         "wrap": True, "color": TEXT_DARK, "margin": "xs"},
        {
            "type": "box",
            "layout": "baseline",
            "margin": "md",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": accent_color,
                    "cornerRadius": "8px",
                    "paddingAll": "6px",
                    "paddingStart": "10px",
                    "paddingEnd": "10px",
                    "contents": [
                        {"type": "text", "text": f"{highlight_icon} {highlight_value}",
                         "size": "sm", "weight": "bold", "color": "#FFFFFF", "align": "center"},
                    ],
                },
            ],
        },
    ]

    if rows:
        body_contents.append({"type": "separator", "margin": "lg"})
        body_contents.append({
            "type": "box",
            "layout": "vertical",
            "margin": "lg",
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
        })

    if notes:
        body_contents.append({"type": "separator", "margin": "lg"})
        body_contents.append({
            "type": "text", "text": notes, "size": "xs", "color": TEXT_MUTED,
            "wrap": True, "margin": "lg",
        })

    footer_contents = [{
        "type": "box",
        "layout": "baseline",
        "contents": [
            {"type": "text", "text": "STATUS", "size": "xs", "color": TEXT_MUTED, "flex": 1},
            {"type": "text", "text": sync_label, "size": "xs", "wrap": True, "weight": "bold",
             "align": "end", "flex": 2,
             "color": COLOR_SYNCED if synced else COLOR_NOT_SYNCED},
        ],
    }]
    if low_conf_label:
        footer_contents.append({"type": "text", "text": low_conf_label, "size": "xs",
                                "color": TEXT_MUTED, "wrap": True, "margin": "xs"})

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
