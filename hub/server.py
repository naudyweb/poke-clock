#!/usr/bin/env python3
"""
Poké-Hub Local HTTP REST API & Web Control Server.
Runs on 0.0.0.0:8765 — 100% local PC control with instant screen pushing.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from hub.mdns_publisher import MdnsPublisher
from hub.state_engine import PokeHubEngine

UI_HTML_PATH = Path(__file__).resolve().parent / "ui.html"
_UI_CACHE_LOCK = threading.Lock()
_UI_CACHE: tuple[int, bytes, bytes, str] | None = None


def _get_ui_html_bundle() -> tuple[bytes, bytes, str]:
    """Return (raw_html, gzip_html, etag_hash) cached in RAM, reloading only on mtime_ns change."""
    global _UI_CACHE
    mtime_ns = UI_HTML_PATH.stat().st_mtime_ns
    with _UI_CACHE_LOCK:
        if _UI_CACHE is not None and _UI_CACHE[0] == mtime_ns:
            return _UI_CACHE[1], _UI_CACHE[2], _UI_CACHE[3]
    html = UI_HTML_PATH.read_bytes()
    gz_html = gzip.compress(html, compresslevel=6)
    etag_hash = hashlib.sha256(html).hexdigest()[:16]
    with _UI_CACHE_LOCK:
        _UI_CACHE = (mtime_ns, html, gz_html, etag_hash)
    return html, gz_html, etag_hash


def _get_ui_html() -> bytes:
    """Return ui.html bytes cached in RAM, reloading only when mtime_ns changes."""
    return _get_ui_html_bundle()[0]


def _etag_matches(header_value: str, etag: str, etag_hash: str) -> bool:
    """Check whether an If-None-Match header matches the current ETag."""
    if not header_value or not etag:
        return False
    for raw_token in header_value.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if token == "*":
            return True
        if token.startswith("W/"):
            token = token[2:].strip()
        if token == etag or token.strip('"') == etag_hash:
            return True
    return False


def make_handler(engine: PokeHubEngine, mdns: MdnsPublisher | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            pass  # Keep journald clean

        def _send_json(self, code: int, payload: dict) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(min(length, 65536))
            if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                return {
                    key: values[-1]
                    for key, values in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()
                }
            return json.loads(raw.decode("utf-8"))

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, If-None-Match")
            self.end_headers()

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                html, gz_html, etag_hash = _get_ui_html_bundle()
                etag = f'"{etag_hash}"'
                if _etag_matches(self.headers.get("If-None-Match", ""), etag, etag_hash):
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    return
                accept_enc = self.headers.get("Accept-Encoding", "").lower()
                use_gzip = "gzip" in accept_enc
                body_bytes = gz_html if use_gzip else html
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Vary", "Accept-Encoding")
                if use_gzip:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            if path == "/api/status":
                payload = engine.get_status()
                if mdns is not None:
                    payload["hub"] = mdns.get_info()
                self._send_json(200, payload)
                return

            if path.startswith("/api/preview/"):
                card = path[len("/api/preview/"):].strip("/")
                jpg, etag_hash = engine.get_preview_with_etag(card)
                if not jpg:
                    self._send_json(404, {"ok": False, "error": "preview unavailable"})
                    return
                etag = f'"{etag_hash}"' if etag_hash else ""
                if etag and _etag_matches(self.headers.get("If-None-Match", ""), etag, etag_hash):
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                if etag:
                    self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(jpg)))
                self.end_headers()
                self.wfile.write(jpg)
                return

            self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json()
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": f"invalid json: {exc}"})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"ok": False, "error": "JSON body must be an object"})
                return
            try:
                if path == "/api/show":
                    card = str(body.get("card") or "")
                    self._send_json(200, engine.show_card(card))
                elif path == "/api/auto":
                    self._send_json(200, engine.set_auto())
                elif path == "/api/config":
                    self._send_json(200, engine.update_config(body))
                elif path == "/api/notify":
                    title = str(body.get("title") or "AVISO!")
                    message = str(body.get("message") or "")
                    pokemon = str(body.get("pokemon") or "pikachu")
                    app_name = str(body.get("app_name") or "")
                    raw_ttl = body.get("ttl")
                    ttl = 12 if raw_ttl is None or raw_ttl == "" else int(raw_ttl)
                    self._send_json(
                        200,
                        engine.send_notify(
                            title,
                            message,
                            pokemon=pokemon,
                            ttl=ttl,
                            app_name=app_name,
                            source="manual",
                        ),
                    )
                elif path == "/api/notify/test-desktop":
                    self._send_json(200, engine.test_desktop_notify())
                elif path == "/api/refresh":
                    engine.refresh_all_data()
                    self._send_json(200, {"ok": True, "status": engine.get_status()})
                elif path == "/api/brightness":
                    raw_val = body.get("value")
                    if raw_val is None or raw_val == "":
                        raw_val = body.get("val")
                    level = 95 if raw_val is None or raw_val == "" else int(raw_val)
                    if hasattr(engine, "set_brightness"):
                        self._send_json(200, engine.set_brightness(level))
                    else:
                        self._send_json(200, engine.update_config({"brightness": level}))
                elif path == "/api/display":
                    on = body.get("on")
                    if not isinstance(on, bool):
                        self._send_json(400, {"ok": False, "error": "on must be a boolean"})
                        return
                    result = engine.set_display_power(on)
                    self._send_json(200 if result.get("ok") else 409, result)
                elif path == "/api/rotation":
                    rot = int(body.get("val", 0))
                    self._send_json(200, engine.update_config({"rotation": rot}))
                else:
                    self._send_json(404, {"ok": False, "error": "not found"})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Poké-Hub PC-as-Brain Server")
    parser.add_argument("--bind", default=os.environ.get("POKE_HUB_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("POKE_HUB_PORT", "8765")))
    parser.add_argument("--domain", default=os.environ.get("POKE_HUB_DOMAIN", "pokeclock.local"))
    parser.add_argument("--no-mdns", action="store_true", help="Disable Avahi .local mDNS publishing")
    args = parser.parse_args()

    engine = PokeHubEngine()
    mdns: MdnsPublisher | None = None
    if not args.no_mdns and args.domain:
        mdns = MdnsPublisher(
            domain=args.domain,
            port=args.port,
            target_host=engine.config.get("host"),
        )

    httpd = ThreadingHTTPServer((args.bind, args.port), make_handler(engine, mdns=mdns))
    httpd.daemon_threads = True
    if mdns is not None:
        mdns.start()
    engine.start()

    def _shutdown(signum, _frame):
        if mdns is not None:
            mdns.stop()
        engine.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    hub_url = mdns.get_info()["url"] if mdns is not None else f"http://{args.bind}:{args.port}"
    print(
        f"[poke-hub] Listening on http://{args.bind}:{args.port} | URL: {hub_url} "
        f"(driver={engine.driver.name}, host={engine.config['host']})",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if mdns is not None:
            mdns.stop()
        engine.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
