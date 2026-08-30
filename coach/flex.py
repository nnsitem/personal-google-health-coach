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
COLOR_FOOD = "#FF9100"
COLOR_DRINK = "#3A86FF"
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


# ---------------------------------------------------------------------------
# Localized static labels for the data-driven report cards.
#
# The Gemini narrative already comes back in the user's language; these are the
# card's *static* strings (section headers, sleep-stage legend, readiness pill,
# comparison captions). Keyed by 'en'/'th' to match coach.food's LABELS pattern.
# Row labels ("Resting HR", "Steps / day", …) live here too so the daily/weekly
# view-models render them localized. Emoji glyphs are language-agnostic and kept.
# ---------------------------------------------------------------------------
REPORT_LABELS = {
    "en": {
        "daily_brief": "Daily Brief",
        "weekly_report": "Weekly Report",
        "recovery": "❤️ RECOVERY",
        "sleep": "🛌 SLEEP",
        "activity": "🚶 ACTIVITY",
        "todays_focus": "🎯 TODAY'S FOCUS",
        "steps_7day": "🚶 STEPS · 7-DAY",
        "sleep_7day": "🛌 SLEEP · 7-DAY",
        "weekly_averages": "📈 WEEKLY AVERAGES",
        "key_insight": "💡 KEY INSIGHT",
        "asleep": "asleep",
        "leg_deep": "🟦 Deep", "leg_rem": "🟪 REM", "leg_awake": "Awake",
        "resting_hr": "Resting HR", "hrv": "HRV", "spo2": "SpO₂",
        "steps": "Steps", "azm": "Active-zone min", "calories": "Calories",
        "steps_per_day": "Steps / day", "sleep_per_night": "Sleep / night",
        "vs_avg": "vs avg", "vs_last_wk": "vs last wk",
        "rd_well": "✅ Well recovered", "rd_under": "⚠️ Under-recovered",
        "rd_fatigue": "🩺 Possible fatigue signal", "rd_normal": "• Normal recovery",
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "months": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    },
    "th": {
        "daily_brief": "สรุปสุขภาพวันนี้",
        "weekly_report": "รายงานประจำสัปดาห์",
        "recovery": "❤️ การฟื้นตัว",
        "sleep": "🛌 การนอน",
        "activity": "🚶 กิจกรรม",
        "todays_focus": "🎯 โฟกัสวันนี้",
        "steps_7day": "🚶 ก้าวเดิน · 7 วัน",
        "sleep_7day": "🛌 การนอน · 7 วัน",
        "weekly_averages": "📈 ค่าเฉลี่ยรายสัปดาห์",
        "key_insight": "💡 ข้อสังเกตสำคัญ",
        "asleep": "หลับ",
        "leg_deep": "🟦 หลับลึก", "leg_rem": "🟪 REM", "leg_awake": "ตื่น",
        "resting_hr": "หัวใจขณะพัก", "hrv": "HRV", "spo2": "SpO₂",
        "steps": "ก้าวเดิน", "azm": "แอคทีฟโซน (นาที)", "calories": "แคลอรี่",
        "steps_per_day": "ก้าว/วัน", "sleep_per_night": "นอน/คืน",
        "vs_avg": "เทียบค่าเฉลี่ย", "vs_last_wk": "เทียบสัปดาห์ก่อน",
        "rd_well": "✅ ฟื้นตัวดี", "rd_under": "⚠️ ฟื้นตัวไม่พอ",
        "rd_fatigue": "🩺 อาจมีสัญญาณอ่อนล้า", "rd_normal": "• ฟื้นตัวปกติ",
        "weekdays": ["จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส.", "อา."],
        "months": ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                   "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."],
    },
}


def lang_code(language: str | None) -> str:
    """Normalize a language name/code to 'th' or 'en' for label lookup."""
    l = (language or "").strip().lower()
    if l.startswith("th") or "thai" in l or "ไทย" in l:
        return "th"
    return "en"


def report_labels(language: str | None) -> dict:
    """The localized static-label set for the report cards (falls back to en)."""
    return REPORT_LABELS.get(lang_code(language), REPORT_LABELS["en"])


# A header-derived headline longer than this reads as cramped/overwhelming
# painted across a colored bar, so it's left in the body as regular text instead.
_MAX_HEADLINE_CHARS = 160


