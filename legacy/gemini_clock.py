#!/usr/bin/env python3
"""
Gemini Token Usage Dashboard for JUZIPi SD PRO (Smart Weather Clock)
Reads local Antigravity CLI/IDE transcripts, renders a crisp 240x240 dashboard
(Pokémon GBC Gold/Silver/Crystal pixel-art theme by default, or modern dark theme),
and uploads it to the display's /photo/upload endpoint only when data changes.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_HOST = "192.168.18.26"
DEFAULT_THEME = "pokemon"
PHOTO_NAME = "gemini.jpg"
STATE_FILE = Path.home() / ".cache" / "gemini-clock" / "state.json"

TRANSCRIPT_GLOBS = [
    str(Path.home() / ".gemini" / "antigravity-cli" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"),
    str(Path.home() / ".gemini" / "antigravity" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"),
    str(Path.home() / ".gemini" / "antigravity-ide" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"),
]

DAY_LABELS_ES = ["L", "M", "X", "J", "V", "S", "D"]

# Crisp 5x6 authentic Game Boy pixel font (variable narrow width for . and :)
PIXEL_FONT: dict[str, list[str]] = {
    "0": ["01110", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00110", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010"],
    "5": ["11111", "10000", "11110", "00001", "10001", "01110"],
    "6": ["01110", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000"],
    "8": ["01110", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "01111", "00001", "00010", "01100"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001"],
    "B": ["11110", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10000", "10111", "10001", "10001", "01110"],
    "H": ["10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["111", "010", "010", "010", "010", "111"],
    "J": ["00111", "00010", "00010", "10010", "10010", "01100"],
    "K": ["10001", "10010", "11100", "10010", "10001", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "11110", "10010", "10001", "10001"],
    "S": ["01111", "10000", "01110", "00001", "10001", "01110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "11011", "10001"],
    "X": ["10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100"],
    "Z": ["11111", "00010", "00100", "01000", "10000", "11111"],
    "v": ["0000", "0000", "1001", "1001", "0110", "0110"],
    ".": ["00", "00", "00", "00", "11", "11"],
    ":": ["00", "11", "00", "00", "11", "00"],
    "%": ["11001", "11010", "00100", "01011", "10011", "00000"],
    "-": ["0000", "0000", "1111", "0000", "0000", "0000"],
    "/": ["0001", "0010", "0010", "0100", "0100", "1000"],
    " ": ["000", "000", "000", "000", "000", "000"],
}

# Classic Game Boy Color (Gen 2) 18x18 Pikachu sprite
PIKACHU_SPRITE = [
    ".KK........KK.....",
    ".KKK......KKK.....",
    "..KYK....KYK......",
    "...KYKKKKYK.......",
    "...KYYYYYYK...KKK.",
    "..KYYYYYYYYK.KYYYK",
    "..KYWKYYKWYK.KYYK.",
    "..KYKKYYKKYKKYYK..",
    "..KRRYYYYRRKYYK...",
    "..KRRYYYYRRKBK....",
    "...KYYYYYYKBK.....",
    "..KYYYYYYYYK......",
    "..KYSYYYYYSK......",
    "..KYYYYYYYSK......",
    "...KYYYYYSK.......",
    "...KSYYYSSK.......",
    "..KSSKKKKSSK......",
    "..KKK....KKK......",
]

PIKACHU_PALETTE = {
    "K": "#181820",
    "Y": "#F8D030",
    "S": "#D89818",
    "W": "#FFFFFF",
    "R": "#F03838",
    "B": "#985818",
}

POKEBALL_SPRITE = [
    "..KKKK..",
    ".KRRRRK.",
    "KRRWRRRK",
    "KKKWWKKK",
    "KKWKKWKK",
    "KWWWWWKK",
    ".KWWWWK.",
    "..KKKK..",
]

POKEBALL_PALETTE = {
    "K": "#181820",
    "R": "#E83838",
    "W": "#F8F8F0",
}

SPARKLE_SPRITE = [
    "...C...",
    "...C...",
    "..CWC..",
    "CCWWWCC",
    "..CWC..",
    "...C...",
    "...C...",
]

SPARKLE_PALETTE = {
    "C": "#3898F8",
    "W": "#FFFFFF",
}


def pixel_text_width(text: str, scale: int, gap: int = 1) -> int:
    if not text:
        return 0
    total = 0
    for idx, ch in enumerate(text):
        glyph = PIXEL_FONT.get(ch, PIXEL_FONT.get(ch.upper(), PIXEL_FONT[" "]))
        total += len(glyph[0]) * scale
        if idx < len(text) - 1:
            total += gap * scale
    return total


def render_pixel_text(
    text: str,
    x: int,
    y: int,
    scale: int,
    color: str,
    align: str = "left",
    gap: int = 1,
    shadow: str | None = None,
) -> str:
    w = pixel_text_width(text, scale, gap)
    if align == "right":
        start_x = x - w
    elif align == "center":
        start_x = x - w // 2
    else:
        start_x = x

    rects: list[str] = []
    if shadow:
        rects.append(render_pixel_text(text, x + scale, y + scale, scale, shadow, align=align, gap=gap, shadow=None))

    cx = start_x
    for ch in text:
        glyph = PIXEL_FONT.get(ch, PIXEL_FONT.get(ch.upper(), PIXEL_FONT[" "]))
        gw = len(glyph[0])
        for ry, row in enumerate(glyph):
            rx = 0
            while rx < gw:
                if row[rx] == "1":
                    run = 1
                    while rx + run < gw and row[rx + run] == "1":
                        run += 1
                    rects.append(
                        f'<rect x="{cx + rx * scale}" y="{y + ry * scale}" '
                        f'width="{run * scale}" height="{scale}" fill="{color}"/>'
                    )
                    rx += run
                else:
                    rx += 1
        cx += (gw + gap) * scale
    return "".join(rects)


def render_sprite(grid: list[str], palette: dict[str, str], x: int, y: int, scale: int) -> str:
    rects: list[str] = []
    for ry, row in enumerate(grid):
        rx = 0
        gw = len(row)
        while rx < gw:
            ch = row[rx]
            if ch in palette:
                run = 1
                while rx + run < gw and row[rx + run] == ch:
                    run += 1
                rects.append(
                    f'<rect x="{x + rx * scale}" y="{y + ry * scale}" '
                    f'width="{run * scale}" height="{scale}" fill="{palette[ch]}"/>'
                )
                rx += run
            else:
                rx += 1
    return "".join(rects)


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 100_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 10_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 100_000:
        return f"{n / 1_000:.0f}K"
    if n >= 10_000:
        return f"{n / 1_000:.1f}K"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return str(n)


def fmt_model_name(raw: str) -> str:
    if not raw or raw == "unknown":
        return "Antigravity"
    name = raw.strip()
    name = re.sub(r"-(low|medium|high|xhigh|max)$", "", name, flags=re.IGNORECASE)
    parts = re.split(r"[-_]+", name)
    pretty = " ".join(p.upper() if p.lower() in {"ai", "pro"} and len(p) <= 2 else p.capitalize() for p in parts if p)
    if len(pretty) > 16:
        pretty = pretty[:15].rstrip() + "…"
    return pretty or "Antigravity"


def parse_iso_local(ts: str, local_tz) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(local_tz)
    except Exception:
        return None


def collect_usage(gemini_only: bool = False) -> dict:
    local_now = datetime.now().astimezone()
    local_tz = local_now.tzinfo
    today = local_now.date()
    week_dates = [today - timedelta(days=i) for i in range(6, -1, -1)]
    week_set = set(week_dates)

    daily_total = defaultdict(int)
    today_in = 0
    today_out = 0
    today_cache = 0
    today_steps = 0
    today_sessions: set[str] = set()
    all_total = 0
    latest_model = ""
    latest_ts: datetime | None = None
    model_totals_today: dict[str, int] = defaultdict(int)
    seen_steps: set[tuple[str, int]] = set()

    files: set[str] = set()
    for pattern in TRANSCRIPT_GLOBS:
        for path in glob.glob(pattern):
            files.add(os.path.realpath(path))

    for path in sorted(files):
        conv_id = Path(path).parents[2].name
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"input_tokens"' not in line and '"output_tokens"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    it = int(rec.get("input_tokens") or 0)
                    ot = int(rec.get("output_tokens") or 0)
                    ct = int(rec.get("cache_read_tokens") or 0)
                    if it == 0 and ot == 0 and ct == 0:
                        continue

                    model = str(rec.get("model") or "")
                    if gemini_only and "gemini" not in model.lower():
                        continue

                    step_idx = rec.get("step_index")
                    if step_idx is not None:
                        key = (conv_id, int(step_idx))
                        if key in seen_steps:
                            continue
                        seen_steps.add(key)

                    step_total = it + ot + ct
                    all_total += step_total

                    dt = parse_iso_local(str(rec.get("created_at") or ""), local_tz)
                    if dt is None:
                        continue

                    d = dt.date()
                    if d in week_set:
                        daily_total[d] += step_total

                    if d == today:
                        today_in += it
                        today_out += ot
                        today_cache += ct
                        today_steps += 1
                        today_sessions.add(conv_id)
                        if model:
                            model_totals_today[model] += step_total

                    if model and (latest_ts is None or dt > latest_ts):
                        latest_ts = dt
                        latest_model = model
        except OSError:
            continue

    today_total = today_in + today_out + today_cache
    week_total = sum(daily_total[d] for d in week_dates)
    denom = today_in + today_cache
    cache_pct = int(round((today_cache / denom) * 100)) if denom > 0 else 0

    active_model = latest_model
    if not active_model and model_totals_today:
        active_model = max(model_totals_today.items(), key=lambda kv: kv[1])[0]

    return {
        "today_date": today.isoformat(),
        "updated_hm": local_now.strftime("%H:%M"),
        "today_total": today_total,
        "today_in": today_in,
        "today_out": today_out,
        "today_cache": today_cache,
        "today_steps": today_steps,
        "today_sessions": len(today_sessions),
        "cache_pct": cache_pct,
        "week_total": week_total,
        "all_total": all_total,
        "active_model": fmt_model_name(active_model),
        "week_days": [
            {
                "date": d.isoformat(),
                "label": DAY_LABELS_ES[d.weekday()],
                "total": daily_total[d],
                "is_today": d == today,
            }
            for d in week_dates
        ],
    }


def data_fingerprint(stats: dict, theme: str) -> str:
    payload = {
        "theme": theme,
        "today_date": stats["today_date"],
        "today_total": stats["today_total"],
        "today_in": stats["today_in"],
        "today_out": stats["today_out"],
        "today_cache": stats["today_cache"],
        "today_steps": stats["today_steps"],
        "today_sessions": stats["today_sessions"],
        "week_total": stats["week_total"],
        "active_model": stats["active_model"],
        "week_days": [d["total"] for d in stats["week_days"]],
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_pokemon_svg(stats: dict) -> str:
    today_total_str = fmt_tokens(stats["today_total"])
    in_str = fmt_tokens(stats["today_in"])
    out_str = fmt_tokens(stats["today_out"])
    cache_str = fmt_tokens(stats["today_cache"])
    week_total_str = fmt_tokens(stats["week_total"])
    updated_hm = stats["updated_hm"]
    steps = stats["today_steps"]
    lv_val = min(99, max(1, steps)) if steps > 0 else 1
    cache_pct = stats["cache_pct"]

    # Pokémon GBC HP bar color thresholds (green / yellow / red)
    if cache_pct >= 50:
        hp_color = "#30B848"
        hp_hi = "#78E878"
    elif cache_pct >= 20:
        hp_color = "#F8C828"
        hp_hi = "#FFF080"
    else:
        hp_color = "#E83838"
        hp_hi = "#F88888"

    hp_max_w = 72
    hp_fill_w = max(2, int(round((cache_pct / 100.0) * hp_max_w))) if cache_pct > 0 else 0
    # Snap HP bar width to 2px pixel grid
    hp_fill_w = (hp_fill_w // 2) * 2

    poke_name = "GEMINI"

    # Choose big hero scale so it always fits cleanly inside left HUD (max width ~126px)
    hero_scale = 4
    if pixel_text_width(today_total_str, 4) > 126:
        hero_scale = 3

    # 7-day pixel bar chart inside middle panel (x: 16..224, y: 126..152)
    max_day = max((d["total"] for d in stats["week_days"]), default=0)
    bar_parts: list[str] = []
    start_x = 20
    slot_w = 29
    bar_w = 20
    base_y = 148
    max_h = 20

    for idx, day in enumerate(stats["week_days"]):
        bx = start_x + idx * slot_w
        cx = bx + bar_w // 2
        val = day["total"]
        if max_day > 0 and val > 0:
            h = max(4, int(round((val / max_day) * max_h)))
            h = (h // 2) * 2
        else:
            h = 2
        by = base_y - h
        if day["is_today"]:
            fill = "#F8B800"
            hi = "#FFE878"
            border = "#181820"
            lbl_col = "#181820"
        elif val > 0:
            fill = "#3898F8"
            hi = "#88D0FF"
            border = "#181820"
            lbl_col = "#485868"
        else:
            fill = "#D0D8C8"
            hi = "#D0D8C8"
            border = "#98A494"
            lbl_col = "#788478"

        if h >= 4:
            bar_parts.append(
                f'<rect x="{bx}" y="{by}" width="{bar_w}" height="{h}" fill="{border}"/>'
                f'<rect x="{bx + 2}" y="{by + 2}" width="{bar_w - 4}" height="{max(2, h - 4)}" fill="{fill}"/>'
                f'<rect x="{bx + 2}" y="{by + 2}" width="{bar_w - 4}" height="2" fill="{hi}"/>'
            )
        else:
            bar_parts.append(f'<rect x="{bx}" y="{by}" width="{bar_w}" height="2" fill="{border}"/>')

        bar_parts.append(render_pixel_text(day["label"], cx, 152, 2, lbl_col, align="center"))

    bars_markup = "".join(bar_parts)

    # Pixel-art SVG parts
    pokeball = render_sprite(POKEBALL_SPRITE, POKEBALL_PALETTE, 8, 10, 2)
    pikachu = render_sprite(PIKACHU_SPRITE, PIKACHU_PALETTE, 158, 16, 4)
    sparkle = render_sprite(SPARKLE_SPRITE, SPARKLE_PALETTE, 218, 10, 2)

    title_txt = render_pixel_text(poke_name, 28, 12, 2, "#181820")
    lv_txt = render_pixel_text(f"Lv{lv_val}", 152, 12, 2, "#181820", align="right")
    hp_lbl = render_pixel_text("HP", 16, 33, 1, "#F8C828")
    pct_txt = render_pixel_text(f"{cache_pct}%", 152, 30, 2, "#181820", align="right")

    hero_y = 50 if hero_scale == 4 else 54
    hero_txt = render_pixel_text(today_total_str, 18, hero_y, hero_scale, "#181820", shadow="#C8D0C0")

    exp_lbl = render_pixel_text("EXP 7D", 16, 110, 2, "#2868C8")
    week_txt = render_pixel_text(week_total_str, 224, 110, 2, "#181820", align="right")

    # Bottom GBC dialogue box lines
    line1_l = render_pixel_text(f"IN:{in_str}", 22, 186, 2, "#181820")
    line1_r = render_pixel_text(f"OUT:{out_str}", 218, 186, 2, "#181820", align="right")
    line2_l = render_pixel_text(f"CCH:{cache_str}", 22, 206, 2, "#181820")
    line2_r = render_pixel_text(updated_hm, 202, 206, 2, "#606870", align="right")

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240" shape-rendering="crispEdges">
  <!-- Classic GBC Battle Screen Cream Background -->
  <rect width="240" height="240" fill="#F8F8F0"/>

  <!-- Subtle GBC Top Sky / Arena Band -->
  <rect x="0" y="0" width="240" height="6" fill="#E8F0D8"/>

  <!-- Stepped Pixel Grass Battle Pad (Right) -->
  <rect x="166" y="76" width="56" height="4" fill="#98C078"/>
  <rect x="158" y="80" width="72" height="4" fill="#B8D898"/>
  <rect x="154" y="84" width="80" height="8" fill="#C8E4A8"/>
  <rect x="158" y="92" width="72" height="4" fill="#98C078"/>
  <rect x="166" y="96" width="56" height="2" fill="#78A058"/>

  <!-- Sprites -->
  {pokeball}
  {pikachu}
  {sparkle}

  <!-- Top-Left Classic Pokémon Battle HUD Frame (L-Bracket + Arrow Tip) -->
  {title_txt}
  {lv_txt}

  <!-- HP Pill + Pixel Bar -->
  <rect x="12" y="30" width="22" height="12" fill="#181820"/>
  {hp_lbl}
  <rect x="34" y="30" width="{hp_max_w + 4}" height="12" fill="#181820"/>
  <rect x="36" y="32" width="{hp_max_w}" height="8" fill="#F8F8F0"/>
  <rect x="36" y="32" width="{hp_fill_w}" height="8" fill="{hp_color}"/>
  <rect x="36" y="32" width="{hp_fill_w}" height="2" fill="{hp_hi}"/>
  {pct_txt}

  <!-- Big Today Token Readout -->
  {hero_txt}

  <!-- Iconic GBC L-Bracket Under HUD -->
  <rect x="8" y="28" width="4" height="62" fill="#181820"/>
  <rect x="8" y="88" width="138" height="4" fill="#181820"/>
  <rect x="146" y="86" width="4" height="6" fill="#181820"/>
  <rect x="150" y="90" width="4" height="2" fill="#181820"/>
  <!-- Small EXP accent strip under L-bracket -->
  <rect x="12" y="92" width="132" height="2" fill="#3898F8"/>

  <!-- Middle 7-Day EXP Stat Box -->
  <rect x="10" y="104" width="224" height="64" fill="#C8D0C0"/>
  <rect x="8" y="102" width="224" height="64" fill="#181820"/>
  <rect x="10" y="104" width="220" height="60" fill="#FFFFFF"/>
  {exp_lbl}
  {week_txt}
  {bars_markup}

  <!-- Bottom Classic Pokémon Gold/Silver/Crystal Double-Border Dialogue Box -->
  <!-- Outer frame with notched corners -->
  <rect x="10" y="172" width="220" height="62" fill="#181820"/>
  <rect x="8" y="174" width="224" height="58" fill="#181820"/>
  <!-- White gap -->
  <rect x="12" y="176" width="216" height="54" fill="#F8F8F0"/>
  <!-- Inner border -->
  <rect x="14" y="178" width="212" height="50" fill="#181820"/>
  <rect x="16" y="180" width="208" height="46" fill="#F8F8F0"/>
  <!-- Iconic 4 Corner Pixel Ornaments -->
  <rect x="10" y="174" width="6" height="6" fill="#181820"/>
  <rect x="12" y="176" width="2" height="2" fill="#F8F8F0"/>
  <rect x="224" y="174" width="6" height="6" fill="#181820"/>
  <rect x="226" y="176" width="2" height="2" fill="#F8F8F0"/>
  <rect x="10" y="226" width="6" height="6" fill="#181820"/>
  <rect x="12" y="228" width="2" height="2" fill="#F8F8F0"/>
  <rect x="224" y="226" width="6" height="6" fill="#181820"/>
  <rect x="226" y="228" width="2" height="2" fill="#F8F8F0"/>

  <!-- Dialogue Box Stats -->
  {line1_l}
  {line1_r}
  {line2_l}
  {line2_r}

  <!-- Classic Red Pixel Down-Arrow Cursor -->
  <rect x="210" y="208" width="10" height="2" fill="#E83838"/>
  <rect x="212" y="210" width="6" height="2" fill="#E83838"/>
  <rect x="214" y="212" width="2" height="2" fill="#E83838"/>
</svg>
'''


def build_dark_svg(stats: dict) -> str:
    today_total_str = fmt_tokens(stats["today_total"])
    in_str = fmt_tokens(stats["today_in"])
    out_str = fmt_tokens(stats["today_out"])
    cache_str = fmt_tokens(stats["today_cache"])
    week_total_str = fmt_tokens(stats["week_total"])
    model_str = html.escape(stats["active_model"])
    updated_hm = html.escape(stats["updated_hm"])
    sessions = stats["today_sessions"]
    steps = stats["today_steps"]
    cache_pct = stats["cache_pct"]
    ses_label = f"{sessions} ses · {steps} req" if sessions else "sin actividad"

    max_day = max((d["total"] for d in stats["week_days"]), default=0)
    bar_svg_parts = []
    start_x = 22
    slot_w = 28
    bar_w = 18
    base_y = 183
    max_h = 26

    for idx, day in enumerate(stats["week_days"]):
        bx = start_x + idx * slot_w
        cx = bx + bar_w / 2
        val = day["total"]
        if max_day > 0 and val > 0:
            h = max(4, int(round((val / max_day) * max_h)))
        else:
            h = 3
        by = base_y - h
        fill = "url(#barToday)" if day["is_today"] else ("#33476E" if val > 0 else "#1B2234")
        lbl_color = "#E2E8F0" if day["is_today"] else "#64748B"
        lbl_weight = "700" if day["is_today"] else "500"
        bar_svg_parts.append(
            f'<rect x="{bx}" y="{by}" width="{bar_w}" height="{h}" rx="3" fill="{fill}"/>'
            f'<text x="{cx:.1f}" y="196" text-anchor="middle" font-family="SF Pro Display, Noto Sans, sans-serif" '
            f'font-size="10" font-weight="{lbl_weight}" fill="{lbl_color}">{day["label"]}</text>'
        )

    bars_markup = "\n    ".join(bar_svg_parts)
    hero_size = 38 if len(today_total_str) <= 6 else 32

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240">
  <defs>
    <linearGradient id="bgGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#060913"/>
      <stop offset="100%" stop-color="#0B1020"/>
    </linearGradient>
    <radialGradient id="topGlow" cx="0.22" cy="0.06" r="0.65">
      <stop offset="0%" stop-color="#3B82F6" stop-opacity="0.28"/>
      <stop offset="55%" stop-color="#8B5CF6" stop-opacity="0.10"/>
      <stop offset="100%" stop-color="#060913" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="gemGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#38BDF8"/>
      <stop offset="50%" stop-color="#818CF8"/>
      <stop offset="100%" stop-color="#E879F9"/>
    </linearGradient>
    <linearGradient id="barToday" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#38BDF8"/>
      <stop offset="100%" stop-color="#8B5CF6"/>
    </linearGradient>
    <linearGradient id="accentLine" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#38BDF8"/>
      <stop offset="50%" stop-color="#818CF8"/>
      <stop offset="100%" stop-color="#C084FC"/>
    </linearGradient>
  </defs>
  <rect width="240" height="240" fill="url(#bgGrad)"/>
  <rect width="240" height="240" fill="url(#topGlow)"/>
  <g transform="translate(14, 13)">
    <path d="M10 0 C10.6 5.6 14.4 9.4 20 10 C14.4 10.6 10.6 14.4 10 20 C9.4 14.4 5.6 10.6 0 10 C5.6 9.4 9.4 5.6 10 0 Z" fill="url(#gemGrad)"/>
  </g>
  <text x="40" y="28" font-family="SF Pro Display, Noto Sans, sans-serif" font-size="14" font-weight="700" letter-spacing="0.8" fill="#F8FAFC">GEMINI</text>
  <text x="101" y="28" font-family="SF Pro Display, Noto Sans, sans-serif" font-size="11" font-weight="500" fill="#7C8DB5">TOKENS</text>
  <text x="226" y="27" text-anchor="end" font-family="JetBrains Mono, monospace" font-size="11" font-weight="600" fill="#94A3B8">{updated_hm}</text>
  <rect x="12" y="40" width="216" height="84" rx="14" fill="#10172A" stroke="#1E2B4A" stroke-width="1.2"/>
  <rect x="24" y="40" width="60" height="2" rx="1" fill="url(#accentLine)"/>
  <text x="22" y="57" font-family="SF Pro Display, Noto Sans, sans-serif" font-size="10" font-weight="700" letter-spacing="0.9" fill="#7C8DB5">HOY</text>
  <text x="218" y="57" text-anchor="end" font-family="SF Pro Display, Noto Sans, sans-serif" font-size="10" font-weight="500" fill="#64748B">{ses_label}</text>
  <text x="22" y="96" font-family="SF Pro Rounded, SF Pro Display, Noto Sans, sans-serif" font-size="{hero_size}" font-weight="800" fill="#FFFFFF">{today_total_str}</text>
  <circle cx="25" cy="112" r="3" fill="#38BDF8"/>
  <text x="32" y="116" font-family="JetBrains Mono, monospace" font-size="10" font-weight="600" fill="#BAE6FD">IN {in_str}</text>
  <circle cx="101" cy="112" r="3" fill="#C084FC"/>
  <text x="108" y="116" font-family="JetBrains Mono, monospace" font-size="10" font-weight="600" fill="#E9D5FF">OUT {out_str}</text>
  <circle cx="177" cy="112" r="3" fill="#34D399"/>
  <text x="184" y="116" font-family="JetBrains Mono, monospace" font-size="10" font-weight="600" fill="#A7F3D0">{cache_pct}%</text>
  <rect x="12" y="131" width="216" height="72" rx="12" fill="#0D1322" stroke="#1A243D" stroke-width="1"/>
  <text x="22" y="146" font-family="SF Pro Display, Noto Sans, sans-serif" font-size="10" font-weight="700" letter-spacing="0.6" fill="#7C8DB5">7 DÍAS</text>
  <text x="218" y="146" text-anchor="end" font-family="JetBrains Mono, monospace" font-size="11" font-weight="700" fill="#93C5FD">{week_total_str}</text>
  <g>
    {bars_markup}
  </g>
  <rect x="12" y="209" width="216" height="22" rx="8" fill="#10172A" stroke="#1E2B4A" stroke-width="1"/>
  <circle cx="22" cy="220" r="3.2" fill="#34D399"/>
  <text x="30" y="224" font-family="SF Pro Display, Noto Sans, sans-serif" font-size="10.5" font-weight="600" fill="#CBD5E1">{model_str}</text>
  <text x="220" y="224" text-anchor="end" font-family="JetBrains Mono, monospace" font-size="10" font-weight="500" fill="#64748B">CCH {cache_str}</text>
</svg>
'''


def build_svg(stats: dict, theme: str = DEFAULT_THEME) -> str:
    if theme == "dark":
        return build_dark_svg(stats)
    return build_pokemon_svg(stats)


def render_jpg(svg_content: str, out_jpg: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="gemini-clock-") as tmpdir:
        tmp = Path(tmpdir)
        svg_path = tmp / "card.svg"
        png_path = tmp / "card.png"
        svg_path.write_text(svg_content, encoding="utf-8")

        subprocess.run(
            ["rsvg-convert", "-w", "240", "-h", "240", str(svg_path), "-o", str(png_path)],
            check=True,
        )
        # Baseline (non-progressive) 4:2:0 JPEG required by ESP8266 TJpgDec
        # Use 4:4:4? No, TJpgDec supports 4:2:0 and 4:4:4; 4:2:0 Q92 keeps pixel art crisp and tiny.
        subprocess.run(
            [
                "magick",
                str(png_path),
                "-background", "#F8F8F0",
                "-flatten",
                "-interlace", "none",
                "-sampling-factor", "4:2:0",
                "-strip",
                "-quality", "92",
                str(out_jpg),
            ],
            check=True,
        )


def http_get(host: str, path: str, timeout: float = 5.0) -> bytes:
    url = f"http://{host}{path}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def upload_photo(host: str, jpg_path: Path, filename: str = PHOTO_NAME, timeout: float = 10.0) -> None:
    boundary = "----GeminiClockBoundary7MA4YWxkTrZu0gW"
    data = jpg_path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")

    url = f"http://{host}/photo/upload"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Upload failed with HTTP {resp.status}")


def setup_device_slideshow(host: str, only_gemini_photo: bool = True, enable_photo_theme: bool = True) -> None:
    if only_gemini_photo:
        raw = http_get(host, "/photo/list")
        listing = json.loads(raw.decode("utf-8"))
        for f in listing.get("files", []):
            name = f.get("name", "")
            want = 1 if name == PHOTO_NAME else 0
            cur = 1 if f.get("enabled", True) else 0
            if cur != want:
                http_get(host, f"/photo/toggle?name={urllib.parse.quote(name)}&state={want}")
    if enable_photo_theme:
        http_get(host, "/theme/toggle?id=2&state=1")


def load_last_hash() -> str:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("hash", "")
    except Exception:
        return ""


def save_last_hash(fp: str, stats: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"hash": fp, "updated_at": datetime.now().isoformat(), "today_total": stats["today_total"]}, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload Antigravity Gemini token usage dashboard to JUZIPi SD PRO.")
    parser.add_argument("--host", default=os.environ.get("GEMINI_CLOCK_HOST", DEFAULT_HOST), help="Device IP or hostname")
    parser.add_argument("--theme", choices=["pokemon", "dark"], default=os.environ.get("GEMINI_CLOCK_THEME", DEFAULT_THEME), help="Visual style")
    parser.add_argument("--force", action="store_true", help="Upload even if token stats have not changed")
    parser.add_argument("--preview", type=Path, help="Render JPEG to this local path without uploading")
    parser.add_argument("--setup-device", action="store_true", help="Enable Photo theme and keep only gemini.jpg active in slideshow")
    parser.add_argument("--gemini-only", action="store_true", help="Count only models with 'gemini' in their name")
    parser.add_argument("--json", action="store_true", help="Print collected stats as JSON")
    args = parser.parse_args()

    stats = collect_usage(gemini_only=args.gemini_only)
    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))

    svg = build_svg(stats, theme=args.theme)

    if args.preview:
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        render_jpg(svg, args.preview)
        print(f"Preview rendered: {args.preview} ({args.preview.stat().st_size} bytes)")
        return 0

    fp = data_fingerprint(stats, args.theme)
    if not args.force and fp == load_last_hash():
        print(f"No changes (today={fmt_tokens(stats['today_total'])}), skipping upload to save flash.")
        return 0

    with tempfile.TemporaryDirectory(prefix="gemini-clock-up-") as tmpdir:
        jpg_path = Path(tmpdir) / PHOTO_NAME
        render_jpg(svg, jpg_path)
        size = jpg_path.stat().st_size
        try:
            was_disabled = False
            if not args.setup_device:
                try:
                    listing = json.loads(http_get(args.host, "/photo/list").decode("utf-8"))
                    for f in listing.get("files", []):
                        if f.get("name") == PHOTO_NAME and f.get("enabled") is False:
                            was_disabled = True
                            break
                except Exception:
                    pass

            upload_photo(args.host, jpg_path, PHOTO_NAME)

            if args.setup_device:
                setup_device_slideshow(args.host, only_gemini_photo=True, enable_photo_theme=True)
            elif was_disabled:
                http_get(args.host, f"/photo/toggle?name={urllib.parse.quote(PHOTO_NAME)}&state=0")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"Device unreachable at {args.host}: {exc}", file=sys.stderr)
            return 1

    save_last_hash(fp, stats)
    print(f"Uploaded {PHOTO_NAME} ({size} bytes, theme={args.theme}) to http://{args.host}/ — today={fmt_tokens(stats['today_total'])}, 7d={fmt_tokens(stats['week_total'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
