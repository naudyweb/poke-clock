"""Tests for the Yellow Duck conversation endpoint and Gemini client."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub.server import make_handler
from hub.yellow_duck import YellowDuckError, reply_to_yellow_duck


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class TestYellowDuck(unittest.TestCase):
    def test_gemini_request_keeps_key_server_side_and_sends_duck_prompt(self) -> None:
        response = FakeResponse({
            "candidates": [{
                "content": {"parts": [{"text": "¿Qué esperabas que ocurriera?"}]},
            }],
        })
        messages = [{"role": "user", "text": "La prueba falla."}]

        with mock.patch.dict(os.environ, {
            "GEMINI_API_KEY": "secret-test-key",
            "YELLOW_DUCK_GEMINI_MODEL": "gemini-2.5-flash",
        }), mock.patch("hub.yellow_duck.urlopen", return_value=response) as mocked_urlopen:
            answer = reply_to_yellow_duck(messages)

        self.assertEqual(answer, "¿Qué esperabas que ocurriera?")
        request = mocked_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-goog-api-key"), "secret-test-key")
        self.assertNotIn("secret-test-key", request.full_url)
        payload = json.loads(request.data)
        self.assertEqual(payload["contents"], [{
            "role": "user",
            "parts": [{"text": "La prueba falla."}],
        }])
        self.assertIn("Psyduck", payload["systemInstruction"]["parts"][0]["text"])

    def test_missing_api_key_is_reported_without_request(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("hub.yellow_duck.urlopen") as mocked_urlopen:
            with self.assertRaises(YellowDuckError) as raised:
                reply_to_yellow_duck([{"role": "user", "text": "Hola"}])

        self.assertEqual(raised.exception.status_code, 503)
        mocked_urlopen.assert_not_called()

    def test_rejects_empty_or_oversized_conversations(self) -> None:
        with self.assertRaises(YellowDuckError) as empty:
            reply_to_yellow_duck([])
        with self.assertRaises(YellowDuckError) as oversized:
            reply_to_yellow_duck([{"role": "user", "text": "x" * 2001}])

        self.assertEqual(empty.exception.status_code, 400)
        self.assertEqual(oversized.exception.status_code, 400)

    def test_gemini_quota_error_is_returned_as_rate_limit(self) -> None:
        provider_error = HTTPError(
            "https://generativelanguage.googleapis.com/", 429, "quota", {}, io.BytesIO(b""),
        )
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), mock.patch(
            "hub.yellow_duck.urlopen", side_effect=provider_error,
        ):
            with self.assertRaises(YellowDuckError) as raised:
                reply_to_yellow_duck([{"role": "user", "text": "Hola"}])

        self.assertEqual(raised.exception.status_code, 429)

    def test_urlencoded_form_fallback_escapes_chat_content(self) -> None:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None))
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{port}/api/yellow-duck"
        message = "<script>alert('x')</script>"
        request = Request(
            url,
            data=urlencode({"history": "[]", "message": message}).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "text/html",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"http://127.0.0.1:{port}",
            },
        )

        try:
            with mock.patch("hub.server.reply_to_yellow_duck", return_value="Respuesta segura."):
                with urlopen(request, timeout=5) as response:
                    page = response.read().decode("utf-8")
                    self.assertEqual(response.headers.get_content_type(), "text/html")
            self.assertNotIn(message, page)
            self.assertIn("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;", page)
            self.assertIn("Respuesta segura.", page)
            self.assertIn('action="/api/yellow-duck"', page)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_endpoint_requires_same_origin_and_limits_requests(self) -> None:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None))
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{port}/api/yellow-duck"

        def post(origin: str) -> tuple[int, dict]:
            request = Request(
                url,
                data=json.dumps({"messages": [{"role": "user", "text": "Hola"}]}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Origin": origin},
            )
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except HTTPError as response:
                try:
                    return response.code, json.loads(response.read().decode("utf-8"))
                finally:
                    response.close()

        try:
            with mock.patch("hub.server.YELLOW_DUCK_RATE_LIMIT", 1), mock.patch(
                "hub.server.reply_to_yellow_duck", return_value="Vamos paso a paso.",
            ) as mocked_reply:
                status, payload = post(f"http://127.0.0.1:{port}")
                self.assertEqual((status, payload), (200, {"ok": True, "reply": "Vamos paso a paso."}))
                self.assertEqual(post("https://example.com")[0], 403)
                self.assertEqual(post(f"http://127.0.0.1:{port}")[0], 429)
                self.assertEqual(mocked_reply.call_count, 1)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()