class FlexReply:
    """Wraps a Flex Message payload so a reply channel can carry either plain
    text or a Flex bubble through the same tuple/list contract."""

    __slots__ = ("alt_text", "bubble", "coaching_note")

    def __init__(self, alt_text: str, bubble: dict, coaching_note: str | None = None):
        self.alt_text = alt_text[:400]  # LINE altText hard limit
        self.bubble = bubble
        # Carries a photo-log's AI coaching tip through to the caller, which
        # places it on the progress card (not this bubble) for photo logs.
        self.coaching_note = coaching_note


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


def delta_chip(current, baseline, higher_is_better: bool,
               flat_pct: float = 3.0) -> dict | None:
    """Chip comparing `current` to a `baseline` (e.g. today vs 7-day average).

    Returns {"text": "▲ 12%", "tone": "good|bad|flat"} or None when a comparison
    can't be made. `higher_is_better` decides whether an increase is healthy
    (steps) or not (resting HR). Moves under `flat_pct` read as steady ("≈").
    The comparison basis ("vs avg") is shown once in the section header, not
    repeated on every chip — that keeps each chip short enough never to wrap.
    """
    if current is None or not baseline:
        return None
    pct = (current - baseline) / baseline * 100
    if abs(pct) < flat_pct:
        return {"text": "≈", "tone": "flat"}
    up = pct > 0
    arrow = "▲" if up else "▼"
    tone = "good" if (up == higher_is_better) else "bad"
    return {"text": f"{arrow} {abs(round(pct))}%", "tone": tone}


def trend_chip(trend_str: str | None, higher_is_better: bool) -> dict | None:
    """Chip from a build_trends() week-over-week string like 'up 12% vs last
    week' / 'down 5% vs last week' / 'steady'. Returns a short "▲ 12%" / "≈"
    chip; the "vs last wk" basis is shown once in the section header."""
    if not trend_str:
        return None
    if trend_str == "steady":
        return {"text": "≈", "tone": "flat"}
    up = trend_str.startswith("up")
    import re
    m = re.search(r"(\d+)%", trend_str)
    pct = m.group(1) if m else ""
    arrow = "▲" if up else "▼"
    tone = "good" if (up == higher_is_better) else "bad"
    return {"text": f"{arrow} {pct}%", "tone": tone}


def _section_label(text: str, color: str, caption: str | None = None) -> dict:
    """A small uppercase section header. When `caption` is given (e.g. the
    comparison basis "vs avg"), it's rendered as muted text on the right so
    the ▲/▼ chips below don't each need to spell it out."""
    label = {"type": "text", "text": text, "size": "xs", "weight": "bold",
             "color": color, "wrap": True, "gravity": "center", "flex": 1}
    if not caption:
        return label
    return {"type": "box", "layout": "horizontal", "contents": [
        label,
        {"type": "text", "text": caption, "size": "xxs", "color": TEXT_MUTED,
         "align": "end", "gravity": "center", "wrap": False, "flex": 0},
    ]}


# Fixed pixel widths for the value / chip columns of a stat row. LINE sizes a
# flex child from its CONTENT first and only shares LEFTOVER space by the flex
# ratio, so a flex-weighted value column is as wide as its own text — a chip-less
# row (whose right neighbor is an empty filler) then ends up a different width
# than a chip'd row, and the values fail to line up (the bug seen on-device).
# Giving the value and chip fixed-width boxes makes every row's geometry
# identical, so values right-align to one clean column regardless of chip.
_VALUE_COL_WIDTH = "78px"   # fits "807 kcal" / "6,614" at sm-bold
_CHIP_COL_WIDTH = "48px"    # fits "▼ 99%"


