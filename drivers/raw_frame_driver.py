"""
Ultra-fast Raw RAM Frame Driver for custom PokeDisplay ESP8266 firmware.
Streams 240x240 baseline JPEGs directly into ESP8266 RAM via POST /frame
and blits straight to the ST7789 SPI display (~120-180ms, zero flash wear).
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse

from drivers.base import DisplayDriver

_RETRYABLE_HTTP_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.CannotSendRequest,
    http.client.BadStatusLine,
    http.client.ResponseNotReady,
    http.client.IncompleteRead,
    BrokenPipeError,
    ConnectionResetError,
)


class RawFrameDriver(DisplayDriver):
    name = "raw_frame"

    def __init__(self, host: str, timeout: float = 4.0) -> None:
        self.host = host
        self.timeout = timeout
        self._lock = threading.RLock()
        self._conn: http.client.HTTPConnection | None = None
        self._last_pushed_key: tuple[str, int, int, str] = ("", 0, 0, "")
        self._display_power_supported = False
        self.last_push_ms = 0
        self.last_error = ""

    def _ensure_conn(self) -> http.client.HTTPConnection:
        if self._conn is None:
            if ":" in self.host:
                self._conn = http.client.HTTPConnection(self.host, timeout=self.timeout)
            else:
                self._conn = http.client.HTTPConnection(self.host, 80, timeout=self.timeout)
        return self._conn

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def close(self) -> None:
        with self._lock:
            self._close_conn()

    def _request(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None) -> bytes:
        headers: dict[str, str] = {"Connection": "keep-alive"}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        if content_type:
            headers["Content-Type"] = content_type

        with self._lock:
            last_exc: Exception | None = None
            for attempt in range(2):
                conn = self._ensure_conn()
                try:
                    conn.request(method, path, body=body, headers=headers)
                    resp = conn.getresponse()
                    data = resp.read()
                    if resp.will_close:
                        self._close_conn()
                    if resp.status >= 400:
                        raise RuntimeError(f"HTTP {resp.status} {resp.reason}")
                    return data
                except _RETRYABLE_HTTP_ERRORS as exc:
                    last_exc = exc
                    self._close_conn()
                    if attempt == 0:
                        continue
                    raise
                except Exception:
                    self._close_conn()
                    raise
            assert last_exc is not None
            raise last_exc

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
        key = ("full", 0, 0, content_hash)
        with self._lock:
            if not force and key == self._last_pushed_key:
                return {"skipped": True, "ms": 0}
            t0 = time.monotonic()
            try:
                raw = self._post_frame_multipart(jpg_bytes)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                self._last_pushed_key = key
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
                self._last_pushed_key = ("", 0, 0, "")
                raise

    def push_region(self, card_id: str, jpg_bytes: bytes, content_hash: str, x: int = 0, y: int = 0) -> dict:
        """Stream a partial strip/region JPEG straight into ESP8266 RAM (~40-50ms)."""
        key = ("region", int(x), int(y), content_hash)
        with self._lock:
            if key == self._last_pushed_key:
                return {"skipped": True, "ms": 0}
            t0 = time.monotonic()
            try:
                raw = self._post_frame_multipart(jpg_bytes, x=x, y=y)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                self._last_pushed_key = key
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
                self._last_pushed_key = ("", 0, 0, "")
                raise

    def sync_offline_config(
        self,
        city: str,
        weatherkey: str,
        celsius: bool,
        hour12: bool,
        trainer_name: str,
        weather: dict | None = None,
        pokemon: str | None = None,
        palette: str | None = None,
        weather_icon: int | None = None,
    ) -> bool:
        """Sync city/key/units (+ optional weather snapshot, pokemon, palette, icon) to ESP8266."""
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
        if pokemon is not None:
            poke_clean = str(pokemon).strip().lower()
            if poke_clean:
                query["poke"] = poke_clean
        if palette is not None:
            pal_clean = str(palette).strip().lower()
            if pal_clean:
                query["pal"] = pal_clean
        if weather_icon is not None:
            query["wicon"] = int(weather_icon)

        params = urllib.parse.urlencode(query)
        with self._lock:
            try:
                self._request("GET", f"/offline_cfg?{params}")
                return True
            except Exception:
                return False

    def enter_standby(self) -> bool:
        """Best-effort request to switch ESP8266 into its autonomous offline screen immediately."""
        with self._lock:
            prev_timeout = self.timeout
            short_timeout = min(prev_timeout, 1.5)
            self.timeout = short_timeout
            if self._conn is not None:
                self._conn.timeout = short_timeout
                if self._conn.sock is not None:
                    try:
                        self._conn.sock.settimeout(short_timeout)
                    except Exception:
                        pass
            try:
                self._request("GET", "/standby")
                return True
            except Exception:
                self._close_conn()
                return False
            finally:
                self.timeout = prev_timeout
                if self._conn is not None:
                    self._conn.timeout = prev_timeout
                    if self._conn.sock is not None:
                        try:
                            self._conn.sock.settimeout(prev_timeout)
                        except Exception:
                            pass

    def invalidate_cache(self) -> None:
        with self._lock:
            self._last_pushed_key = ("", 0, 0, "")

    def set_brightness(self, level: int) -> bool:
        val = max(0, min(100, int(level)))
        with self._lock:
            try:
                self._request("GET", f"/brightness?val={val}")
                return True
            except Exception as exc:
                self.last_error = str(exc)
                return False

    @property
    def supports_display_power(self) -> bool:
        return self._display_power_supported

    @staticmethod
    def _status_supports_display_power(status: dict) -> bool:
        if status.get("device") != "poke-display" or not isinstance(status.get("display_on"), bool):
            return False
        version = str(status.get("version") or "").strip().lower().removeprefix("v")
        version_parts = version.split(".")
        if len(version_parts) != 3 or not all(part.isdigit() for part in version_parts):
            return False
        return tuple(int(part) for part in version_parts) >= (1, 5, 0)

    def check_display_power_support(self) -> bool:
        status = self.get_status()
        return self._status_supports_display_power(status)

    def set_display_power(self, on: bool) -> bool:
        if not self.check_display_power_support():
            self.last_error = "display power requires compatible PokeDisplay firmware 1.5.0 or newer"
            return False
        with self._lock:
            try:
                raw = self._request("GET", f"/display?on={1 if on else 0}")
                result = json.loads(raw.decode("utf-8"))
                if result.get("ok") is not True or result.get("display_on") is not on:
                    raise RuntimeError("firmware did not confirm the requested display state")
                self._last_pushed_key = ("", 0, 0, "")
                self.last_error = ""
                return True
            except Exception as exc:
                self.last_error = str(exc)
                return False

    def set_rotation(self, rotation: int) -> bool:
        rot = int(rotation) % 4
        with self._lock:
            try:
                self._request("GET", f"/rotation?val={rot}")
                self._last_pushed_key = ("", 0, 0, "")
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
                self._display_power_supported = self._status_supports_display_power(data)
                return data
            except Exception as exc:
                self._display_power_supported = False
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
