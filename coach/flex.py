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

# Trend-delta chip tones (used by the data-driven report cards).
TREND_GOOD = "#2E7D5B"   # a move in the healthy direction
TREND_BAD = "#C0392B"    # a move in the unhealthy direction
TREND_FLAT = "#8A8A8A"   # within noise / no baseline

# Sleep-stage bar segment colors (deep → awake).
STAGE_COLORS = {
    "DEEP": "#1D4ED8",
    "LIGHT": "#60A5FA",
    "REM": "#A78BFA",
    "AWAKE": "#D1D5DB",
}
# Faded fill for the "rest of the week" bars in the weekly steps chart.
BAR_STRONG = "#3D5A80"
BAR_FADED = "#9DB2CE"

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


# ---------------------------------------------------------------------------
# Data-driven report cards (daily brief / weekly report)
#
# Unlike build_report_bubble (which just wraps Gemini's free text), these render
# the numbers themselves as visual elements — a readiness pill, itemized stat
# rows with ▲/▼ trend chips, a proportional sleep-stage bar, and a box-based
# 7-day bar chart — and demote the Gemini text to a single "narrative" section.
# Callers pass a compact view-model; this module stays purely presentational.
# ---------------------------------------------------------------------------

# A "chip" is the small colored ▲12% badge next to a stat. Callers build them
# with delta_chip()/trend_chip() below; the builders just render {text, tone}.
_CHIP_TONE_COLOR = {"good": TREND_GOOD, "bad": TREND_BAD, "flat": TREND_FLAT}


def delta_chip(current, baseline, higher_is_better: bool, *, unit: str = "vs avg",
               flat_pct: float = 3.0) -> dict | None:
    """Chip comparing `current` to a `baseline` (e.g. today vs 7-day average).

    Returns {"text": "▲ 12% vs avg", "tone": "good|bad|flat"} or None when a
    comparison can't be made. `higher_is_better` decides whether an increase is
    healthy (steps) or not (resting HR). Moves under `flat_pct` read as steady.
    """
    if current is None or not baseline:
        return None
    pct = (current - baseline) / baseline * 100
    if abs(pct) < flat_pct:
        return {"text": f"≈ {unit}", "tone": "flat"}
    up = pct > 0
    arrow = "▲" if up else "▼"
    tone = "good" if (up == higher_is_better) else "bad"
    return {"text": f"{arrow} {abs(round(pct))}% {unit}", "tone": tone}


def trend_chip(trend_str: str | None, higher_is_better: bool) -> dict | None:
    """Chip from a build_trends() week-over-week string like 'up 12% vs last
    week' / 'down 5% vs last week' / 'steady'."""
    if not trend_str:
        return None
    if trend_str == "steady":
        return {"text": "steady", "tone": "flat"}
    up = trend_str.startswith("up")
    import re
    m = re.search(r"(\d+)%", trend_str)
    pct = m.group(1) if m else ""
    arrow = "▲" if up else "▼"
    tone = "good" if (up == higher_is_better) else "bad"
    return {"text": f"{arrow} {pct}% vs last wk".replace("  ", " "), "tone": tone}


def _section_label(text: str, color: str) -> dict:
    return {"type": "text", "text": text, "size": "xs", "weight": "bold",
            "color": color, "wrap": True}


def _stat_row(label: str, value: str, chip: dict | None) -> dict:
    """A receipt-style row: muted label on the left, bold value (+ optional
    trend chip) right-aligned."""
    contents = [
        {"type": "text", "text": label, "size": "sm", "color": TEXT_MUTED,
         "flex": 5, "gravity": "center"},
        {"type": "text", "text": value, "size": "sm", "weight": "bold",
         "color": TEXT_DARK, "align": "end", "gravity": "center",
         "flex": 3 if chip else 6},
    ]
    if chip:
        contents.append({
            "type": "text", "text": chip["text"], "size": "xs", "weight": "bold",
            "color": _CHIP_TONE_COLOR.get(chip["tone"], TREND_FLAT),
            "align": "end", "gravity": "center", "flex": 4,
        })
    return {"type": "box", "layout": "horizontal", "contents": contents}