def _stat_row(label: str, value: str, chip: dict | None) -> dict:
    """A receipt-style row: muted label on the left (flexes + may wrap), the
    bold value in a fixed-width right-aligned column, then a fixed-width trend
    chip column (empty when there's no chip).

    Both the value and chip columns are fixed-width boxes, so their right edges
    sit at the same x on every row — values line up cleanly whether or not a
    row has a chip, and a long value can never shove the chip onto a new line."""
    # LINE rejects text components with an empty string ("text": ""), so
    # chip-less rows use a filler instead of an invisible text node.
    if chip:
        chip_col = {"type": "box", "layout": "vertical", "flex": 0, "width": _CHIP_COL_WIDTH,
                    "contents": [
                        {"type": "text", "text": chip["text"], "size": "xs", "weight": "bold",
                         "color": _CHIP_TONE_COLOR.get(chip["tone"], TREND_FLAT),
                         "align": "end", "gravity": "center", "wrap": False},
                    ]}
    else:
        chip_col = {"type": "box", "layout": "vertical", "flex": 0, "width": _CHIP_COL_WIDTH,
                    "contents": [{"type": "filler"}]}
    return {"type": "box", "layout": "horizontal", "contents": [
        {"type": "text", "text": label, "size": "sm", "color": TEXT_MUTED,
         "flex": 1, "gravity": "center", "wrap": True},
        {"type": "box", "layout": "vertical", "flex": 0, "width": _VALUE_COL_WIDTH,
         "contents": [
            {"type": "text", "text": value, "size": "sm", "weight": "bold",
             "color": TEXT_DARK, "align": "end", "gravity": "center", "wrap": False},
         ]},
        chip_col,
    ]}


def _sleep_stage_bar(stage_min: dict, labels: dict | None = None) -> list[dict]:
    """A proportional deep/light/rem/awake bar + a small legend. Returns the
    body components to append (empty when there's no stage data)."""
    L = labels or REPORT_LABELS["en"]
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
    for stage, key in (("DEEP", "leg_deep"), ("REM", "leg_rem"), ("AWAKE", "leg_awake")):
        mins = round(stage_min.get(stage, 0) or 0)
        if mins > 0:
            legend.append({"type": "text", "text": f"{L[key]} {mins}m", "size": "xxs",
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
                              narrative: str, labels: dict | None = None) -> dict:
    """Daily brief card: readiness pill in the header, then Recovery / Sleep /
    Activity stat sections, then a Gemini 'Today's Focus' narrative.

    `labels` is a localized REPORT_LABELS[...] set (defaults to English)."""
    L = labels or REPORT_LABELS["en"]
    body: list[dict] = []

    if recovery_rows:
        body.append(_section_label(L["recovery"], color, caption=L["vs_avg"]))
        body.append({"type": "box", "layout": "vertical", "margin": "sm", "spacing": "sm",
                     "contents": [_stat_row(*r) for r in recovery_rows]})

    if sleep_stage_min and any(sleep_stage_min.values()):
        if body:
            body.append({"type": "separator", "margin": "lg"})
        body.append({"type": "box", "layout": "vertical", "margin": "lg" if len(body) > 1 else "none",
                     "contents": [_section_label(sleep_label or L["sleep"], color),
                                  *_sleep_stage_bar(sleep_stage_min, L)]})

    if activity_rows:
        if body:
            body.append({"type": "separator", "margin": "lg"})
        body.append({"type": "box", "layout": "vertical", "margin": "lg" if body else "none",
                     "contents": [_section_label(L["activity"], color, caption=L["vs_avg"]),
                                  {"type": "box", "layout": "vertical", "margin": "sm",
                                   "spacing": "sm", "contents": [_stat_row(*r) for r in activity_rows]}]})

    body += _narrative_block(L["todays_focus"], color, narrative)
    if not body:
        body = [{"type": "text", "text": narrative or "—", "size": "sm",
                 "color": TEXT_DARK, "wrap": True}]

    return {
        "type": "bubble", "size": "mega",
        "header": _report_header(L["daily_brief"], "🌅", color, date_label, readiness_pill),
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body},
    }


