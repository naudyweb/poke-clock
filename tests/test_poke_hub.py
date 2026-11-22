"""Unit tests for Poké-Hub state engine, priority rules, card rendering, and REST API."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from drivers.base import DisplayDriver
from hub.mdns_publisher import MdnsPublisher
from hub.server import make_handler
from hub.state_engine import PokeHubEngine, render_svg_to_full_and_strip, render_svg_to_jpg_bytes
from screens import build_clock_svg, build_notify_svg, build_spotify_svg, build_weather_svg


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


if __name__ == "__main__":
    unittest.main()
