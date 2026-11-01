import http.client
import io
import unittest
import urllib.error

from wfsync import httpjson
from wfsync.wealthfolio import ImportRefused, UnexpectedResponse, Wealthfolio


class ErrorOpener:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def open(self, req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, self.status, "x", {}, io.BytesIO(self.body))


class ErrorReason(unittest.TestCase):
    def reason(self, body):
        with self.assertRaises(httpjson.HttpError) as cm:
            httpjson.request_json(ErrorOpener(400, body), "GET", "https://x.test/")
        return str(cm.exception)

    def test_codes_pass_free_text_does_not(self):
        self.assertEqual(self.reason(b'{"errorCode": 205, "error": "Invalid parameters"}'), "HTTP 400: 205")
        self.assertEqual(self.reason(b'{"code": "BusinessException"}'), "HTTP 400: BusinessException")
        # Free text may carry portfolio figures: never into the reason.
        self.assertEqual(self.reason(b'{"error": "Insufficient quantity: have 12.5, need 20"}'), "HTTP 400: error")
        self.assertEqual(self.reason(b'{"error": "12.5"}'), "HTTP 400: error")
        # A bare number only as Bitvavo's `errorCode`, never in `code` or `error`.
        self.assertEqual(self.reason(b'{"error": "20"}'), "HTTP 400: error")
        self.assertEqual(self.reason(b'{"code": 20}'), "HTTP 400: error")
        self.assertEqual(self.reason(b"not json"), "HTTP 400: error")


class Paging(unittest.TestCase):
    def wf(self, total, cap, ignore_page=False):
        wf = Wealthfolio("http://wf.test", "https://o.test")
        acts = [{"id": str(i), "comment": f"wfsync:x:{i}"} for i in range(total)]

        def call(method, path, body=None, timeout=300):
            page = 0 if ignore_page else body["page"]
            size = min(body["pageSize"], cap)
            return {"data": acts[page * size:(page + 1) * size], "meta": {"totalRowCount": total}}
        wf.call = call
        return wf

    def test_page_size_cap_keeps_all_references(self):
        self.assertEqual(len(self.wf(1000, 100).activities("a")), 1000)

    def test_server_ignoring_the_page_terminates(self):
        self.assertEqual(len(self.wf(30, 500, ignore_page=True).activities("a")), 30)


class ResponseShape(unittest.TestCase):
    def wf(self, response):
        wf = Wealthfolio("http://wf.test", "https://o.test")
        wf.call = lambda *a, **k: response
        return wf

    def test_changed_search_shape_stops(self):
        for response in (None, {}, {"data": []}, {"data": None, "meta": {"totalRowCount": 0}}, {"items": []},
                         {"data": [{"id": "1"}], "meta": {"totalRowCount": 1}},
                         {"data": [{"comment": "x"}], "meta": {"totalRowCount": 1}}, {"data": [], "meta": {}}):
            with self.assertRaises(UnexpectedResponse, msg=response):
                self.wf(response).activities("a")

    def test_search_ending_below_its_total_stops(self):
        with self.assertRaises(UnexpectedResponse):
            self.wf({"data": [], "meta": {"totalRowCount": 3}}).activities("a")

    def test_changed_account_shape_stops(self):
        for response in (None, {}, [{"id": "1"}], [{"provider": "wfsync", "providerAccountId": "x"}]):
            with self.assertRaises(UnexpectedResponse, msg=response):
                self.wf(response).ensure_account("x", "X", "CASH", "EUR")

    def test_known_account_is_found(self):
        acc = {"id": "1", "provider": "wfsync", "providerAccountId": "x"}
        self.assertEqual(self.wf([acc]).ensure_account("x", "X", "CASH", "EUR"), "1")
        self.assertIsNone(self.wf([]).ensure_account("x", "X", "CASH", "EUR", create=False))

    def test_check_needs_a_list_of_rows(self):
        for response in (None, {}, [{"lineNumber": 1}]):
            with self.assertRaises(UnexpectedResponse, msg=response):
                self.wf(response).check([{"lineNumber": 1}])


