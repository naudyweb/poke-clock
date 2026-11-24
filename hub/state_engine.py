"""
Poké-Hub PC-as-Brain State Engine.
Owns all rendering, scheduling, priority rules (notify > pinned > spotify > rotation),
desktop notification queue/filtering, Gemini token tracking, weather caching,
and instant frame pushing to DisplayDriver.
"""

from __future__ import annotations

import copy
import glob
import hashlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from drivers.base import DisplayDriver
from drivers.raw_frame_driver import RawFrameDriver
from drivers.stock_driver import StockFirmwareDriver
from hub.desktop_notify_watcher import VALID_POKEMON, DesktopNotifyWatcher
from hub.render_engine import BACKEND_NAME, render_svg_to_full_and_strip, render_svg_to_jpg
from hub.spotify_watcher import SpotifyWatcher, query_spotify_playing
from screens import (
    ANIM_STRIP_H,
    DAYS_SHORT_ES,
    OFFLINE_PALETTE_ORDER,
    OFFLINE_POKEMON_ORDER,
    build_clock_svg,
    build_gemini_svg,
    build_notify_svg,
    build_spotify_svg,
    build_weather_svg,
    resolve_palette,
    resolve_palette_bg,
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
    "desktop_notify_enabled": True,
    "desktop_notify_ttl": 12,
    "desktop_notify_dnd_night": False,
    "desktop_notify_pokemon": "pikachu",
    "desktop_notify_filter_mode": "ignore",
    "desktop_notify_apps": ["spotify", "poke-hub", "osd", "volume", "brightness"],
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
    "gemini_daily_limit": 40_000_000,
}

