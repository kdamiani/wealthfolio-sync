import http.client
import json
import os
import socket
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from wfsync import __version__, web

ORIGIN = "https://wealthfolio.example.test"
ALL_SOURCES = "trading212,bitvavo,bitget,monefit,goandgrow,debitum"


def setUpModule():
    patcher = mock.patch.dict(os.environ, {"WFSYNC_SOURCES": ALL_SOURCES})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class FakeWf:
    def __init__(self, good):
        self.good = good
        self.calls = 0

    def session_valid(self, value):
        self.calls += 1
        return value == self.good


class Render(unittest.TestCase):
    def test_escapes_source_data(self):
        page = web.render({"results": {"bitget": {"status": "<script>", "error": "<b>boom</b>"}}})
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;b&gt;boom&lt;/b&gt;", page)

    def test_running_disables_button_and_refreshes(self):
        page = web.render({"running": True})
        self.assertIn("disabled", page)
        self.assertIn("http-equiv='refresh'", page)


class Version(unittest.TestCase):
    def test_page_shows_the_package_version(self):
        self.assertIn(f"wealthfolio-sync {__version__}", web.render({}))


class Details(unittest.TestCase):
    def test_missing_row_counts_are_named(self):
        text = " | ".join(web.details({"new": 1, "duplicates": 2, "not_imported": 3, "pending": 4, "held_back": 5,
                                       "invalid": 6, "skipped": {"unsupported": 7}}))
        self.assertIn("2 not imported: Wealthfolio reports a duplicate", text)
        self.assertIn("3 skipped by Wealthfolio on import", text)
        self.assertIn("4 waiting for a price, retried on the next run", text)
        self.assertIn("5 held back (other leg not imported)", text)
        self.assertIn("6 rejected (fields: -)", text)
        self.assertIn("7 skipped: unsupported", text)


class Selection(unittest.TestCase):
    def render(self, selected, status=None):
        with mock.patch.dict(os.environ, {"WFSYNC_SOURCES": selected}):
            return web.render(status or {})

    def test_only_selected_sources_show(self):
        page = self.render("bitget,debitum")
        self.assertIn("Bitget", page)
        self.assertIn("action='/sync/debitum'", page)
        self.assertIn("action='/sync/run'", page)
        for other in ("Trading 212", "Bitvavo", "action='/sync/monefit'", "action='/sync/goandgrow'"):
            self.assertNotIn(other, page)

    def test_unselected_source_results_are_hidden(self):
        page = self.render("bitget",
                           {"results": {"bitget": {"status": "ok"}, "bitvavo": {"status": "error", "error": "stale"},
                                        "wealthfolio": {"status": "error", "error": "login"}}})
        self.assertNotIn("stale", page)
        self.assertIn("login", page)
        self.assertIn(web.LABELS["bitget"], page)

    def test_no_selection_explains_itself(self):
        for selected in ("", " , "):
            page = self.render(selected)
            self.assertIn("No source is selected", page)
            self.assertIn("WFSYNC_SOURCES", page)
            self.assertIn("trading212", page)  # the valid names, so the page alone is enough to fix it
            self.assertNotIn("<form", page)

    def test_upload_sources_alone_have_no_sync_button(self):
        page = self.render("monefit")
        self.assertIn("action='/sync/monefit'", page)
        self.assertNotIn("action='/sync/run'", page)
        self.assertNotIn("<table", page)
        self.assertNotIn("No source is selected", page)


class Cookie(unittest.TestCase):
    def test_session_survives_cookies_simplecookie_rejects(self):
        for header in ('foo={"x":1}; wf_session=abc', "theme=dark mode; wf_session=abc", 'a=b;wf_session="abc"'):
            self.assertEqual(web.session_cookie(header), "abc", header)
        self.assertIsNone(web.session_cookie("my_wf_session=abc; other=1"))
        self.assertEqual(web.session_cookie("wf_session=; wf_session=real"), "real")
        # Only token characters travel on to /auth/me.
        for bad in ("wf_session=a b", "wf_session=a\tX-Evil: 1", "wf_session=" + "a" * 513, "wf_session=a,b"):
            self.assertIsNone(web.session_cookie(bad), bad)
        self.assertIsNone(web.session_cookie(None))


