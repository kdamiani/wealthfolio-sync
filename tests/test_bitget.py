import base64
import hashlib
import hmac
import io
import json
import os
import time
import unittest
import urllib.error
import urllib.parse
from decimal import Decimal
from itertools import pairwise
from unittest import mock

from wfsync.httpjson import HttpError
from wfsync.sources import bitget as g

TS = 1_580_000_000_000
HOUR = TS - TS % g.HOUR_MS


def rec(rid, kind, coin, amount, fee="0", biz=None, ts=TS, balance="0"):
    return {"id": rid, "spotTaxType": kind, "coin": coin, "amount": amount, "fee": fee, "balance": balance,
            "bizOrderId": biz or f"b{rid}", "ts": str(ts)}


def mapped(*records, rates=None):
    return g.map_activities({"currency": "EUR", "records": list(records), "rates": rates or {}}, "A")


USDT_EUR = {("USDTEUR", HOUR): "0.9"}


class Trades(unittest.TestCase):
    def test_coin_buy_imports_matching_usdt_sale(self):
        rows, skipped = mapped(rec("1", "Buy", "AAA", "2", fee="-0.002", biz="o1"),
                               rec("2", "Sell", "USDT", "-300", biz="o1"), rates=USDT_EUR)
        self.assertEqual(skipped, [])
        buy, sell = rows
        self.assertEqual((buy["activityType"], buy["symbol"], buy["currency"], buy["comment"]),
                         ("BUY", "AAA", "EUR", "wfsync:bitget:spot:1"))
        # 300 USDT at 0.9 = 270 EUR for 2 AAA gross: 135 EUR each; the fee comes off the coins.
        self.assertEqual((buy["quantity"], buy["unitPrice"], buy["fee"]), ("1.998", "135", "0.27"))
        self.assertEqual((sell["activityType"], sell["symbol"], sell["quantity"], sell["unitPrice"], sell["comment"]),
                         ("SELL", "USDT", "300", "0.9", "wfsync:bitget:spot:2"))
        self.assertEqual((buy[g.GROUP_KEY], sell[g.GROUP_KEY]), ("wfsync:bitget:spot:1", "wfsync:bitget:spot:1"))
        cash_out = Decimal(buy["quantity"]) * Decimal(buy["unitPrice"]) + Decimal(buy["fee"])
        self.assertEqual(cash_out, Decimal(sell["quantity"]) * Decimal(sell["unitPrice"]))

    def test_coin_sale_imports_matching_usdt_purchase(self):
        rows, skipped = mapped(rec("3", "Sell", "CCC", "-100", biz="o2"),
                               rec("4", "Buy", "USDT", "200", fee="-0.2", biz="o2"), rates=USDT_EUR)
        self.assertEqual(skipped, [])
        sell, buy = rows
        self.assertEqual((sell["activityType"], sell["symbol"], sell["quantity"], sell["unitPrice"], sell["fee"]),
                         ("SELL", "CCC", "100", "1.8", "0.18"))
        self.assertEqual((buy["activityType"], buy["symbol"], buy["quantity"], buy["unitPrice"]),
                         ("BUY", "USDT", "199.8", "0.9"))
        cash_in = Decimal(sell["quantity"]) * Decimal(sell["unitPrice"]) - Decimal(sell["fee"])
        self.assertEqual(cash_in, Decimal(buy["quantity"]) * Decimal(buy["unitPrice"]))

    def test_missing_rate_skips_instead_of_guessing(self):
        rows, skipped = mapped(rec("1", "Buy", "AAA", "2", biz="o1"), rec("2", "Sell", "USDT", "-300", biz="o1"))
        self.assertEqual((rows, [s.reason for s in skipped]), ([], ["USDT price missing"]))

    def test_missing_usdt_rate_waits_only_while_recent(self):
        now = int(time.time() * 1000)
        for age_days, waits in ((1, True), (8, False)):
            ts = now - age_days * 86_400_000
            _, skipped = mapped(rec("1", "Buy", "AAA", "2", biz="o1", ts=ts),
                                rec("2", "Sell", "USDT", "-300", biz="o1", ts=ts))
            self.assertEqual([s.transient for s in skipped], [waits])

    def test_unsupported_shapes_are_skipped(self):
        _, skipped = mapped(rec("1", "Buy", "AAA", "1", biz="o"), rec("2", "Sell", "USDT", "-100", biz="o"),
                            rec("3", "Buy", "AAA", "1", biz="o"), rates=USDT_EUR)
        self.assertEqual([s.reason for s in skipped], ["order with partial fills not supported yet"] * 3)
        _, skipped = mapped(rec("1", "Buy", "AAA", "1", biz="p"), rec("2", "Sell", "EEE", "-0.1", biz="p"),
                            rates=USDT_EUR)
        self.assertEqual([s.reason for s in skipped], ["trading pair not supported yet"])
        _, skipped = mapped(rec("1", "Buy", "AAA", "1", biz="q"), rec("2", "Sell", "USDT", "-100", fee="-0.1", biz="q"),
                            rates=USDT_EUR)
        self.assertEqual([s.reason for s in skipped], ["fee on the paying side not supported yet"])