def build_weekly_report_bubble(*, range_label: str, color: str,
                               steps_series: list[tuple[str, float]],
                               average_rows: list[tuple[str, str, dict | None]],
                               narrative: str, labels: dict | None = None,
                               sleep_series: list[tuple[str, float]] | None = None) -> dict:
    """Weekly report card: 7-day steps + sleep bar charts, weekly averages
    with week-over-week trend chips, and a Gemini 'Key Insight' narrative.

    `labels` is a localized REPORT_LABELS[...] set (defaults to English)."""
    L = labels or REPORT_LABELS["en"]
    body: list[dict] = []

    steps_chart = _mini_bar_chart(steps_series) if steps_series else []
    if steps_chart:
        body.append(_section_label(L["steps_7day"], color))
        body += steps_chart

    sleep_chart = _mini_bar_chart(sleep_series) if sleep_series else []
    if sleep_chart:
        if body:
            body.append({"type": "separator", "margin": "lg"})
        body.append(_section_label(L["sleep_7day"], color))
        body += sleep_chart

    if average_rows:
        if body:
            body.append({"type": "separator", "margin": "lg"})
        body.append({"type": "box", "layout": "vertical", "margin": "lg" if body else "none",
                     "contents": [_section_label(L["weekly_averages"], color, caption=L["vs_last_wk"]),
                                  {"type": "box", "layout": "vertical", "margin": "sm",
                                   "spacing": "sm", "contents": [_stat_row(*r) for r in average_rows]}]})

    body += _narrative_block(L["key_insight"], color, narrative)
    if not body:
        body = [{"type": "text", "text": narrative or "—", "size": "sm",
                 "color": TEXT_DARK, "wrap": True}]

    return {
        "type": "bubble", "size": "mega",
        "header": _report_header(L["weekly_report"], "📊", color, range_label, None),
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body},
    }


def build_log_bubble(*, name: str, kicker: str, accent_color: str,
                     highlight: tuple[str, str], rows: list[tuple[str, str]],
                     notes: str | None, synced: bool, sync_label: str,
                     low_conf_label: str | None = None,
                     image_url: str | None = None,
                     coaching_note: str | None = None,
                     items: list[tuple[str, str]] | None = None) -> dict:
    """A food/drink log confirmation card.

    Layout (top to bottom): optional hero photo, the logged item's name as a
    bold title (quantity stripped since it's shown in the badge), a colored
    highlight badge for the single most important stat (kcal for food, volume
    for drinks), an optional itemized breakdown (`items`, for a multi-dish
    plate — each dish name + its own kcal), the rest of the macros as
    receipt-style itemized rows (plate TOTALS when `items` is given), an
    optional muted notes line, and a footer STATUS row.
    """
    import re as _re
    highlight_icon, highlight_value = highlight

    # Strip quantity from name — e.g. "ชาเยอร์บามาเต (750ml)" → "ชาเยอร์บามาเต"
    # Removes trailing parenthesized numbers/units like (1 โคน), (750ml), (2 glasses)
    display_name = _re.sub(r'\s*\([\d.,]+\s*[^)]*\)\s*$', '', name).strip() or name

    body_contents = [
        {"type": "text", "text": display_name, "weight": "bold", "size": "lg",
         "wrap": True, "color": TEXT_DARK},
    ]

    # Highlight badge row (kcal/ml badge only — no meal slot)
    badge_row_contents = [
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
    ]

    body_contents.append({
        "type": "box",
        "layout": "horizontal",
        "margin": "md",
        "contents": badge_row_contents,
    })

    if items:
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
                        {"type": "text", "text": item_name, "size": "sm", "color": TEXT_DARK,
                         "flex": 3, "wrap": True},
                        {"type": "text", "text": item_value, "size": "sm", "color": TEXT_MUTED,
                         "align": "end", "flex": 2},
                    ],
                }
                for item_name, item_value in items
            ],
        })

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

    if coaching_note:
        body_contents.append({"type": "separator", "margin": "lg"})
        body_contents.append({
            "type": "text", "text": coaching_note, "size": "xs", "color": "#555555",
            "wrap": True, "margin": "md",
        })

    footer_contents = []
    # Only show footer when there's a problem (sync failed or low confidence)
    if not synced:
        footer_contents.append({
            "type": "box",
            "layout": "baseline",
            "contents": [
                {"type": "text", "text": "STATUS", "size": "xs", "color": TEXT_MUTED, "flex": 1},
                {"type": "text", "text": sync_label, "size": "xs", "wrap": True, "weight": "bold",
                 "align": "end", "flex": 2, "color": COLOR_NOT_SYNCED},
            ],
        })
    if low_conf_label:
        footer_contents.append({"type": "text", "text": low_conf_label, "size": "xs",
                                "color": TEXT_MUTED, "wrap": True, "margin": "xs"})

    bubble = {
        "type": "bubble",
        "size": "kilo",
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body_contents},
    }
    if footer_contents:
        bubble["footer"] = {"type": "box", "layout": "vertical", "paddingAll": "12px",
                            "spacing": "xs", "contents": footer_contents}
    if image_url:
        bubble["hero"] = {"type": "image", "url": image_url, "size": "full",
                          "aspectRatio": "20:13", "aspectMode": "cover"}
    return bubble


