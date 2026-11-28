"""Tests for the official Claude Code rate-limit feed."""

from __future__ import annotations

import stat
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub.claude_usage import normalize_claude_usage, read_claude_usage, write_claude_usage
from hub import state_engine
from scripts.claude_usage_statusline import format_statusline
from screens import build_claude_svg


class TestClaudeUsage(unittest.TestCase):
    def _svg_group_bounds(self, svg: str, group_id: str) -> tuple[int, int, int, int]:
        root = ET.fromstring(svg)
        group = root.find(f".//*[@id='{group_id}']")
        self.assertIsNotNone(group, f"SVG group {group_id!r} is missing")
        rects = [node for node in group.iter() if node.tag.endswith("rect")]
        self.assertTrue(rects, f"SVG group {group_id!r} has no visible pixels")
        bounds = [
            (
                int(rect.attrib["x"]),
                int(rect.attrib["y"]),
                int(rect.attrib["x"]) + int(rect.attrib["width"]),
                int(rect.attrib["y"]) + int(rect.attrib["height"]),
            )
            for rect in rects
        ]
        return (
            min(rect[0] for rect in bounds),
            min(rect[1] for rect in bounds),
            max(rect[2] for rect in bounds),
            max(rect[3] for rect in bounds),
        )

    def _assert_svg_groups_do_not_overlap(self, svg: str, first: str, second: str) -> None:
        a = self._svg_group_bounds(svg, first)
        b = self._svg_group_bounds(svg, second)
        self.assertTrue(
            a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1],
            f"SVG groups {first!r} and {second!r} overlap: {a} and {b}",
        )

    def test_claude_card_text_regions_do_not_overlap_without_real_data(self) -> None:
        svg = build_claude_svg({"rate_limits": {}, "quota_source": None})

        for first, second in (
            ("claude-header-title", "claude-header-status"),
            ("claude-header-status", "claude-primary-label"),
            ("claude-header-status", "claude-pokemon"),
            ("claude-primary-label", "claude-primary-value"),
            ("claude-weekly-title", "claude-weekly-value"),
            ("claude-reset-5h-label", "claude-reset-5h-value"),
            ("claude-reset-7d-label", "claude-reset-7d-value"),
            ("claude-reset-7d-value", "claude-statusline-hint"),
        ):
            with self.subTest(first=first, second=second):
                self._assert_svg_groups_do_not_overlap(svg, first, second)

        self.assertIn('id="claude-weekly-empty"', svg)
        self.assertNotIn('id="claude-weekly-bar"', svg)

    def test_claude_card_text_regions_do_not_overlap_with_real_data(self) -> None:
        svg = build_claude_svg({
            "rate_limits": {
                "five_hour": {"used_percentage": 43},
                "seven_day": {"used_percentage": 72},
            },
            "quota_source": "claude-code-statusline",
        })

        for first, second in (
            ("claude-header-title", "claude-header-status"),
            ("claude-header-status", "claude-primary-label"),
            ("claude-header-status", "claude-pokemon"),
            ("claude-primary-label", "claude-primary-value"),
            ("claude-weekly-title", "claude-weekly-value"),
            ("claude-reset-5h-label", "claude-reset-5h-value"),
            ("claude-reset-7d-label", "claude-reset-7d-value"),
            ("claude-reset-7d-value", "claude-statusline-hint"),
        ):
            with self.subTest(first=first, second=second):
                self._assert_svg_groups_do_not_overlap(svg, first, second)

        self.assertIn('id="claude-weekly-bar"', svg)
        self.assertNotIn('id="claude-weekly-empty"', svg)

    def test_normalizes_official_rate_limit_windows(self) -> None:
        now = 1_800_000_000
        snapshot = normalize_claude_usage({
            "rate_limits": {
                "five_hour": {"used_percentage": 42.5, "resets_at": now + 1800},
                "seven_day": {"used_percentage": 77, "resets_at": now + 86_400},
            },
        }, now=now)

        self.assertEqual(snapshot["source"], "claude-code-statusline")
        self.assertEqual(snapshot["updated_at"], now)
        self.assertEqual(snapshot["rate_limits"]["five_hour"], {
            "used_percentage": 42.5,
            "resets_at": now + 1800,
        })
        self.assertEqual(snapshot["rate_limits"]["seven_day"]["used_percentage"], 77.0)
        self.assertIn("5h 43%", format_statusline(snapshot))
        self.assertIn("7d 77%", format_statusline(snapshot))

    def test_ignores_invalid_and_expired_windows(self) -> None:
        now = 1_800_000_000
        snapshot = normalize_claude_usage({
            "rate_limits": {
                "five_hour": {"used_percentage": 101, "resets_at": now + 500},
                "seven_day": {"used_percentage": 15, "resets_at": now},
            },
        }, now=now)

        self.assertEqual(snapshot["rate_limits"], {})
        self.assertEqual(format_statusline(snapshot), "Claude · cuota real no disponible")

    def test_snapshot_is_private_atomic_data_and_expires(self) -> None:
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "claude-rate-limits.json"
            payload = {
                "rate_limits": {
                    "five_hour": {"used_percentage": 50, "resets_at": now + 3600},
                },
            }
            write_claude_usage(payload, path=path, now=now)

            loaded = read_claude_usage(path=path, now=now + 10)
            stale = read_claude_usage(path=path, now=now + 86_401)
            mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(loaded["source"], "claude-code-statusline")
        self.assertEqual(loaded["rate_limits"]["five_hour"]["used_percentage"], 50.0)
        self.assertEqual(mode, 0o600)
        self.assertIsNone(stale["source"])
        self.assertEqual(stale["rate_limits"], {})

    def test_collector_and_card_use_official_limits_not_session_totals(self) -> None:
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "claude-rate-limits.json"
            write_claude_usage({
                "rate_limits": {
                    "five_hour": {"used_percentage": 43, "resets_at": now + 1800},
                    "seven_day": {"used_percentage": 72, "resets_at": now + 86_400},
                },
            }, path=path, now=now)
            with (
                mock.patch.object(state_engine, "CLAUDE_RATE_LIMITS_PATH", path),
                mock.patch.object(state_engine, "CLAUDE_PROJECT_GLOB", f"{temp_dir}/no-sessions/**/*.jsonl"),
                mock.patch("hub.claude_usage.time.time", return_value=now),
            ):
                stats = state_engine.collect_claude_usage()

        self.assertEqual(stats["today_total"], 0)
        self.assertEqual(stats["week_total"], 0)
        self.assertEqual(stats["quota_source"], "claude-code-statusline")
        self.assertEqual(stats["rate_limits"]["five_hour"]["used_percentage"], 43.0)
        self.assertFalse(stats["out_of_tokens"])
        svg = build_claude_svg(stats)
        self.assertIn("Uso oficial Claude Code: 5h 43%, 7d 72%.", svg)

    def test_claude_card_marks_quota_unavailable_instead_of_showing_estimate(self) -> None:
        svg = build_claude_svg({
            "today_total": 987654,
            "week_total": 1234567,
            "rate_limits": {},
            "quota_source": None,
        })

        self.assertIn("Cuota real de Claude Code no disponible", svg)
        self.assertNotIn("987.65K", svg)


if __name__ == "__main__":
    unittest.main()