class Fiat(unittest.TestCase):
    def test_eur_deposits(self):
        rows, _ = mapped(rec("1", "FIAT_BANK_TRANSFER_DEPOSIT_SUCCESS_USER_IN", "EUR", "500"),
                         rec("2", "fiat_recharge_in", "EUR", "100"))
        self.assertEqual([(r["activityType"], r["currency"], r["amount"]) for r in rows],
                         [("DEPOSIT", "EUR", "500"), ("DEPOSIT", "EUR", "100")])

    def test_conversion_uses_the_rate_paid(self):
        rows, skipped = mapped(rec("1", "fiat_balance_user_out", "EUR", "-90", biz="c"),
                               rec("2", "fiat_balance_success_user_in", "USDT", "100", biz="c"))
        self.assertEqual(skipped, [])
        self.assertEqual([(r["activityType"], r["symbol"], r["quantity"], r["unitPrice"], r["comment"]) for r in rows],
                         [("BUY", "USDT", "100", "0.9", "wfsync:bitget:spot:2")])

    def test_fiat_purchase_pairs_with_later_deposit(self):
        rows, skipped = mapped(rec("1", g.FIAT_BUY_OUT, "EUR", "-45"),
                               rec("2", "Deposit", "USDT", "50", ts=TS + 5 * 60_000))
        self.assertEqual(skipped, [])
        self.assertEqual([(r["activityType"], r["symbol"], r["quantity"], r["unitPrice"], r["comment"]) for r in rows],
                         [("BUY", "USDT", "50", "0.9", "wfsync:bitget:spot:1")])

    def test_fiat_purchase_ignores_earlier_and_imported_deposits(self):
        out = rec("11", g.FIAT_BUY_OUT, "EUR", "-45", ts=TS + 5 * 60_000)
        early = rec("10", "Deposit", "USDT", "20", ts=TS)  # an external deposit just before
        own = rec("12", "Deposit", "USDT", "50", ts=TS + 8 * 60_000)
        later = (TS + 8 * 60_000) - (TS + 8 * 60_000) % g.HOUR_MS
        rates = {("USDTEUR", HOUR): "0.9", ("USDTEUR", later): "0.9"}
        rows, skipped = mapped(early, out, own, rates=rates)
        self.assertEqual(skipped, [])
        self.assertEqual(sorted((r["comment"], r["activityType"], r["quantity"]) for r in rows),
                         [("wfsync:bitget:spot:10", "TRANSFER_IN", "20"), ("wfsync:bitget:spot:11", "BUY", "50")])
        # An earlier run imported `own` alone; the purchase showing up later must not re-pair it.
        data = {"currency": "EUR", "records": [out, own], "rates": rates, "known": {"wfsync:bitget:spot:12"}}
        rows, skipped = g.map_activities(data, "A")
        self.assertEqual([r["comment"] for r in rows], ["wfsync:bitget:spot:12"])
        self.assertEqual([(s.ref, s.reason) for s in skipped],
                         [("wfsync:bitget:spot:11", "fiat purchase: USDT credit already imported on its own")])

    def test_nearest_deposit_after_the_purchase_wins(self):
        out = rec("1", g.FIAT_BUY_OUT, "EUR", "-45")
        rows, _ = mapped(rec("3", "Deposit", "USDT", "7", ts=TS + 9 * 60_000), out,
                         rec("2", "Deposit", "USDT", "50", ts=TS + 60_000),
                         rates={("USDTEUR", HOUR): "0.9"})
        self.assertIn(("wfsync:bitget:spot:1", "BUY", "50"),
                      [(r["comment"], r["activityType"], r["quantity"]) for r in rows])

    def test_fiat_purchase_without_deposit_is_skipped(self):
        late = TS + 20 * 60_000
        rows, skipped = mapped(rec("1", g.FIAT_BUY_OUT, "EUR", "-45"), rec("2", "Deposit", "USDT", "50", ts=late),
                               rates={("USDTEUR", late - late % g.HOUR_MS): "0.9"})
        self.assertEqual([s.reason for s in skipped], ["fiat purchase without USDT credit"])
        self.assertEqual([(r["activityType"], r["isExternal"]) for r in rows], [("TRANSFER_IN", True)])