def _sleep_stage_bar(stage_min: dict) -> list[dict]:
    """A proportional deep/light/rem/awake bar + a small legend. Returns the
    body components to append (empty when there's no stage data)."""
    segments = []
    for stage in ("DEEP", "LIGHT", "REM", "AWAKE"):
        mins = round(stage_min.get(stage, 0) or 0)
        if mins > 0:
            segments.append({
                "type": "box", "layout": "vertical", "flex": mins,
                "backgroundColor": STAGE_COLORS[stage],
                "contents": [{"type": "filler"}],
            })
    if not segments:
        return []
    legend = []
    for stage, glyph in (("DEEP", "🟦 Deep"), ("REM", "🟪 REM"), ("AWAKE", "Awake")):
        mins = round(stage_min.get(stage, 0) or 0)
        if mins > 0:
            legend.append({"type": "text", "text": f"{glyph} {mins}m", "size": "xxs",
                           "color": TEXT_MUTED, "flex": 0})
    return [
        {"type": "box", "layout": "horizontal", "height": "12px",
         "cornerRadius": "6px", "margin": "md", "contents": segments},
        {"type": "box", "layout": "horizontal", "margin": "sm",
         "spacing": "md", "contents": legend or [{"type": "filler"}]},
    ]


def _mini_bar_chart(series: list[tuple[str, float]], strong_last: int = 5) -> list[dict]:
    """A box-based bar chart (LINE Flex has no chart primitive). `series` is a
    list of (label, value); the last `len-strong_last` bars are faded to hint
    'earlier in the week'. Returns body components (empty when all-zero)."""
    values = [max(0.0, v or 0) for _, v in series]
    top = max(values) if values else 0
    if top <= 0:
        return []
    scale = 1000  # integer flex resolution
    columns = []
    for i, (_, v) in enumerate(series):
        v = max(0.0, v or 0)
        bar_flex = max(1, round(v / top * scale)) if v > 0 else 0
        gap_flex = scale - bar_flex
        color = BAR_STRONG if i < strong_last else BAR_FADED
        col_children = []
        if gap_flex > 0:
            col_children.append({"type": "box", "layout": "vertical", "flex": gap_flex,
                                 "contents": [{"type": "filler"}]})
        if bar_flex > 0:
            col_children.append({"type": "box", "layout": "vertical", "flex": bar_flex,
                                 "backgroundColor": color, "cornerRadius": "2px",
                                 "contents": [{"type": "filler"}]})
        columns.append({"type": "box", "layout": "vertical", "flex": 1,
                        "contents": col_children or [{"type": "filler"}]})
    labels = [{"type": "text", "text": lbl, "size": "xxs", "color": TEXT_MUTED,
               "align": "center", "flex": 1} for lbl, _ in series]
    return [
        {"type": "box", "layout": "horizontal", "height": "62px", "spacing": "sm",
         "margin": "md", "contents": columns},
        {"type": "box", "layout": "horizontal", "margin": "sm", "contents": labels},
    ]


def _report_header(title: str, emoji: str, color: str, subtitle: str,
                   pill_text: str | None) -> dict:
    contents = [_kicker(f"{emoji} {title}", "#FFFFFF")]
    if subtitle:
        contents.append({"type": "text", "text": subtitle, "size": "sm",
                         "weight": "bold", "color": "#FFFFFF", "margin": "xs"})
    if pill_text:
        contents.append({
            "type": "box", "layout": "vertical", "flex": 0, "margin": "md",
            "backgroundColor": "#FFFFFF", "cornerRadius": "8px",
            "paddingAll": "6px", "paddingStart": "10px", "paddingEnd": "10px",
            "contents": [{"type": "text", "text": pill_text, "size": "xs",
                          "weight": "bold", "color": color, "wrap": True}],
        })
    return {"type": "box", "layout": "vertical", "backgroundColor": color,
            "paddingAll": "16px", "spacing": "xs", "contents": contents}


