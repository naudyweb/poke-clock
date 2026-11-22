#!/usr/bin/env python3
"""
Poké-Clock Unified Daemon for JUZIPi SD PRO (Smart Weather Clock)
Manages all 4 Pokémon Game Boy Color screens automatically:
  - poke_clock.jpg   (PokéGear Clock — updates once per minute when enabled)
  - poke_weather.jpg (Weather + 3-Day Forecast — updates every 15 min)
  - gemini.jpg       (Antigravity Gemini Tokens — updates when tokens change)
  - spotify.jpg      (Jigglypuff Now Playing — auto-enables ONLY while Spotify plays!)
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from screens import (
    DAYS_SHORT_ES,
    build_clock_svg,
    build_gemini_svg,
    build_spotify_svg,
    build_weather_svg,
    fmt_tokens,
)

DEFAULT_HOST = "192.168.18.26"
STATE_FILE = Path.home() / ".cache" / "gemini-clock" / "poke_state.json"

PHOTO_CLOCK = "poke_clock.jpg"
PHOTO_WEATHER = "poke_weather.jpg"
PHOTO_GEMINI = "gemini.jpg"
PHOTO_SPOTIFY = "spotify.jpg"
ALL_POKE_PHOTOS = {PHOTO_CLOCK, PHOTO_WEATHER, PHOTO_GEMINI, PHOTO_SPOTIFY}

TRANSCRIPT_GLOBS = [
    str(Path.home() / ".gemini" / "antigravity-cli" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"),
    str(Path.home() / ".gemini" / "antigravity" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"),
    str(Path.home() / ".gemini" / "antigravity-ide" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"),
]

DAY_LABELS_ES = ["L", "M", "X", "J", "V", "S", "D"]


# ============================================================
# Rendering & ESP8266 HTTP Helpers
# ============================================================
def render_jpg(svg_content: str, out_jpg: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="poke-clock-") as tmpdir:
        tmp = Path(tmpdir)
        svg_path = tmp / "card.svg"
        png_path = tmp / "card.png"
        svg_path.write_text(svg_content, encoding="utf-8")
        subprocess.run(
            ["rsvg-convert", "-w", "240", "-h", "240", str(svg_path), "-o", str(png_path)],
            check=True,
        )
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
    req = urllib.request.Request(f"http://{host}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_json(host: str, path: str, timeout: float = 5.0) -> dict:
    return json.loads(http_get(host, path, timeout=timeout).decode("utf-8"))


def upload_photo(host: str, jpg_path: Path, filename: str, timeout: float = 10.0) -> None:
    boundary = "----PokeClockBoundary7MA4YWxkTrZu0gW"
    data = jpg_path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        f"http://{host}/photo/upload",
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Upload failed HTTP {resp.status}")


def toggle_photo(host: str, filename: str, enabled: bool) -> None:
    state = 1 if enabled else 0
    http_get(host, f"/photo/toggle?name={urllib.parse.quote(filename)}&state={state}")


def toggle_theme(host: str, theme_id: int, enabled: bool) -> None:
    state = 1 if enabled else 0
    http_get(host, f"/theme/toggle?id={theme_id}&state={state}")


def upload_svg_card(host: str, filename: str, svg: str, preserve_disabled: bool = True, photo_map: dict[str, bool] | None = None) -> int:
    was_disabled = False
    if preserve_disabled and photo_map is not None and filename in photo_map:
        was_disabled = not photo_map[filename]

    with tempfile.TemporaryDirectory(prefix="poke-up-") as tmpdir:
        jpg_path = Path(tmpdir) / filename
        render_jpg(svg, jpg_path)
        size = jpg_path.stat().st_size
        upload_photo(host, jpg_path, filename)
        # Refresh ESP8266 in-memory file index so /photo/toggle immediately sees new files
        try:
            http_get_json(host, "/photo/list", timeout=4)
        except Exception:
            pass
        if was_disabled:
            toggle_photo(host, filename, False)
        return size


# ============================================================
# Data Collectors: Gemini, Weather, Spotify (MPRIS via busctl)
# ============================================================
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


def collect_gemini_usage() -> dict:
    local_now = datetime.now().astimezone()
    local_tz = local_now.tzinfo
    today = local_now.date()
    week_dates = [today - timedelta(days=i) for i in range(6, -1, -1)]
    week_set = set(week_dates)

    daily_total: dict = defaultdict(int)
    today_in = today_out = today_cache = today_steps = 0
    today_sessions: set[str] = set()
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
                    step_idx = rec.get("step_index")
                    if step_idx is not None:
                        key = (conv_id, int(step_idx))
                        if key in seen_steps:
                            continue
                        seen_steps.add(key)

                    step_total = it + ot + ct
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
        except OSError:
            continue

    today_total = today_in + today_out + today_cache
    week_total = sum(daily_total[d] for d in week_dates)
    denom = today_in + today_cache
    cache_pct = int(round((today_cache / denom) * 100)) if denom > 0 else 0

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


def fetch_weather(device_cfg: dict) -> dict | None:
    city = str(device_cfg.get("city") or "Paracas").strip()
    key = str(device_cfg.get("weatherkey") or "").strip()
    if not key:
        return None

    local_now = datetime.now().astimezone()
    local_tz = local_now.tzinfo
    q = urllib.parse.quote(city)

    try:
        cur_url = f"https://api.openweathermap.org/data/2.5/weather?q={q}&appid={key}&units=metric&lang=es"
        with urllib.request.urlopen(cur_url, timeout=8) as resp:
            cur = json.loads(resp.read().decode("utf-8"))

        fc_url = f"https://api.openweathermap.org/data/2.5/forecast?q={q}&appid={key}&units=metric&lang=es&cnt=32"
        with urllib.request.urlopen(fc_url, timeout=8) as resp:
            fc_raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"[weather] Fetch failed: {exc}", file=sys.stderr)
        return None

    # Aggregate next 3 calendar days
    today = local_now.date()
    by_day: dict = defaultdict(lambda: {"temps": [], "mains": []})
    for item in fc_raw.get("list", []):
        dt = datetime.fromtimestamp(int(item["dt"]), tz=timezone.utc).astimezone(local_tz)
        d = dt.date()
        if d <= today:
            continue
        by_day[d]["temps"].append(float(item["main"]["temp"]))
        if item.get("weather"):
            by_day[d]["mains"].append(str(item["weather"][0].get("main", "Clouds")))

    forecast = []
    for d in sorted(by_day.keys())[:3]:
        temps = by_day[d]["temps"]
        mains = by_day[d]["mains"]
        # Pick dominant condition (prefer Rain if present)
        if any("rain" in m.lower() or "thunder" in m.lower() for m in mains):
            dom_main = "Rain"
        elif mains:
            dom_main = max(set(mains), key=mains.count)
        else:
            dom_main = "Clouds"
        forecast.append({
            "label": DAYS_SHORT_ES[d.weekday()],
            "min": min(temps),
            "max": max(temps),
            "main": dom_main,
        })

    w0 = (cur.get("weather") or [{}])[0]
    return {
        "city": cur.get("name") or city,
        "temp": float(cur["main"]["temp"]),
        "humidity": int(cur["main"]["humidity"]),
        "wind_ms": float((cur.get("wind") or {}).get("speed", 0)),
        "main": str(w0.get("main", "Clouds")),
        "description": str(w0.get("description", "")),
        "updated_hm": local_now.strftime("%H:%M"),
        "forecast": forecast,
    }


def _busctl_json(args: list[str]) -> dict | list | None:
    try:
        res = subprocess.run(
            ["busctl", "--user", "--json=short", *args],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None
        return json.loads(res.stdout)
    except Exception:
        return None


def get_spotify_playing() -> dict | None:
    """
    Detect active Spotify playback via MPRIS D-Bus (both native Spotify desktop app
    and Spotify Web Player in Brave/Chromium/Firefox).
    Returns {"title", "artist", "album"} ONLY when PlaybackStatus == "Playing", else None.
    """
    names_data = _busctl_json(["list"])
    if not isinstance(names_data, list):
        return None

    mpris_players = [
        entry["name"]
        for entry in names_data
        if isinstance(entry, dict) and str(entry.get("name", "")).startswith("org.mpris.MediaPlayer2.")
    ]
    # Check native Spotify first
    mpris_players.sort(key=lambda n: (0 if "spotify" in n.lower() else 1, n))

    for dest in mpris_players:
        status_obj = _busctl_json([
            "get-property", dest, "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player", "PlaybackStatus",
        ])
        if not isinstance(status_obj, dict) or status_obj.get("data") != "Playing":
            continue

        meta_obj = _busctl_json([
            "get-property", dest, "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player", "Metadata",
        ])
        meta = meta_obj.get("data") if isinstance(meta_obj, dict) else None
        if not isinstance(meta, dict):
            continue

        def get_str(k: str) -> str:
            v = meta.get(k)
            if isinstance(v, dict):
                d = v.get("data")
                if isinstance(d, list):
                    return ", ".join(str(x) for x in d if x)
                return str(d or "")
            return ""

        is_native_spotify = "spotify" in dest.lower()
        url = get_str("xesam:url").lower()
        art = get_str("mpris:artUrl").lower()
        trackid = get_str("mpris:trackid").lower()
        is_web_spotify = ("spotify.com" in url) or ("scdn.co" in art) or ("spotify" in trackid)

        if not (is_native_spotify or is_web_spotify):
            continue

        title = get_str("xesam:title").strip()
        artist = get_str("xesam:artist").strip()
        album = get_str("xesam:album").strip()
        if not title and not artist:
            continue

        return {"title": title, "artist": artist, "album": album}

    return None


def stable_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ============================================================
# Daemon & CLI Entrypoints
# ============================================================
def run_setup_all(host: str) -> int:
    print(f"[setup] Configuring all Pokémon GBC screens on http://{host}/ ...")
    device_cfg = http_get_json(host, "/config")
    now = datetime.now().astimezone()
    weather = fetch_weather(device_cfg)
    gemini = collect_gemini_usage()

    # 1. Upload Clock, Weather, Gemini
    clock_svg = build_clock_svg(now, device_cfg, weather)
    s1 = upload_svg_card(host, PHOTO_CLOCK, clock_svg, preserve_disabled=False)
    print(f"[setup] Uploaded {PHOTO_CLOCK} ({s1} bytes)")

    if weather:
        weather_svg = build_weather_svg(weather, device_cfg)
        s2 = upload_svg_card(host, PHOTO_WEATHER, weather_svg, preserve_disabled=False)
        print(f"[setup] Uploaded {PHOTO_WEATHER} ({s2} bytes)")

    gemini_svg = build_gemini_svg(gemini)
    s3 = upload_svg_card(host, PHOTO_GEMINI, gemini_svg, preserve_disabled=False)
    print(f"[setup] Uploaded {PHOTO_GEMINI} ({s3} bytes)")

    # 2. Check Spotify right now (upload placeholder only if playing, else disable if present)
    track = get_spotify_playing()
    if track:
        sp_svg = build_spotify_svg(track)
        s4 = upload_svg_card(host, PHOTO_SPOTIFY, sp_svg, preserve_disabled=False)
        print(f"[setup] Spotify playing -> uploaded {PHOTO_SPOTIFY} ({s4} bytes)")

    # 3. Enable Photo theme (id=2) and disable all non-Pokémon factory themes
    toggle_theme(host, 2, True)
    themes = http_get_json(host, "/theme/list").get("themes", [])
    for t in themes:
        tid = int(t["id"])
        if tid != 2 and t.get("enabled"):
            toggle_theme(host, tid, False)

    # 4. Enable our 3 active Pokémon cards (+ spotify only if playing), disable factory photos
    listing = http_get_json(host, "/photo/list")
    want_on = {PHOTO_CLOCK, PHOTO_WEATHER, PHOTO_GEMINI}
    if track:
        want_on.add(PHOTO_SPOTIFY)
    for f in listing.get("files", []):
        name = f.get("name", "")
        want = name in want_on
        cur = f.get("enabled", True) is not False
        if cur != want:
            toggle_photo(host, name, want)

    state = load_state()
    state["clock_hm"] = now.strftime("%Y-%m-%d %H:%M")
    state["gemini_hash"] = stable_hash({k: gemini[k] for k in gemini if k != "updated_hm"})
    if weather:
        state["weather_hash"] = stable_hash({k: weather[k] for k in weather if k != "updated_hm"})
    state["spotify_hash"] = stable_hash(track) if track else ""
    state["spotify_active"] = bool(track)
    save_state(state)
    print("[setup] Done! All active screens on the device are now 100% Pokémon GBC.")
    return 0


def run_preview_all(out_dir: Path, host: str) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        device_cfg = http_get_json(host, "/config", timeout=3)
    except Exception:
        device_cfg = {"city": "Paracas", "celsius": True, "hour12": True}

    now = datetime.now().astimezone()
    weather = fetch_weather(device_cfg) or {
        "city": "Paracas", "temp": 24.5, "humidity": 83, "wind_ms": 5.6,
        "main": "Clouds", "description": "nubes dispersas", "updated_hm": now.strftime("%H:%M"),
        "forecast": [
            {"label": "DOM", "min": 18, "max": 25, "main": "Clear"},
            {"label": "LUN", "min": 17, "max": 23, "main": "Clouds"},
            {"label": "MAR", "min": 16, "max": 21, "main": "Rain"},
        ],
    }
    gemini = collect_gemini_usage()
    track = get_spotify_playing() or {
        "title": "Pokemon Theme (Gotta Catch 'Em All)",
        "artist": "Jason Paige",
        "album": "Pokemon 2.B.A. Master",
    }

    cards = [
        ("preview_clock.jpg", build_clock_svg(now, device_cfg, weather)),
        ("preview_weather.jpg", build_weather_svg(weather, device_cfg)),
        ("preview_gemini.jpg", build_gemini_svg(gemini)),
        ("preview_spotify.jpg", build_spotify_svg(track)),
    ]
    for fname, svg in cards:
        p = out_dir / fname
        render_jpg(svg, p)
        print(f"Rendered {p} ({p.stat().st_size} bytes)")
    return 0


def run_daemon(host: str) -> int:
    running = True

    def _stop(signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    state = load_state()
    device_cfg: dict = {}
    cached_weather: dict | None = None
    last_cfg_ts = 0.0
    last_weather_ts = 0.0
    last_gemini_ts = 0.0
    last_clock_check_hm = ""
    spotify_active_on_device: bool | None = None

    print(f"[daemon] Poké-Clock daemon started for http://{host}/ (poll=3s)", flush=True)

    while running:
        loop_start = time.monotonic()
        now_ts = time.time()
        local_now = datetime.now().astimezone()

        try:
            # Refresh device config every 5 minutes
            if not device_cfg or (now_ts - last_cfg_ts) >= 300:
                device_cfg = http_get_json(host, "/config", timeout=4)
                last_cfg_ts = now_ts

            # ---- 1. SPOTIFY (every 3s — instant enable on Play, instant disable on Pause/Stop) ----
            track = get_spotify_playing()
            if track:
                track_hash = stable_hash(track)
                if track_hash != state.get("spotify_hash"):
                    svg = build_spotify_svg(track)
                    size = upload_svg_card(host, PHOTO_SPOTIFY, svg, preserve_disabled=False)
                    state["spotify_hash"] = track_hash
                    save_state(state)
                    print(f"[spotify] Playing: {track['title']} — {track['artist']} ({size}B)", flush=True)
                    spotify_active_on_device = None  # confirm toggle below

                if spotify_active_on_device is not True:
                    # Ensure ESP8266 index includes spotify.jpg before toggling
                    http_get_json(host, "/photo/list", timeout=4)
                    toggle_photo(host, PHOTO_SPOTIFY, True)
                    toggle_theme(host, 2, True)
                    spotify_active_on_device = True
                    state["spotify_active"] = True
                    save_state(state)
                    print("[spotify] Enabled spotify.jpg on screen", flush=True)
            else:
                if spotify_active_on_device is not False:
                    listing = http_get_json(host, "/photo/list", timeout=4)
                    for f in listing.get("files", []):
                        if f.get("name") == PHOTO_SPOTIFY and f.get("enabled", True) is not False:
                            toggle_photo(host, PHOTO_SPOTIFY, False)
                            print("[spotify] Paused/Stopped -> disabled spotify.jpg", flush=True)
                            break
                    spotify_active_on_device = False
                    state["spotify_active"] = False
                    save_state(state)

            # ---- 2. WEATHER (every 15 min) ----
            if cached_weather is None or (now_ts - last_weather_ts) >= 900:
                w = fetch_weather(device_cfg)
                if w:
                    cached_weather = w
                    last_weather_ts = now_ts
                    w_hash = stable_hash({k: w[k] for k in w if k != "updated_hm"})
                    if w_hash != state.get("weather_hash"):
                        listing = http_get_json(host, "/photo/list", timeout=4)
                        photo_map = {f["name"]: (f.get("enabled", True) is not False) for f in listing.get("files", [])}
                        if photo_map.get(PHOTO_WEATHER, True):
                            svg = build_weather_svg(w, device_cfg)
                            size = upload_svg_card(host, PHOTO_WEATHER, svg, preserve_disabled=True, photo_map=photo_map)
                            state["weather_hash"] = w_hash
                            save_state(state)
                            print(f"[weather] Updated {PHOTO_WEATHER} ({w['city']} {w['temp']:.1f}C, {size}B)", flush=True)

            # ---- 3. POKÉGEAR CLOCK (once per minute, ONLY when enabled on device) ----
            cur_hm = local_now.strftime("%Y-%m-%d %H:%M")
            if cur_hm != last_clock_check_hm:
                last_clock_check_hm = cur_hm
                if cur_hm != state.get("clock_hm"):
                    listing = http_get_json(host, "/photo/list", timeout=4)
                    photo_map = {f["name"]: (f.get("enabled", True) is not False) for f in listing.get("files", [])}
                    if photo_map.get(PHOTO_CLOCK, True):
                        svg = build_clock_svg(local_now, device_cfg, cached_weather)
                        size = upload_svg_card(host, PHOTO_CLOCK, svg, preserve_disabled=True, photo_map=photo_map)
                        state["clock_hm"] = cur_hm
                        save_state(state)
                        print(f"[clock] Updated {PHOTO_CLOCK} ({local_now.strftime('%H:%M')}, {size}B)", flush=True)

            # ---- 4. GEMINI TOKENS (every 30s, ONLY when token counts change) ----
            if (now_ts - last_gemini_ts) >= 30:
                last_gemini_ts = now_ts
                gemini = collect_gemini_usage()
                g_hash = stable_hash({k: gemini[k] for k in gemini if k != "updated_hm"})
                if g_hash != state.get("gemini_hash"):
                    listing = http_get_json(host, "/photo/list", timeout=4)
                    photo_map = {f["name"]: (f.get("enabled", True) is not False) for f in listing.get("files", [])}
                    svg = build_gemini_svg(gemini)
                    size = upload_svg_card(host, PHOTO_GEMINI, svg, preserve_disabled=True, photo_map=photo_map)
                    state["gemini_hash"] = g_hash
                    save_state(state)
                    print(f"[gemini] Updated {PHOTO_GEMINI} (today={fmt_tokens(gemini['today_total'])}, {size}B)", flush=True)

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[daemon] Device/network transient error: {exc}", file=sys.stderr, flush=True)
            time.sleep(5)
            continue
        except Exception as exc:
            print(f"[daemon] Unexpected error: {exc}", file=sys.stderr, flush=True)

        elapsed = time.monotonic() - loop_start
        sleep_for = max(0.5, 3.0 - elapsed)
        time.sleep(sleep_for)

    print("[daemon] Stopped cleanly.", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Pokémon GBC Unified Display Daemon for JUZIPi SD PRO")
    parser.add_argument("--host", default=os.environ.get("GEMINI_CLOCK_HOST", DEFAULT_HOST))
    parser.add_argument("--setup-all", action="store_true", help="Upload all Pokémon cards and switch device to 100%% Pokémon mode")
    parser.add_argument("--preview-all", type=Path, help="Render all 4 Pokémon cards into this directory")
    parser.add_argument("--daemon", action="store_true", help="Run continuous background sync daemon")
    args = parser.parse_args()

    if args.preview_all:
        return run_preview_all(args.preview_all, args.host)
    if args.setup_all:
        return run_setup_all(args.host)
    if args.daemon:
        return run_daemon(args.host)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
