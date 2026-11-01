import io
import threading
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from wfsync import httpjson
from wfsync.httpjson import HttpError, default_opener, request_json


class Limited:
    """Returns 429 `limit` times, then `body`; records the headers of every attempt."""

    def __init__(self, limit=1, body=b'{"ok": true}', headers=None):
        self.limit, self.body, self.limit_headers = limit, body, headers or {}
        self.seen = []

    def open(self, req, timeout=None):
        self.seen.append(dict(req.header_items()))
        if len(self.seen) <= self.limit:
            raise urllib.error.HTTPError(req.full_url, 429, "x", self.limit_headers, io.BytesIO(b"{}"))
        return io.BytesIO(self.body)


class Retry(unittest.TestCase):
    def test_sign_runs_for_every_attempt(self):
        stamps = iter(("first", "second"))
        opener = Limited()
        request_json(opener, "GET", "http://x.test/", sign=lambda: {"X-Stamp": next(stamps)}, backoff=0)
        self.assertEqual([h["X-stamp"] for h in opener.seen], ["first", "second"])

    def test_retry_after_seconds_replace_the_backoff(self):
        with mock.patch.object(httpjson.time, "sleep") as sleep:
            request_json(Limited(headers={"Retry-After": "7"}), "GET", "http://x.test/", backoff=15)
        sleep.assert_called_once_with(7)

    def test_retry_after_date_is_converted(self):
        when = datetime(2020, 3, 1, 12, 0, 30, tzinfo=UTC)
        with mock.patch.object(httpjson, "now", return_value=when - timedelta(seconds=20)), \
             mock.patch.object(httpjson.time, "sleep") as sleep:
            request_json(Limited(headers={"Retry-After": format_datetime(when, usegmt=True)}), "GET", "http://x.test/")
        sleep.assert_called_once_with(20)

    def test_retry_after_capped_garbage_ignored(self):
        with mock.patch.object(httpjson.time, "sleep") as sleep:
            request_json(Limited(headers={"Retry-After": "86400"}), "GET", "http://x.test/")
            request_json(Limited(headers={"Retry-After": "soon"}), "GET", "http://x.test/", backoff=3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [httpjson.MAX_RETRY_AFTER_S, 3])


class Numbers(unittest.TestCase):
    def test_floats_stay_exact(self):
        res = request_json(Limited(limit=0, body=b'{"n": 0.1234567890123456789}'), "GET", "http://x.test/")
        self.assertEqual(res["n"], Decimal("0.1234567890123456789"))

        class Echo:
            def open(self, req, timeout=None):
                return io.BytesIO(req.data)
        self.assertEqual(request_json(Echo(), "POST", "http://x.test/", {"n": Decimal("0.1")}), {"n": "0.1"})


class Redirect(unittest.TestCase):
    def test_redirect_keeps_credentials_home(self):
        seen = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

        class Source(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/x")
                self.end_headers()

            def log_message(self, *a):
                pass

        source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        for s in (target, source):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(HttpError) as cm:
                request_json(default_opener(), "GET", f"http://127.0.0.1:{source.server_address[1]}/api",
                             headers={"Authorization": "Basic secret"}, retries=1)
            self.assertEqual(cm.exception.status, 302)
            self.assertEqual(seen, [])
        finally:
            for s in (target, source):
                s.shutdown()
                s.server_close()
