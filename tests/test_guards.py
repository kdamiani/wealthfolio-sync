import http.client
import io
import json
import os
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from wfsync import sync, web
from wfsync.httpjson import HttpError, request_json
from wfsync.sources import bitvavo as b
from wfsync.wealthfolio import Wealthfolio


def serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def quiet(cls):
    cls.log_message = lambda self, *a: None
    return cls


class WealthfolioRedirect(unittest.TestCase):
    def test_session_does_not_follow_redirect(self):
        seen = []

        @quiet
        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Cookie"))
                self.send_response(200)
                self.end_headers()

        target, target_url = serve(Target)

        @quiet
        class Source(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target_url + "/x")
                self.end_headers()

        source, source_url = serve(Source)
        try:
            wf = Wealthfolio(source_url, "https://o.test")  # no opener argument: the default one
            wf.session = "sess"
            with self.assertRaises(HttpError) as cm:
                wf.call("GET", "/accounts")
            self.assertEqual(cm.exception.status, 302)
            self.assertFalse(wf.session_valid("sess"))
            self.assertEqual(seen, [])
        finally:
            for s in (source, target):
                s.shutdown()
                s.server_close()


class Login(unittest.TestCase):
    def client(self, cookies, status=200):
        class Resp(io.BytesIO):
            def __init__(self):
                super().__init__(b"{}")
                self.headers = mock.Mock(get_all=lambda name: cookies)

        class Opener:
            def open(self, req, timeout=None):
                if status != 200:
                    raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(b"{}"))
                return Resp()

        return Wealthfolio("http://wf.test", "https://o.test", opener=Opener())

    def test_takes_only_the_session_cookie(self):
        wf = self.client(["theme=dark; Path=/", "wf_session=abc.def; Path=/; HttpOnly; Secure; SameSite=Strict"])
        wf.login("pw")
        self.assertEqual(wf.session, "abc.def")

    def test_missing_cookie_and_wrong_password_fail(self):
        with self.assertRaises(HttpError) as cm:
            self.client(["theme=dark"]).login("pw")
        self.assertEqual(str(cm.exception), "HTTP 500: no session cookie")
        with self.assertRaises(HttpError) as cm:
            self.client([], status=401).login("pw")
        self.assertEqual(str(cm.exception), "HTTP 401: login failed")


class SessionExpiry(unittest.TestCase):
    def test_cached_session_is_rechecked(self):
        wf = mock.Mock()
        wf.session_valid.return_value = True
        checker = web.SessionChecker(wf)
        with mock.patch.object(web.time, "monotonic", return_value=1000.0):
            self.assertTrue(checker.valid("s"))
            self.assertTrue(checker.valid("s"))
        self.assertEqual(wf.session_valid.call_count, 1)
        wf.session_valid.return_value = False  # logged out in Wealthfolio meanwhile
        with mock.patch.object(web.time, "monotonic", return_value=1000.0 + web.SESSION_CACHE_S + 1):
            self.assertFalse(checker.valid("s"))
        self.assertEqual(wf.session_valid.call_count, 2)