class Transfers(unittest.TestCase):
    def test_coin_deposit_is_valued_via_usdt(self):
        rows, _ = mapped(rec("1", "Deposit", "BBB", "100"), rates={("USDTEUR", HOUR): "0.9", ("BBBUSDT", HOUR): "0.5"})
        self.assertEqual([(r["activityType"], r["symbol"], r["quantity"], r["unitPrice"], r["isExternal"])
                          for r in rows],
                         [("TRANSFER_IN", "BBB", "100", "0.45", True)])

    def test_outgoing_transfers_include_the_fee(self):
        rows, _ = mapped(rec("1", "Withdrawal", "CCC", "-10", fee="-0.2"), rec("2", "Transfer out", "USDT", "-50"))
        self.assertEqual([(r["activityType"], r["symbol"], r["quantity"]) for r in rows],
                         [("TRANSFER_OUT", "CCC", "10.2"), ("TRANSFER_OUT", "USDT", "50")])

    def test_transfer_in_from_futures_and_airdrop(self):
        rows, _ = mapped(rec("1", "Transfer in", "USDT", "20"), rec("2", "Airdrop Reward-A", "USDT", "1"),
                         rates=USDT_EUR)
        self.assertEqual([(r["activityType"], r.get("subtype"), r["unitPrice"]) for r in rows],
                         [("TRANSFER_IN", None, "0.9"), ("INTEREST", "STAKING_REWARD", "0.9")])
        self.assertEqual(rows[1]["amount"], "0.9")

    def test_unknown_types_are_named(self):
        _, skipped = mapped(rec("1", "Convert", "GGG", "1"), rec("2", "fiat_withdraw", "EUR", "-5"))
        self.assertEqual(sorted(s.reason for s in skipped),
                         ["EUR transaction fiat_withdraw", "transaction type Convert"])


class Prices(unittest.TestCase):
    def test_only_new_records_need_prices(self):
        events = g.plan([rec("1", "Buy", "AAA", "1", biz="o"), rec("2", "Sell", "USDT", "-100", biz="o"),
                         rec("3", "Deposit", "BBB", "5", ts=TS + g.HOUR_MS), rec("4", "Withdrawal", "CCC", "-1")])
        self.assertEqual(g.needed_prices(events, set()),
                         [("BBBUSDT", HOUR + g.HOUR_MS), ("USDTEUR", HOUR), ("USDTEUR", HOUR + g.HOUR_MS)])
        known = {"wfsync:bitget:spot:1", "wfsync:bitget:spot:2"}
        self.assertEqual(g.needed_prices(events, known), [("BBBUSDT", HOUR + g.HOUR_MS), ("USDTEUR", HOUR + g.HOUR_MS)])

    def test_eur_prices_prefer_the_direct_pair(self):
        prices = g.eur_prices({"USDTEUR": "0.9", "GGGEUR": "50000", "GGGUSDT": "60000", "BBBUSDT": "0.5",
                               "EEEGGG": "0.03"})
        self.assertEqual(prices, {"USDT": Decimal("0.9"), "GGG": Decimal("50000"), "BBB": Decimal("0.45")})


