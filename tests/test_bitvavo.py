import hashlib
import hmac
import io
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock

from wfsync.httpjson import HttpError
from wfsync.sources import bitvavo as b
from wfsync.sources.crypto import verified_pair


def item(tid, kind, **kw):
    base = {"transactionId": tid, "type": kind, "executedAt": "2020-02-01T12:00:00.000Z", "priceCurrency": None,
            "priceAmount": None, "sentCurrency": None, "sentAmount": None, "receivedCurrency": None,
            "receivedAmount": None, "feesCurrency": None, "feesAmount": None, "address": None}
    base.update(kw)
    return base


def mapped(*items, prices=None):
    return b.map_activities({"currency": "EUR", "items": list(items), "prices": prices or {}}, "A")


class Trades(unittest.TestCase):
    def test_buy_adds_fee_on_top(self):
        rows, skipped = mapped(item("1", "buy", priceCurrency="EUR", priceAmount="150", sentCurrency="EUR",
                                    sentAmount="30", receivedCurrency="AAA", receivedAmount="0.2",
                                    feesCurrency="EUR", feesAmount="0.075"))
        self.assertEqual(skipped, [])
        r = rows[0]
        self.assertEqual((r["activityType"], r["symbol"], r["instrumentType"], r["currency"]),
                         ("BUY", "AAA", "CRYPTO", "EUR"))
        self.assertEqual((r["quantity"], r["unitPrice"], r["fee"]), ("0.2", "150", "0.075"))
        self.assertEqual(r["comment"], "wfsync:bitvavo:buy:1")

    def test_sell(self):
        rows, _ = mapped(item("2", "sell", priceCurrency="EUR", priceAmount="2", sentCurrency="CCC", sentAmount="10",
                              receivedCurrency="EUR", receivedAmount="20", feesCurrency="EUR", feesAmount="0.05"))
        self.assertEqual((rows[0]["activityType"], rows[0]["symbol"], rows[0]["quantity"]), ("SELL", "CCC", "10"))

    def test_fee_in_coin_is_skipped(self):
        rows, skipped = mapped(item("3", "buy", priceCurrency="EUR", priceAmount="100", sentCurrency="EUR",
                                    sentAmount="10", receivedCurrency="AAA", receivedAmount="0.1",
                                    feesCurrency="AAA", feesAmount="0.001"))
        self.assertEqual((rows, [s.reason for s in skipped]), ([], ["fee in the coin not supported yet"]))


class CashAndRewards(unittest.TestCase):
    def test_fiat_deposit_and_withdrawal(self):
        rows, _ = mapped(item("4", "deposit", receivedCurrency="EUR", receivedAmount="100", feesCurrency="EUR",
                              feesAmount="0"),
                         item("5", "withdrawal", sentCurrency="EUR", sentAmount="40"))
        self.assertEqual([(r["activityType"], r["amount"]) for r in rows], [("DEPOSIT", "100"), ("WITHDRAWAL", "40")])
        self.assertNotIn("fee", rows[0])

    def test_staking_reward_needs_a_price(self):
        rows, skipped = mapped(
            item("6", "staking", receivedCurrency="AAA", receivedAmount="0.01", priceCurrency="EUR", priceAmount="150"),
            item("7", "staking", receivedCurrency="AAA", receivedAmount="0.01"))
        self.assertEqual((rows[0]["activityType"], rows[0]["subtype"], Decimal(rows[0]["amount"])),
                         ("INTEREST", "STAKING_REWARD", Decimal("1.5")))
        self.assertEqual([s.reason for s in skipped], ["reward without a price"])

    def test_crypto_deposit_uses_arrival_price(self):
        rows, skipped = mapped(item("9", "deposit", receivedCurrency="BBB", receivedAmount="100", address="x"),
                               item("10", "deposit", receivedCurrency="DDD", receivedAmount="5"), prices={"9": "0.25"})
        r = rows[0]
        self.assertEqual((r["activityType"], r["symbol"], r["currency"], r["quantity"], r["unitPrice"],
                          r["isExternal"]),
                         ("TRANSFER_IN", "BBB", "EUR", "100", "0.25", True))
        self.assertEqual([s.reason for s in skipped], ["crypto deposit without a price"])

    def test_crypto_transfer_with_fee_is_skipped(self):
        rows, skipped = mapped(
            item("11", "deposit", receivedCurrency="BBB", receivedAmount="100", feesCurrency="BBB", feesAmount="1"),
            item("12", "withdrawal", sentCurrency="BBB", sentAmount="100", feesCurrency="BBB", feesAmount="1",
                 priceCurrency="EUR", priceAmount="0.5"),
            prices={"11": "0.25"})
        self.assertEqual(rows, [])
        self.assertEqual([s.reason for s in skipped], ["fee on a crypto transfer not supported yet"] * 2)

    def test_missing_price_waits_only_while_recent(self):
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        _, skipped = mapped(item("9", "deposit", receivedCurrency="BBB", receivedAmount="1", executedAt=recent),
                            item("7", "staking", receivedCurrency="AAA", receivedAmount="0.01", executedAt=recent),
                            item("6", "staking", receivedCurrency="AAA", receivedAmount="0.01", executedAt=old),
                            item("5", "deposit", receivedCurrency="BBB", receivedAmount="1", executedAt=old),
                            item("8", "margin_loan_borrow"))
        self.assertEqual([s.transient for s in skipped], [True, True, False, False, False])

    def test_unknown_type_is_skipped(self):
        rows, skipped = mapped(item("8", "margin_loan_borrow"))
        self.assertEqual(rows, [])
        self.assertEqual(skipped[0].reason, "transaction type margin_loan_borrow")


