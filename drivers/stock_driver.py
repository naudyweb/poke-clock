"""
Fallback Stock Firmware Driver for JUZIPi SD PRO (single-slot /photo mode).
Automatically used if the device is running the factory JUZIPi firmware.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

from drivers.base import DisplayDriver


class StockFirmwareDriver(DisplayDriver):
    name = "stock_photo"

    def __init__(self, host: str, timeout: float = 8.0) -> None:
        self.host = host
        self.timeout = timeout
        self._lock = threading.Lock()
        self._uploaded_hashes: dict[str, str] = {}
        self._active_card: str = ""
        self.last_push_ms = 0
        self.last_error = ""

    def _get(self, path: str) -> bytes:
        req = urllib.request.Request(f"http://{self.host}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def _upload_jpg(self, filename: str, jpg_bytes: bytes) -> None:
        boundary = "----PokeHubStockBoundary7MA4YWxk"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8") + jpg_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            f"http://{self.host}/photo/upload",
            data=body,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Stock upload HTTP {resp.status}")

    def push_frame(self, card_id: str, jpg_bytes: bytes, content_hash: str, force: bool = False) -> dict:
        filename = f"poke_{card_id}.jpg" if not card_id.endswith(".jpg") else card_id
        with self._lock:
            t0 = time.monotonic()
            need_upload = force or (self._uploaded_hashes.get(filename) != content_hash)
            need_switch = force or (self._active_card != filename)
            if not need_upload and not need_switch:
                return {"skipped": True, "ms": 0}

            if need_upload:
                self._upload_jpg(filename, jpg_bytes)
                self._uploaded_hashes[filename] = content_hash

            listing = json.loads(self._get("/photo/list").decode("utf-8"))
            for f in listing.get("files", []):
                name = f.get("name", "")
                want = 1 if name == filename else 0
                cur = 1 if f.get("enabled", True) is not False else 0
                if cur != want:
                    self._get(f"/photo/toggle?name={urllib.parse.quote(name)}&state={want}")
            self._get("/theme/toggle?id=2&state=1")
            self._active_card = filename
            self.last_push_ms = int((time.monotonic() - t0) * 1000)
            return {"ok": True, "pc_ms": self.last_push_ms, "uploaded": need_upload}

    def set_brightness(self, level: int) -> bool:
        val = max(2, min(99, int(level)))
        with self._lock:
            try:
                self._get(f"/api/set?key=lcd_brightness&value={val}")
                return True
            except Exception as exc:
                self.last_error = str(exc)
                return False

    def get_status(self) -> dict:
        with self._lock:
            try:
                cfg = json.loads(self._get("/config").decode("utf-8"))
                return {
                    "online": True,
                    "driver": self.name,
                    "city": cfg.get("city"),
                    "brightness": cfg.get("brightness"),
                    "last_push_ms": self.last_push_ms,
                }
            except Exception as exc:
                self.last_error = str(exc)
                return {"online": False, "driver": self.name, "error": str(exc)}

    def ping(self) -> bool:
        with self._lock:
            try:
                self._get("/config")
                return True
            except Exception:
                return False
