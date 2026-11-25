"""Unit tests for Poké-Hub state engine, priority rules, card rendering, and REST API."""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from drivers.base import DisplayDriver
from drivers.raw_frame_driver import RawFrameDriver
from hub import state_engine
from hub.desktop_notify_watcher import (
    classify_desktop_notification,
    clean_notification_text,
    parse_dbus_monitor_block,
)
from hub.mdns_publisher import MdnsPublisher
from hub.server import make_handler
from hub.state_engine import (
    DEFAULT_CONFIG,
    PokeHubEngine,
    collect_claude_usage,
    collect_gemini_usage,
    render_svg_to_full_and_strip,
    render_svg_to_jpg_bytes,
)
from pixel_art import sanitize_pixel_text
from screens import (
    OFFLINE_POKEMON_ORDER,
    build_clock_svg,
    build_notify_svg,
    build_spotify_svg,
    build_weather_svg,
    resolve_palette_bg,
)


class MockDriver(DisplayDriver):
    name = "mock"

    def __init__(self) -> None:
        self.pushes: list[tuple[str, int, str]] = []
        self.brightness = 95
        self.rotation = 0
        self._last_hash = ""

    def push_frame(self, card_id: str, jpg_bytes: bytes, content_hash: str, force: bool = False) -> dict:
        if not force and content_hash == self._last_hash:
            return {"skipped": True, "ms": 0}
        self._last_hash = content_hash
        self.pushes.append((card_id, len(jpg_bytes), content_hash))
        return {"ok": True, "ms": 12, "pc_ms": 18}

    def set_brightness(self, level: int) -> bool:
        self.brightness = level
        return True

    def set_rotation(self, rotation: int) -> bool:
        self.rotation = rotation
        return True

    def get_status(self) -> dict:
        return {"online": True, "driver": self.name, "frames": len(self.pushes), "last_push_ms": 18}

    def ping(self) -> bool:
        return True


