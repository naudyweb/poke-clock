"""Read and persist Claude Code's official rate-limit status-line data."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path

CLAUDE_USAGE_PATH = Path(
    os.environ.get(
        "POKE_HUB_CLAUDE_RATE_LIMITS_PATH",
        str(Path.home() / ".cache" / "poke-hub" / "claude-rate-limits.json"),
    )
)
CLAUDE_USAGE_MAX_AGE_S = 24 * 60 * 60
CLAUDE_RATE_WINDOWS = ("five_hour", "seven_day")


def normalize_claude_usage(payload: object, now: float | None = None) -> dict:
    now_ts = time.time() if now is None else now
    raw_windows = payload.get("rate_limits") if isinstance(payload, dict) else None
    windows: dict[str, dict[str, int | float]] = {}
    if isinstance(raw_windows, dict):
        for name in CLAUDE_RATE_WINDOWS:
            raw = raw_windows.get(name)
            if not isinstance(raw, dict):
                continue
            percentage = raw.get("used_percentage")
            resets_at = raw.get("resets_at")
            if (
                isinstance(percentage, bool)
                or not isinstance(percentage, (int, float))
                or not math.isfinite(percentage)
                or percentage < 0
                or percentage > 100
                or isinstance(resets_at, bool)
                or not isinstance(resets_at, (int, float))
                or not math.isfinite(resets_at)
                or resets_at <= now_ts
            ):
                continue
            windows[name] = {
                "used_percentage": float(percentage),
                "resets_at": int(resets_at),
            }
    return {
        "source": "claude-code-statusline",
        "updated_at": int(now_ts),
        "rate_limits": windows,
    }


def write_claude_usage(payload: object, path: Path | None = None, now: float | None = None) -> dict:
    snapshot = normalize_claude_usage(payload, now=now)
    target = path or CLAUDE_USAGE_PATH
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".claude-rate-limits-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(snapshot, file, ensure_ascii=False)
            file.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return snapshot


def read_claude_usage(path: Path | None = None, now: float | None = None) -> dict:
    target = path or CLAUDE_USAGE_PATH
    try:
        snapshot = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"source": None, "updated_at": None, "rate_limits": {}}

    now_ts = time.time() if now is None else now
    if not isinstance(snapshot, dict) or snapshot.get("source") != "claude-code-statusline":
        return {"source": None, "updated_at": None, "rate_limits": {}}
    updated_at = snapshot.get("updated_at")
    if (
        isinstance(updated_at, bool)
        or not isinstance(updated_at, (int, float))
        or not math.isfinite(updated_at)
        or updated_at > now_ts + 300
        or now_ts - updated_at > CLAUDE_USAGE_MAX_AGE_S
    ):
        return {"source": None, "updated_at": None, "rate_limits": {}}

    normalized = normalize_claude_usage(snapshot, now=now_ts)
    return {
        "source": normalized["source"] if normalized["rate_limits"] else None,
        "updated_at": int(updated_at),
        "rate_limits": normalized["rate_limits"],
    }