class FakeWf:
    def __init__(self, quotes):
        self.quotes, self.calls = quotes, []

    def quote(self, symbol, mic=None):
        self.calls.append(symbol)
        return self.quotes.get(symbol, {})


class ResolveCoins(unittest.TestCase):
    # EUR pairs lie close to the exchange price, a bare symbol can be far off.
    def test_price_confirms_pair_and_alias(self):
        wf = FakeWf({"BBB-EUR": {"currency": "EUR", "price": 0.599}, "POL28321-EUR": {"currency": "EUR", "price": 0.2}})
        rows = [{"symbol": "BBB", "instrumentType": "CRYPTO"}, {"symbol": "BBB", "instrumentType": "CRYPTO"},
                {"symbol": "POL", "instrumentType": "CRYPTO"}, {"symbol": "", "activityType": "DEPOSIT"}]
        manual = b.resolve_listings(wf, rows, {"tickers": {"BBB": "0.6", "POL": "0.2"}})
        self.assertEqual(manual, [])
        self.assertEqual([(r["symbol"], r.get("quoteCcy")) for r in rows],
                         [("BBB-EUR", "EUR"), ("BBB-EUR", "EUR"), ("POL28321-EUR", "EUR"), ("", None)])
        self.assertEqual(wf.calls, ["BBB-EUR", "POL28321-EUR"])

    def test_mismatch_or_missing_quote_means_manual(self):
        wf = FakeWf({"CCC-EUR": {"currency": "EUR", "price": 24}, "DDD-EUR": {"currency": "USD", "price": 2}})
        rows = [{"symbol": c, "instrumentType": "CRYPTO"} for c in ("CCC", "DDD", "NEW")]
        manual = b.resolve_listings(wf, rows, {"tickers": {"CCC": "2", "DDD": "2", "NEW": "1"}})
        self.assertEqual(manual, ["CCC", "DDD", "NEW"])
        self.assertEqual({(r["symbol"], r["quoteMode"], r["quoteCcy"]) for r in rows},
                         {("CCC", "MANUAL", "EUR"), ("DDD", "MANUAL", "EUR"), ("NEW", "MANUAL", "EUR")})

    def test_held_coin_keeps_its_asset(self):
        # Yahoo 30% off: a held coin must not move to a second, MANUAL asset.
        wf = FakeWf({"GGG-EUR": {"currency": "EUR", "price": 130}})
        def act(i, symbol, mode, ref="wfsync:bitvavo:buy:"):
            return {"assetId": i and f"a{i}", "assetSymbol": symbol, "assetPricingMode": mode,
                    "instrumentType": "CRYPTO", "comment": ref + str(i)}
        existing = [act(1, "GGG", "MARKET"), act(2, "POL28321", "MARKET"), act(3, "FFF", "MANUAL"),
                    act(None, "EEE", None)]
        rows = [{"symbol": c, "instrumentType": "CRYPTO"} for c in ("GGG", "POL", "FFF", "EEE")]
        manual = b.resolve_listings(wf, rows, {"tickers": {"GGG": "100", "EEE": "3000"}}, existing)
        self.assertEqual([(r["symbol"], r.get("quoteMode")) for r in rows],
                         [("GGG-EUR", None), ("POL28321-EUR", None), ("FFF", "MANUAL"), ("EEE", "MANUAL")])
        self.assertEqual(manual, ["EEE", "FFF"])
        self.assertEqual(wf.calls, ["EEE-EUR"])  # only the coin without an asset is checked

    def test_manual_alias_and_hand_typed_activities(self):
        # A MANUAL asset keeps the bare coin (`POL`), not the Yahoo base; activities typed by hand
        # (no wfsync reference) do not prove a checked price.
        wf = FakeWf({"POL28321-EUR": {"currency": "EUR", "price": 0.2}, "CCC-EUR": {"currency": "EUR", "price": 2}})
        existing = [{"assetId": "a1", "assetSymbol": "POL", "assetPricingMode": "MANUAL", "instrumentType": "CRYPTO",
                     "comment": "wfsync:bitvavo:buy:1"},
                    {"assetId": "a2", "assetSymbol": "CCC", "assetPricingMode": "MARKET", "instrumentType": "CRYPTO",
                     "comment": "by hand"}]
        rows = [{"symbol": c, "instrumentType": "CRYPTO"} for c in ("POL", "CCC")]
        manual = b.resolve_listings(wf, rows, {"tickers": {"POL": "0.2", "CCC": "2"}}, existing)
        self.assertEqual([(r["symbol"], r.get("quoteMode")) for r in rows], [("POL", "MANUAL"), ("CCC-EUR", None)])
        self.assertEqual((manual, wf.calls), (["POL"], ["CCC-EUR"]))

    def test_tolerance_edge(self):
        self.assertEqual(verified_pair(FakeWf({"AAA-EUR": {"currency": "EUR", "price": 104.9}}), "AAA", "100"),
                         "AAA-EUR")
        self.assertIsNone(verified_pair(FakeWf({"AAA-EUR": {"currency": "EUR", "price": 105.1}}), "AAA", "100"))
        self.assertIsNone(verified_pair(FakeWf({"AAA-EUR": {"currency": "EUR", "price": 100}}), "AAA", None))


