import fcntl
import io
import json
import os
import tempfile
import threading
import types
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest import mock

from wfsync import __main__ as main
from wfsync import config, sync
from wfsync.httpjson import HttpError
from wfsync.rows import Skipped
from wfsync.sources import trading212
from wfsync.sources.trading212 import Trading212
from wfsync.wealthfolio import ImportRefused


class FakeWf:
    """Accepts only the symbols in `known`."""

    def __init__(self, known):
        self.known = known

    def check(self, rows):
        out = []
        for r in rows:
            ok = r["symbol"] in self.known
            out.append(dict(r, isValid=ok, errors=None if ok else {"symbol": ["not found"]}))
        return out


class Dedupe(unittest.TestCase):
    def test_existing_rows_are_not_resent(self):
        class Wf(FakeWf):
            def __init__(self):
                super().__init__({"A", "B"})
                self.imported = []
                self.has_account = True

            def ensure_account(self, *a, create=True):
                return "acc" if create or self.has_account else None

            def activities(self, account_id):
                return [{"id": "1", "comment": "wfsync:x:a"}, {"id": "2", "comment": "typed by hand"}]

            def import_rows(self, rows):
                self.imported.extend(r["comment"] for r in rows)
                return {"imported": len(rows), "skipped": 0}

        module = types.SimpleNamespace(
            SOURCE_ID="x", ACCOUNT_NAME="X",
            map_activities=lambda data, acc: ([{"date": "2", "symbol": "A", "comment": "wfsync:x:a"},
                                               {"date": "1", "symbol": "B", "comment": "wfsync:x:b"}], []))
        client = types.SimpleNamespace(fetch=lambda: {"currency": "EUR"})
        wf = Wf()
        result = sync.sync_source(wf, module, "SECURITIES", client, dry_run=False)
        self.assertEqual(wf.imported, ["wfsync:x:b"])
        self.assertEqual(result["new"], 1)
        wf.imported.clear()
        sync.sync_source(wf, module, "SECURITIES", client, dry_run=True)
        self.assertEqual(wf.imported, [])
        # A dry run without an account writes nothing, not even the account.
        wf.has_account = False
        self.assertEqual(sync.sync_source(wf, module, "SECURITIES", client, dry_run=True)["new"], None)


class FetchIntegrity(unittest.TestCase):
    """Rows within one fetch and Wealthfolio's check response must add up to what was sent."""

    def run_source(self, rows, check=None):
        imported = []
        wf = mock.Mock()
        wf.ensure_account.return_value = "acc"
        wf.activities.return_value = []
        wf.check.side_effect = check or (lambda chunk: [dict(r, isValid=True) for r in chunk])

        def import_rows(chunk):
            imported.extend(r["comment"] for r in chunk)
            return {"imported": len(chunk), "skipped": 0}
        wf.import_rows.side_effect = import_rows
        module = types.SimpleNamespace(SOURCE_ID="x", ACCOUNT_NAME="X", map_activities=lambda data, acc: (rows, []))
        client = types.SimpleNamespace(fetch=lambda: {"currency": "EUR"})
        return sync.sync_source(wf, module, "CASH", client, dry_run=False), imported

    def test_repeated_row_is_imported_once(self):
        row = {"date": "1", "symbol": "", "comment": "wfsync:x:1", "amount": "5"}
        result, imported = self.run_source([row, dict(row), {"date": "2", "symbol": "", "comment": "wfsync:x:2"}])
        self.assertEqual((imported, result["new"]), (["wfsync:x:1", "wfsync:x:2"], 2))

    def test_different_rows_sharing_reference_stop_source(self):
        with self.assertRaisesRegex(RuntimeError, "share one source reference"):
            self.run_source([{"date": "1", "comment": "wfsync:x:1", "amount": "5"},
                             {"date": "1", "comment": "wfsync:x:1", "amount": "6"}])

    def test_check_row_mismatch_stops_the_source(self):
        rows = [{"date": "1", "symbol": "", "comment": "wfsync:x:1"},
                {"date": "2", "symbol": "", "comment": "wfsync:x:2"}]
        for check in (lambda chunk: [dict(chunk[0], isValid=True)],
                      lambda chunk: [dict(chunk[0], isValid=True)] * 2,
                      lambda chunk: [dict(r, isValid=True, lineNumber=r["lineNumber"] + 1) for r in chunk]):
            with self.assertRaisesRegex(RuntimeError, "does not match our line numbers"):
                self.run_source([dict(r) for r in rows], check)

    def test_imported_skips_are_not_reported_again(self):
        wf = mock.Mock()
        wf.ensure_account.return_value = "acc"
        wf.activities.return_value = [{"comment": "wfsync:x:old"}]
        module = types.SimpleNamespace(SOURCE_ID="x", ACCOUNT_NAME="X", map_activities=lambda data, acc: (
            [], [Skipped("wfsync:x:old", "old"), Skipped("wfsync:x:new", "new")]))
        client = types.SimpleNamespace(fetch=lambda: {"currency": "EUR"})
        self.assertEqual(sync.sync_source(wf, module, "CASH", client, dry_run=False)["skipped"], {"new": 1})