class Partial(unittest.TestCase):
    def test_rejected_rows_make_the_source_partial(self):
        class Wf:
            def ensure_account(self, *a, create=True):
                return "acc"

            def activities(self, account_id):
                return []

            def check(self, rows):
                return [dict(r, isValid=r["symbol"] == "A", errors=None if r["symbol"] == "A" else {"symbol": ["x"]})
                        for r in rows]

            def import_rows(self, rows):
                return {"imported": len(rows), "skipped": 0}

        module = types.SimpleNamespace(SOURCE_ID="x", ACCOUNT_NAME="X", map_activities=lambda data, acc: (
            [{"date": "1", "symbol": "A", "comment": "wfsync:x:a"},
             {"date": "2", "symbol": "Z", "comment": "wfsync:x:z"}], []))
        result = sync.sync_source(Wf(), module, "SECURITIES", types.SimpleNamespace(fetch=lambda: {"currency": "EUR"}),
                                  False)
        self.assertEqual((result["status"], result["new"], result["invalid"], result["invalid_fields"]),
                         ("partial", 1, 1, ["symbol"]))

    def test_flagged_duplicates_are_not_imported(self):
        imported = []

        class Wf:
            def ensure_account(self, *a, create=True):
                return "acc"

            def activities(self, account_id):
                return []

            def check(self, rows):
                return [dict(r, isValid=True, duplicateOfId="d" if r["symbol"] == "D" else None,
                             duplicateOfLineNumber=1 if r["symbol"] == "L" else None) for r in rows]

            def import_rows(self, rows):
                imported.extend(r["symbol"] for r in rows)
                return {"imported": len(rows), "skipped": 0}

        module = types.SimpleNamespace(SOURCE_ID="x", ACCOUNT_NAME="X", map_activities=lambda data, acc: (
            [{"date": "1", "symbol": s, "comment": f"wfsync:x:{s}"} for s in "ADL"], []))
        sync.sync_source(Wf(), module, "SECURITIES", types.SimpleNamespace(fetch=lambda: {"currency": "EUR"}), False)
        self.assertEqual(imported, ["A"])


class TradeLegs(unittest.TestCase):
    def test_rejected_leg_holds_back_its_partner(self):
        imported = []

        class Wf:
            def ensure_account(self, *a, create=True):
                return "acc"

            def activities(self, account_id):
                return []

            def check(self, rows):
                self.sent = rows
                return [dict(r, isValid=r["symbol"] != "BAD") for r in rows]

            def import_rows(self, rows):
                imported.extend(r["comment"] for r in rows)
                return {"imported": len(rows), "skipped": 0}

        rows = [{"date": "1", "symbol": "BAD", "comment": "wfsync:x:1", "_group": "g1"},
                {"date": "1", "symbol": "USDT", "comment": "wfsync:x:2", "_group": "g1"},
                {"date": "2", "symbol": "OK", "comment": "wfsync:x:3", "_group": "g2"},
                {"date": "2", "symbol": "USDT", "comment": "wfsync:x:4", "_group": "g2"},
                {"date": "3", "symbol": "ALONE", "comment": "wfsync:x:5"}]
        module = types.SimpleNamespace(SOURCE_ID="x", ACCOUNT_NAME="X", map_activities=lambda data, acc: (rows, []))
        wf = Wf()
        result = sync.sync_source(wf, module, "SECURITIES", types.SimpleNamespace(fetch=lambda: {"currency": "EUR"}),
                                  False)
        self.assertEqual(imported, ["wfsync:x:3", "wfsync:x:4", "wfsync:x:5"])
        self.assertEqual((result["status"], result["invalid"], result["held_back"]), ("partial", 1, 1))
        self.assertFalse(any("_group" in r for r in wf.sent))  # Wealthfolio never sees the key

    def test_check_without_line_numbers_stops_source(self):
        class Wf:
            def ensure_account(self, *a, create=True):
                return "acc"

            def activities(self, account_id):
                return []

            def check(self, rows):
                return [{k: v for k, v in r.items() if k != "lineNumber"} | {"isValid": True} for r in rows]

            def import_rows(self, rows):
                raise AssertionError("imported")

        rows = [{"date": "1", "symbol": "A", "comment": "wfsync:x:1", "_group": "g1"}]
        module = types.SimpleNamespace(SOURCE_ID="x", ACCOUNT_NAME="X", map_activities=lambda data, acc: (rows, []))
        with self.assertRaises(RuntimeError):
            sync.sync_source(Wf(), module, "SECURITIES", types.SimpleNamespace(fetch=lambda: {"currency": "EUR"}),
                             False)