class MalformedStatus(unittest.TestCase):
    def test_malformed_status_still_renders(self):
        page = web.render({"results": {"bitget": {"status": "ok", "skipped": 3, "manual_pricing": "ZZZ"},
                                       "bitvavo": "ok"}})
        self.assertIn("ok", page)
        self.assertIn("No run yet", web.render({"results": [1]}))
        self.assertNotIn("(-)", web.render({}))
        self.assertIn("(nightly)", web.render({"trigger": "nightly"}))
        with mock.patch.object(web.sync, "NIGHTLY_AT", (4, 5)):
            self.assertIn("every night at 04:05", web.render({}))


class LocalTime(unittest.TestCase):
    def test_utc_stamp_in_local_time(self):
        with mock.patch.dict("os.environ", {"TZ": "Africa/Johannesburg"}):
            time.tzset()
            self.assertEqual(web.local_time("2020-06-15T01:30:00+00:00"), "2020-06-15 03:30")
        time.tzset()
        self.assertEqual(web.local_time(None), "-")


class SessionCache(unittest.TestCase):
    def test_only_valid_sessions_are_cached(self):
        wf = FakeWf("good")
        checker = web.SessionChecker(wf)
        self.assertTrue(checker.valid("good"))
        self.assertTrue(checker.valid("good"))
        self.assertEqual(wf.calls, 1)
        self.assertFalse(checker.valid("bad"))
        self.assertFalse(checker.valid("bad"))
        self.assertEqual(wf.calls, 3)
        self.assertFalse(checker.valid(None))


class Endpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(web.SessionChecker(FakeWf("good")), ORIGIN))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def req(self, method, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        r.read()
        return r.status

    def test_page_requires_session(self):
        self.assertEqual(self.req("GET", "/sync"), 303)
        with mock.patch("wfsync.sync.load_status", return_value={}):
            self.assertEqual(self.req("GET", "/sync", {"Cookie": "wf_session=good"}), 200)

    def test_run_requires_origin_and_session(self):
        with mock.patch("wfsync.sync.run") as run:
            self.assertEqual(self.req("POST", "/sync/run", {"Cookie": "wf_session=good"}), 403)
            self.assertEqual(self.req("POST", "/sync/run", {"Origin": ORIGIN, "Cookie": "wf_session=bad"}), 403)
            self.assertEqual(self.req("POST", "/sync/run",
                                      {"Origin": "https://evil.test", "Cookie": "wf_session=good"}), 403)
            run.assert_not_called()
            self.assertEqual(self.req("POST", "/sync/run", {"Origin": ORIGIN, "Cookie": "wf_session=good"}), 303)
            run.assert_called_once()

    def body(self, path, headers):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.request("GET", path, headers=headers)
        return c.getresponse().read().decode()

    def test_stale_running_flag_shows_the_button(self):
        good = {"Cookie": "wf_session=good"}
        with mock.patch("wfsync.sync.load_status", return_value={"running": True}), \
             mock.patch("wfsync.sync.is_running", return_value=False):
            self.assertIn("Sync now", self.body("/sync", good))
        with mock.patch("wfsync.sync.load_status", return_value={"running": True}), \
             mock.patch("wfsync.sync.is_running", return_value=True):
            self.assertIn("disabled", self.body("/sync", good))

    def test_broken_request_line_gets_400(self):
        with socket.create_connection(("127.0.0.1", self.httpd.server_address[1]), timeout=5) as s:
            s.sendall(b"GET /sync HTTP/9x\r\n\r\n")
            response = b""
            while chunk := s.recv(4096):
                response += chunk
        # An invalid version gets an HTTP/0.9 style response: the error page without a status line.
        self.assertIn(b"Error code: 400", response)

    def test_rejected_request_line_omits_query_string(self):
        with self.assertLogs("wfsync.web", "INFO") as logs:
            with socket.create_connection(("127.0.0.1", self.httpd.server_address[1]), timeout=5) as s:
                s.sendall(b"GET /sync?token=abc extra HTTP/1.1\r\n\r\n")
                while s.recv(4096):
                    pass
            time.sleep(0.2)  # the handler thread logs after the response
        text = "\n".join(logs.output)
        self.assertIn("400", text)
        self.assertNotIn("token", text)

    def test_stalled_client_is_dropped(self):
        handler = web.make_handler(web.SessionChecker(FakeWf("good")), ORIGIN)
        self.assertTrue(handler.timeout and handler.timeout <= 60)
        quick = type("Quick", (handler,), {"timeout": 0.3})  # same mechanism, shorter wait
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), quick)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5) as s:
                s.sendall(b"GET /sync HT")  # never finishes the request line
                start = time.monotonic()
                while s.recv(4096):
                    pass
                self.assertLess(time.monotonic() - start, 3)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_broken_cookie_means_logged_out(self):
        self.assertEqual(self.req("GET", "/sync", {"Cookie": "wf_session=\"a\\015\\012X: 1\""}), 303)

    def test_health_is_open_unknown_paths_404(self):
        self.assertEqual(self.req("GET", "/healthz"), 200)
        self.assertEqual(self.req("GET", "/api/v1/accounts", {"Cookie": "wf_session=good"}), 404)