class Limited:
    """Returns 429 once, then an empty success; records each attempt's headers."""

    def __init__(self):
        self.seen = []

    def open(self, req, timeout=None):
        self.seen.append(dict(req.header_items()))
        if len(self.seen) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(b"{}"))
        return io.BytesIO(b"[]")


class RetrySignature(unittest.TestCase):
    def test_retry_carries_a_fresh_valid_signature(self):
        opener = Limited()
        client = b.Bitvavo("key", "secret", opener=opener)
        with mock.patch.object(b.time, "time", side_effect=[1.0, 2.0]), mock.patch("wfsync.httpjson.time.sleep"):
            client.get("/path")
        self.assertEqual([h["Bitvavo-access-timestamp"] for h in opener.seen], ["1000", "2000"])
        for headers in opener.seen:
            message = (headers["Bitvavo-access-timestamp"] + "GET/v2/path").encode()
            self.assertEqual(headers["Bitvavo-access-signature"],
                             hmac.new(b"secret", message, hashlib.sha256).hexdigest())


class Client(unittest.TestCase):
    def client(self, responses):
        c = b.Bitvavo("k", "s")
        c.paths = []

        def get(path):
            c.paths.append(path)
            return responses(path)
        c.get = get
        return c

    def test_full_history_and_deposit_prices(self):
        hour = 1583143200000
        dep = item("9", "deposit", receivedCurrency="BBB", receivedAmount="1", executedAt="2020-03-02T10:12:34.000Z")
        tickers = [{"market": "BBB-EUR", "price": "0.6"}, {"market": "BBB-USDC", "price": "0.7"}, {"market": "X-EUR"}]

        def responses(p):
            if p.startswith("/account/history"):
                return {"items": [dep, item("4", "deposit", receivedCurrency="EUR")], "totalPages": 1}
            return tickers if p == "/ticker/price" else [[hour, "0.25", "1", "1", "0.5", "1"]]
        c = self.client(responses)
        data = c.fetch()
        self.assertIn("fromDate=0", c.paths[0])
        self.assertEqual(data["tickers"], {"BBB": "0.6"})
        self.assertEqual(data["prices"], {})
        c.complete(data, {"wfsync:bitvavo:deposit:9"})  # already imported: no lookup
        self.assertEqual(data["prices"], {})
        c.complete(data, set())
        self.assertEqual(c.paths[-1], f"/BBB-EUR/candles?interval=1h&end={hour + 3600000}&limit=1")
        self.assertEqual(data["prices"], {"9": "0.25"})

    def test_400_and_404_mean_no_price(self):
        at = "2020-03-02T10:12:34.000Z"
        for status in (400, 404):
            self.assertIsNone(self.client(lambda p: (_ for _ in ()).throw(HttpError(status, "x"))).price_at("FFF", at))
        for status in (429, 500, 503):
            with self.assertRaises(HttpError):
                self.client(lambda p: (_ for _ in ()).throw(HttpError(status, "x"))).price_at("FFF", at)

    def test_empty_hour_uses_recent_close(self):
        hour = 1583143200000
        at = "2020-03-02T10:12:34.000Z"
        recent = self.client(lambda p: [[hour - 3600000, "0.6", "1", "1", "0.61", "1"]])
        self.assertEqual(recent.price_at("FFF", at), "0.61")
        stale = self.client(lambda p: [[hour - 25 * 3600000, "0.6", "1", "1", "0.61", "1"]])
        self.assertIsNone(stale.price_at("FFF", at))
        self.assertIsNone(self.client(lambda p: []).price_at("FFF", at))


if __name__ == "__main__":
    unittest.main()