class DryRun(unittest.TestCase):
    def test_dry_run_is_no_success(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sync, "STATE_DIR", d), \
             mock.patch.object(sync, "sync_all", return_value={"x": {"status": "ok"}}):
            status = sync.run(dry_run=True, trigger="test")
            with open(os.path.join(d, "status.json")) as f:
                stored = json.load(f)
        self.assertTrue(status["ok"])
        self.assertNotIn("last_success", stored)


class RateLimit(unittest.TestCase):
    def test_429_is_retried(self):
        calls = []

        class Opener:
            def open(self, req, timeout=None):
                calls.append(1)
                if len(calls) == 1:
                    raise urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(b"{}"))
                return io.BytesIO(b'{"ok": true}')

        self.assertEqual(request_json(Opener(), "GET", "http://x.test/", backoff=0), {"ok": True})
        self.assertEqual(len(calls), 2)


class BitvavoPaging(unittest.TestCase):
    def test_all_pages_are_read(self):
        paths = []

        def get(path):
            paths.append(path)
            if path.startswith("/account/history"):
                page = int(path.split("page=")[1].split("&")[0])
                return {"items": [{"transactionId": str(page)}], "totalPages": 2}
            return []

        client = b.Bitvavo("k", "s")
        with mock.patch.object(client, "get", side_effect=get), mock.patch.object(b.time, "sleep"):
            data = client.fetch()
        self.assertEqual([i["transactionId"] for i in data["items"]], ["1", "2"])
        self.assertTrue(any("page=2" in p for p in paths))

    def test_missing_candle_gives_no_price(self):
        client = b.Bitvavo("k", "s")
        with mock.patch.object(client, "get", side_effect=HttpError(400, "205")):
            self.assertIsNone(client.price_at("XYZ", "2020-02-01T12:00:00.000Z"))


class BitvavoFees(unittest.TestCase):
    def test_third_currency_fee_is_skipped(self):
        it = {"transactionId": "1", "type": "buy", "executedAt": "2020-02-01T12:00:00.000Z", "priceCurrency": "EUR",
              "priceAmount": "100", "sentCurrency": "EUR", "sentAmount": "10", "receivedCurrency": "AAA",
              "receivedAmount": "0.1", "feesCurrency": "GGG", "feesAmount": "0.00001"}
        rows, skipped = b.map_activities({"items": [it], "prices": {}}, "A")
        self.assertEqual((rows, [s.reason for s in skipped]), ([], ["fee in a third currency"]))


class AccessLog(unittest.TestCase):
    def test_log_omits_query_string(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                    web.make_handler(web.SessionChecker(mock.Mock()), "https://o.test"))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with self.assertLogs("wfsync.web", "INFO") as logs:
                c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
                c.request("GET", "/healthz?token=abc")
                c.getresponse().read()
                c.close()
                time.sleep(0.2)
            text = "\n".join(logs.output)
            self.assertIn("/healthz", text)
            self.assertNotIn("token", text)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_to_another_path_starts_nothing(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                    web.make_handler(web.SessionChecker(mock.Mock()), "https://o.test"))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with mock.patch("wfsync.sync.run") as run:
                c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
                c.request("POST", "/sync/runx", headers={"Origin": "https://o.test", "Cookie": "wf_session=s"})
                self.assertEqual(c.getresponse().status, 404)
                c.close()
            run.assert_not_called()
        finally:
            httpd.shutdown()
            httpd.server_close()


class TransportFrames(unittest.TestCase):
    def test_wealthfolio_error_names_the_calling_function(self):
        class Opener:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError(req.full_url, 500, "x", {}, io.BytesIO(b"{}"))

        wf = Wealthfolio("http://wf.test", "https://o.test", opener=Opener())
        try:
            wf.check([{"symbol": "A"}])
            self.fail("no HttpError")
        except HttpError as e:
            text = sync.error_text(e)
        self.assertEqual(text, "HTTP 500: error in wealthfolio.py check")