class Opener:
    """Returns `routes(path, query)` as the response; records every request."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def open(self, req, timeout=None):
        self.requests.append(req)
        url = urllib.parse.urlsplit(req.full_url)
        return io.BytesIO(json.dumps(self.routes(url.path, dict(urllib.parse.parse_qsl(url.query)))).encode())


class Client(unittest.TestCase):
    def test_fetch_walks_windows_and_signs_requests(self):
        def routes(path, q):
            if path == "/api/v2/spot/market/tickers":
                return {"code": "00000", "data": [{"symbol": "USDTEUR", "lastPr": "0.9"}]}
            if int(q["endTime"]) == now:  # newest window: one record
                return {"code": "00000", "data": [rec("9", "Deposit", "USDT", "1", balance="1")]}
            return {"code": "00000", "data": []}

        now = g.HISTORY_START_MS + 3 * g.WINDOW_MS + 5
        opener = Opener(routes)
        data = g.Bitget("bg_k", "s3cret", "phrase", opener=opener, pause=0).fetch(now_ms=now)
        self.assertEqual([r["id"] for r in data["records"]], ["9"])
        self.assertEqual(data["eur_prices"], {"USDT": Decimal("0.9")})
        tax = [r for r in opener.requests if "tax" in r.full_url]
        windows = [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.full_url).query)) for r in tax]
        self.assertEqual(len(windows), 4)
        self.assertEqual(int(windows[-1]["startTime"]), g.HISTORY_START_MS)
        for a, b in pairwise(windows):
            self.assertEqual(int(b["endTime"]), int(a["startTime"]) - 1)
            self.assertLessEqual(int(a["endTime"]) - int(a["startTime"]), g.WINDOW_MS)
        first = tax[0]
        target = first.full_url[len(g.BASE_URL):]
        expected = base64.b64encode(hmac.new(b"s3cret",
                                             (first.get_header("Access-timestamp") + "GET" + target).encode(),
                                             hashlib.sha256).digest()).decode()
        self.assertEqual(first.get_header("Access-sign"), expected)
        self.assertEqual(first.get_header("Access-passphrase"), "phrase")
        ticker = next(r for r in opener.requests if "tickers" in r.full_url)
        self.assertIsNone(ticker.get_header("Access-key"))  # public data goes unsigned

    def test_full_page_halves_the_window(self):
        span = (g.HISTORY_START_MS, g.HISTORY_START_MS + 10_000)
        stamps = [span[0] + i * 10 for i in range(g.PAGE_LIMIT + 5)]  # more than a page, spread over 5 s

        def routes(path, q):
            if "tickers" in path:
                return {"code": "00000", "data": []}
            lo, hi = int(q["startTime"]), int(q["endTime"])
            hits = [t for t in stamps if lo <= t <= hi]
            # like Bitget: an ascending page of at most `limit`, no way to page on
            return {"code": "00000",
                    "data": [rec(str(t), "Deposit", "USDT", "1", ts=t, balance=str(stamps.index(t) + 1))
                             for t in hits[:int(q["limit"])]]}

        data = g.Bitget("k", "s", "p", opener=Opener(routes), pause=0).fetch(now_ms=span[1])
        self.assertEqual(sorted(int(r["id"]) for r in data["records"]), stamps)

    def test_missing_record_breaks_the_balance_chain(self):
        records = [rec("1", "Deposit", "USDT", "10", balance="10", ts=TS),
                   rec("3", "Withdrawal", "USDT", "-4", balance="2", ts=TS + 2)]
        self.assertEqual(g.chain_breaks(records), ["USDT"])  # record 2 (-4, balance 6) is missing
        records.insert(1, rec("2", "Withdrawal", "USDT", "-4", balance="6", ts=TS + 1))
        self.assertEqual(g.chain_breaks(records), [])
        fee = [rec("1", "Buy", "AAA", "2", fee="-0.002", balance="1.998")]
        self.assertEqual(g.chain_breaks(fee), [])

        def routes(path, q):
            if "tickers" in path:
                return {"code": "00000", "data": []}
            return {"code": "00000", "data": [rec("1", "Deposit", "USDT", "10", balance="11")]}

        with self.assertRaises(RuntimeError):
            g.Bitget("k", "s", "p", opener=Opener(routes), pause=0).fetch(now_ms=g.HISTORY_START_MS + 10)

    def test_api_error_keeps_code_not_text(self):
        opener = Opener(lambda path, q: {"code": "40014", "msg": "have 12.5"})
        with self.assertRaises(HttpError) as cm:
            g.Bitget("k", "s", "p", opener=opener, pause=0).get("/api/v2/x")
        self.assertEqual(str(cm.exception), "HTTP 200: 40014")

    def test_price_is_the_candle_open(self):
        def routes(path, q):
            start = int(q["endTime"]) - g.HOUR_MS
            return {"code": "00000", "data": [[str(start), "0.91", "0.92", "0.9", "0.915", "1", "1", "1"]]}

        client = g.Bitget("k", "s", "p", opener=Opener(routes), pause=0)
        self.assertEqual(client.price_at("USDTEUR", HOUR), "0.91")
        stale = Opener(lambda path, q: {"code": "00000",
                                        "data": [[str(HOUR - g.HOUR_MS), "1", "1", "1", "1", "1", "1", "1"]]})
        self.assertIsNone(g.Bitget("k", "s", "p", opener=stale, pause=0).price_at("USDTEUR", HOUR))


class Limited:
    """Returns 429 once, then an empty success; records each attempt's headers."""

    def __init__(self, body):
        self.body, self.seen = body, []

    def open(self, req, timeout=None):
        self.seen.append(dict(req.header_items()))
        if len(self.seen) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(b"{}"))
        return io.BytesIO(self.body)