def source(rows, skipped=()):
    return types.SimpleNamespace(SOURCE_ID="x", ACCOUNT_NAME="X",
                                 map_activities=lambda data, acc: (rows, list(skipped)))


def client():
    return types.SimpleNamespace(fetch=lambda: {"currency": "EUR"})


class RecordingWf:
    """Wealthfolio with `check` and `import_rows` scripted per row, recording every batch."""

    def __init__(self, flags=None, outcome=None):
        self.flags, self.outcome = flags or {}, outcome
        self.checked, self.batches = [], []

    def ensure_account(self, *a, create=True):
        return "acc"

    def activities(self, account_id):
        return []

    def check(self, rows):
        self.checked.append(rows)
        return [dict(r, isValid=True) | self.flags.get(r["comment"], {}) for r in rows]

    def import_rows(self, rows):
        self.batches.append([r["comment"] for r in rows])
        return self.outcome(rows) if self.outcome else {"imported": len(rows), "skipped": 0}


class Duplicates(unittest.TestCase):
    ROWS = [{"date": "1", "symbol": "A", "comment": "wfsync:x:1", "_group": "g"},
            {"date": "1", "symbol": "USDT", "comment": "wfsync:x:2", "_group": "g"},
            {"date": "2", "symbol": "B", "comment": "wfsync:x:3"}]

    def test_duplicate_makes_the_source_partial(self):
        for flag in ({"duplicateOfId": "d"}, {"duplicateOfLineNumber": 1}):
            wf = RecordingWf({"wfsync:x:3": flag})
            result = sync.sync_source(wf, source([dict(r) for r in self.ROWS]), "CASH", client(), False)
            self.assertEqual((result["status"], result["duplicates"], result["new"]), ("partial", 1, 2))
            self.assertEqual(wf.batches, [["wfsync:x:1", "wfsync:x:2"]])

    def test_duplicate_leg_holds_back_its_partner(self):
        wf = RecordingWf({"wfsync:x:1": {"duplicateOfId": "d"}})
        result = sync.sync_source(wf, source([dict(r) for r in self.ROWS]), "CASH", client(), False)
        self.assertEqual((result["duplicates"], result["held_back"], result["new"]), (1, 1, 1))
        self.assertEqual(wf.batches, [["wfsync:x:3"]])

    def test_invalid_duplicate_counts_once(self):
        wf = RecordingWf({"wfsync:x:3": {"isValid": False, "duplicateOfId": "d"}})
        result = sync.sync_source(wf, source([dict(r) for r in self.ROWS]), "CASH", client(), False)
        self.assertEqual((result["invalid"], result["duplicates"]), (1, 0))


class ImportOutcome(unittest.TestCase):
    def test_new_counts_what_wealthfolio_imported(self):
        rows = [{"date": "1", "symbol": "A", "comment": f"wfsync:x:{i}"} for i in range(3)]
        wf = RecordingWf(outcome=lambda chunk: {"imported": 2, "skipped": 1})
        result = sync.sync_source(wf, source(rows), "CASH", client(), False)
        self.assertEqual((result["status"], result["new"], result["not_imported"]), ("partial", 2, 1))

    def test_dry_run_counts_would_be_imports(self):
        wf = RecordingWf()
        result = sync.sync_source(wf, source([{"date": "1", "symbol": "A", "comment": "wfsync:x:1"}]), "CASH", client(),
                                  True)
        self.assertEqual((result["status"], result["new"], wf.batches), ("ok", 1, []))

    def test_failed_import_keeps_earlier_batches(self):
        rows = [{"date": str(i), "symbol": "A", "comment": f"wfsync:x:{i}"} for i in range(sync.BATCH + 5)]
        done = []

        def fail_second(chunk):
            if done:
                raise ImportRefused("batch refused")
            done.extend(r["comment"] for r in chunk)
            return {"imported": len(chunk), "skipped": 0}

        wf = RecordingWf(outcome=fail_second)
        with self.assertRaises(ImportRefused):
            sync.sync_source(wf, source([dict(r) for r in rows]), "CASH", client(), False)
        self.assertEqual(len(done), sync.BATCH)
        again = RecordingWf()
        again.activities = lambda account_id: [{"id": c, "comment": c} for c in done]
        result = sync.sync_source(again, source([dict(r) for r in rows]), "CASH", client(), False)
        self.assertEqual(result["new"], 5)