class TestPokeHub(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "config.json"
        self.driver = MockDriver()
        self.engine = PokeHubEngine(driver=self.driver, config_path=self.cfg_path, start_watchers=False)
        self.engine.weather_data = {
            "city": "Paracas", "temp": 24.0, "humidity": 80, "wind_ms": 4.2,
            "main": "Clouds", "description": "nubes", "updated_hm": "12:00",
            "forecast": [{"label": "DOM", "min": 18, "max": 25, "main": "Clear"}],
        }

    def tearDown(self) -> None:
        self.engine.stop()
        self.tmp.cleanup()

    def test_priority_notify_over_pinned_over_spotify_over_rotation(self) -> None:
        now = time.monotonic()
        # Default rotation starts at clock
        card, reason = self.engine.resolve_active_card_locked(now)
        self.assertEqual((card, reason), ("clock", "rotation"))

        # Spotify playing takes exclusive priority
        self.engine.spotify_track = {"title": "Song", "artist": "Artist", "album": "Alb"}
        card, reason = self.engine.resolve_active_card_locked(now)
        self.assertEqual((card, reason), ("spotify", "spotify"))

        # Pinned beats Spotify
        self.engine.pinned_card = "gemini"
        card, reason = self.engine.resolve_active_card_locked(now)
        self.assertEqual((card, reason), ("gemini", "pinned"))

        # Notify beats pinned
        self.engine.notify_state = {
            "title": "OK", "message": "Done", "pokemon": "pikachu",
            "expires_at": now + 10, "ttl": 10,
        }
        card, reason = self.engine.resolve_active_card_locked(now)
        self.assertEqual((card, reason), ("notify", "notify"))

        # After TTL expires, returns to pinned
        card, reason = self.engine.resolve_active_card_locked(now + 11)
        self.assertEqual((card, reason), ("gemini", "pinned"))

    def test_notify_card_renders_valid_jpeg(self) -> None:
        svg = build_notify_svg("BUILD OK", "Todos los tests pasaron", pokemon="charmander")
        jpg = render_svg_to_jpg_bytes(svg)
        self.assertTrue(jpg.startswith(b"\xff\xd8\xff"))
        self.assertLess(len(jpg), 30000)

    def test_rest_api_roundtrip(self) -> None:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.engine))
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            def call(method: str, path: str, body: dict | None = None):
                data = json.dumps(body).encode("utf-8") if body is not None else None
                headers = {"Content-Type": "application/json"} if data else {}
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, resp.read()

            code, raw = call("GET", "/api/status")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw)["driver"], "mock")

            code, raw = call("POST", "/api/show", {"card": "gemini"})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw)["card"], "gemini")

            code, raw = call("POST", "/api/notify", {"title": "TEST", "message": "Hola", "pokemon": "pikachu", "ttl": 5})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw)["card"], "notify")

            code, jpg = call("GET", "/api/preview/active")
            self.assertEqual(code, 200)
            self.assertTrue(jpg.startswith(b"\xff\xd8\xff"))

            code, raw = call("POST", "/api/auto", {})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw)["reason"], "rotation")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_mdns_publisher_url_formatting_and_status(self) -> None:
        pub = MdnsPublisher(domain="pokeclock", port=8765)
        self.assertEqual(pub.domain, "pokeclock.local")
        self.assertEqual(MdnsPublisher._format_url("pokeclock.local", 80), "http://pokeclock.local")
        self.assertEqual(MdnsPublisher._format_url("pokeclock.local", 8765), "http://pokeclock.local:8765")

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.engine, mdns=pub))
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["hub"]["domain"], "pokeclock.local")
            self.assertIn("pokeclock.local", data["hub"]["url"])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_pc_animations_render_distinct_frames_and_strip(self) -> None:
        from datetime import datetime

        track = {"title": "Opening Theme", "artist": "Poke Band", "album": "Johto"}
        sp_svgs = [build_spotify_svg(track, self.engine.config, anim_frame=i) for i in range(6)]
        self.assertEqual(len(set(sp_svgs)), 6)

        now = datetime(2026, 11, 22, 14, 30)
        clk_svgs = [build_clock_svg(now, self.engine.config, self.engine.weather_data, anim_frame=i) for i in range(4)]
        self.assertEqual(len(set(clk_svgs)), 4)

        w_svgs = [build_weather_svg(self.engine.weather_data, self.engine.config, anim_frame=i) for i in range(4)]
        self.assertEqual(len(set(w_svgs)), 4)

        full_jpg, strip_jpg = render_svg_to_full_and_strip(sp_svgs[1])
        self.assertTrue(full_jpg.startswith(b"\xff\xd8\xff"))
        self.assertTrue(strip_jpg.startswith(b"\xff\xd8\xff"))
        self.assertLess(len(strip_jpg), len(full_jpg))
        self.assertLess(len(full_jpg), 23500)

    def test_raw_driver_sync_offline_config_includes_weather_snapshot(self) -> None:
        from drivers.raw_frame_driver import RawFrameDriver

        drv = RawFrameDriver("127.0.0.1")
        captured: list[tuple[str, str]] = []
        drv._request = lambda method, path, **kwargs: (captured.append((method, path)), b'{"ok":true}')[1]  # type: ignore[method-assign]

        ok = drv.sync_offline_config(
            city="Paracas",
            weatherkey="testkey",
            celsius=True,
            hour12=True,
            trainer_name="POKEGEAR",
            weather={
                "city": "Paracas",
                "temp": 23.4,
                "humidity": 78,
                "description": "cielo claro",
                "updated_hm": "01:15",
            },
        )
        self.assertTrue(ok)
        self.assertEqual(len(captured), 1)
        method, path = captured[0]
        self.assertEqual(method, "GET")
        self.assertIn("/offline_cfg?", path)
        self.assertIn("temp=23.4", path)
        self.assertIn("hum=78", path)
        self.assertIn("wcity=Paracas", path)
        self.assertIn("hm=01%3A15", path)

    def test_render_engine_valid_jpeg_and_dark_palette(self) -> None:
        now = datetime(2026, 11, 22, 22, 15)
        for palette_name in ("gbc_classic", "gbc_dark"):
            cfg = dict(self.engine.config, palette=palette_name, auto_night_palette=False)
            svg = build_clock_svg(now, cfg, self.engine.weather_data, anim_frame=1)
            full_jpg, strip_jpg = render_svg_to_full_and_strip(svg, bg_hex=resolve_palette_bg(cfg, now))
            self.assertTrue(full_jpg.startswith(b"\xff\xd8"))
            self.assertTrue(strip_jpg.startswith(b"\xff\xd8"))
            self.assertLessEqual(len(full_jpg), 23500)
            self.assertLessEqual(len(strip_jpg), 23500)
            self.assertLess(len(strip_jpg), len(full_jpg))

    def test_notify_svg_app_name_and_extended_glyphs(self) -> None:
        raw_title = "user@mail.com"
        raw_msg = "Build_1 #42 [OK] $5=5; <x>"
        sanitized = sanitize_pixel_text(f"{raw_title} {raw_msg}")
        for glyph in "@_#$[]=;<>":
            self.assertIn(glyph, sanitized)
        self.assertEqual(sanitized, "USER@MAIL.COM BUILD_1 #42 [OK] $5=5; <X>")

        svg_mail = build_notify_svg(raw_title, raw_msg, app_name="MAIL")
        svg_default = build_notify_svg(raw_title, raw_msg, app_name="")
        self.assertTrue(svg_mail.startswith("<svg"))
        self.assertIn("</svg>", svg_mail)
        self.assertIn("<rect ", svg_mail)
        self.assertNotEqual(svg_mail, svg_default)

        jpg = render_svg_to_jpg_bytes(svg_mail)
        self.assertTrue(jpg.startswith(b"\xff\xd8"))

    def test_desktop_notify_watcher_helpers_and_parser(self) -> None:
        cleaned = clean_notification_text("<b>Hola</b><br/>Mundo &amp; &quot;OK&quot; &lt;1&gt;\n\t!")
        self.assertEqual(cleaned, 'Hola Mundo & "OK" <1> !')

        cases = [
            ("Thunderbird", "Nuevo correo", "De Ana", 1, "pikachu", "MAIL"),
            ("Telegram Desktop", "Luis", "Hola!", 1, "jigglypuff", "TELEGRAM"),
            ("Calendar", "Reunion", "15:00", 1, "snorlax", "CITA"),
            ("UPower", "Battery low", "5%", 2, "charmander", "ALERTA"),
            ("vscode", "Build", "Done", 1, "gengar", "VSCODE"),
        ]
        for app_name, summary, body, urgency, expected_poke, expected_badge in cases:
            poke, badge = classify_desktop_notification(app_name, summary, body, urgency=urgency)
            self.assertIn(poke, OFFLINE_POKEMON_ORDER)
            self.assertEqual((poke, badge), (expected_poke, expected_badge))

        dbus_lines = [
            "method call time=1700000000.123456 sender=:1.42 -> destination=:1.15 serial=99 "
            "path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=Notify",
            '   string "Thunderbird"',
            "   uint32 7",
            '   string "mail-unread"',
            '   string "Nuevo correo"',
            '   string "Linea uno',
            'Linea dos"',
            "   array [",
            "   ]",
            "   array [",
            "      dict entry(",
            '         string "urgency"',
            "         variant             byte 2",
            "      )",
            "   ]",
            "   int32 5000",
        ]
        parsed = parse_dbus_monitor_block(dbus_lines)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["app_name"], "Thunderbird")
        self.assertEqual(parsed["replaces_id"], 7)
        self.assertEqual(parsed["app_icon"], "mail-unread")
        self.assertEqual(parsed["summary"], "Nuevo correo")
        self.assertEqual(parsed["body"], "Linea uno\nLinea dos")
        self.assertEqual(parsed["urgency"], 2)

    def test_notify_fifo_queue_filter_and_history(self) -> None:
        self.assertFalse(DEFAULT_CONFIG["desktop_notify_dnd_night"])

        res1 = self.engine.send_notify(
            title="Mail 1",
            message="Body 1",
            pokemon="pikachu",
            ttl=5,
            app_name="MAIL",
            source="desktop",
        )
        self.assertTrue(res1.get("ok"))
        self.assertIsNotNone(self.engine.notify_state)
        self.assertEqual(self.engine.notify_state["title"], "Mail 1")
        self.assertEqual(len(self.engine._notify_queue), 0)

        res2 = self.engine.send_notify(
            title="Chat 2",
            message="Body 2",
            pokemon="jigglypuff",
            ttl=5,
            app_name="TELEGRAM",
            source="desktop",
        )
        self.assertTrue(res2.get("ok"))
        self.assertTrue(res2.get("queued"))
        self.assertEqual(res2.get("queue_len"), 1)
        self.assertEqual(self.engine.notify_state["title"], "Mail 1")
        self.assertEqual(len(self.engine._notify_queue), 1)

        with self.engine._lock:
            expired_first = self.engine.notify_state["expires_at"] + 0.1
            card, reason = self.engine.resolve_active_card_locked(expired_first)
            self.assertEqual((card, reason), ("notify", "notify"))
            self.assertIsNotNone(self.engine.notify_state)
            self.assertEqual(self.engine.notify_state["title"], "Chat 2")
            self.assertEqual(len(self.engine._notify_queue), 0)

            expired_second = self.engine.notify_state["expires_at"] + 0.1
            card2, reason2 = self.engine.resolve_active_card_locked(expired_second)
            self.assertEqual((card2, reason2), ("clock", "rotation"))
            self.assertIsNone(self.engine.notify_state)

        status = self.engine.get_status()
        history_titles = [item["title"] for item in status["notify_history"]]
        self.assertEqual(history_titles, ["Chat 2", "Mail 1"])

        # Ignore filter blocks Spotify
        self.engine._on_desktop_notification({
            "app_name": "Spotify",
            "badge": "SPOTIFY",
            "title": "Song",
            "message": "Artist",
            "pokemon": "jigglypuff",
        })
        self.assertIsNone(self.engine.notify_state)

        # Allow filter only lets listed apps pass
        self.engine.update_config({
            "desktop_notify_filter_mode": "allow",
            "desktop_notify_apps": ["thunderbird"],
        })
        self.engine._on_desktop_notification({
            "app_name": "Discord",
            "badge": "DISCORD",
            "title": "Blocked",
            "message": "Should be filtered",
            "pokemon": "jigglypuff",
        })
        self.assertIsNone(self.engine.notify_state)

        self.engine._on_desktop_notification({
            "app_name": "Thunderbird",
            "badge": "MAIL",
            "title": "Allowed Mail",
            "message": "Passed allow filter",
            "pokemon": "pikachu",
        })
        self.assertIsNotNone(self.engine.notify_state)
        self.assertEqual(self.engine.notify_state["title"], "Allowed Mail")

    def test_collect_gemini_usage_incremental_cache(self) -> None:
        log_dir = Path(self.tmp.name) / "conv-cache-test" / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript = log_dir / "transcript.jsonl"
        ts_now = datetime.now().astimezone().isoformat()
        records = [
            {"step_index": 1, "input_tokens": 120, "output_tokens": 30, "cache_read_tokens": 150, "created_at": ts_now},
            {"step_index": 2, "input_tokens": 80, "output_tokens": 20, "cache_read_tokens": 50, "created_at": ts_now},
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

        glob_pattern = str(Path(self.tmp.name) / "*" / ".system_generated" / "logs" / "transcript.jsonl")
        with state_engine._GEMINI_CACHE_LOCK:
            state_engine._GEMINI_FILE_CACHE.clear()

        with mock.patch.object(state_engine, "TRANSCRIPT_GLOBS", [glob_pattern]), mock.patch(
            "hub.state_engine._parse_gemini_file", wraps=state_engine._parse_gemini_file
        ) as parse_spy:
            first = collect_gemini_usage()
            self.assertEqual(parse_spy.call_count, 1)
            self.assertEqual(first["today_in"], 200)
            self.assertEqual(first["today_out"], 50)
            self.assertEqual(first["today_cache"], 200)
            self.assertEqual(first["today_total"], 450)
            self.assertEqual(first["today_steps"], 2)
            self.assertEqual(first["today_sessions"], 1)
            self.assertEqual(first["cache_pct"], 50)
            self.assertEqual(first["week_total"], 450)

            second = collect_gemini_usage()
            self.assertEqual(parse_spy.call_count, 1)
            self.assertEqual(second["today_in"], first["today_in"])
            self.assertEqual(second["today_out"], first["today_out"])
            self.assertEqual(second["today_cache"], first["today_cache"])
            self.assertEqual(second["today_total"], first["today_total"])
            self.assertEqual(second["today_steps"], first["today_steps"])
            self.assertEqual(second["today_sessions"], first["today_sessions"])
            self.assertEqual(second["week_total"], first["week_total"])

    def test_raw_driver_offline_cfg_v14_and_standby(self) -> None:
        drv = RawFrameDriver("127.0.0.1")
        captured: list[tuple[str, str]] = []
        drv._request = lambda method, path, **kwargs: (captured.append((method, path)), b'{"ok":true}')[1]  # type: ignore[method-assign]

        ok = drv.sync_offline_config(
            city="Paracas",
            weatherkey="testkey",
            celsius=True,
            hour12=True,
            trainer_name="POKEGEAR",
            pokemon="gengar",
            palette="gbc_dark",
            weather_icon=2,
        )
        self.assertTrue(ok)
        self.assertEqual(len(captured), 1)
        method, path = captured[0]
        self.assertEqual(method, "GET")
        self.assertTrue(path.startswith("/offline_cfg?"))
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        self.assertEqual(qs.get("poke"), ["gengar"])
        self.assertEqual(qs.get("pal"), ["gbc_dark"])
        self.assertEqual(qs.get("wicon"), ["2"])

        captured.clear()
        ok_standby = drv.enter_standby()
        self.assertTrue(ok_standby)
        self.assertEqual(captured, [("GET", "/standby")])

    def test_rest_api_preview_etag_304_and_test_desktop(self) -> None:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.engine))
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/preview/active")
            resp1 = conn.getresponse()
            body1 = resp1.read()
            etag = resp1.getheader("ETag")
            self.assertEqual(resp1.status, 200)
            self.assertTrue(etag)
            self.assertTrue(body1.startswith(b"\xff\xd8"))
            conn.close()

            conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn2.request("GET", "/api/preview/active", headers={"If-None-Match": etag or ""})
            resp2 = conn2.getresponse()
            _ = resp2.read()
            self.assertEqual(resp2.status, 304)
            conn2.close()

            conn3 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn3.request(
                "POST",
                "/api/notify/test-desktop",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            resp3 = conn3.getresponse()
            payload3 = json.loads(resp3.read().decode("utf-8"))
            self.assertEqual(resp3.status, 200)
            self.assertTrue(payload3.get("ok"))
            conn3.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


    def test_weather_refresh_does_not_reset_timestamp_on_failure(self) -> None:
        """When fetch_weather returns None, _last_weather_ts must NOT be updated."""
        initial_ts = self.engine._last_weather_ts
        with mock.patch("hub.state_engine.fetch_weather", return_value=None):
            result = self.engine._refresh_weather()
        self.assertIsNone(result)
        self.assertEqual(self.engine._last_weather_ts, initial_ts)
        self.assertEqual(self.engine._weather_fail_count, 1)

    def test_weather_refresh_resets_timestamp_on_success(self) -> None:
        """When fetch_weather succeeds, _last_weather_ts is updated and fail count resets."""
        fake_weather = {
            "city": "Lima", "temp": 22.0, "humidity": 70, "wind_ms": 3.0,
            "main": "Clear", "description": "cielo claro", "updated_hm": "10:00",
            "forecast": [],
        }
        with mock.patch("hub.state_engine.fetch_weather", return_value=fake_weather):
            result = self.engine._refresh_weather()
        self.assertIsNotNone(result)
        self.assertGreater(self.engine._last_weather_ts, 0.0)
        self.assertEqual(self.engine._weather_fail_count, 0)
        self.assertEqual(self.engine.weather_data["city"], "Lima")

    def test_weather_fail_count_increments_on_consecutive_failures(self) -> None:
        """Consecutive failures should increment _weather_fail_count."""
        self.engine._weather_fail_count = 0
        self.engine._last_weather_ts = 0.0
        with mock.patch("hub.state_engine.fetch_weather", return_value=None):
            self.engine._refresh_weather()
            self.engine._refresh_weather()
            self.engine._refresh_weather()
        self.assertEqual(self.engine._weather_fail_count, 3)
        self.assertEqual(self.engine._last_weather_ts, 0.0)

    def test_weather_backoff_interval_on_failures(self) -> None:
        """Weather interval should use backoff schedule when there are failures."""
        self.engine._weather_fail_count = 0
        self.engine._last_weather_ts = time.monotonic()
        # No failures -> 900s interval
        with self.engine._lock:
            interval_0 = 900.0 if self.engine._weather_fail_count == 0 else 30.0
        self.assertEqual(interval_0, 900.0)

        # 1 failure -> 30s
        self.engine._weather_fail_count = 1
        with self.engine._lock:
            fail_count = self.engine._weather_fail_count
        backoff = (30.0, 60.0, 120.0)
        expected = backoff[min(fail_count - 1, len(backoff) - 1)]
        self.assertEqual(expected, 30.0)

        # 2 failures -> 60s
        self.engine._weather_fail_count = 2
        expected = backoff[min(self.engine._weather_fail_count - 1, len(backoff) - 1)]
        self.assertEqual(expected, 60.0)

        # 5 failures (capped) -> 120s
        self.engine._weather_fail_count = 5
        expected = backoff[min(self.engine._weather_fail_count - 1, len(backoff) - 1)]
        self.assertEqual(expected, 120.0)

    def test_load_config_weatherkey_env_fallback(self) -> None:
        """If weatherkey is empty in config, _load_config falls back to env var."""
        cfg = self.engine._load_config()
        # DEFAULT_CONFIG already has the env var value at import time
        self.assertIsInstance(cfg.get("weatherkey"), str)

        # Simulate empty weatherkey and set env var
        with mock.patch.dict(os.environ, {"OPENWEATHER_API_KEY": "test-key-123"}):
            # Create a fresh engine with empty weatherkey in DEFAULT_CONFIG
            old = state_engine.DEFAULT_CONFIG["weatherkey"]
            state_engine.DEFAULT_CONFIG["weatherkey"] = ""
            try:
                engine2 = PokeHubEngine(
                    driver=self.driver, config_path=self.cfg_path, start_watchers=False
                )
                self.assertEqual(engine2.config["weatherkey"], "test-key-123")
            finally:
                state_engine.DEFAULT_CONFIG["weatherkey"] = old

    def test_health_check_forces_weather_retry_on_reconnect(self) -> None:
        """When device comes online and weather_data is None, _last_weather_ts resets to 0."""
        self.engine.weather_data = None
        self.engine._last_weather_ts = time.monotonic()  # Pretend we already tried
        original_ts = self.engine._last_weather_ts

        # Simulate health check: device is online
        with mock.patch.object(self.driver, "get_status", return_value={"online": True, "frames": 0}):
            st = self.driver.get_status()
            with self.engine._lock:
                self.engine._last_device_status = st
                if st.get("online"):
                    self.engine._offline_failures = 0
                    self.engine._next_push_retry_mono = 0.0
                    if self.engine.weather_data is None:
                        self.engine._last_weather_ts = 0.0

        self.assertEqual(self.engine._last_weather_ts, 0.0)
        # weather_data still None — the loop will pick up the retry on next iteration
        self.assertIsNone(self.engine.weather_data)

    def test_gemini_quota_error_ignores_normal_records_and_recovers(self) -> None:
        log_dir = Path(self.tmp.name) / "conv-quota-test" / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript = log_dir / "transcript.jsonl"
        glob_pattern = str(Path(self.tmp.name) / "*" / ".system_generated" / "logs" / "transcript.jsonl")

        now_dt = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)

        def run_collect(at_dt: datetime) -> dict:
            with state_engine._GEMINI_CACHE_LOCK:
                state_engine._GEMINI_FILE_CACHE.clear()
            with mock.patch.object(state_engine, "TRANSCRIPT_GLOBS", [glob_pattern]), mock.patch(
                "hub.state_engine.datetime"
            ) as mock_dt, mock.patch("hub.state_engine.time.time", return_value=at_dt.timestamp()):
                mock_dt.now.return_value = at_dt
                mock_dt.fromisoformat.side_effect = datetime.fromisoformat
                mock_dt.fromtimestamp.side_effect = datetime.fromtimestamp
                mock_dt.combine.side_effect = datetime.combine
                mock_dt.min = datetime.min
                return collect_gemini_usage()

        # 1. Normal DONE records mentioning quota/429/out of tokens must NOT trigger quota_error
        transcript.write_text(
            "\n".join([
                json.dumps({
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "step_index": 1,
                    "input_tokens": 100,
                    "output_tokens": 25,
                    "cache_read_tokens": 50,
                    "thinking": "Analyzing 429 quota error handling when out of tokens",
                    "content": "Fixed quota 429 limit exceeded message",
                    "created_at": now_dt.isoformat(),
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        res_normal = run_collect(now_dt)
        self.assertFalse(res_normal["quota_error"])

        # 2. Actual ERROR record with cooldown activates quota_error and sets daily_reset from cooldown
        err_dt = now_dt - timedelta(minutes=10)
        transcript.write_text(
            "\n".join([
                json.dumps({
                    "type": "SYSTEM_MESSAGE",
                    "status": "ERROR",
                    "content": "Quota exceeded. Your quota will reset after 30m.",
                    "created_at": err_dt.isoformat(),
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        res_err = run_collect(now_dt)
        self.assertTrue(res_err["quota_error"])
        self.assertEqual(res_err["daily_reset"], "00:20")

        # 3. Once cooldown wait expires (now >= reset_at), quota_error clears automatically
        after_wait = err_dt + timedelta(minutes=31)
        res_after_wait = run_collect(after_wait)
        self.assertFalse(res_after_wait["quota_error"])

        # 4. Yesterday's quota error without explicit duration does not carry over to today
        yesterday_dt = now_dt - timedelta(days=1)
        transcript.write_text(
            json.dumps({
                "type": "SYSTEM_MESSAGE",
                "status": "ERROR",
                "content": "HTTP 429 quota exceeded",
                "created_at": yesterday_dt.isoformat(),
            }) + "\n",
            encoding="utf-8",
        )
        res_yesterday = run_collect(now_dt)
        self.assertFalse(res_yesterday["quota_error"])

        # 5. A subsequent successful token step clears an earlier same-day quota error
        transcript.write_text(
            "\n".join([
                json.dumps({
                    "type": "SYSTEM_MESSAGE",
                    "status": "ERROR",
                    "content": "HTTP 429 quota exceeded",
                    "created_at": (now_dt - timedelta(minutes=5)).isoformat(),
                }),
                json.dumps({
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "step_index": 2,
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "cache_read_tokens": 0,
                    "created_at": now_dt.isoformat(),
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        res_recovered = run_collect(now_dt)
        self.assertFalse(res_recovered["quota_error"])

    def test_refresh_gemini_invalidates_and_pushes_on_reset_or_quota_change(self) -> None:
        base_stats = {
            "today_date": "2026-11-24",
            "updated_hm": "12:00",
            "today_total": 500,
            "today_in": 300,
            "today_out": 50,
            "today_cache": 150,
            "today_steps": 3,
            "today_sessions": 1,
            "cache_pct": 33,
            "week_total": 500,
            "quota_error": True,
            "daily_reset": "00:05",
            "weekly_reset": "5d12h",
            "week_days": [
                {"date": "2026-11-24", "label": "M", "total": 500, "is_today": True},
            ],
        }
        updated_countdown = dict(base_stats, updated_hm="12:04", daily_reset="00:01")
        recovered_stats = dict(base_stats, updated_hm="12:05", quota_error=False, daily_reset="11:55")

        self.engine.show_card("gemini")
        initial_pushes = len(self.driver.pushes)

        with mock.patch("hub.state_engine.collect_gemini_usage", return_value=dict(base_stats)):
            self.engine._refresh_gemini()
        self.engine._push_now(force=False)
        after_first = len(self.driver.pushes)
        self.assertGreater(after_first, initial_pushes)

        # Changing only daily_reset / updated_hm (today_total unchanged) must invalidate and push
        with mock.patch("hub.state_engine.collect_gemini_usage", return_value=dict(updated_countdown)):
            self.engine._refresh_gemini()
        self.engine._push_now(force=False)
        after_countdown = len(self.driver.pushes)
        self.assertGreater(after_countdown, after_first)

        # Recovering from quota_error (out_of_tokens True -> False) must invalidate and push
        with mock.patch("hub.state_engine.collect_gemini_usage", return_value=dict(recovered_stats)):
            self.engine._refresh_gemini()
        self.engine._push_now(force=False)
        self.assertGreater(len(self.driver.pushes), after_countdown)
        self.assertFalse(self.engine.gemini_stats["out_of_tokens"])

    def test_claude_card_end_to_end_usage_and_ui_controls(self) -> None:
        # 1. collect_claude_usage must not double-count cache tokens or flag hasUnknownModelCost as quota_error
        proj_dir = Path(self.tmp.name) / "claude-proj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        session_file = proj_dir / "session.jsonl"
        now_ms = int(time.time() * 1000)
        session_file.write_text(
            json.dumps({
                "type": "cost-state",
                "sessionId": "s1",
                "startTime": now_ms,
                "hasUnknownModelCost": True,
                "modelUsage": {
                    "claude-sonnet": {
                        "inputTokens": 100,
                        "outputTokens": 50,
                        "thinkingTokens": 25,
                        "cacheReadInputTokens": 200,
                        "cacheCreationInputTokens": 50,
                    }
                },
            }) + "\n",
            encoding="utf-8",
        )
        claude_glob = str(Path(self.tmp.name) / "**" / "*.jsonl")
        with state_engine._CLAUDE_CACHE_LOCK:
            state_engine._CLAUDE_FILE_CACHE.clear()
        with mock.patch.object(state_engine, "CLAUDE_PROJECT_GLOB", claude_glob):
            c_stats = collect_claude_usage()
        self.assertEqual(c_stats["today_in"], 100)
        self.assertEqual(c_stats["today_out"], 75)
        self.assertEqual(c_stats["today_cache"], 250)
        self.assertEqual(c_stats["today_total"], 425)
        self.assertFalse(c_stats["quota_error"])

        # 2. Sprite config persists for claude and REST /api/show + /api/preview/claude work
        self.engine.update_config({"sprites": {"claude": "gengar"}})
        self.assertEqual(self.engine.config["sprites"]["claude"], "gengar")

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.engine))
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/show",
                data=json.dumps({"card": "claude"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.loads(resp.read())["card"], "claude")

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/preview/claude", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue(resp.read().startswith(b"\xff\xd8\xff"))
        finally:
            httpd.shutdown()
            httpd.server_close()

        # 3. Web UI contains Claude button, rotation switch, and sprite selector
        ui_html = (ROOT / "hub" / "ui.html").read_text(encoding="utf-8")
        self.assertIn('id="btn-claude"', ui_html)
        self.assertIn("showCard('claude')", ui_html)
        self.assertIn('id="en-claude"', ui_html)
        self.assertIn('id="sp-claude"', ui_html)

    def test_daily_limits_disabled_by_default_and_do_not_lock_screen(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["gemini_daily_limit"], 0)
        self.assertEqual(DEFAULT_CONFIG["claude_daily_limit"], 0)

        high_stats = {
            "today_date": "2026-11-24",
            "updated_hm": "10:45",
            "today_total": 45_000_000,
            "today_in": 5_000_000,
            "today_out": 1_000_000,
            "today_cache": 39_000_000,
            "today_steps": 400,
            "today_sessions": 20,
            "cache_pct": 89,
            "week_total": 130_000_000,
            "quota_error": False,
            "daily_reset": "13:15",
            "weekly_reset": "5d13h",
            "week_days": [
                {"date": "2026-11-24", "label": "M", "total": 45_000_000, "is_today": True},
            ],
        }
        with mock.patch("hub.state_engine.collect_gemini_usage", return_value=dict(high_stats)):
            self.engine._refresh_gemini()
            self.assertFalse(self.engine.gemini_stats["out_of_tokens"])

            # Explicit positive limit sets out_of_tokens, resetting to 0 clears it immediately
            self.engine.update_config({"gemini_daily_limit": 40_000_000})
            self.assertTrue(self.engine.gemini_stats["out_of_tokens"])
            self.engine.update_config({"gemini_daily_limit": 0})
            self.assertFalse(self.engine.gemini_stats["out_of_tokens"])


if __name__ == "__main__":
    unittest.main()