def multipart(files, boundary="xYz"):
    out = b""
    for name, data in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"a.csv\"\r\n"
                "Content-Type: text/csv\r\n\r\n").encode() + data + b"\r\n"
    return out + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


class Upload(unittest.TestCase):
    def test_file_field_is_read_verbatim(self):
        data = "Datum;Gebühren\r\n01.03.2020;0\r\n".encode()
        body, ctype = multipart({"other": b"x", web.UPLOAD_FIELD: data})
        self.assertEqual(web.upload_file(ctype, body), data)
        self.assertIsNone(web.upload_file(ctype, multipart({"other": b"x"})[0]))
        self.assertIsNone(web.upload_file("application/x-www-form-urlencoded", b"statement=x"))

    def test_card_shows_last_upload_escaped(self):
        page = web.render({"uploads": {"monefit": {"status": "error", "error": "transaction type \"<i>\" is unknown",
                                                   "at": "2020-03-27T10:00:00+00:00"}}})
        self.assertEqual(page.count("Import statement"), 3)
        self.assertIn("action='/sync/monefit'", page)
        self.assertIn("action='/sync/goandgrow'", page)
        self.assertIn("action='/sync/debitum'", page)
        self.assertIn("<h2>Go &amp; Grow</h2>", page)
        # Go & Grow, Debitum; Monefit shows its last upload
        self.assertEqual(page.count("No statement imported yet"), 2)
        self.assertIn("&lt;i&gt;", page)
        self.assertNotIn("<i>", page)
        self.assertEqual(web.render({}).count("No statement imported yet"), 3)


class UploadEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(web.SessionChecker(FakeWf("good")), ORIGIN))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def post(self, headers, body=b"", path="/sync/monefit"):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.request("POST", path, body=body, headers=headers)
        r = c.getresponse()
        r.read()
        return r.status

    def test_upload_requires_origin_and_session(self):
        body, ctype = multipart({web.UPLOAD_FIELD: b"csv"})
        with mock.patch("wfsync.sync.import_statement", return_value={"status": "ok"}) as imp:
            self.assertEqual(self.post({"Cookie": "wf_session=good", "Content-Type": ctype}, body), 403)
            self.assertEqual(self.post({"Origin": ORIGIN, "Cookie": "wf_session=bad", "Content-Type": ctype}, body),
                             403)
            imp.assert_not_called()
            self.assertEqual(self.post({"Origin": ORIGIN, "Cookie": "wf_session=good", "Content-Type": ctype}, body),
                             303)
            imp.assert_called_once_with("monefit", b"csv")
            good = {"Origin": ORIGIN, "Cookie": "wf_session=good", "Content-Type": ctype}
            self.assertEqual(self.post(good, body, path="/sync/goandgrow"), 303)
            imp.assert_called_with("goandgrow", b"csv")
            self.assertEqual(self.post(good, body, path="/sync/debitum"), 303)
            imp.assert_called_with("debitum", b"csv")
            for path in ("/sync/other", "/sync/", "/monefit", "monefit", "/sync/monefit/x"):
                self.assertEqual(self.post(good, body, path=path), 404, path)
            self.assertEqual(imp.call_count, 3)

    def test_unselected_upload_is_404(self):
        body, ctype = multipart({web.UPLOAD_FIELD: b"csv"})
        good = {"Origin": ORIGIN, "Cookie": "wf_session=good", "Content-Type": ctype}
        with mock.patch.dict(os.environ, {"WFSYNC_SOURCES": "goandgrow"}), \
             mock.patch("wfsync.sync.import_statement", return_value={"status": "ok"}) as imp:
            self.assertEqual(self.post(good, body, path="/sync/monefit"), 404)
            imp.assert_not_called()
            self.assertEqual(self.post(good, body, path="/sync/goandgrow"), 303)

    def test_logged_out_post_hides_the_selection(self):
        body, ctype = multipart({web.UPLOAD_FIELD: b"csv"})
        unrelated = {"Origin": "https://elsewhere.example.test", "Content-Type": ctype}
        logged_out = {"Origin": ORIGIN, "Content-Type": ctype}
        with mock.patch.dict(os.environ, {"WFSYNC_SOURCES": "goandgrow"}):
            for path in ("/sync/monefit", "/sync/goandgrow", "/sync/other"):
                self.assertEqual(self.post(unrelated, body, path=path), 403, path)
                self.assertEqual(self.post(logged_out, body, path=path), 403, path)

    def test_status_json_shows_only_selected_sources(self):
        state = {"results": {"bitget": {"status": "ok"}, "bitvavo": {"status": "ok"}, "wealthfolio": {"status": "ok"}},
                 "uploads": {"monefit": {"status": "ok"}, "debitum": {"status": "ok"}}}
        with mock.patch.dict(os.environ, {"WFSYNC_SOURCES": "bitget,debitum"}), \
             mock.patch("wfsync.sync.load_status", return_value=state):
            c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
            c.request("GET", "/sync/status.json", headers={"Cookie": "wf_session=good"})
            shown = json.loads(c.getresponse().read())
        self.assertEqual(sorted(shown["results"]), ["bitget", "wealthfolio"])
        self.assertEqual(sorted(shown["uploads"]), ["debitum"])

    def test_too_large_missing_or_busy(self):
        good = {"Origin": ORIGIN, "Cookie": "wf_session=good"}
        with mock.patch("wfsync.sync.import_statement", return_value=None) as imp:
            self.assertEqual(self.post(dict(good, **{"Content-Length": str(web.MAX_UPLOAD + 1)})), 413)
            self.assertEqual(self.post(dict(good, **{"Content-Length": "-1"})), 411)
            body, ctype = multipart({"other": b"x"})
            self.assertEqual(self.post(dict(good, **{"Content-Type": ctype}), body), 400)
            imp.assert_not_called()
            body, ctype = multipart({web.UPLOAD_FIELD: b"csv"})
            self.assertEqual(self.post(dict(good, **{"Content-Type": ctype}), body), 409)


if __name__ == "__main__":
    unittest.main()
