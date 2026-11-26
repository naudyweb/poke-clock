#!/usr/bin/env python3
"""Forward Claude Code's official rate-limit windows to Poké-Hub."""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from hub.claude_usage import normalize_claude_usage, write_claude_usage


def format_statusline(snapshot: dict) -> str:
    windows = snapshot.get("rate_limits", {})
    labels = (("five_hour", "5h"), ("seven_day", "7d"))
    available = [
        f"{label} {math.floor(windows[name]['used_percentage'] + 0.5)}%"
        for name, label in labels
        if name in windows
    ]
    if not available:
        return "Claude · cuota real no disponible"
    return "Claude · " + " / ".join(available)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}

    now = time.time()
    try:
        snapshot = write_claude_usage(payload, now=now)
    except OSError:
        snapshot = normalize_claude_usage(payload, now=now)
    print(format_statusline(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())