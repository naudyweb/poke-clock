"""
Poké-Hub PC-as-Brain State Engine.
Owns all rendering, scheduling, priority rules (notify > pinned > spotify > rotation),
Gemini token tracking, weather caching, and instant frame pushing to DisplayDriver.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from drivers.base import DisplayDriver
from drivers.raw_frame_driver import RawFrameDriver
from drivers.stock_driver import StockFirmwareDriver
from hub.spotify_watcher import SpotifyWatcher, query_spotify_playing
from screens import (
    ANIM_STRIP_H,
    DAYS_SHORT_ES,
    build_clock_svg,
    build_gemini_svg,
    build_notify_svg,
    build_spotify_svg,
    build_weather_svg,
)

CONFIG_PATH = Path.home() / ".config" / "poke-hub" / "config.json"

DEFAULT_CONFIG = {
    "host": "192.168.18.26",
    "city": "Paracas",
    "weatherkey": os.environ.get("OPENWEATHER_API_KEY", ""),
    "trainer_name": "POKEGEAR",
    "palette": "gbc_classic",
    "auto_night_palette": False,
    "night_mode": False,
    "night_start": 23,
    "night_end": 7,
    "night_brightness": 18,
    "celsius": True,
    "hour12": True,
    "brightness": 95,
    "rotation": 0,
    "rotate_interval": 12,
    "spotify_priority": True,
    "animations": True,
    "sprites": {
        "clock_day": "bulbasaur",
        "clock_night": "gengar",
        "gemini": "pikachu",
        "spotify": "jigglypuff",
        "weather": "auto",
    },
    "enabled_cards": {
        "clock": True,
        "weather": True,
        "gemini": True,
    },
}

TRANSCRIPT_GLOBS = [
    str(Path.home() / ".gemini" / "antigravity-cli" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"),
    str(Path.home() / ".gemini" / "antigravity" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"),
    str(Path.home() / ".gemini" / "antigravity-ide" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"),
]

DAY_LABELS_ES = ["L", "M", "X", "J", "V", "S", "D"]


def render_svg_to_full_and_strip(svg_content: str, strip_h: int = ANIM_STRIP_H) -> tuple[bytes, bytes]:
    """Render a 240x240 SVG into both a full baseline JPEG and a top-strip partial JPEG."""
    with tempfile.TemporaryDirectory(prefix="poke-hub-") as tmpdir:
        tmp = Path(tmpdir)
        svg_path = tmp / "card.svg"
        png_path = tmp / "card.png"
        jpg_path = tmp / "card.jpg"
        strip_path = tmp / "strip.jpg"
        svg_path.write_text(svg_content, encoding="utf-8")
        subprocess.run(
            ["rsvg-convert", "-w", "240", "-h", "240", str(svg_path), "-o", str(png_path)],
            check=True,
        )
        full_data = b""
        strip_data = b""
        for quality in ("88", "80", "70"):
            subprocess.run(
                [
                    "magick",
                    str(png_path),
                    "-background", "#F8F8F0",
                    "-flatten",
                    "-interlace", "none",
                    "-sampling-factor", "4:2:0",
                    "-strip",
                    "-quality", quality,
                    "(", "+clone", "-crop", f"240x{strip_h}+0+0", "+repage", "-write", str(strip_path), "+delete", ")",
                    str(jpg_path),
                ],
                check=True,
            )
            full_data = jpg_path.read_bytes()
            strip_data = strip_path.read_bytes()
            if len(full_data) <= 23500 and len(strip_data) <= 23500:
                return full_data, strip_data
        return full_data, strip_data


def render_svg_to_jpg_bytes(svg_content: str) -> bytes:
    full_data, _ = render_svg_to_full_and_strip(svg_content)
    return full_data


def _parse_iso_local(ts: str, local_tz) -> datetime | None:
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
                    dt = _parse_iso_local(str(rec.get("created_at") or ""), local_tz)
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


def fetch_weather(city: str, api_key: str) -> dict | None:
    if not city or not api_key:
        return None
    local_now = datetime.now().astimezone()
    local_tz = local_now.tzinfo
    q = urllib.parse.quote(city)
    try:
        cur_url = f"https://api.openweathermap.org/data/2.5/weather?q={q}&appid={api_key}&units=metric&lang=es"
        with urllib.request.urlopen(cur_url, timeout=8) as resp:
            cur = json.loads(resp.read().decode("utf-8"))
        fc_url = f"https://api.openweathermap.org/data/2.5/forecast?q={q}&appid={api_key}&units=metric&lang=es&cnt=32"
        with urllib.request.urlopen(fc_url, timeout=8) as resp:
            fc_raw = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PokeHubEngine:
    def __init__(self, driver: DisplayDriver | None = None, config_path: Path = CONFIG_PATH, start_watchers: bool = True) -> None:
        self.config_path = config_path
        self.config = self._load_config()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._start_watchers = start_watchers

        self.driver: DisplayDriver = driver or self._detect_driver(self.config["host"])

        # Live runtime state
        self.pinned_card: str | None = None
        self.notify_state: dict | None = None  # {"title","message","pokemon","expires_at"}
        self.spotify_track: dict | None = None
        self.gemini_stats: dict | None = None
        self.weather_data: dict | None = None

        self.active_card: str = "clock"
        self.active_reason: str = "rotation"
        self._rotation_idx: int = 0
        self._last_rotate_ts: float = time.monotonic()
        self._last_weather_ts: float = 0.0
        self._last_gemini_ts: float = 0.0
        self._last_clock_hm: str = ""
        self._last_device_status: dict = {}

        # Rendered JPEG cache per card + pre-rendered PC-side animation bundles
        self._jpg_cache: dict[str, tuple[str, bytes]] = {}
        self._anim_cache: dict[str, list[dict]] = {}
        self._last_base_key: tuple[str, str] = ("", "")
        self._anim_idx: int = 0
        self._last_anim_ts: float = 0.0
        self._spotify_watcher: SpotifyWatcher | None = None
        self._worker_thread: threading.Thread | None = None

    def _detect_driver(self, host: str) -> DisplayDriver:
        raw = RawFrameDriver(host)
        if raw.ping():
            return raw
        stock = StockFirmwareDriver(host)
        if stock.ping():
            return stock
        # Default to RawFrameDriver (will auto-connect once ESP8266 boots)
        return raw

    def _load_config(self) -> dict:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            saved = json.loads(self.config_path.read_text(encoding="utf-8"))
            for k, v in saved.items():
                if k in ("enabled_cards", "sprites") and isinstance(v, dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception:
            pass
        return cfg

    def _save_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8")

    # ============================================================
    # Lifecycle
    # ============================================================
    def start(self) -> None:
        self._sync_offline_config()
        self.refresh_all_data()
        if self._start_watchers:
            self._spotify_watcher = SpotifyWatcher(self._on_spotify_event)
            self._spotify_watcher.start()
        self._worker_thread = threading.Thread(target=self._run_loop, name="poke-hub-engine", daemon=True)
        self._worker_thread.start()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._spotify_watcher:
            self._spotify_watcher.stop()

    # ============================================================
    # External Commands (REST API / CLI / Watchers)
    # ============================================================
    def _on_spotify_event(self, track: dict | None) -> None:
        with self._lock:
            changed = track != self.spotify_track
            self.spotify_track = track
            if track is not None:
                self._render_card_locked("spotify")
            else:
                self._jpg_cache.pop("spotify", None)
                self._anim_cache.pop("spotify", None)
                if self.pinned_card == "spotify":
                    self.pinned_card = None
        if changed:
            print(f"[spotify] {'Playing: ' + track['title'] + ' — ' + track['artist'] if track else 'Paused/Stopped'}", flush=True)
            self._wake.set()

    def show_card(self, card: str) -> dict:
        card = card.strip().lower()
        if card not in ("clock", "weather", "gemini", "spotify"):
            raise ValueError(f"Unknown card: {card}")
        with self._lock:
            if card == "spotify" and not self.spotify_track:
                self.spotify_track = query_spotify_playing()
                if not self.spotify_track:
                    raise ValueError("Spotify is not currently playing")
            self.notify_state = None
            self.pinned_card = card
            self._render_card_locked(card)
        return self._push_now(force=True)

    def set_auto(self) -> dict:
        with self._lock:
            self.pinned_card = None
            self.notify_state = None
            self._last_rotate_ts = time.monotonic()
        return self._push_now(force=True)

    def send_notify(self, title: str, message: str, pokemon: str = "pikachu", ttl: int = 12) -> dict:
        ttl = max(2, min(300, int(ttl)))
        with self._lock:
            self.notify_state = {
                "title": title,
                "message": message,
                "pokemon": pokemon,
                "expires_at": time.monotonic() + ttl,
                "ttl": ttl,
            }
            self._render_card_locked("notify")
        return self._push_now(force=True)

    def update_config(self, patch: dict) -> dict:
        re_render_weather = False
        re_render_all = False
        sync_offline = False
        with self._lock:
            if "enabled_cards" in patch and isinstance(patch["enabled_cards"], dict):
                for k in ("clock", "weather", "gemini"):
                    if k in patch["enabled_cards"]:
                        self.config["enabled_cards"][k] = bool(patch["enabled_cards"][k])
                if not any(self.config["enabled_cards"].values()):
                    self.config["enabled_cards"]["clock"] = True

            if "sprites" in patch and isinstance(patch["sprites"], dict):
                for k in ("clock_day", "clock_night", "gemini", "spotify", "weather"):
                    if k in patch["sprites"]:
                        self.config["sprites"][k] = str(patch["sprites"][k]).strip().lower()
                re_render_all = True

            for key in ("rotate_interval", "brightness", "rotation", "night_start", "night_end", "night_brightness"):
                if key in patch:
                    self.config[key] = int(patch[key])
            for key in ("spotify_priority", "animations", "celsius", "hour12", "auto_night_palette", "night_mode"):
                if key in patch:
                    self.config[key] = bool(patch[key])
                    if key in ("animations", "celsius", "hour12", "auto_night_palette"):
                        re_render_all = True
                    if key in ("celsius", "hour12"):
                        sync_offline = True
            for key in ("city", "weatherkey", "host", "palette", "trainer_name"):
                if key in patch:
                    val = str(patch[key]).strip()
                    if key == "trainer_name":
                        self.config[key] = val or "POKEGEAR"
                        re_render_all = True
                        sync_offline = True
                    elif val:
                        self.config[key] = val
                        if key in ("city", "weatherkey"):
                            re_render_weather = True
                            sync_offline = True
                        if key == "palette":
                            re_render_all = True

            self._save_config()

        if "brightness" in patch or "night_mode" in patch or "night_brightness" in patch:
            self._apply_effective_brightness()
        if "rotation" in patch:
            self.driver.set_rotation(self.config["rotation"])
        if "host" in patch:
            self.driver = self._detect_driver(self.config["host"])
            sync_offline = True
        if sync_offline:
            self._sync_offline_config()
        if re_render_weather:
            self._refresh_weather()
        if re_render_all:
            with self._lock:
                self._jpg_cache.clear()
                self._anim_cache.clear()
                self._last_base_key = ("", "")
                self._render_card_locked("clock")
                self._render_card_locked("weather")
                self._render_card_locked("gemini")
                if self.spotify_track:
                    self._render_card_locked("spotify")
            self._push_now(force=True)
        self._wake.set()
        return self.get_status()

    def _sync_offline_config(self) -> None:
        if isinstance(self.driver, RawFrameDriver):
            self.driver.sync_offline_config(
                city=str(self.config.get("city") or "Paracas"),
                weatherkey=str(self.config.get("weatherkey") or ""),
                celsius=bool(self.config.get("celsius", True)),
                hour12=bool(self.config.get("hour12", True)),
                trainer_name=str(self.config.get("trainer_name") or "POKEGEAR"),
            )

    def _in_night_window(self, now: datetime) -> bool:
        ns = int(self.config.get("night_start", 23))
        ne = int(self.config.get("night_end", 7))
        h = now.hour
        return (h >= ns or h < ne) if ns > ne else (ns <= h < ne)

    def _apply_effective_brightness(self) -> None:
        now = datetime.now().astimezone()
        if self.config.get("night_mode") and self._in_night_window(now):
            target = int(self.config.get("night_brightness", 18))
        else:
            target = int(self.config.get("brightness", 95))
        self.driver.set_brightness(target)

    def refresh_all_data(self) -> None:
        self._refresh_gemini()
        self._refresh_weather()
        with self._lock:
            if self._start_watchers:
                self.spotify_track = query_spotify_playing()
            self._render_card_locked("clock")
            if self.spotify_track:
                self._render_card_locked("spotify")
        self._wake.set()

    def get_preview(self, card: str) -> bytes | None:
        with self._lock:
            if card == "active":
                card = self.active_card
            if card not in self._jpg_cache:
                self._render_card_locked(card)
            item = self._jpg_cache.get(card)
            return item[1] if item else None

    def get_status(self) -> dict:
        with self._lock:
            notify_rem = 0
            if self.notify_state:
                notify_rem = max(0, int(round(self.notify_state["expires_at"] - time.monotonic())))
            return {
                "active_card": self.active_card,
                "active_reason": self.active_reason,
                "pinned_card": self.pinned_card,
                "driver": self.driver.name,
                "host": self.config["host"],
                "spotify": self.spotify_track,
                "gemini_today": (self.gemini_stats or {}).get("today_total", 0),
                "gemini_week": (self.gemini_stats or {}).get("week_total", 0),
                "weather": {
                    "city": (self.weather_data or {}).get("city", self.config["city"]),
                    "temp": (self.weather_data or {}).get("temp"),
                    "description": (self.weather_data or {}).get("description", ""),
                },
                "notify_remaining_s": notify_rem,
                "config": self.config,
                "device": self._last_device_status,
            }

    # ============================================================
    # Priority Resolution & Rendering
    # ============================================================
    def resolve_active_card_locked(self, now_mono: float) -> tuple[str, str]:
        # 1. Active notification (highest priority)
        if self.notify_state:
            if now_mono < self.notify_state["expires_at"]:
                return "notify", "notify"
            self.notify_state = None
            self._jpg_cache.pop("notify", None)
            self._anim_cache.pop("notify", None)

        # 2. Manually pinned screen
        if self.pinned_card:
            if self.pinned_card == "spotify" and not self.spotify_track:
                self.pinned_card = None
            else:
                return self.pinned_card, "pinned"

        # 3. Exclusive Spotify priority when playing
        if self.spotify_track and self.config.get("spotify_priority", True):
            return "spotify", "spotify"

        # 4. Auto-rotation managed by PC
        enabled = [c for c in ("clock", "weather", "gemini") if self.config["enabled_cards"].get(c, True)]
        if self.spotify_track and not self.config.get("spotify_priority", True):
            enabled.append("spotify")
        if not enabled:
            enabled = ["clock"]

        interval = max(3, int(self.config.get("rotate_interval", 12)))
        if len(enabled) > 1 and (now_mono - self._last_rotate_ts) >= interval:
            self._rotation_idx = (self._rotation_idx + 1) % len(enabled)
            self._last_rotate_ts = now_mono

        self._rotation_idx %= len(enabled)
        return enabled[self._rotation_idx], "rotation"

    def _animations_enabled(self) -> bool:
        # Animations run ONLY on the PC with RAM-stream firmware (never on flash-based stock firmware)
        return bool(self.config.get("animations", True)) and isinstance(self.driver, RawFrameDriver)

    @staticmethod
    def _anim_frame_count(card: str) -> int:
        return 6 if card == "spotify" else 4

    @staticmethod
    def _anim_interval(card: str) -> float:
        return 0.35 if card == "spotify" else 0.65

    def _build_card_svg_locked(self, card: str, now: datetime, anim_frame: int = 0) -> str | None:
        if card == "clock":
            return build_clock_svg(now, self.config, self.weather_data, anim_frame=anim_frame)
        if card == "weather":
            w = self.weather_data or {
                "city": self.config["city"], "temp": 22.0, "humidity": 75, "wind_ms": 3.0,
                "main": "Clouds", "description": "sin datos", "updated_hm": now.strftime("%H:%M"), "forecast": [],
            }
            return build_weather_svg(w, self.config, anim_frame=anim_frame)
        if card == "gemini":
            g = self.gemini_stats or collect_gemini_usage()
            self.gemini_stats = g
            return build_gemini_svg(g, self.config, anim_frame=anim_frame)
        if card == "spotify" and self.spotify_track:
            return build_spotify_svg(self.spotify_track, self.config, anim_frame=anim_frame)
        if card == "notify" and self.notify_state:
            return build_notify_svg(
                self.notify_state["title"],
                self.notify_state["message"],
                self.notify_state.get("pokemon", "pikachu"),
                self.config,
                anim_frame=anim_frame,
            )
        return None

    def _render_card_locked(self, card: str) -> tuple[str, bytes] | None:
        now = datetime.now().astimezone()
        if card == "clock":
            self._last_clock_hm = now.strftime("%Y-%m-%d %H:%M")

        count = self._anim_frame_count(card) if self._animations_enabled() else 1
        svgs: list[str] = []
        for idx in range(count):
            svg = self._build_card_svg_locked(card, now, anim_frame=idx)
            if not svg:
                return None
            svgs.append(svg)

        if len(svgs) == 1:
            rendered = [render_svg_to_full_and_strip(svgs[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(4, len(svgs))) as pool:
                rendered = list(pool.map(render_svg_to_full_and_strip, svgs))

        frames: list[dict] = []
        for full_jpg, strip_jpg in rendered:
            frames.append({
                "hash": _sha256(full_jpg),
                "full": full_jpg,
                "strip_hash": _sha256(strip_jpg),
                "strip": strip_jpg,
            })

        self._anim_cache[card] = frames
        self._jpg_cache[card] = (frames[0]["hash"], frames[0]["full"])
        return frames[0]["hash"], frames[0]["full"]

    def _refresh_gemini(self) -> None:
        stats = collect_gemini_usage()
        with self._lock:
            old = self.gemini_stats
            self.gemini_stats = stats
            self._last_gemini_ts = time.monotonic()
            changed = (
                old is None
                or old["today_total"] != stats["today_total"]
                or old["today_steps"] != stats["today_steps"]
                or old["today_date"] != stats["today_date"]
            )
            if changed or "gemini" not in self._jpg_cache:
                self._render_card_locked("gemini")

    def _refresh_weather(self) -> None:
        w = fetch_weather(self.config["city"], self.config["weatherkey"])
        with self._lock:
            self._last_weather_ts = time.monotonic()
            if w:
                self.weather_data = w
                self._render_card_locked("weather")
                self._render_card_locked("clock")

    def _push_now(self, force: bool = False) -> dict:
        now_mono = time.monotonic()
        with self._lock:
            card, reason = self.resolve_active_card_locked(now_mono)
            self.active_card = card
            self.active_reason = reason
            frames = self._anim_cache.get(card)
            if not frames:
                if not self._render_card_locked(card):
                    return {"ok": False, "error": "no frame"}
                frames = self._anim_cache.get(card) or []
            if not frames:
                return {"ok": False, "error": "no frame"}

            base_key = (card, frames[0]["hash"])
            need_full = force or (base_key != self._last_base_key)
            if need_full:
                frame = frames[0]
                self._anim_idx = 0
                self._jpg_cache[card] = (frame["hash"], frame["full"])
            else:
                if not self._animations_enabled() or len(frames) <= 1:
                    return {"ok": True, "card": card, "reason": reason, "skipped": True, "ms": 0}
                interval = self._anim_interval(card)
                if (now_mono - self._last_anim_ts) < interval:
                    return {"ok": True, "card": card, "reason": reason, "skipped": True, "ms": 0}
                self._anim_idx = (self._anim_idx + 1) % len(frames)
                frame = frames[self._anim_idx]
                self._jpg_cache[card] = (frame["hash"], frame["full"])

            use_partial = (not need_full) and bool(self._last_device_status.get("partial_frame", True))

        try:
            if need_full:
                res = self.driver.push_frame(card, frame["full"], frame["hash"], force=force)
                if not res.get("skipped"):
                    print(
                        f"[push] {card} ({reason}) -> {len(frame['full'])}B in {res.get('pc_ms', '?')}ms "
                        f"(esp={res.get('ms', '?')}ms)",
                        flush=True,
                    )
                with self._lock:
                    self._last_base_key = base_key
                    self._last_anim_ts = time.monotonic()
            elif use_partial and isinstance(self.driver, RawFrameDriver):
                res = self.driver.push_region(card, frame["strip"], frame["strip_hash"], x=0, y=0)
                with self._lock:
                    self._last_anim_ts = time.monotonic()
            else:
                res = self.driver.push_frame(card, frame["full"], frame["hash"], force=False)
                with self._lock:
                    self._last_anim_ts = time.monotonic()
            return {"ok": True, "card": card, "reason": reason, **res}
        except Exception as exc:
            print(f"[push] Error sending {card}: {exc}", flush=True)
            with self._lock:
                self._last_base_key = ("", "")
            return {"ok": False, "card": card, "error": str(exc)}

    def _run_loop(self) -> None:
        last_health_ts = 0.0
        while not self._stop.is_set():
            wait_s = 1.0
            if self._animations_enabled():
                with self._lock:
                    interval = self._anim_interval(self.active_card)
                    elapsed = time.monotonic() - self._last_anim_ts
                wait_s = max(0.05, min(1.0, interval - elapsed))
            self._wake.wait(timeout=wait_s)
            self._wake.clear()
            if self._stop.is_set():
                break

            now_mono = time.monotonic()
            now_dt = datetime.now().astimezone()

            # 1. Minute tick for clock + night brightness/palette
            cur_hm = now_dt.strftime("%Y-%m-%d %H:%M")
            if cur_hm != self._last_clock_hm:
                with self._lock:
                    self._render_card_locked("clock")
                    if self.config.get("auto_night_palette"):
                        for c in ("weather", "gemini", "spotify"):
                            self._jpg_cache.pop(c, None)
                            self._anim_cache.pop(c, None)
                if self.config.get("night_mode"):
                    self._apply_effective_brightness()

            # 2. Gemini check every 8s (fast local file scan)
            if (now_mono - self._last_gemini_ts) >= 8.0:
                self._refresh_gemini()

            # 3. Weather check every 15 min
            if (now_mono - self._last_weather_ts) >= 900.0:
                self._refresh_weather()

            # 4. Health check & auto-re-detect driver every 20s
            if (now_mono - last_health_ts) >= 20.0:
                last_health_ts = now_mono
                st = self.driver.get_status()
                self._last_device_status = st
                if not st.get("online"):
                    # Try re-detecting in case firmware was swapped
                    new_drv = self._detect_driver(self.config["host"])
                    if new_drv.name != self.driver.name:
                        self.driver = new_drv
                        print(f"[hub] Switched driver to {new_drv.name}", flush=True)
                elif isinstance(self.driver, RawFrameDriver) and st.get("frames", 1) == 0:
                    # Device just rebooted -> force re-push active frame & sync offline config!
                    self.driver.invalidate_cache()
                    self._last_base_key = ("", "")
                    self.driver.set_brightness(self.config["brightness"])
                    if self.config.get("rotation", 0):
                        self.driver.set_rotation(self.config["rotation"])
                    self._sync_offline_config()

            # 5. Push active frame or next PC-side animation strip
            self._push_now(force=False)