class ImportResponse(unittest.TestCase):
    ROWS = [{"lineNumber": 1}, {"lineNumber": 2}]

    def run_import(self, summary=None, lines=(1, 2)):
        wf = Wealthfolio("http://wf.test", "https://o.test")
        summary = {"total": 2, "imported": 2, "skipped": 0, "success": True} | (summary or {})
        wf.call = lambda *a, **k: {"activities": [{"lineNumber": n} for n in lines], "summary": summary}
        return wf.import_rows(self.ROWS)

    def test_counts_come_from_the_summary(self):
        self.assertEqual(self.run_import({"imported": 1, "skipped": 1}), {"imported": 1, "skipped": 1})

    def test_summary_must_cover_the_rows_sent(self):
        for summary in ({"total": 1}, {"imported": 1}):
            with self.assertRaises(UnexpectedResponse, msg=summary):
                self.run_import(summary)

    def test_response_rows_must_match_rows_sent(self):
        for lines in ((1,), (1, 3), (1, 1)):
            with self.assertRaises(UnexpectedResponse, msg=lines):
                self.run_import(lines=lines)

    def test_changed_shape_stops(self):
        wf = Wealthfolio("http://wf.test", "https://o.test")
        for response in (None, {}, {"activities": [], "summary": {}}):
            wf.call = lambda *a, response=response, **k: response
            with self.assertRaises(UnexpectedResponse, msg=response):
                wf.import_rows(self.ROWS)

    def test_refused_batch_raises_without_its_message(self):
        with self.assertRaises(ImportRefused) as cm:
            self.run_import({"imported": 0, "skipped": 2, "success": False, "errorMessage": "row 1: 12.5"})
        self.assertNotIn("12.5", str(cm.exception))


class Quote(unittest.TestCase):
    def test_failed_quote_raises_unknown_is_empty(self):
        wf = Wealthfolio("http://wf.test", "https://o.test")
        wf.call = lambda *a, **k: (_ for _ in ()).throw(httpjson.HttpError(502, "error"))
        with self.assertRaises(httpjson.HttpError):
            wf.quote("ABC", "XETR")
        wf.call = lambda *a, **k: {"currency": None, "price": None}
        self.assertEqual(wf.quote("AAA-EUR").get("currency"), None)


class Response:
    def __init__(self, status, ctype, url):
        self.status, self.url = status, url
        self.headers = {"Content-Type": ctype}

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SessionCheck(unittest.TestCase):
    def check(self, status, ctype, url=None, value="x"):
        class Opener:
            def open(self, req, timeout=None):
                if "\n" in req.get_header("Cookie"):
                    raise ValueError("Invalid header value")
                return Response(status, ctype, url or req.full_url)
        return Wealthfolio("http://wf.test", "https://o.test", opener=Opener()).session_valid(value)

    def test_only_wealthfolio_json_counts(self):
        self.assertTrue(self.check(200, "application/json"))
        self.assertFalse(self.check(200, "text/html"))  # SPA fallback after an update
        self.assertFalse(self.check(200, "application/json", url="http://wf.test/login"))  # redirected
        self.assertFalse(self.check(200, "application/json", value="a\nb"))  # header error denies

    def test_broken_reply_denies_instead_of_raising(self):
        class Broken:
            def open(self, req, timeout=None):
                raise http.client.BadStatusLine("garbage")
        self.assertFalse(Wealthfolio("http://wf.test", "https://o.test", opener=Broken()).session_valid("x"))


class Backups(unittest.TestCase):
    def test_backup_name_cannot_escape_the_path(self):
        wf = Wealthfolio("http://wf.test", "https://o.test")
        seen = []
        wf.call = lambda method, path, *a, **k: seen.append(path)
        wf.delete_backup("../../accounts")
        self.assertEqual(seen, ["/utilities/database/backups/..%2F..%2Faccounts"])


if __name__ == "__main__":
    unittest.main()
