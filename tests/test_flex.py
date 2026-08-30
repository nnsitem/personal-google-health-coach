"""Weekly report Flex bubble — the 7-day sleep bar chart added 2026-08-28
(DESIGN-V3.md #3) reuses the same native _mini_bar_chart component the steps
chart already used, rather than a separate rendering pipeline.
"""

import json
import unittest

from coach.flex import build_weekly_report_bubble, REPORT_LABELS


def _find_section_labels(bubble: dict) -> list[str]:
    """All text strings in the body, for a simple 'is this section present'
    check without asserting the exact Flex box tree shape."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and "text" in node:
                found.append(node["text"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(bubble)
    return found


class WeeklySleepChart(unittest.TestCase):
    def _bubble(self, sleep_series):
        return build_weekly_report_bubble(
            range_label="1 Jan – 7 Jan",
            color="#000000",
            steps_series=[("M", 8000), ("T", 9000)],
            sleep_series=sleep_series,
            average_rows=[],
            narrative="Good week overall.",
            labels=REPORT_LABELS["en"],
        )

    def test_sleep_section_appears_when_there_is_sleep_data(self):
        bubble = self._bubble([("M", 7.0), ("T", 6.5)])
        texts = _find_section_labels(bubble)
        self.assertTrue(any("SLEEP" in t for t in texts))

    def test_sleep_section_is_omitted_when_theres_nothing_to_chart(self):
        # All-zero (or empty) series must not render an empty/misleading chart.
        bubble = self._bubble([("M", 0), ("T", 0)])
        texts = _find_section_labels(bubble)
        self.assertFalse(any("SLEEP" in t for t in texts))

    def test_sleep_section_is_omitted_when_series_is_none(self):
        bubble = self._bubble(None)
        texts = _find_section_labels(bubble)
        self.assertFalse(any("SLEEP" in t for t in texts))

    def test_bubble_is_json_serializable(self):
        # It's sent to LINE as JSON — a stray tuple/set/non-primitive would
        # only surface at send time otherwise.
        bubble = self._bubble([("M", 7.0)])
        json.dumps(bubble)  # must not raise

    def test_steps_chart_still_present_alongside_sleep_chart(self):
        # The new chart must not have displaced the existing one.
        bubble = self._bubble([("M", 7.0)])
        texts = _find_section_labels(bubble)
        self.assertTrue(any("STEPS" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