TRANSCRIPT_GLOBS = [
    str(Path.home() / ".gemini" / "antigravity-cli" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"),
    str(Path.home() / ".gemini" / "antigravity" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"),
    str(Path.home() / ".gemini" / "antigravity-ide" / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl"),
]

DAY_LABELS_ES = ["L", "M", "X", "J", "V", "S", "D"]

_BACKOFF_SCHEDULE = (5.0, 15.0, 30.0)
_WEATHER_BACKOFF_SCHEDULE = (30.0, 60.0, 120.0)
_BRIGHTNESS_SAVE_DEBOUNCE_S = 1.5
_GEMINI_RETENTION_S = 8 * 86400

_GEMINI_CACHE_LOCK = threading.Lock()
# Maps realpath -> (st_mtime_ns, st_size, parsed_buckets)
_GEMINI_FILE_CACHE: dict[str, tuple[int, int, dict]] = {}


def render_svg_to_jpg_bytes(svg_content: str, bg_hex: str = "#F8F8F0") -> bytes:
    """Compatibility helper for rendering a single 240x240 JPEG frame."""
    return render_svg_to_jpg(svg_content, bg_hex=bg_hex)


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


def _parse_gemini_file(path: str, local_tz, tz_offset) -> dict:
    conv_id = Path(path).parents[2].name
    by_date: dict = {}
    seen_steps: set[int] = set()
    has_quota_error = False

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                continue

            # Detect quota/limit errors in any record
            status = str(rec.get("status", "")).upper()
            content = str(rec.get("content", "")).lower()
            thinking = str(rec.get("thinking", "")).lower()
            rec_text = content + " " + thinking
            if (
                status == "ERROR"
                or "quota" in rec_text
                or "429" in rec_text
                or ("limit" in rec_text and ("exceed" in rec_text or "reached" in rec_text or "exceeded" in rec_text))
                or ("token" in rec_text and ("limit" in rec_text or "exceed" in rec_text or "out" in rec_text))
            ):
                has_quota_error = True

            if '"input_tokens"' not in line and '"output_tokens"' not in line:
                continue
            it = int(rec.get("input_tokens") or 0)
            ot = int(rec.get("output_tokens") or 0)
            ct = int(rec.get("cache_read_tokens") or 0)
            if it == 0 and ot == 0 and ct == 0:
                continue
            step_idx = rec.get("step_index")
            if step_idx is not None:
                s_idx = int(step_idx)
                if s_idx in seen_steps:
                    continue
                seen_steps.add(s_idx)

            dt = _parse_iso_local(str(rec.get("created_at") or ""), local_tz)
            if dt is None:
                continue
            d = dt.date()
            step_total = it + ot + ct
            prev = by_date.get(d)
            if prev is None:
                by_date[d] = [step_total, it, ot, ct, 1]
            else:
                prev[0] += step_total
                prev[1] += it
                prev[2] += ot
                prev[3] += ct
                prev[4] += 1

    frozen_dates = {d: tuple(vals) for d, vals in by_date.items()}
    return {
        "conv_id": conv_id,
        "tz_offset": tz_offset,
        "by_date": frozen_dates,
        "has_quota_error": has_quota_error,
    }


def collect_gemini_usage() -> dict:
    local_now = datetime.now().astimezone()
    local_tz = local_now.tzinfo
    tz_offset = local_now.utcoffset()
    today = local_now.date()
    week_dates = [today - timedelta(days=i) for i in range(6, -1, -1)]
    week_set = set(week_dates)

    now_ts = time.time()
    cutoff_mtime = now_ts - _GEMINI_RETENTION_S

    daily_total: dict = defaultdict(int)
    today_in = today_out = today_cache = today_steps = 0
    today_sessions: set[str] = set()
    any_quota_error = False

    files: set[str] = set()
    for pattern in TRANSCRIPT_GLOBS:
        for path in glob.glob(pattern):
            files.add(os.path.realpath(path))

    with _GEMINI_CACHE_LOCK:
        stale = [k for k in _GEMINI_FILE_CACHE if k not in files]
        for k in stale:
            del _GEMINI_FILE_CACHE[k]

    for path in sorted(files):
        try:
            st = os.stat(path)
        except OSError:
            with _GEMINI_CACHE_LOCK:
                _GEMINI_FILE_CACHE.pop(path, None)
            continue

        if st.st_mtime < cutoff_mtime:
            with _GEMINI_CACHE_LOCK:
                _GEMINI_FILE_CACHE.pop(path, None)
            continue

        with _GEMINI_CACHE_LOCK:
            cached = _GEMINI_FILE_CACHE.get(path)
            if (
                cached is not None
                and cached[0] == st.st_mtime_ns
                and cached[1] == st.st_size
                and cached[2].get("tz_offset") == tz_offset
            ):
                buckets = cached[2]
            else:
                buckets = None

        if buckets is None:
            try:
                buckets = _parse_gemini_file(path, local_tz, tz_offset)
            except OSError:
                continue
            with _GEMINI_CACHE_LOCK:
                _GEMINI_FILE_CACHE[path] = (st.st_mtime_ns, st.st_size, buckets)

        by_date = buckets["by_date"]
        for d in week_set:
            row = by_date.get(d)
            if row is not None:
                daily_total[d] += row[0]

        # Track quota errors from transcripts
        if buckets.get("has_quota_error"):
            any_quota_error = True

        today_row = by_date.get(today)
        if today_row is not None and today_row[4] > 0:
            today_in += today_row[1]
            today_out += today_row[2]
            today_cache += today_row[3]
            today_steps += today_row[4]
            today_sessions.add(buckets["conv_id"])

    today_total = today_in + today_out + today_cache
    week_total = sum(daily_total[d] for d in week_dates)
    denom = today_in + today_cache
    cache_pct = int(round((today_cache / denom) * 100)) if denom > 0 else 0

    # Daily reset: next midnight
    tomorrow = today + timedelta(days=1)
    midnight_tomorrow = datetime.combine(tomorrow, datetime.min.time(), tzinfo=local_tz)
    daily_reset_delta = midnight_tomorrow - local_now
    daily_reset_h = int(daily_reset_delta.total_seconds() // 3600)
    daily_reset_m = int((daily_reset_delta.total_seconds() % 3600) // 60)
    daily_reset_str = f"{daily_reset_h:02d}:{daily_reset_m:02d}"

    # Weekly reset: next Monday midnight (week ends Sunday 23:59)
    # weekday(): Monday=0, Sunday=6. Days until next Monday:
    days_until_monday = (7 - local_now.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7  # if today is Monday, reset is next Monday
    next_monday = today + timedelta(days=days_until_monday)
    midnight_monday = datetime.combine(next_monday, datetime.min.time(), tzinfo=local_tz)
    weekly_reset_delta = midnight_monday - local_now
    weekly_reset_total_h = int(weekly_reset_delta.total_seconds() // 3600)
    weekly_reset_m = int((weekly_reset_delta.total_seconds() % 3600) // 60)
    if weekly_reset_total_h >= 24:
        w_days = weekly_reset_total_h // 24
        w_hours = weekly_reset_total_h % 24
        weekly_reset_str = f"{w_days}d{w_hours:02d}h"
    else:
        weekly_reset_str = f"{weekly_reset_total_h:02d}:{weekly_reset_m:02d}"

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
        "quota_error": any_quota_error,
        "daily_reset": daily_reset_str,
        "weekly_reset": weekly_reset_str,
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
        "query_city": city,
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


def _resolve_weather_icon(weather_data: dict | None, now: datetime) -> int | None:
    """Map weather condition to firmware WeatherIconIndex (0=sun,1=cloud,2=rain,3=storm,4=moon)."""
    if not weather_data:
        return None
    is_night = now.hour >= 19 or now.hour < 6
    cond_raw = str(
        weather_data.get("cond")
        or weather_data.get("main")
        or weather_data.get("description")
        or ""
    ).strip().lower()
    if not cond_raw:
        return None
    if any(k in cond_raw for k in ("thunder", "storm", "tormenta", "rayo")):
        return 3
    if any(k in cond_raw for k in ("rain", "drizzle", "lluv", "chubasco", "aguacero")):
        return 2
    if any(k in cond_raw for k in ("cloud", "nub", "mist", "fog", "haze", "niebl", "brum", "overcast", "cubierto", "snow")):
        return 1
    if any(k in cond_raw for k in ("clear", "sun", "despej", "claro", "sol")):
        return 4 if is_night else 0
    return 1


class PokeHubEngine:
    def __init__(self, driver: DisplayDriver | None = None, config_path: Path = CONFIG_PATH, start_watchers: bool = True) -> None:
        self.config_path = config_path
        self.config = self._load_config()
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._start_watchers = start_watchers

        self._external_driver = driver is not None
        self._needs_driver_detect = driver is None
        self.driver: DisplayDriver = driver if driver is not None else RawFrameDriver(self.config["host"])

        # Live runtime state
        self.pinned_card: str | None = None
        self.notify_state: dict | None = None
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
        self._last_night_state: bool | None = None

        # Notification FIFO queue & history
        self._notify_queue: deque[dict] = deque(maxlen=8)
        self._notify_history: deque[dict] = deque(maxlen=12)

        # Offline backoff & debounced config persistence
        self._offline_failures: int = 0
        self._next_push_retry_mono: float = 0.0
        self._config_dirty_at: float | None = None

        # Weather retry backoff
        self._weather_fail_count: int = 0

        # Rendered JPEG cache per card + pre-rendered PC-side animation bundles
        self._jpg_cache: dict[str, tuple[str, bytes]] = {}
        self._anim_cache: dict[str, list[dict]] = {}
        self._card_gen: dict[str, int] = {}
        self._last_base_key: tuple[str, str] = ("", "")
        self._last_active_frame_hash: str = ""
        self._frame_seq: int = 0
        self._anim_idx: int = 0
        self._last_anim_ts: float = 0.0

        self._render_pool: ThreadPoolExecutor | None = (
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="poke-render")
            if BACKEND_NAME != "in_memory"
            else None
        )
        self._spotify_watcher: SpotifyWatcher | None = None
        self._desktop_watcher: DesktopNotifyWatcher | None = None
        self._worker_thread: threading.Thread | None = None

    def _detect_driver(self, host: str) -> DisplayDriver:
        raw = RawFrameDriver(host)
        if raw.ping():
            return raw
        raw._close_conn()
        stock = StockFirmwareDriver(host)
        if stock.ping():
            return stock
        # Default to RawFrameDriver (will auto-connect once ESP8266 boots)
        return raw

    def _load_config(self) -> dict:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        try:
            saved = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for k, v in saved.items():
                    if k in ("enabled_cards", "sprites") and isinstance(v, dict):
                        cfg[k].update(v)
                    elif k == "desktop_notify_apps" and isinstance(v, list):
                        cfg[k] = [str(x).strip().lower() for x in v if str(x).strip()]
                    else:
                        cfg[k] = v
        except Exception:
            pass
        # Fallback: si weatherkey está vacío, intentar variable de entorno
        if not cfg.get("weatherkey"):
            cfg["weatherkey"] = os.environ.get("OPENWEATHER_API_KEY", "")
        return cfg

    def _save_config(self) -> None:
        with self._lock:
            payload = json.dumps(self.config, indent=2, ensure_ascii=False)
            self._config_dirty_at = None
        tmp_path = self.config_path.with_suffix(".tmp")
        with self._save_lock:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, self.config_path)

    def _flush_config_if_dirty(self) -> None:
        with self._lock:
            if self._config_dirty_at is None:
                return
        self._save_config()

    # ============================================================
    # Lifecycle
    # ============================================================
    def start(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._stop.clear()
        if self._start_watchers:
            self._spotify_watcher = SpotifyWatcher(self._on_spotify_event)
            self._spotify_watcher.start()
            default_poke = str(self.config.get("desktop_notify_pokemon") or "pikachu").strip().lower()
            self._desktop_watcher = DesktopNotifyWatcher(
                self._on_desktop_notification,
                default_pokemon=default_poke,
            )
            self._desktop_watcher.start()
        self._worker_thread = threading.Thread(target=self._run_loop, name="poke-hub-engine", daemon=True)
        self._worker_thread.start()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._spotify_watcher:
            self._spotify_watcher.stop()
        if self._desktop_watcher:
            self._desktop_watcher.stop()

        worker = self._worker_thread
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2.5)

        try:
            self._flush_config_if_dirty()
        except Exception:
            pass

        if self._render_pool is not None:
            try:
                self._render_pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._render_pool = None

        with self._lock:
            drv = self.driver
            ext_drv = self._external_driver
        if isinstance(drv, RawFrameDriver):
            if not ext_drv:
                drv.enter_standby()
            drv._close_conn()

    # ============================================================
    # External Commands (REST API / CLI / Watchers)
    # ============================================================
    def _on_spotify_event(self, track: dict | None) -> None:
        snapshot = None
        with self._lock:
            changed = track != self.spotify_track
            self.spotify_track = track
            if track is not None:
                if changed or "spotify" not in self._jpg_cache:
                    self._invalidate_cards_locked("spotify")
                    snapshot = self._snapshot_for_render_locked("spotify")
            else:
                self._invalidate_cards_locked("spotify")
                if self.pinned_card == "spotify":
                    self.pinned_card = None
        if snapshot is not None:
            self._render_and_store("spotify", snapshot)
        if changed:
            print(
                f"[spotify] {'Playing: ' + track['title'] + ' — ' + track['artist'] if track else 'Paused/Stopped'}",
                flush=True,
            )
            self._wake.set()

    def _on_desktop_notification(self, event: dict) -> None:
        now_dt = datetime.now().astimezone()
        with self._lock:
            if not self.config.get("desktop_notify_enabled", True):
                return
            if self.config.get("desktop_notify_dnd_night", False) and self._in_night_window(now_dt):
                return
            mode = str(self.config.get("desktop_notify_filter_mode") or "ignore").strip().lower()
            raw_apps = self.config.get("desktop_notify_apps") or []
            apps = [str(a).strip().lower() for a in raw_apps if str(a).strip()]
            ttl = max(2, min(300, int(self.config.get("desktop_notify_ttl", 12))))
            default_poke = str(self.config.get("desktop_notify_pokemon") or "pikachu").strip().lower()

        app_name = str(event.get("app_name") or "").strip()
        badge = str(event.get("badge") or "").strip()
        app_low = app_name.lower()
        badge_low = badge.lower()
        matched = any((pat in app_low) or (pat in badge_low) for pat in apps)

        if mode == "allow":
            if not matched:
                return
        elif matched:
            return

        pokemon = str(event.get("pokemon") or default_poke or "pikachu").strip().lower()
        if pokemon not in VALID_POKEMON:
            pokemon = default_poke if default_poke in VALID_POKEMON else "pikachu"

        self.send_notify(
            title=str(event.get("title") or "AVISO"),
            message=str(event.get("message") or ""),
            pokemon=pokemon,
            ttl=ttl,
            app_name=badge or app_name,
            source="desktop",
            replaces_id=int(event.get("replaces_id") or 0),
        )

    def show_card(self, card: str) -> dict:
        card = card.strip().lower()
        if card not in ("clock", "weather", "gemini", "spotify"):
            raise ValueError(f"Unknown card: {card}")
        if card == "spotify":
            with self._lock:
                has_track = bool(self.spotify_track)
            if not has_track:
                track = query_spotify_playing()
                if not track:
                    raise ValueError("Spotify is not currently playing")
                with self._lock:
                    if not self.spotify_track:
                        self.spotify_track = track
        with self._lock:
            self.notify_state = None
            self._notify_queue.clear()
            self._invalidate_cards_locked("notify")
            self.pinned_card = card
            self._invalidate_cards_locked(card)
            snapshot = self._snapshot_for_render_locked(card)
        if snapshot is not None:
            self._render_and_store(card, snapshot)
        res = self._push_now(force=True, manual=True)
        self._wake.set()
        return res

    def set_auto(self) -> dict:
        with self._lock:
            self.pinned_card = None
            self.notify_state = None
            self._notify_queue.clear()
            self._invalidate_cards_locked("notify")
            self._last_rotate_ts = time.monotonic()
        res = self._push_now(force=True, manual=True)
        self._wake.set()
        return res

    def _record_notify_history_locked(self, item: dict, now_dt: datetime) -> None:
        self._notify_history.appendleft({
            "title": str(item.get("title") or ""),
            "message": str(item.get("message") or ""),
            "pokemon": str(item.get("pokemon") or "pikachu"),
            "app_name": str(item.get("app_name") or ""),
            "source": str(item.get("source") or "manual"),
            "ts": time.time(),
            "hm": now_dt.strftime("%H:%M"),
        })

    def send_notify(
        self,
        title: str,
        message: str = "",
        pokemon: str = "pikachu",
        ttl: int = 12,
        app_name: str = "",
        source: str = "manual",
        replaces_id: int = 0,
    ) -> dict:
        ttl = max(2, min(300, int(ttl)))
        src = (source or "manual").strip().lower()
        app_clean = (app_name or "").strip()
        rep_id = max(0, int(replaces_id or 0))
        now_mono = time.monotonic()
        now_dt = datetime.now().astimezone()

        snapshot = None
        with self._lock:
            has_active = self.notify_state is not None and now_mono < self.notify_state["expires_at"]
            if src == "desktop" and has_active:
                active_rep = int((self.notify_state or {}).get("replaces_id") or 0)
                if not (rep_id > 0 and rep_id == active_rep):
                    if rep_id > 0:
                        for q_item in self._notify_queue:
                            if q_item.get("replaces_id") == rep_id:
                                q_item.update({
                                    "title": title,
                                    "message": message,
                                    "pokemon": pokemon,
                                    "ttl": ttl,
                                    "app_name": app_clean,
                                    "source": src,
                                })
                                self._record_notify_history_locked(q_item, now_dt)
                                self._wake.set()
                                return {
                                    "ok": True,
                                    "queued": True,
                                    "replaced_queued": True,
                                    "queue_len": len(self._notify_queue),
                                }

                    last_item = self._notify_queue[-1] if self._notify_queue else self.notify_state
                    if last_item and (
                        last_item.get("title"),
                        last_item.get("message"),
                        last_item.get("app_name", ""),
                    ) == (title, message, app_clean):
                        return {
                            "ok": True,
                            "queued": False,
                            "duplicate": True,
                            "queue_len": len(self._notify_queue),
                        }

                    item = {
                        "title": title,
                        "message": message,
                        "pokemon": pokemon,
                        "ttl": ttl,
                        "app_name": app_clean,
                        "source": src,
                        "replaces_id": rep_id,
                    }
                    self._notify_queue.append(item)
                    self._record_notify_history_locked(item, now_dt)
                    self._wake.set()
                    return {"ok": True, "queued": True, "queue_len": len(self._notify_queue)}

            self.notify_state = {
                "title": title,
                "message": message,
                "pokemon": pokemon,
                "expires_at": now_mono + ttl,
                "ttl": ttl,
                "app_name": app_clean,
                "source": src,
                "replaces_id": rep_id,
            }
            self._record_notify_history_locked(self.notify_state, now_dt)
            self._invalidate_cards_locked("notify")
            self._last_base_key = ("", "")
            snapshot = self._snapshot_for_render_locked("notify")

        if snapshot is not None:
            self._render_and_store("notify", snapshot)
        res = self._push_now(force=True, manual=(src != "desktop"))
        self._wake.set()
        return res

    def test_desktop_notify(self) -> dict:
        """Simulate a mail notification immediately for Web UI testing."""
        with self._lock:
            ttl = max(2, min(300, int(self.config.get("desktop_notify_ttl", 12))))
        return self.send_notify(
            title="Correo de prueba",
            message="Conexion PC -> Pantalla OK",
            pokemon="pikachu",
            ttl=ttl,
            app_name="MAIL",
            source="manual",
        )

    def set_brightness(self, level: int) -> dict:
        """Set display brightness immediately with debounced config.json persistence."""
        val = max(0, min(100, int(level)))
        with self._lock:
            self.config["brightness"] = val
            self._config_dirty_at = time.monotonic()
        self._apply_effective_brightness()
        self._wake.set()
        return self.get_status()

    def update_config(self, patch: dict) -> dict:
        re_render_weather = False
        re_render_all = False
        sync_offline = False
        new_host: str | None = None
        new_rot: int | None = None
        new_default_poke: str | None = None

        with self._lock:
            if "enabled_cards" in patch and isinstance(patch["enabled_cards"], dict):
                for k in ("clock", "weather", "gemini"):
                    if k in patch["enabled_cards"]:
                        self.config["enabled_cards"][k] = bool(patch["enabled_cards"][k])
                if not any(self.config["enabled_cards"].values()):
                    self.config["enabled_cards"]["clock"] = True

            if "sprites" in patch and isinstance(patch["sprites"], dict):
                for k in ("clock_day", "clock_night", "clock", "gemini", "spotify", "weather"):
                    if k in patch["sprites"]:
                        self.config["sprites"][k] = str(patch["sprites"][k]).strip().lower()
                re_render_all = True
                sync_offline = True

            for key in ("rotate_interval", "brightness", "rotation", "night_start", "night_end", "night_brightness", "desktop_notify_ttl"):
                if key in patch:
                    val_int = int(patch[key])
                    if key == "desktop_notify_ttl":
                        val_int = max(2, min(300, val_int))
                    elif key in ("brightness", "night_brightness"):
                        val_int = max(0, min(100, val_int))
                    elif key in ("night_start", "night_end"):
                        val_int = val_int % 24
                    self.config[key] = val_int
                    if key == "rotation":
                        new_rot = val_int
                    if key in ("night_start", "night_end") and self.config.get("auto_night_palette"):
                        re_render_all = True
                        sync_offline = True

            for key in (
                "spotify_priority",
                "animations",
                "celsius",
                "hour12",
                "auto_night_palette",
                "night_mode",
                "desktop_notify_enabled",
                "desktop_notify_dnd_night",
            ):
                if key in patch:
                    self.config[key] = bool(patch[key])
                    if key in ("animations", "celsius", "hour12", "auto_night_palette"):
                        re_render_all = True
                    if key in ("celsius", "hour12", "auto_night_palette"):
                        sync_offline = True

            for key in ("city", "weatherkey", "host", "palette", "trainer_name", "desktop_notify_pokemon", "desktop_notify_filter_mode"):
                if key in patch:
                    val = str(patch[key]).strip()
                    if key == "trainer_name":
                        self.config[key] = val or "POKEGEAR"
                        re_render_all = True
                        sync_offline = True
                    elif key == "desktop_notify_pokemon":
                        poke = val.lower()
                        if poke in VALID_POKEMON:
                            self.config[key] = poke
                            new_default_poke = poke
                    elif key == "desktop_notify_filter_mode":
                        self.config[key] = "allow" if val.lower() == "allow" else "ignore"
                    elif val:
                        self.config[key] = val
                        if key in ("city", "weatherkey"):
                            re_render_weather = True
                            sync_offline = True
                        if key == "palette":
                            re_render_all = True
                            sync_offline = True
                        if key == "host":
                            new_host = val

            if "desktop_notify_apps" in patch:
                raw_apps = patch["desktop_notify_apps"]
                if isinstance(raw_apps, str):
                    items = [s.strip().lower() for s in raw_apps.split(",") if s.strip()]
                elif isinstance(raw_apps, list):
                    items = [str(s).strip().lower() for s in raw_apps if str(s).strip()]
                else:
                    items = []
                self.config["desktop_notify_apps"] = items

            if any(k in patch for k in ("auto_night_palette", "palette", "night_start", "night_end")):
                self._last_night_state = self._in_night_window(datetime.now().astimezone())

            worker_alive = self._worker_thread is not None and self._worker_thread.is_alive()
            brightness_only_debounce = (
                set(patch.keys()) == {"brightness"}
                and worker_alive
            )
            if brightness_only_debounce:
                self._config_dirty_at = time.monotonic()

            desktop_watcher = self._desktop_watcher

        if not brightness_only_debounce:
            self._save_config()

        if new_default_poke and desktop_watcher is not None:
            with desktop_watcher._lock:
                desktop_watcher._default_pokemon = new_default_poke

        if "brightness" in patch or "night_mode" in patch or "night_brightness" in patch or "night_start" in patch or "night_end" in patch:
            self._apply_effective_brightness()
        if new_rot is not None:
            with self._lock:
                drv = self.driver
            drv.set_rotation(new_rot)
        if new_host is not None:
            detected = self._detect_driver(new_host)
            with self._lock:
                old_drv = self.driver
                self.driver = detected
                self._needs_driver_detect = False
                self._offline_failures = 0
                self._next_push_retry_mono = 0.0
                self._last_base_key = ("", "")
            if isinstance(old_drv, RawFrameDriver) and old_drv is not detected:
                old_drv._close_conn()
            sync_offline = True
        if sync_offline:
            self._sync_offline_config()
        if re_render_weather:
            self._refresh_weather()
        if re_render_all:
            with self._lock:
                self._invalidate_cards_locked()
                self._last_base_key = ("", "")
                active = self.active_card
                snap = self._snapshot_for_render_locked(active)
            if snap is not None:
                self._render_and_store(active, snap)
            self._push_now(force=True, manual=True)
        self._wake.set()
        return self.get_status()

    def _resolved_offline_palette(self, cfg: dict, now: datetime) -> str:
        pal_name = str(cfg.get("palette") or "gbc_classic").strip().lower()
        if cfg.get("auto_night_palette") and self._in_night_window(now):
            pal_name = "gbc_dark"
        if pal_name not in OFFLINE_PALETTE_ORDER:
            return "gbc_classic"
        return pal_name

    def _resolved_offline_pokemon(self, cfg: dict, now: datetime) -> str:
        sprites = cfg.get("sprites") or {}
        is_clock_night = now.hour >= 19 or now.hour < 6
        poke_key = "clock_night" if is_clock_night else "clock_day"
        default_poke = "gengar" if is_clock_night else "bulbasaur"
        poke = str(sprites.get("clock") or sprites.get(poke_key) or default_poke).strip().lower()
        if poke not in OFFLINE_POKEMON_ORDER:
            return "pikachu"
        return poke

    def _sync_offline_config(self, include_weather: bool = True) -> None:
        now = datetime.now().astimezone()
        with self._lock:
            drv = self.driver
            if not isinstance(drv, RawFrameDriver):
                return
            cfg = copy.deepcopy(self.config)
            weather_data = self.weather_data

        city = str(cfg.get("city") or "Paracas")
        weather_snapshot = None
        if include_weather and weather_data:
            w_city = str(weather_data.get("query_city") or weather_data.get("city") or "")
            if not w_city or w_city.strip().lower() == city.strip().lower():
                weather_snapshot = weather_data

        pokemon = self._resolved_offline_pokemon(cfg, now)
        palette = self._resolved_offline_palette(cfg, now)
        weather_icon = _resolve_weather_icon(weather_snapshot, now)

        drv.sync_offline_config(
            city=city,
            weatherkey=str(cfg.get("weatherkey") or ""),
            celsius=bool(cfg.get("celsius", True)),
            hour12=bool(cfg.get("hour12", True)),
            trainer_name=str(cfg.get("trainer_name") or "POKEGEAR"),
            weather=weather_snapshot,
            pokemon=pokemon,
            palette=palette,
            weather_icon=weather_icon,
        )

    def _in_night_window(self, now: datetime) -> bool:
        ns = int(self.config.get("night_start", 23))
        ne = int(self.config.get("night_end", 7))
        h = now.hour
        return (h >= ns or h < ne) if ns > ne else (ns <= h < ne)

    def _apply_effective_brightness(self) -> None:
        now = datetime.now().astimezone()
        with self._lock:
            drv = self.driver
            if self.config.get("night_mode") and self._in_night_window(now):
                target = int(self.config.get("night_brightness", 18))
            else:
                target = int(self.config.get("brightness", 95))
        drv.set_brightness(target)

    def refresh_all_data(self) -> None:
        self._refresh_gemini()
        self._refresh_weather()
        if self._start_watchers:
            track = query_spotify_playing()
            with self._lock:
                if track != self.spotify_track:
                    self.spotify_track = track
                    self._invalidate_cards_locked("spotify")
        with self._lock:
            active = self.active_card
            snap = None if active in self._jpg_cache else self._snapshot_for_render_locked(active)
        if snap is not None:
            self._render_and_store(active, snap)
        self._wake.set()

    def get_preview_with_etag(self, card: str) -> tuple[bytes, str]:
        card_key = (card or "").strip().lower()
        with self._lock:
            if card_key == "active":
                card_key = self.active_card
            item = self._jpg_cache.get(card_key)
            if item is not None:
                return item[1], item[0]
            snapshot = self._snapshot_for_render_locked(card_key)

        if snapshot is None:
            return b"", ""

        rendered = self._render_and_store(card_key, snapshot)
        if rendered is not None:
            return rendered[1], rendered[0]

        with self._lock:
            item = self._jpg_cache.get(card_key)
            if item is not None:
                return item[1], item[0]
        return b"", ""

    def get_preview(self, card: str) -> bytes | None:
        jpg, _ = self.get_preview_with_etag(card)
        return jpg or None

    def get_status(self) -> dict:
        with self._lock:
            notify_rem = 0
            if self.notify_state:
                notify_rem = max(0, int(round(self.notify_state["expires_at"] - time.monotonic())))
            watcher = self._desktop_watcher
            desktop_status = None
            payload = {
                "active_card": self.active_card,
                "active_reason": self.active_reason,
                "pinned_card": self.pinned_card,
                "driver": self.driver.name,
                "host": self.config["host"],
                "spotify": copy.deepcopy(self.spotify_track),
                "gemini_today": (self.gemini_stats or {}).get("today_total", 0),
                "gemini_week": (self.gemini_stats or {}).get("week_total", 0),
                "weather": {
                    "city": (self.weather_data or {}).get("city", self.config["city"]),
                    "temp": (self.weather_data or {}).get("temp"),
                    "description": (self.weather_data or {}).get("description", ""),
                },
                "notify_remaining_s": notify_rem,
                "notify_queue_len": len(self._notify_queue),
                "notify_history": list(self._notify_history),
                "render_backend": BACKEND_NAME,
                "frame_seq": self._frame_seq,
                "config": copy.deepcopy(self.config),
                "device": dict(self._last_device_status),
            }
        if watcher is not None:
            desktop_status = watcher.get_status()
        else:
            desktop_status = {
                "running": False,
                "backend": "disabled",
                "dnd_active": False,
                "received_count": 0,
                "dropped_dnd_count": 0,
            }
        payload["desktop_notify"] = desktop_status
        return payload

    # ============================================================
    # Priority Resolution & Rendering
    # ============================================================
    def resolve_active_card_locked(self, now_mono: float) -> tuple[str, str]:
        # 1. Active notification (highest priority), advancing FIFO queue on expiry
        if self.notify_state:
            if now_mono < self.notify_state["expires_at"]:
                return "notify", "notify"
            self.notify_state = None
            self._invalidate_cards_locked("notify")
            if self._notify_queue:
                nxt = self._notify_queue.popleft()
                self.notify_state = {
                    **nxt,
                    "expires_at": now_mono + nxt["ttl"],
                }
                self._last_base_key = ("", "")
                return "notify", "notify"
        elif self._notify_queue:
            nxt = self._notify_queue.popleft()
            self.notify_state = {
                **nxt,
                "expires_at": now_mono + nxt["ttl"],
            }
            self._invalidate_cards_locked("notify")
            self._last_base_key = ("", "")
            return "notify", "notify"

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

    def _invalidate_cards_locked(self, *cards: str) -> None:
        targets = cards or ("clock", "weather", "gemini", "spotify", "notify")
        for c in targets:
            self._jpg_cache.pop(c, None)
            self._anim_cache.pop(c, None)
            self._card_gen[c] = self._card_gen.get(c, 0) + 1

    def _snapshot_for_render_locked(self, card: str) -> dict | None:
        if card not in ("clock", "weather", "gemini", "spotify", "notify"):
            return None
        if card == "spotify" and not self.spotify_track:
            return None
        if card == "notify" and not self.notify_state:
            return None

        now = datetime.now().astimezone()
        cfg = copy.deepcopy(self.config)
        # Keep resolve_palette reference valid (no-op check) and compute background color
        _ = resolve_palette(cfg, now)
        bg_hex = resolve_palette_bg(cfg, now)
        count = self._anim_frame_count(card) if self._animations_enabled() else 1

        return {
            "card": card,
            "gen": self._card_gen.get(card, 0),
            "now": now,
            "clock_hm": now.strftime("%Y-%m-%d %H:%M"),
            "cfg": cfg,
            "bg_hex": bg_hex,
            "count": count,
            "weather_data": copy.deepcopy(self.weather_data),
            "gemini_stats": copy.deepcopy(self.gemini_stats),
            "spotify_track": copy.deepcopy(self.spotify_track),
            "notify_state": copy.deepcopy(self.notify_state),
        }

    def _build_card_svg_from_snapshot(
        self,
        card: str,
        snapshot: dict,
        anim_frame: int = 0,
    ) -> str | None:
        cfg = snapshot["cfg"]
        now = snapshot["now"]
        if card == "clock":
            return build_clock_svg(now, cfg, snapshot["weather_data"], anim_frame=anim_frame)
        if card == "weather":
            w = snapshot["weather_data"] or {
                "city": cfg["city"],
                "temp": 22.0,
                "humidity": 75,
                "wind_ms": 3.0,
                "main": "Clouds",
                "description": "sin datos",
                "updated_hm": now.strftime("%H:%M"),
                "forecast": [],
            }
            return build_weather_svg(w, cfg, anim_frame=anim_frame, now=now)
        if card == "gemini":
            g = snapshot["gemini_stats"]
            if g is None:
                g = collect_gemini_usage()
                snapshot["gemini_stats"] = g
            # Ensure out_of_tokens flag is present for older cached stats
            if "out_of_tokens" not in g:
                daily_limit = int(cfg.get("gemini_daily_limit", 0))
                if daily_limit > 0 and g["today_total"] >= daily_limit:
                    g["out_of_tokens"] = True
                elif g.get("quota_error"):
                    g["out_of_tokens"] = True
                else:
                    g["out_of_tokens"] = False
            return build_gemini_svg(g, cfg, anim_frame=anim_frame, now=now)
        if card == "spotify" and snapshot["spotify_track"]:
            return build_spotify_svg(snapshot["spotify_track"], cfg, anim_frame=anim_frame, now=now)
        if card == "notify" and snapshot["notify_state"]:
            n = snapshot["notify_state"]
            return build_notify_svg(
                n["title"],
                n["message"],
                n.get("pokemon", "pikachu"),
                cfg,
                anim_frame=anim_frame,
                app_name=str(n.get("app_name") or ""),
                now=now,
            )
        return None

    def _render_card_unlocked(self, card_id: str, snapshot: dict) -> tuple[list[dict], dict | None] | None:
        """Build SVGs and render full+strip JPEGs strictly outside self._lock."""
        count = int(snapshot["count"])
        bg_hex = str(snapshot["bg_hex"])

        svgs: list[str] = []
        for idx in range(count):
            svg = self._build_card_svg_from_snapshot(card_id, snapshot, anim_frame=idx)
            if not svg:
                return None
            svgs.append(svg)

        if len(svgs) == 1 or BACKEND_NAME == "in_memory" or self._render_pool is None:
            rendered = [render_svg_to_full_and_strip(svg, bg_hex=bg_hex) for svg in svgs]
        else:
            try:
                rendered = list(
                    self._render_pool.map(
                        lambda s: render_svg_to_full_and_strip(s, bg_hex=bg_hex),
                        svgs,
                    )
                )
            except RuntimeError:
                rendered = [render_svg_to_full_and_strip(svg, bg_hex=bg_hex) for svg in svgs]

        frames: list[dict] = []
        for full_jpg, strip_jpg in rendered:
            frames.append({
                "hash": _sha256(full_jpg),
                "full": full_jpg,
                "strip_hash": _sha256(strip_jpg),
                "strip": strip_jpg,
            })
        return frames, snapshot.get("gemini_stats")

    def _render_and_store(self, card: str, snapshot: dict) -> tuple[str, bytes] | None:
        result = self._render_card_unlocked(card, snapshot)
        if result is None:
            return None
        frames, computed_gemini = result
        with self._lock:
            if card == "gemini" and self.gemini_stats is None and computed_gemini is not None:
                self.gemini_stats = computed_gemini
            if self._card_gen.get(card, 0) != snapshot["gen"]:
                return None
            self._anim_cache[card] = frames
            self._jpg_cache[card] = (frames[0]["hash"], frames[0]["full"])
            if card == "clock":
                self._last_clock_hm = snapshot["clock_hm"]
            if self.active_card == card and frames[0]["hash"] != self._last_active_frame_hash:
                self._last_active_frame_hash = frames[0]["hash"]
                self._frame_seq += 1
            return frames[0]["hash"], frames[0]["full"]

    def _render_card_locked(self, card: str) -> tuple[str, bytes] | None:
        """Compatibility helper when callers expect synchronous render for a card."""
        with self._lock:
            snap = self._snapshot_for_render_locked(card)
        if snap is None:
            return None
        return self._render_and_store(card, snap)

    def _refresh_gemini(self) -> None:
        stats = collect_gemini_usage()
        # Compute out_of_tokens: quota error in logs OR daily limit exceeded
        daily_limit = int(self.config.get("gemini_daily_limit", 0))
        if daily_limit > 0 and stats["today_total"] >= daily_limit:
            stats["out_of_tokens"] = True
        elif stats.get("quota_error"):
            stats["out_of_tokens"] = True
        else:
            stats["out_of_tokens"] = False
        snap = None
        with self._lock:
            old = self.gemini_stats
            self.gemini_stats = stats
            self._last_gemini_ts = time.monotonic()
            changed = (
                old is None
                or old["today_total"] != stats["today_total"]
                or old["today_steps"] != stats["today_steps"]
                or old["today_date"] != stats["today_date"]
                or old.get("out_of_tokens") != stats.get("out_of_tokens")
                or old.get("quota_error") != stats.get("quota_error")
            )
            if changed:
                self._invalidate_cards_locked("gemini")
            if (changed or "gemini" not in self._jpg_cache) and self.active_card == "gemini":
                snap = self._snapshot_for_render_locked("gemini")
        if snap is not None:
            self._render_and_store("gemini", snap)

    def _refresh_weather(self) -> dict | None:
        with self._lock:
            city = str(self.config["city"])
            api_key = str(self.config["weatherkey"])
        w = fetch_weather(city, api_key)
        snap = None
        active = ""
        with self._lock:
            if w:
                self._last_weather_ts = time.monotonic()
                self._weather_fail_count = 0
                self.weather_data = w
                self._invalidate_cards_locked("weather", "clock")
                active = self.active_card
                if active in ("weather", "clock"):
                    snap = self._snapshot_for_render_locked(active)
            else:
                self._weather_fail_count += 1
        if snap is not None:
            self._render_and_store(active, snap)
        if w:
            self._sync_offline_config(include_weather=True)
        return w

    def _push_now(self, force: bool = False, manual: bool | None = None) -> dict:
        reset_backoff = force if manual is None else manual
        now_mono = time.monotonic()

        for _ in range(2):
            with self._lock:
                if reset_backoff:
                    self._offline_failures = 0
                    self._next_push_retry_mono = 0.0

                prev_card = self.active_card
                card, reason = self.resolve_active_card_locked(now_mono)
                self.active_card = card
                self.active_reason = reason
                frames = self._anim_cache.get(card)
                snapshot = None if frames else self._snapshot_for_render_locked(card)

            if not frames:
                if snapshot is None:
                    return {"ok": False, "error": "no frame"}
                rendered = self._render_and_store(card, snapshot)
                with self._lock:
                    frames = self._anim_cache.get(card)
                    if frames:
                        card_now, reason = self.resolve_active_card_locked(now_mono)
                        if card_now != card:
                            continue
                        self.active_card = card
                        self.active_reason = reason
                if not frames:
                    if rendered is None:
                        continue
                    return {"ok": False, "error": "no frame"}

            with self._lock:
                base_key = (card, frames[0]["hash"])
                need_full = force or (card != prev_card) or (base_key != self._last_base_key)
                if need_full:
                    frame = frames[0]
                    self._anim_idx = 0
                else:
                    if not self._animations_enabled() or len(frames) <= 1:
                        return {"ok": True, "card": card, "reason": reason, "skipped": True, "ms": 0}
                    interval = self._anim_interval(card)
                    if (now_mono - self._last_anim_ts) < interval:
                        return {"ok": True, "card": card, "reason": reason, "skipped": True, "ms": 0}
                    self._anim_idx = (self._anim_idx + 1) % len(frames)
                    frame = frames[self._anim_idx]

                self._jpg_cache[card] = (frame["hash"], frame["full"])
                if frame["hash"] != self._last_active_frame_hash or card != prev_card:
                    self._last_active_frame_hash = frame["hash"]
                    self._frame_seq += 1

                if not reset_backoff and now_mono < self._next_push_retry_mono:
                    return {
                        "ok": False,
                        "card": card,
                        "reason": reason,
                        "error": "offline backoff",
                        "backoff": True,
                    }

                drv = self.driver
                use_partial = (not need_full) and bool(self._last_device_status.get("partial_frame", True))
            break
        else:
            return {"ok": False, "error": "no frame"}

        try:
            if need_full:
                res = drv.push_frame(card, frame["full"], frame["hash"], force=force)
                if not res.get("skipped"):
                    print(
                        f"[push] {card} ({reason}) -> {len(frame['full'])}B in {res.get('pc_ms', '?')}ms "
                        f"(esp={res.get('ms', '?')}ms)",
                        flush=True,
                    )
                with self._lock:
                    self._last_base_key = base_key
                    self._last_anim_ts = time.monotonic()
                    self._offline_failures = 0
                    self._next_push_retry_mono = 0.0
            elif use_partial and isinstance(drv, RawFrameDriver):
                res = drv.push_region(card, frame["strip"], frame["strip_hash"], x=0, y=0)
                with self._lock:
                    self._last_anim_ts = time.monotonic()
                    self._offline_failures = 0
                    self._next_push_retry_mono = 0.0
            else:
                res = drv.push_frame(card, frame["full"], frame["hash"], force=False)
                with self._lock:
                    self._last_anim_ts = time.monotonic()
                    self._offline_failures = 0
                    self._next_push_retry_mono = 0.0
            return {"ok": True, "card": card, "reason": reason, **res}
        except Exception as exc:
            print(f"[push] Error sending {card}: {exc}", flush=True)
            with self._lock:
                self._last_base_key = ("", "")
                self._offline_failures += 1
                delay = _BACKOFF_SCHEDULE[min(self._offline_failures - 1, len(_BACKOFF_SCHEDULE) - 1)]
                self._next_push_retry_mono = time.monotonic() + delay
            return {"ok": False, "card": card, "error": str(exc)}

    def _run_loop(self) -> None:
        last_health_ts = 0.0

        # Non-blocking startup network bootstrap
        with self._lock:
            needs_detect = self._needs_driver_detect and not self._external_driver
            host = str(self.config.get("host") or "192.168.18.26")
            self._needs_driver_detect = False
        if needs_detect and not self._stop.is_set():
            detected = self._detect_driver(host)
            old_drv: DisplayDriver | None = None
            with self._lock:
                if str(self.config.get("host") or "") == host and detected.name != self.driver.name:
                    old_drv = self.driver
                    self.driver = detected
            if isinstance(old_drv, RawFrameDriver) and old_drv is not detected:
                old_drv._close_conn()

        if not self._stop.is_set():
            self._refresh_gemini()
        if not self._stop.is_set():
            w = self._refresh_weather()
            if not w:
                self._sync_offline_config(include_weather=False)
        if not self._stop.is_set():
            with self._lock:
                night_mode = bool(self.config.get("night_mode"))
                self._last_night_state = self._in_night_window(datetime.now().astimezone())
            if night_mode:
                self._apply_effective_brightness()
        if not self._stop.is_set():
            self._push_now(force=False)

        while not self._stop.is_set():
            now_m = time.monotonic()
            wait_s = 1.0
            with self._lock:
                in_backoff = now_m < self._next_push_retry_mono
                if self.notify_state is not None:
                    rem = max(0.05, self.notify_state["expires_at"] - now_m)
                    wait_s = min(wait_s, rem)
                if not in_backoff and self._animations_enabled():
                    interval = self._anim_interval(self.active_card)
                    elapsed = now_m - self._last_anim_ts
                    wait_s = min(wait_s, max(0.05, interval - elapsed))
                if self._config_dirty_at is not None:
                    dirty_rem = max(0.05, _BRIGHTNESS_SAVE_DEBOUNCE_S - (now_m - self._config_dirty_at))
                    wait_s = min(wait_s, dirty_rem)

            self._wake.wait(timeout=wait_s)
            self._wake.clear()
            if self._stop.is_set():
                break

            now_mono = time.monotonic()
            now_dt = datetime.now().astimezone()

            # 0. Debounced config flush (e.g. brightness slider)
            should_flush_cfg = False
            with self._lock:
                if self._config_dirty_at is not None and (now_mono - self._config_dirty_at) >= _BRIGHTNESS_SAVE_DEBOUNCE_S:
                    should_flush_cfg = True
            if should_flush_cfg:
                try:
                    self._flush_config_if_dirty()
                except Exception:
                    pass

            # 1. Minute tick + night transition check (only invalidate all on transition)
            cur_hm = now_dt.strftime("%Y-%m-%d %H:%M")
            in_night = self._in_night_window(now_dt)
            snap_clock = None
            sync_night_pal = False
            apply_night_bri = False

            with self._lock:
                prev_night = self._last_night_state
                if prev_night is None:
                    self._last_night_state = in_night
                elif in_night != prev_night:
                    self._last_night_state = in_night
                    if self.config.get("auto_night_palette"):
                        self._invalidate_cards_locked()
                        sync_night_pal = True
                    if self.config.get("night_mode"):
                        apply_night_bri = True

                if cur_hm != self._last_clock_hm:
                    self._last_clock_hm = cur_hm
                    self._invalidate_cards_locked("clock")
                    if self.active_card == "clock" and self.config["enabled_cards"].get("clock", True):
                        snap_clock = self._snapshot_for_render_locked("clock")

            if snap_clock is not None:
                self._render_and_store("clock", snap_clock)
            if apply_night_bri:
                self._apply_effective_brightness()
            if sync_night_pal:
                self._sync_offline_config(include_weather=True)

            # 2. Gemini check every 8s (incremental stat-cached scan)
            if (now_mono - self._last_gemini_ts) >= 8.0:
                self._refresh_gemini()

            # 3. Weather check — use backoff on failures, 15 min on success
            with self._lock:
                weather_interval = (
                    _WEATHER_BACKOFF_SCHEDULE[min(self._weather_fail_count - 1, len(_WEATHER_BACKOFF_SCHEDULE) - 1)]
                    if self._weather_fail_count > 0
                    else 900.0
                )
            if (now_mono - self._last_weather_ts) >= weather_interval:
                self._refresh_weather()

            # 4. Health check & auto-re-detect driver every 20s
            if (now_mono - last_health_ts) >= 20.0:
                last_health_ts = now_mono
                with self._lock:
                    drv = self.driver
                    host = str(self.config["host"])
                    ext_drv = self._external_driver
                st = drv.get_status()
                with self._lock:
                    self._last_device_status = st
                    if st.get("online"):
                        self._offline_failures = 0
                        self._next_push_retry_mono = 0.0
                        # If weather data is missing, force a retry now that we're online
                        if self.weather_data is None:
                            self._last_weather_ts = 0.0
                if not st.get("online"):
                    if not ext_drv:
                        new_drv = self._detect_driver(host)
                        if new_drv.name != drv.name:
                            old_drv = None
                            with self._lock:
                                if str(self.config["host"]) == host:
                                    old_drv = self.driver
                                    self.driver = new_drv
                                    self._offline_failures = 0
                                    self._next_push_retry_mono = 0.0
                                    self._last_base_key = ("", "")
                            if isinstance(old_drv, RawFrameDriver) and old_drv is not new_drv:
                                old_drv._close_conn()
                            print(f"[hub] Switched driver to {new_drv.name}", flush=True)
                elif isinstance(drv, RawFrameDriver) and st.get("frames", 1) == 0:
                    # Device just rebooted -> force re-push active frame & sync offline config!
                    drv.invalidate_cache()
                    with self._lock:
                        self._last_base_key = ("", "")
                        rot = int(self.config.get("rotation", 0))
                    self._apply_effective_brightness()
                    if rot:
                        drv.set_rotation(rot)
                    self._sync_offline_config()

            # 5. Push active frame or next PC-side animation strip
            self._push_now(force=False)