def build_daily_progress_bubble(*, current: dict, targets: dict,
                                lang: str = "en",
                                coaching_note: str | None = None) -> dict | None:
    """A daily nutrition progress card showing current vs target with progress bars.

    `current`: {"kcal": int, "protein_g": int, "fat_g": int, "carbs_g": int, "water_ml": int}
    `targets`: same keys with the daily goal values.

    Returns None when nothing has been consumed yet (all zeros).
    """
    if all(current.get(k, 0) <= 0 for k in ("kcal", "protein_g", "fat_g", "carbs_g", "water_ml")):
        return None

    title = "📊 สรุปวันนี้" if lang == "th" else "📊 Today's Progress"

    # Nutrient row config: (key, emoji, label_th, label_en, unit)
    nutrients = [
        ("kcal", "🔥", "พลังงาน", "Energy", "kcal"),
        ("protein_g", "💪", "โปรตีน", "Protein", "g"),
        ("carbs_g", "🍞", "คาร์บ", "Carbs", "g"),
        ("fat_g", "🥑", "ไขมัน", "Fat", "g"),
        ("water_ml", "💧", "น้ำ", "Water", "ml"),
    ]

    rows = []
    for key, emoji, label_th, label_en, unit in nutrients:
        cur = current.get(key, 0)
        tgt = targets.get(key, 0)
        if tgt <= 0:
            continue
        pct = min(cur / tgt, 1.0)  # cap at 100% for the bar
        label = label_th if lang == "th" else label_en
        # Determine bar color: green when goal met, warm yellow while in progress
        bar_color = "#7DCCAD" if cur >= tgt else "#FFC349"
        goal_met = cur >= tgt
        remaining = max(0, tgt - cur)

        # Progress row: label + value on the right (🎉 when goal met)
        goal_icon = " 🎉" if goal_met else ""
        if unit == "kcal":
            value_text = f"{cur:,}/{tgt:,}"
        elif unit == "ml":
            value_text = f"{cur:,}/{tgt:,}"
        else:
            value_text = f"{cur}/{tgt}"

        rows.append({"type": "box", "layout": "vertical", "spacing": "xs",
                     "margin": "md" if rows else "sm", "contents": [
            # Label row: emoji+name on left, value on right
            {"type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": f"{emoji} {label}", "size": "xs",
                 "color": TEXT_MUTED, "flex": 1},
                {"type": "text", "text": f"{value_text} {unit}{goal_icon}", "size": "xs",
                 "color": TEXT_DARK, "weight": "bold", "align": "end", "flex": 0},
            ]},
            # Progress bar
            {"type": "box", "layout": "horizontal", "height": "6px",
             "cornerRadius": "3px", "backgroundColor": "#E5E7EB", "contents": [
                {"type": "box", "layout": "vertical",
                 "width": f"{max(1, round(pct * 100))}%",
                 "backgroundColor": bar_color,
                 "contents": [{"type": "filler"}]},
                {"type": "filler"},
            ]},
        ]})

    if not rows:
        return None

    # Remaining summary at the bottom
    remain_parts = []
    for key, emoji, label_th, label_en, unit in nutrients:
        remaining = max(0, targets.get(key, 0) - current.get(key, 0))
        if remaining > 0 and targets.get(key, 0) > 0:
            lbl = label_th if lang == "th" else label_en
            if unit == "kcal" or unit == "ml":
                remain_parts.append(f"{remaining:,}{unit}")
            else:
                remain_parts.append(f"{remaining}g {lbl}")

    footer_text = ""
    if remain_parts:
        prefix = "เหลืออีก" if lang == "th" else "Remaining"
        footer_text = f"{prefix}: {' · '.join(remain_parts[:3])}"  # show top 3

    contents = [
        *rows,
    ]
    if footer_text:
        contents.append({"type": "separator", "margin": "md"})
        contents.append({"type": "text", "text": footer_text, "size": "xxs",
                         "color": TEXT_MUTED, "wrap": True, "margin": "sm"})

    if coaching_note:
        contents.append({"type": "separator", "margin": "md"})
        contents.append({"type": "text", "text": coaching_note, "size": "xs",
                         "color": "#555555", "wrap": True, "margin": "sm"})

    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "14px",
            "contents": contents,
        },
    }