class RetrySignature(unittest.TestCase):
    def test_retry_carries_a_fresh_valid_signature(self):
        opener = Limited(b'{"code": "00000", "data": []}')
        client = g.Bitget("key", "secret", "phrase", opener=opener, pause=0)
        with mock.patch.object(g.time, "time", side_effect=[1.0, 2.0]), mock.patch("wfsync.httpjson.time.sleep"):
            client.get("/path", a=1)
        self.assertEqual([h["Access-timestamp"] for h in opener.seen], ["1000", "2000"])
        for headers in opener.seen:
            message = (headers["Access-timestamp"] + "GET/path?a=1").encode()
            digest = hmac.new(b"secret", message, hashlib.sha256).digest()
            self.assertEqual(headers["Access-sign"], base64.b64encode(digest).decode())


class NoPrice(unittest.TestCase):
    def test_400_and_404_mean_no_price(self):
        client = g.Bitget("k", "s", "p", pause=0)
        for status in (400, 404):
            with mock.patch.object(client, "get", side_effect=HttpError(status, "x")):
                self.assertIsNone(client.price_at("AAAUSDT", 0))
        for status in (200, 429, 500):
            with mock.patch.object(client, "get", side_effect=HttpError(status, "x")), self.assertRaises(HttpError):
                client.price_at("AAAUSDT", 0)

    def test_missing_price_waits_only_while_recent(self):
        now = int(time.time() * 1000)
        for age_days, waits in ((1, True), (8, False)):
            ts = str(now - age_days * 86_400_000)
            record = {"id": "1", "ts": ts, "coin": "AAA", "spotTaxType": "Deposit", "amount": "1", "fee": "0"}
            _, skipped = g.map_activities({"records": [record], "rates": {}}, "A")
            self.assertEqual([(s.reason, s.transient) for s in skipped], [("price missing", waits)])


class PartialFills(unittest.TestCase):
    def test_every_order_record_is_counted(self):
        records = [{"id": str(i), "ts": "1580000000000", "bizOrderId": "o", "coin": c, "spotTaxType": t, "amount": a,
                    "fee": "0"}
                   for i, (c, t, a) in enumerate((("AAA", "Buy", "1"), ("AAA", "Buy", "1"), ("USDT", "Sell", "-2")))]
        rows, skipped = g.map_activities({"records": records, "rates": {}}, "A")
        self.assertEqual(rows, [])
        self.assertEqual([s.ref for s in skipped], [g.ref(r) for r in records])