class Batches(unittest.TestCase):
    def rows(self, n):
        """`n` two-leg trades on one date."""
        return [{"date": "1", "symbol": s, "comment": f"wfsync:x:{i}:{s}", "_group": f"g{i}"}
                for i in range(n) for s in ("A", "USDT")]

    def test_no_group_is_split_across_batches(self):
        wf = RecordingWf()
        count = sync.BATCH // 2 + sync.BATCH // 4
        sync.sync_source(wf, source(self.rows(count) + [{"date": "1", "symbol": "Z", "comment": "wfsync:x:z"}]),
                         "CASH", client(), False)
        for batch in wf.batches:
            self.assertLessEqual(len(batch), sync.BATCH)
            ids = [c.split(":")[2] for c in batch if c != "wfsync:x:z"]
            self.assertTrue(all(ids.count(i) == 2 for i in ids), "group cut")
        self.assertEqual(sum(map(len, wf.batches)), count * 2 + 1)

    def test_batch_with_odd_offset_closes_early(self):
        wf = RecordingWf()
        lone = {"date": "0", "symbol": "Z", "comment": "wfsync:x:z"}
        sync.sync_source(wf, source([lone] + self.rows(sync.BATCH // 2)), "CASH", client(), False)
        self.assertEqual([len(b) for b in wf.batches], [sync.BATCH - 1, 2])
        self.assertEqual([len(b) for b in wf.checked], [sync.BATCH - 1, 2])


class SkippedStatus(unittest.TestCase):
    def test_known_skip_stays_visible_and_ok(self):
        result = sync.sync_source(RecordingWf(), source([], [Skipped("wfsync:x:1", "unsupported")]), "CASH", client(),
                                  False)
        self.assertEqual((result["status"], result["skipped"], result["pending"]), ("ok", {"unsupported": 1}, 0))

    def test_transient_skip_makes_the_source_partial(self):
        skip = Skipped("wfsync:x:1", "price missing", transient=True)
        result = sync.sync_source(RecordingWf(), source([], [skip]), "CASH", client(), False)
        self.assertEqual((result["status"], result["pending"]), ("partial", 1))


class Origin(unittest.TestCase):
    def origin(self, value):
        with mock.patch.dict(os.environ, {"WFSYNC_ORIGIN": value}):
            return config.public_origin()

    def test_accepts_scheme_host_and_port(self):
        for value in ("https://wealthfolio.example.test", "http://192.0.2.1:8088", "https://a.example.test:8443"):
            self.assertEqual(self.origin(value), value)

    def test_refuses_non_browser_origins(self):
        for value in ("", "wealthfolio.example.test", "https://a.test/", "https://a.test/sync", "https://a.test?x=1",
                      "https://a.test#x", "ftp://a.test", "https://", "https://user@a.test", "https://A.test",
                      "https://a.test:443", "http://a.test:80", "https://a.test:port"):
            with self.assertRaises(config.ConfigError, msg=value):
                self.origin(value)

    def test_unset_is_refused(self):
        with mock.patch.dict(os.environ), self.assertRaises(config.ConfigError):
            os.environ.pop("WFSYNC_ORIGIN", None)
            config.public_origin()


class YahooAliases(unittest.TestCase):
    def aliases(self, value):
        with mock.patch.dict(os.environ, {"WFSYNC_YAHOO_ALIASES": value}):
            return config.yahoo_aliases()

    def test_empty_keeps_the_builtin(self):
        self.assertEqual(self.aliases(" "), config.BUILTIN_YAHOO_ALIASES)

    def test_pairs_extend_and_override(self):
        self.assertEqual(self.aliases("AAA=AAA123, POL=POL9"), {"AAA": "AAA123", "POL": "POL9"})

    def test_bad_format_is_refused(self):
        for value in ("AAA", "AAA=", "=AAA", "AAA=B=C", "AAA=B C", "aaa=AAA1,,BBB"):
            with self.assertRaises(config.ConfigError, msg=value):
                self.aliases(value)


class StartChecks(unittest.TestCase):
    def main(self, *args, **env):
        with mock.patch.dict(os.environ, {"WFSYNC_ORIGIN": "https://o.test"} | env), \
             mock.patch("sys.argv", ["wfsync", *args]), mock.patch.object(sync, "run", return_value={"ok": True}), \
             mock.patch.object(sync, "backup", return_value="x"), self.assertLogs("wfsync", "ERROR"), \
             self.assertRaises(SystemExit) as cm:
            main.main()
        return cm.exception.code

    def test_bad_settings_end_with_exit_78(self):
        for args in (("run",), ("backup",)):
            self.assertEqual(self.main(*args, WFSYNC_ORIGIN="https://o.test/"), main.EXIT_CONFIG)
            self.assertEqual(self.main(*args, WFSYNC_YAHOO_ALIASES="AAA"), main.EXIT_CONFIG)
        self.assertEqual(self.main("run", WFSYNC_SOURCES="nope"), main.EXIT_CONFIG)


class Run(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(sync, "STATE_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def stored(self):
        with open(os.path.join(self.dir.name, "status.json")) as f:
            return json.load(f)

    def test_failure_clears_the_running_flag(self):
        with mock.patch.object(sync, "sync_all", side_effect=KeyError("WFSYNC_ORIGIN")):
            status = sync.run(trigger="test")
        self.assertFalse(status["ok"])
        self.assertFalse(self.stored()["running"])
        self.assertNotIn("last_success", self.stored())

    def test_partial_source_sets_no_last_success(self):
        with mock.patch.object(sync, "sync_all", return_value={"x": {"status": "partial"}}):
            status = sync.run()
        self.assertFalse(status["ok"])
        self.assertNotIn("last_success", self.stored())
        with mock.patch.object(sync, "sync_all", return_value={"x": {"status": "ok", "skipped": {"unsupported": 1}}}):
            self.assertTrue(sync.run()["ok"])
        self.assertIn("last_success", self.stored())

    def test_run_without_results_is_not_ok(self):
        for selected in ("", "bitvavo", "bitvavo,monefit"):
            with mock.patch.dict(os.environ, {"WFSYNC_SOURCES": selected}), \
                 mock.patch.object(sync, "sync_all", return_value={}):
                self.assertFalse(sync.run()["ok"], selected)

    def test_unreadable_lock_reports_no_run(self):
        with mock.patch.object(sync, "lock_path", return_value="/nonexistent-dir/run.lock"):
            self.assertFalse(sync.is_running())

    def test_lock_probe_sees_a_running_sync(self):
        seen = []
        with mock.patch.object(sync, "sync_all",
                               side_effect=lambda dry: seen.append(sync.is_running()) or {"x": {"status": "ok"}}):
            sync.run(trigger="test")
        self.assertEqual(seen, [True])
        self.assertFalse(sync.is_running())
        self.assertTrue(self.stored()["ok"])

    def test_logs_carry_no_figures(self):
        module = types.SimpleNamespace(SOURCE_ID="x")
        wf = mock.Mock()
        with mock.patch.object(sync, "Wealthfolio", return_value=wf), \
             mock.patch.object(sync, "configured_sources", return_value=[(module, "SECURITIES", lambda: None)]), \
             mock.patch.object(sync.secrets, "read_secret", return_value="pw"), \
             mock.patch.dict(os.environ, {"WFSYNC_ORIGIN": "https://o.test"}), \
             mock.patch.object(sync, "sync_source", side_effect=[ValueError("have 12.5, need 20")]), \
             self.assertLogs("wfsync", level="INFO") as logs:
            results = sync.sync_all(False)
        self.assertEqual(results["x"], {"status": "error", "error": "ValueError in sync.py sync_all"})
        self.assertFalse(any(ch.isdigit() for line in logs.output for ch in line), logs.output)


class Nightly(unittest.TestCase):
    def test_due_once_in_the_window(self):
        day = datetime(2020, 3, 25, 3, 30)
        self.assertTrue(main.due(day, None))
        self.assertFalse(main.due(day, day.date()))
        self.assertFalse(main.due(datetime(2020, 3, 25, 3, 29), None))
        self.assertFalse(main.due(datetime(2020, 3, 25, 4, 30), None))  # past the window: next night
        self.assertTrue(main.due(datetime(2020, 3, 25, 4, 29), None))


class NightlyLoop(unittest.TestCase):
    def run_loop(self, moments, run):
        """Walks `nightly_loop` through `moments`, one per pass."""
        clock = mock.Mock()
        clock.now.side_effect = moments
        with mock.patch.object(main, "datetime", clock), mock.patch.object(main.time, "sleep") as sleep, \
             mock.patch.object(sync, "run", side_effect=run) as ran, self.assertRaises(StopIteration):
            sleep.side_effect = [None] * (len(moments) - 1) + [StopIteration]
            main.nightly_loop()
        return ran

    def at(self, day, minute=0):
        return datetime(2020, 3, day, *sync.NIGHTLY_AT) + timedelta(minutes=minute)

    def test_one_run_per_night(self):
        ran = self.run_loop([self.at(1), self.at(1, 1), self.at(2)], None)
        self.assertEqual(ran.call_args_list, [mock.call(trigger="nightly")] * 2)

    def test_nothing_runs_outside_the_window(self):
        self.run_loop([self.at(1, main.NIGHTLY_WINDOW_MIN)], None).assert_not_called()

    def test_loop_survives_failed_run_without_retry(self):
        with self.assertLogs("wfsync", "ERROR") as logs:
            ran = self.run_loop([self.at(1), self.at(1, 1), self.at(2)], [ValueError("x"), {"ok": True}])
        self.assertEqual(ran.call_count, 2)
        self.assertEqual(logs.output, ["ERROR:wfsync:nightly run failed: ValueError"])


class Sources(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"WFSYNC_SOURCES": "trading212,bitvavo,bitget"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_source_without_secrets_shows_as_error(self):
        wf = mock.Mock()
        ran = []
        with tempfile.TemporaryDirectory() as d:
            for name in ("wealthfolio_password", "t212_key", "t212_secret"):
                with open(os.path.join(d, name), "w") as f:
                    f.write("x")
            with mock.patch.object(sync.secrets, "SECRETS_DIR", d), \
                 mock.patch.object(sync, "wealthfolio", return_value=wf), \
                 mock.patch.object(sync, "sync_source",
                                   side_effect=lambda w, m, *a: ran.append(m.SOURCE_ID) or {"status": "ok"}):
                results = sync.sync_all(False)
        self.assertEqual(ran, ["trading212"])
        self.assertEqual(results["bitvavo"], {"status": "error", "error": "Credentials missing (bitvavo_key)"})

    def test_missing_wealthfolio_password_is_named(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sync.secrets, "SECRETS_DIR", d), \
             mock.patch.object(sync, "wealthfolio", return_value=mock.Mock()):
            results = sync.sync_all(False)
        self.assertEqual(results, {"wealthfolio": {"status": "error",
                                                   "error": "Login: Credentials missing (wealthfolio_password)"}})

    def test_error_names_function_not_message(self):
        wf = mock.Mock()
        wf.activities.return_value = []
        client = types.SimpleNamespace(fetch=lambda: {"currency": "EUR"})  # no `orders` key, as after an API rename
        try:
            sync.sync_source(wf, trading212, "SECURITIES", client, False)
        except KeyError as e:
            text = sync.error_text(e)
        self.assertEqual(text, f"KeyError in sources{os.sep}trading212.py map_activities")
        with self.assertRaises(KeyError) as cm:
            raise KeyError("have 12.5, need 20")
        self.assertNotIn("12", sync.error_text(cm.exception))

    def test_http_error_names_the_business_function(self):
        class Opener:
            calls = 0

            def open(self, req, timeout=None):
                Opener.calls += 1
                if Opener.calls == 1:  # account info succeeds, the first page of orders fails
                    return io.BytesIO(b'{"currencyCode": "EUR"}')
                raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(b'{"code": "AuthFailed"}'))

        try:  # not assertRaises: it drops the traceback that error_text reads
            Trading212("k", "s", opener=Opener(), pause=0).fetch()
            self.fail("no HttpError")
        except HttpError as e:
            text = sync.error_text(e)
        self.assertEqual(text, f"HTTP 401: AuthFailed in sources{os.sep}trading212.py fetch")

    def test_empty_secret_file_counts_as_missing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sync.secrets, "SECRETS_DIR", d):
            with open(os.path.join(d, "t212_key"), "w") as f:
                f.write(" \n")
            with self.assertRaises(sync.secrets.MissingSecret) as cm:
                sync.secrets.read_secret("t212_key")
        self.assertEqual(sync.error_text(cm.exception), "Credentials missing (t212_key)")

    def test_non_object_status_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sync, "STATE_DIR", d):
            with open(os.path.join(d, "status.json"), "w") as f:
                f.write("[]")
            self.assertEqual(sync.load_status(), {})


class Backup(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(sync, "STATE_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def wf(self, protection="encrypted"):
        wf = mock.Mock()
        wf.backup.return_value = {"filename": "new.db"}
        # m0 comes from Wealthfolio's own backup button: same endpoint, same reason.
        wf.list_backups.return_value = [
            {"filename": f"m{i}.db", "reason": "manual", "modifiedAt": f"2020-03-2{i}T00:00:00Z",
             "protection": "encrypted"} for i in range(5)
        ] + [{"filename": "new.db", "reason": "manual", "modifiedAt": "2020-03-29T00:00:00Z", "protection": protection},
             {"filename": "pre.db", "reason": "pre_migration", "modifiedAt": "2020-01-01T00:00:00Z",
              "protection": "encrypted"}]
        return wf

    def run_backup(self, wf):
        with mock.patch.object(sync, "Wealthfolio", return_value=wf), \
             mock.patch.object(sync.secrets, "read_secret", return_value="pw"), \
             mock.patch.dict(os.environ, {"WFSYNC_ORIGIN": "https://o.test"}):
            return sync.backup(keep=3)

    def test_prunes_only_its_own_copies(self):
        sync.save_own_backups(["m1.db", "gone.db", "m2.db", "m3.db"])
        wf = self.wf()
        self.assertEqual(self.run_backup(wf), "new.db")
        self.assertEqual([c.args[0] for c in wf.delete_backup.call_args_list], ["m1.db"])
        self.assertEqual(sync.load_own_backups(), ["m2.db", "m3.db", "new.db"])

    def test_failed_delete_keeps_the_name(self):
        sync.save_own_backups(["m1.db", "m2.db", "m3.db"])
        wf = self.wf()
        wf.delete_backup.side_effect = HttpError(500, "error")
        self.assertEqual(self.run_backup(wf), "new.db")
        self.assertEqual(sync.load_own_backups(), ["m1.db", "m2.db", "m3.db", "new.db"])

    def test_new_copy_is_tracked_before_pruning(self):
        sync.save_own_backups(["m1.db", "m2.db", "m3.db"])
        wf = self.wf()
        wf.delete_backup.side_effect = KeyError("unexpected")
        with self.assertRaises(KeyError):
            self.run_backup(wf)
        self.assertIn("new.db", sync.load_own_backups())

    def test_first_run_deletes_nothing(self):
        wf = self.wf()
        self.run_backup(wf)
        wf.delete_backup.assert_not_called()
        self.assertEqual(sync.load_own_backups(), ["new.db"])

    def test_unencrypted_copy_is_refused(self):
        wf = self.wf("none")
        with self.assertRaises(RuntimeError):
            self.run_backup(wf)
        wf.delete_backup.assert_not_called()


class Lock(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(sync, "STATE_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def test_run_waits_for_probe_not_run(self):
        with open(sync.lock_path(), "a") as probe:
            fcntl.flock(probe, fcntl.LOCK_SH)
            threading.Timer(0.25, lambda: fcntl.flock(probe, fcntl.LOCK_UN)).start()
            with mock.patch.object(sync, "sync_all", return_value={"x": {"status": "ok"}}):
                self.assertTrue(sync.run(trigger="test")["ok"])
        with open(sync.lock_path(), "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            with mock.patch.object(sync, "LOCK_RETRY_S", 0.01), mock.patch.object(sync, "sync_all") as sa:
                self.assertIsNone(sync.run(trigger="test"))
                sa.assert_not_called()


class Selection(unittest.TestCase):
    def selected(self, value):
        env = {} if value is None else {"WFSYNC_SOURCES": value}
        with mock.patch.dict(os.environ, env):
            if value is None:
                os.environ.pop("WFSYNC_SOURCES", None)
            return sync.selected_sources()

    def test_names_tolerate_spaces_and_repeats(self):
        self.assertEqual(self.selected("bitvavo, monefit ,bitvavo,"), {"bitvavo", "monefit"})

    def test_every_source_module_can_be_selected(self):
        ids = {"trading212", "bitvavo", "bitget", "monefit", "goandgrow", "debitum"}
        self.assertEqual(self.selected(",".join(sorted(ids))), ids)

    def test_unset_empty_or_blank_selects_nothing(self):
        for value in (None, "", " , "):
            self.assertEqual(self.selected(value), set(), value)

    def test_unknown_name_is_refused_and_named(self):
        for value in ("bitvavo,kraken", "Bitvavo"):
            with self.assertRaises(sync.ConfigError) as cm:
                self.selected(value)
            self.assertIn(value.split(",")[-1], str(cm.exception))
            self.assertIn("trading212", str(cm.exception))  # the message lists what is valid

    def test_only_selected_sources_are_active(self):
        with mock.patch.dict(os.environ, {"WFSYNC_SOURCES": "bitget,debitum"}):
            self.assertEqual([s[0].SOURCE_ID for s in sync.configured_sources()], ["bitget"])
            self.assertEqual(list(sync.upload_sources()), ["debitum"])

    def test_unselected_source_needs_no_secret(self):
        ran = []
        with tempfile.TemporaryDirectory() as d:
            for name in ("wealthfolio_password", "bitvavo_key", "bitvavo_secret"):
                with open(os.path.join(d, name), "w") as f:
                    f.write("x")
            with mock.patch.dict(os.environ, {"WFSYNC_SOURCES": "bitvavo"}), \
                 mock.patch.object(sync.secrets, "SECRETS_DIR", d), \
                 mock.patch.object(sync, "wealthfolio", return_value=mock.Mock()), \
                 mock.patch.object(sync, "sync_source",
                                   side_effect=lambda w, m, *a: ran.append(m.SOURCE_ID) or {"status": "ok"}):
                results = sync.sync_all(False)
        self.assertEqual((ran, list(results)), (["bitvavo"], ["bitvavo"]))

    def test_unselected_upload_is_refused(self):
        with mock.patch.dict(os.environ, {"WFSYNC_SOURCES": "bitvavo"}), self.assertRaises(KeyError):
            sync.import_statement("monefit", b"x")


class UploadOnlyRun(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(sync, "STATE_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def run_with(self, selected):
        with mock.patch.dict(os.environ, {"WFSYNC_SOURCES": selected}), \
             mock.patch.object(sync, "wealthfolio") as wf:
            return sync.run(trigger="test"), wf

    def test_upload_sources_alone_skip_wealthfolio(self):
        status, wf = self.run_with("monefit,debitum")
        self.assertTrue(status["ok"])
        self.assertEqual(status["results"], {})
        wf.assert_not_called()

    def test_idle_run_is_no_success(self):
        status, _ = self.run_with("monefit")
        self.assertNotIn("last_success", status)

    def test_empty_selection_names_the_variable(self):
        with self.assertLogs("wfsync", level="ERROR") as logs:
            status, wf = self.run_with("")
        self.assertFalse(status["ok"])
        self.assertIn("WFSYNC_SOURCES", "\n".join(logs.output))
        wf.assert_not_called()

    def test_failing_api_source_is_not_ok(self):
        with mock.patch.dict(os.environ, {"WFSYNC_SOURCES": "bitvavo,monefit"}), \
             mock.patch.object(sync, "sync_all", return_value={"bitvavo": {"status": "error"}}):
            self.assertFalse(sync.run(trigger="test")["ok"])


class Cli(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"WFSYNC_ORIGIN": "https://o.test"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def exit_code(self, selected, argv=("wfsync", "run"), results=None):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sync, "STATE_DIR", d), \
             mock.patch.dict(os.environ, {"WFSYNC_SOURCES": selected}), mock.patch("sys.argv", list(argv)), \
             mock.patch.object(sync, "sync_all", wraps=sync.sync_all) as sa:
            if results is not None:
                sa.side_effect = lambda dry: results
            with self.assertRaises(SystemExit) as e:
                main.main()
        return e.exception.code

    def test_run_exit_codes_per_selection(self):
        self.assertEqual(self.exit_code("monefit"), 0)  # upload sources only: nothing to fetch
        self.assertEqual(self.exit_code("monefit", ("wfsync", "run", "--dry-run")), 0)
        self.assertEqual(self.exit_code(""), 1)  # nothing selected: nothing was synchronised
        self.assertEqual(self.exit_code("bitvavo", results={"bitvavo": {"status": "ok"}}), 0)
        self.assertEqual(self.exit_code("bitvavo", results={"bitvavo": {"status": "error"}}), 1)
        self.assertEqual(self.exit_code("bitvavo,monefit", results={"bitvavo": {"status": "partial"}}), 1)

    def test_unknown_source_stops_run_and_serve(self):
        for cmd in ("run", "serve"):
            with mock.patch.object(main.web, "serve") as serve, mock.patch.object(main.threading, "Thread") as thread, \
                 mock.patch.object(sync, "run") as run, self.assertLogs("wfsync", level="ERROR") as logs:
                self.assertEqual(self.exit_code("kraken", ("wfsync", cmd)), main.EXIT_CONFIG)
            self.assertIn("kraken", "\n".join(logs.output))
            serve.assert_not_called()
            thread.assert_not_called()
            run.assert_not_called()

    def test_backup_ignores_the_selection(self):
        with mock.patch.dict(os.environ, {"WFSYNC_SOURCES": "kraken"}), mock.patch("sys.argv", ["wfsync", "backup"]), \
             mock.patch.object(sync, "backup", return_value="name.db") as backup, mock.patch("builtins.print"):
            main.main()
        backup.assert_called_once()

    def test_busy_run_has_own_exit_code(self):
        for status, code in ((None, main.EXIT_BUSY), ({"ok": False}, 1), ({"ok": True}, 0)):
            with mock.patch.object(sync, "run", return_value=status), mock.patch("sys.argv", ["wfsync", "run"]):
                with self.assertRaises(SystemExit) as e:
                    main.main()
                self.assertEqual(e.exception.code, code)


class Upload(unittest.TestCase):
    STATEMENT = ("Datum;Details;Betrag;Gebühren;Gesamtes Vermögen;Währung\n"
                 "01.03.2020;Boni;25.00000000;0;1025.00000000;EUR\n"
                 "01.03.2020;Einzahlungen;1000.00000000;0;1000.00000000;EUR\n").encode()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(sync, "STATE_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)
        selected = mock.patch.dict(os.environ, {"WFSYNC_SOURCES": "monefit,goandgrow,debitum"})
        selected.start()
        self.addCleanup(selected.stop)
        sync.save_status({"results": {"bitget": {"status": "ok"}}, "ok": True, "last_success": "x"})

    def stored(self):
        with open(os.path.join(self.dir.name, "status.json")) as f:
            return json.load(f)

    def upload(self, raw, imported=None):
        """`imported`: rows Wealthfolio already holds (None: no Monefit account yet)."""
        wf = mock.Mock()
        wf.ensure_account.side_effect = lambda *a, create: "acc" if create or imported is not None else None
        wf.activities.return_value = [{"comment": r["comment"]} for r in imported or []]
        wf.check.side_effect = lambda rows: [dict(r, isValid=True) for r in rows]
        wf.import_rows.side_effect = lambda rows: {"imported": len(rows), "skipped": 0}
        with mock.patch.object(sync, "Wealthfolio", return_value=wf), \
             mock.patch.object(sync.secrets, "read_secret", return_value="pw"), \
             mock.patch.dict(os.environ, {"WFSYNC_ORIGIN": "https://o.test"}):
            return sync.import_statement("monefit", raw), wf

    def test_upload_leaves_nightly_status_alone(self):
        result, wf = self.upload(self.STATEMENT)
        self.assertEqual((result["status"], result["new"], result["statement"]), ("ok", 2, "2020-03-01 to 2020-03-01"))
        self.assertEqual(wf.ensure_account.call_args_list,
                         [mock.call("monefit", "Monefit SmartSaver", "CASH", "EUR", create=False),
                          mock.call("monefit", "Monefit SmartSaver", "CASH", "EUR", create=True)])
        imported = [r for call in wf.import_rows.call_args_list for r in call.args[0]]
        self.assertEqual([r["activityType"] for r in imported], ["DEPOSIT", "CREDIT"])
        stored = self.stored()
        self.assertEqual(stored["uploads"]["monefit"]["status"], "ok")
        self.assertEqual((stored["results"], stored["ok"], stored["last_success"]),
                         ({"bitget": {"status": "ok"}}, True, "x"))

    def test_broken_statement_writes_nothing(self):
        result, wf = self.upload(self.STATEMENT.replace(b"1025.0", b"1026.0"))
        self.assertEqual(result["status"], "error")
        self.assertIn("Total Wealth does not match", result["error"])
        wf.login.assert_not_called()
        wf.import_rows.assert_not_called()

    def imported(self, raw):
        _, wf = self.upload(raw)
        return [r for call in wf.import_rows.call_args_list for r in call.args[0]]

    def test_same_statement_again_imports_nothing(self):
        first = self.imported(self.STATEMENT)
        result, wf = self.upload(self.STATEMENT, imported=first)
        self.assertEqual((result["status"], result["new"]), ("ok", 0))
        wf.import_rows.assert_not_called()

    def test_longer_statement_imports_only_new_rows(self):
        first = self.imported(self.STATEMENT)
        newest = b"02.03.2020;Ausgezahlte Renditen;0.50000000;0;1025.50000000;EUR\n"
        longer = self.STATEMENT.replace("Währung\n".encode(), "Währung\n".encode() + newest, 1)
        result, wf = self.upload(longer, imported=first)
        self.assertEqual((result["status"], result["new"]), ("ok", 1))
        self.assertEqual([r["activityType"] for r in wf.import_rows.call_args.args[0]], ["INTEREST"])

    def test_refused_first_upload_creates_no_account(self):
        partial = self.STATEMENT.replace(b"01.03.2020;Einzahlungen;1000.00000000;0;1000.00000000;EUR\n", b"")
        result, wf = self.upload(partial)
        self.assertIn("does not begin at the account opening", result["error"])
        wf.ensure_account.assert_called_once_with("monefit", "Monefit SmartSaver", "CASH", "EUR", create=False)

    def test_go_and_grow_uses_own_account(self):
        from tests.test_goandgrow import xlsx
        wf = mock.Mock()
        wf.ensure_account.side_effect = lambda *a, create: "acc" if create else None
        wf.activities.return_value = []
        wf.check.side_effect = lambda rows: [dict(r, isValid=True) for r in rows]
        wf.import_rows.side_effect = lambda rows: {"imported": len(rows), "skipped": 0}
        with mock.patch.object(sync, "Wealthfolio", return_value=wf), \
             mock.patch.object(sync.secrets, "read_secret", return_value="pw"), \
             mock.patch.dict(os.environ, {"WFSYNC_ORIGIN": "https://o.test"}), \
             self.assertLogs("wfsync", level="INFO") as logs:
            result = sync.import_statement("goandgrow", xlsx())
        self.assertEqual((result["status"], result["new"], result["statement"]), ("ok", 4, "2000-03-01 to 2020-03-27"))
        self.assertEqual(wf.ensure_account.call_args_list[-1],
                         mock.call("goandgrow", "Go & Grow", "CASH", "EUR", create=True))
        self.assertEqual(set(self.stored()["uploads"]), {"goandgrow"})
        self.assertEqual(logs.output, ["INFO:wfsync:goandgrow upload: ok"])

    def test_debitum_upload_is_idempotent(self):
        from tests.test_debitum import xlsx
        wf = mock.Mock()
        wf.ensure_account.side_effect = lambda *a, create: "acc" if create else None
        wf.activities.return_value = []
        wf.check.side_effect = lambda rows: [dict(r, isValid=True) for r in rows]
        wf.import_rows.side_effect = lambda rows: {"imported": len(rows), "skipped": 0}
        with mock.patch.object(sync, "Wealthfolio", return_value=wf), \
             mock.patch.object(sync.secrets, "read_secret", return_value="pw"), \
             mock.patch.dict(os.environ, {"WFSYNC_ORIGIN": "https://o.test"}):
            result = sync.import_statement("debitum", xlsx())
            self.assertEqual((result["status"], result["new"], result["statement"]),
                             ("ok", 4, "2019-01-01 to 2020-03-29"))
            self.assertEqual(wf.ensure_account.call_args_list[-1],
                             mock.call("debitum", "Debitum", "CASH", "EUR", create=True))
            imported = [r for call in wf.import_rows.call_args_list for r in call.args[0]]
            self.assertEqual([r["activityType"] for r in imported], ["DEPOSIT", "INTEREST", "TAX", "WITHDRAWAL"])
            wf.import_rows.reset_mock()
            wf.ensure_account.side_effect = lambda *a, create: "acc"
            wf.activities.return_value = [{"comment": r["comment"]} for r in imported]
            again = sync.import_statement("debitum", xlsx())
        self.assertEqual((again["status"], again["new"]), ("ok", 0))
        wf.import_rows.assert_not_called()
        self.assertEqual(set(self.stored()["uploads"]), {"debitum"})

    def test_upload_waits_for_no_running_sync(self):
        with open(sync.lock_path(), "a") as held:
            sync.fcntl.flock(held, sync.fcntl.LOCK_EX)
            with mock.patch.object(sync, "LOCK_RETRY_S", 0):
                self.assertIsNone(sync.import_statement("monefit", self.STATEMENT))
        self.assertNotIn("uploads", self.stored())

    def test_logs_carry_no_figures(self):
        with self.assertLogs("wfsync", level="INFO") as logs:
            self.upload(self.STATEMENT)
        self.assertFalse(any(ch.isdigit() for line in logs.output for ch in line), logs.output)


if __name__ == "__main__":
    unittest.main()
