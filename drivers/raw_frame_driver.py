"""
Ultra-fast Raw RAM Frame Driver for custom PokeDisplay ESP8266 firmware.
Streams 240x240 baseline JPEGs directly into ESP8266 RAM via POST /frame
and blits straight to the ST7789 SPI display (~120-180ms, zero flash wear).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from drivers.base import DisplayDriver


class RawFrameDriver(DisplayDriver):
    name = "raw_frame"

    def __init__(self, host: str, timeout: float = 4.0) -> None:
        self.host = host
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last_pushed_hash = ""
        self.last_push_ms = 0
        self.last_error = ""

    def _request(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None) -> bytes:
        url = f"http://{self.host}{path}"
        headers = {"Connection": "close"}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def _post_frame_multipart(self, jpg_bytes: bytes, x: int = 0, y: int = 0) -> bytes:
        boundary = "----PokeRawFrame7MA4YWxk"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="frame"; filename="frame.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8") + jpg_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        path = "/frame" if (x == 0 and y == 0) else f"/frame?x={x}&y={y}"
        return self._request(
            "POST",
            path,
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def push_frame(self, card_id: str, jpg_bytes: bytes, content_hash: str, force: bool = False) -> dict:
        with self._lock:
            if not force and content_hash == self._last_pushed_hash:
                return {"skipped": True, "ms": 0}
            t0 = time.monotonic()
            try:
                raw = self._post_frame_multipart(jpg_bytes)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                self._last_pushed_hash = content_hash
                self.last_push_ms = elapsed_ms
                self.last_error = ""
                try:
                    info = json.loads(raw.decode("utf-8"))
                except Exception:
                    info = {"ok": True}
                info["pc_ms"] = elapsed_ms
                return info
            except Exception as exc:
                self.last_error = str(exc)
                self._last_pushed_hash = ""
                raise

    def push_region(self, card_id: str, jpg_bytes: bytes, content_hash: str, x: int = 0, y: int = 0) -> dict:
        """Stream a partial strip/region JPEG straight into ESP8266 RAM (~40-50ms)."""
        with self._lock:
            if content_hash == self._last_pushed_hash:
                return {"skipped": True, "ms": 0}
            t0 = time.monotonic()
            try:
                raw = self._post_frame_multipart(jpg_bytes, x=x, y=y)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                self._last_pushed_hash = content_hash
                self.last_push_ms = elapsed_ms
                self.last_error = ""
                try:
                    info = json.loads(raw.decode("utf-8"))
                except Exception:
                    info = {"ok": True}
                info["pc_ms"] = elapsed_ms
                return info
            except Exception as exc:
                self.last_error = str(exc)
                self._last_pushed_hash = ""
                raise

    def sync_offline_config(
        self,
        city: str,
        weatherkey: str,
        celsius: bool,
        hour12: bool,
        trainer_name: str,
        weather: dict | None = None,
    ) -> bool:
        """Sync city/key/units (+ optional weather snapshot) to ESP8266 for offline persistence."""
        query: dict[str, object] = {
            "city": city or "Paracas",
            "key": weatherkey or "",
            "c": 1 if celsius else 0,
            "h12": 1 if hour12 else 0,
            "name": trainer_name or "POKEGEAR",
        }
        if weather and weather.get("temp") is not None:
            query["temp"] = f"{float(weather['temp']):.1f}"
            if weather.get("humidity") is not None:
                query["hum"] = int(weather["humidity"])
            if weather.get("description"):
                query["desc"] = str(weather["description"])
            if weather.get("city"):
                query["wcity"] = str(weather["city"])
            if weather.get("updated_hm"):
                query["hm"] = str(weather["updated_hm"])
        params = urllib.parse.urlencode(query)
        with self._lock:
            try:
                self._request("GET", f"/offline_cfg?{params}")
                return True
            except Exception:
                return False

    def invalidate_cache(self) -> None:
        with self._lock:
            self._last_pushed_hash = ""

    def set_brightness(self, level: int) -> bool:
        val = max(0, min(100, int(level)))
        with self._lock:
            try:
                self._request("GET", f"/brightness?val={val}")
                return True
            except Exception as exc:
                self.last_error = str(exc)
                return False

    def set_rotation(self, rotation: int) -> bool:
        rot = int(rotation) % 4
        with self._lock:
            try:
                self._request("GET", f"/rotation?val={rot}")
                self._last_pushed_hash = ""
                return True
            except Exception as exc:
                self.last_error = str(exc)
                return False

    def get_status(self) -> dict:
        with self._lock:
            try:
                raw = self._request("GET", "/status?keepalive=1")
                data = json.loads(raw.decode("utf-8"))
                data["online"] = True
                data["driver"] = self.name
                data["last_push_ms"] = self.last_push_ms
                return data
            except Exception as exc:
                self.last_error = str(exc)
                return {"online": False, "driver": self.name, "error": str(exc)}

    def ping(self) -> bool:
        with self._lock:
            try:
                raw = self._request("GET", "/status")
                data = json.loads(raw.decode("utf-8"))
                return data.get("device") == "poke-display"
            except Exception:
                return False