def _narrative_block(label: str, color: str, text: str) -> list[dict]:
    text = (text or "").strip()
    if not text:
        return []
    return [
        {"type": "separator", "margin": "lg"},
        {"type": "box", "layout": "vertical", "margin": "lg", "contents": [
            _section_label(label, color),
            {"type": "text", "text": text, "size": "sm", "color": TEXT_DARK,
             "wrap": True, "margin": "sm"},
        ]},
    ]


def build_daily_report_bubble(*, date_label: str, color: str,
                              readiness_pill: str | None,
                              recovery_rows: list[tuple[str, str, dict | None]],
                              sleep_label: str | None, sleep_stage_min: dict | None,
                              activity_rows: list[tuple[str, str, dict | None]],
                              narrative: str) -> dict:
    """Daily brief card: readiness pill in the header, then Recovery / Sleep /
    Activity stat sections, then a Gemini 'Today's Focus' narrative."""
    body: list[dict] = []

    if recovery_rows:
        body.append(_section_label("❤️ RECOVERY", color))
        body.append({"type": "box", "layout": "vertical", "margin": "sm", "spacing": "sm",
                     "contents": [_stat_row(*r) for r in recovery_rows]})

    if sleep_stage_min and any(sleep_stage_min.values()):
        if body:
            body.append({"type": "separator", "margin": "lg"})
        body.append({"type": "box", "layout": "vertical", "margin": "lg" if len(body) > 1 else "none",
                     "contents": [_section_label(sleep_label or "🛌 SLEEP", color),
                                  *_sleep_stage_bar(sleep_stage_min)]})

    if activity_rows:
        if body:
            body.append({"type": "separator", "margin": "lg"})
        body.append({"type": "box", "layout": "vertical", "margin": "lg" if body else "none",
                     "contents": [_section_label("🚶 ACTIVITY", color),
                                  {"type": "box", "layout": "vertical", "margin": "sm",
                                   "spacing": "sm", "contents": [_stat_row(*r) for r in activity_rows]}]})

    body += _narrative_block("🎯 TODAY'S FOCUS", color, narrative)
    if not body:
        body = [{"type": "text", "text": narrative or "—", "size": "sm",
                 "color": TEXT_DARK, "wrap": True}]

    return {
        "type": "bubble", "size": "mega",
        "header": _report_header("Daily Brief", "🌅", color, date_label, readiness_pill),
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body},
    }


def build_weekly_report_bubble(*, range_label: str, color: str,
                               steps_series: list[tuple[str, float]],
                               average_rows: list[tuple[str, str, dict | None]],
                               narrative: str) -> dict:
    """Weekly report card: a 7-day steps bar chart, weekly averages with
    week-over-week trend chips, and a Gemini 'Key Insight' narrative."""
    body: list[dict] = []

    chart = _mini_bar_chart(steps_series) if steps_series else []
    if chart:
        body.append(_section_label("🚶 STEPS · 7-DAY", color))
        body += chart

    if average_rows:
        if body:
            body.append({"type": "separator", "margin": "lg"})
        body.append({"type": "box", "layout": "vertical", "margin": "lg" if body else "none",
                     "contents": [_section_label("📈 WEEKLY AVERAGES", color),
                                  {"type": "box", "layout": "vertical", "margin": "sm",
                                   "spacing": "sm", "contents": [_stat_row(*r) for r in average_rows]}]})

    body += _narrative_block("💡 KEY INSIGHT", color, narrative)
    if not body:
        body = [{"type": "text", "text": narrative or "—", "size": "sm",
                 "color": TEXT_DARK, "wrap": True}]

    return {
        "type": "bubble", "size": "mega",
        "header": _report_header("Weekly Report", "📊", color, range_label, None),
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body},
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
            "layout": "horizontal",
            "margin": "md",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "flex": 0,
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