class FakeQuotes:
    def __init__(self, quotes):
        self.quotes, self.calls = quotes, []

    def quote(self, symbol, mic=None):
        self.calls.append(symbol)
        return self.quotes.get(symbol, {})


class Listings(unittest.TestCase):
    # Some coins exist at Yahoo only in USD, close to the exchange's USDT price.
    def test_usd_pair_without_eur_pair(self):
        wf = FakeQuotes({"GGG-EUR": {"currency": "EUR", "price": 50000}, "HHH-USD": {"currency": "USD", "price": 0.7},
                         "JJJ123-USD": {"currency": "USD", "price": 8},
                         "JJJ-USD": {"currency": "USD", "price": 0.0001}})
        rows = [{"symbol": c, "instrumentType": "CRYPTO"} for c in ("GGG", "HHH", "JJJ", "TINY")]
        data = {"eur_prices": {"GGG": Decimal("50000"), "HHH": Decimal("0.6"), "JJJ": Decimal("7")},
                "usd_prices": {"GGG": Decimal("58000"), "HHH": Decimal("0.7"), "JJJ": Decimal("8")}}
        with mock.patch.dict(os.environ, {"WFSYNC_YAHOO_ALIASES": "JJJ=JJJ123"}):
            manual = g.resolve_listings(wf, rows, data)
        self.assertEqual([(r["symbol"], r["quoteCcy"], r.get("quoteMode")) for r in rows],
                         [("GGG-EUR", "EUR", None), ("HHH-USD", "USD", None), ("JJJ123-USD", "USD", None),
                          ("TINY", "EUR", "MANUAL")])
        self.assertEqual(manual, ["TINY"])
        self.assertNotIn("GGG-USD", wf.calls)  # the EUR pair wins

    def test_held_usd_asset_keeps_its_pair(self):
        wf = FakeQuotes({})
        existing = [{"assetId": "a", "assetSymbol": "HHH", "assetPricingMode": "MARKET", "assetQuoteCcy": "USD",
                     "instrumentType": "CRYPTO", "comment": "wfsync:bitget:spot:1"}]
        rows = [{"symbol": "HHH", "instrumentType": "CRYPTO"}]
        g.resolve_listings(wf, rows, {"eur_prices": {}, "usd_prices": {}}, existing)
        self.assertEqual((rows[0]["symbol"], rows[0]["quoteCcy"], wf.calls), ("HHH-USD", "USD", []))


class Precision(unittest.TestCase):
    def test_tiny_prices_survive_formatting(self):
        rows, _ = mapped(rec("1", "Buy", "TINY", "2000000000", biz="o"), rec("2", "Sell", "USDT", "-120", biz="o"),
                         rec("3", "Deposit", "EEE", "0.00000000123"),
                         rates={("USDTEUR", HOUR): "0.9", ("EEEUSDT", HOUR): "3000"})
        buy = rows[0]
        cost = Decimal(buy["quantity"]) * Decimal(buy["unitPrice"])
        self.assertLess(abs(cost - Decimal("120") * Decimal("0.9")), Decimal("0.000001"))
        self.assertEqual(rows[2]["quantity"], "0.00000000123")


class CompleteKeepsKnown(unittest.TestCase):
    def test_pairing_sees_known_references(self):
        out = rec("11", g.FIAT_BUY_OUT, "EUR", "-45")
        own = rec("12", "Deposit", "USDT", "50", ts=TS + 60_000)
        data = {"currency": "EUR", "records": [out, own], "rates": {}}
        candle = Opener(lambda path, q: {"code": "00000", "data": [[str(int(q["endTime"]) - g.HOUR_MS), "0.9", "0.9",
                                                                    "0.9", "0.9", "1", "1", "1"]]})
        g.Bitget("k", "s", "p", opener=candle, pause=0).complete(data, {"wfsync:bitget:spot:12"})
        _, skipped = g.map_activities(data, "A")
        # The imported deposit fetches no price again; `sync` drops its skip as a known ref.
        self.assertEqual(sorted((s.ref, s.reason) for s in skipped),
                         [("wfsync:bitget:spot:11", "fiat purchase: USDT credit already imported on its own"),
                          ("wfsync:bitget:spot:12", "price missing")])
