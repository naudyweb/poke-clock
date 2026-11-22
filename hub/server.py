#!/usr/bin/env python3
"""
Poké-Hub Local HTTP REST API & Web Control Server.
Runs on 0.0.0.0:8765 — 100% local PC control with instant screen pushing.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from hub.mdns_publisher import MdnsPublisher
from hub.state_engine import PokeHubEngine

UI_HTML_PATH = Path(__file__).resolve().parent / "ui.html"


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
            return json.loads(raw.decode("utf-8"))

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                html = UI_HTML_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return

            if path == "/api/status":
                payload = engine.get_status()
                if mdns is not None:
                    payload["hub"] = mdns.get_info()
                self._send_json(200, payload)
                return

            if path.startswith("/api/preview/"):
                card = path[len("/api/preview/"):].strip("/")
                jpg = engine.get_preview(card)
                if not jpg:
                    self._send_json(404, {"ok": False, "error": "preview unavailable"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
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
                    ttl = int(body.get("ttl") or 12)
                    self._send_json(200, engine.send_notify(title, message, pokemon=pokemon, ttl=ttl))
                elif path == "/api/refresh":
                    engine.refresh_all_data()
                    self._send_json(200, {"ok": True, "status": engine.get_status()})
                elif path == "/api/brightness":
                    level = int(body.get("val", 95))
                    self._send_json(200, engine.update_config({"brightness": level}))
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
