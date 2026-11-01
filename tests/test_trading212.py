import base64
import io
import unittest
from decimal import Decimal

from wfsync.httpjson import HttpError
from wfsync.sources import trading212 as t


def order(fill_id, side, ticker, isin, ccy, qty, price, fx=1, fees=(), ftype="TRADE", at="2020-01-05T10:00:00Z"):
    return {
        "order": {"side": side, "ticker": ticker, "currency": ccy, "status": "FILLED",
                  "instrument": {"ticker": ticker, "isin": isin, "currency": ccy, "name": "Name " + ticker}},
        "fill": {"id": fill_id, "type": ftype, "filledAt": at, "quantity": qty, "price": price,
                 "walletImpact": {"currency": "EUR", "fxRate": fx, "netValue": 0,
                                  "taxes": [{"name": "CURRENCY_CONVERSION_FEE", "quantity": f, "currency": "EUR"}
                                            for f in fees]}},
    }


def data(orders=(), dividends=(), transactions=()):
    return {"currency": "EUR", "orders": list(orders), "dividends": list(dividends), "transactions": list(transactions)}


class Authorization(unittest.TestCase):
    def test_key_and_secret_form_basic_header(self):
        seen = []

        class Opener:
            def open(self, req, timeout=None):
                seen.append(req.get_header("Authorization"))
                return io.BytesIO(b"{}")

        t.Trading212("key", "secret", opener=Opener(), pause=0).get("/x")
        self.assertEqual(seen, ["Basic " + base64.b64encode(b"key:secret").decode()])


class SymbolHint(unittest.TestCase):
    def test_strips_suffixes(self):
        self.assertEqual(t.symbol_hint("CID_US_EQ"), "CID")
        self.assertEqual(t.symbol_hint("FDDd_EQ"), "FDD")
        self.assertEqual(t.symbol_hint(None), "")


def hit(symbol, mic, ccy, name=None):
    # Wealthfolio's canonicalSymbol drops the venue suffix (`CEE.MI` -> `CEE`).
    return {"symbol": symbol, "canonicalSymbol": symbol.split(".")[0], "exchangeMic": mic, "currency": ccy,
            "longName": name}


class FakeWf:
    """Returns searches from a dict and confirms quotes only for `quotes` (symbol, mic) -> ccy."""

    def __init__(self, responses, quotes=None):
        self.responses, self.quotes, self.calls = responses, quotes or {}, []

    def search(self, q):
        self.calls.append(q)
        return self.responses.get(q, [])

    def quote(self, symbol, mic=None):
        self.calls.append(f"quote {symbol}@{mic}")
        ccy = self.quotes.get((symbol, mic))
        return {"currency": ccy, "price": 1} if ccy else {}


class PickListing(unittest.TestCase):
    def pick(self, responses, isin, hint, ccy, name, venue=None, quotes=None):
        return t.pick_listing(FakeWf(responses, quotes), isin, hint, ccy, name, venue)

    def test_isin_wins_in_trading_currency(self):
        # Company Axx: the hint `CAA` is another company on NYSE.
        listings = {"ZZ0000000101": [hit("CAA.PA", "XPAR", "EUR")],
                    "CAA": [hit("CAA", "XNYS", "USD", "Albatross & Company")]}
        self.assertEqual(self.pick(listings, "ZZ0000000101", "CAA", "EUR", "Company Axx", "XPAR")["symbol"], "CAA.PA")
        # Rename Works: the ISIN gives the new ticker, the hint `OLD` only look-alikes.
        renamed = {"ZZ0000000102": [hit("NEWT", "XNYS", "USD")],
                   "OLD": [hit("OLDM", "XNYS", "USD", "Lookalike Mining")]}
        self.assertEqual(self.pick(renamed, "ZZ0000000102", "OLD", "USD", "Rename Works", "XNYS")["symbol"], "NEWT")

    def test_trading_venue_beats_other_main_venue(self):
        # Company Bxx traded on Xetra: the search also offers Frankfurt.
        responses = {"CBB": [hit("CBB.F", "XFRA", "EUR", "Company Bxx Group"),
                             hit("CBB.DE", "XETR", "EUR", "Company Bxx Group")]}
        self.assertEqual(self.pick(responses, "ZZ0000000103", "CBB", "EUR", "Company Bxx", "XETR")["symbol"], "CBB.DE")

    def test_secondary_listing_loses_to_exact_hint(self):
        # Company Cxx: the ISIN only finds German regional listings, Trading 212 trades USD.
        responses = {"ZZ0000000104": [hit("C1C.HA", "XHAN", "EUR"), hit("C1C.SG", "XSTU", "EUR")],
                     "CCC": [hit("CCC", "XNAS", "USD", "Company Cxx, Inc."),
                             hit("9999.HK", "XHKG", "HKD", "Company Cxx")]}
        self.assertEqual(self.pick(responses, "ZZ0000000104", "CCC", "USD", "Company Cxx", "XNAS")["exchangeMic"],
                         "XNAS")

    def test_pseudo_isin_loses_to_main_venue(self):
        # Fund Dxx: `ZZ0000000105.SG` is not where Trading 212 trades it.
        responses = {"ZZ0000000105": [hit("FDA.L", "XLON", "GBp"), hit("ZZ0000000105.SG", "XSTU", "EUR")],
                     "FDD": [hit("FDD.DE", "XETR", "EUR", "Fund Dxx UCITS ETF")]}
        self.assertEqual(self.pick(responses, "ZZ0000000105", "FDD", "EUR", "Fund Dxx (Acc)")["symbol"], "FDD.DE")

    def test_hint_with_other_company_is_rejected(self):
        # `CEE.MI` is Otherbuild; in EUR the name search finds the right one on a regional venue.
        responses = {"CEE": [hit("CEE.MI", "XMIL", "EUR", "Otherbuild S.p.A.")],
                     "Company Exx": [hit("4CEE.TI", None, None), hit("C5E.SG", "XSTU", "EUR", "Company Exx, Inc.")]}
        self.assertEqual(self.pick(responses, "ZZ0000000106", "CEE", "EUR", "Company Exx")["symbol"], "C5E.SG")

    def test_hint_moves_to_trading_venue(self):
        # Company Bxx traded on Xetra, the search returned only Frankfurt.
        responses = {"CBB": [hit("CBB.F", "XFRA", "EUR", "Company Bxx Group")]}
        pinned = self.pick(responses, "ZZ0000000103", "CBB", "EUR", "Company Bxx", "XETR",
                           quotes={("CBB", "XETR"): "EUR"})
        self.assertEqual((pinned["symbol"], pinned["exchangeMic"]), ("CBB.DE", "XETR"))

    def test_failed_quote_fails_the_source(self):
        wf = FakeWf({"CBB": [hit("CBB.F", "XFRA", "EUR", "Company Bxx Group")]})

        def broken(symbol, mic=None):
            raise HttpError(502, "error")
        wf.quote = broken
        with self.assertRaises(HttpError):
            t.pick_listing(wf, "ZZ0000000103", "CBB", "EUR", "Company Bxx", "XETR")

    def test_isin_hit_beats_pinned_old_ticker(self):
        # A renamed ticker: Trading 212 still says OLD, the ISIN knows NEWT.
        responses = {"ZZ0000000102": [hit("NEWT", "XNYS", "USD")]}
        picked = self.pick(responses, "ZZ0000000102", "OLD", "USD", "Rename Works", "XNAS",
                           quotes={("OLD", "XNAS"): "USD"})
        self.assertEqual(picked["symbol"], "NEWT")

    def test_confirmed_quote_pins_hint_to_venue(self):
        responses = {"Some Corp": [hit("CH1.SG", "XSTU", "USD", "Some Corp")]}
        pinned = self.pick(responses, "ZZ0000000199", "CHH", "USD", "Some Corp", "XNAS",
                           quotes={("CHH", "XNAS"): "USD"})
        self.assertEqual((pinned["symbol"], pinned["exchangeMic"]), ("CHH", "XNAS"))
        # Without the confirmation the secondary hit stays.
        self.assertEqual(self.pick(responses, "ZZ0000000199", "CHH", "USD", "Some Corp", "XNAS")["symbol"], "CH1.SG")
        # Without a venue nothing is pinned: Company Exx in USD, delisted at Trading 212.
        listings = {"CEE": [hit("CEE.MI", "XMIL", "EUR", "Otherbuild S.p.A.")]}
        self.assertIsNone(self.pick(listings, "ZZ0000000106", "CEE", "USD", "Company Exx", None,
                                    quotes={("CEE", None): "USD"}))

    def test_empty_isin_search_uses_hint(self):
        wf = FakeWf({"CFW": [hit("CFWL", "XNAS", "USD", "Leverage Fund"),
                             hit("CFW", "XNAS", "USD", "Company Fxx Inc.")]})
        self.assertEqual(t.pick_listing(wf, "ZZ0000000107", "CFW", "USD", "Company Fxx (Class A)", "XNAS")["symbol"],
                         "CFW")
        self.assertEqual(wf.calls, ["ZZ0000000107", "CFW"])

    def test_hint_with_suffix_in_trading_currency(self):
        # Company Gxx traded in EUR: ISIN gives CGSX in USD, the hint CGX finds Xetra.
        responses = {"ZZ0000000108": [hit("CGSX", "XNAS", "USD")],
                     "CGX": [hit("CGX.DE", "XETR", "EUR", "Company Gxx, Inc.")]}
        self.assertEqual(self.pick(responses, "ZZ0000000108", "CGX", "EUR", "Company Gxx", "XETR")["symbol"], "CGX.DE")

    def test_no_listing_without_currency_or_name(self):
        self.assertIsNone(self.pick({"X": [hit("QRS", None, None)]}, "X", "QRS", "USD", "Company Hxx"))
        listings = {"QRS": [hit("QRS", "XNYS", "USD", "Company Hxx Corporation")]}
        self.assertEqual(self.pick(listings, "X", "QRS", "USD", "Company Hxx")["symbol"], "QRS")


class ResolveListings(unittest.TestCase):
    def test_each_instrument_is_searched_once(self):
        wf = FakeWf({"ZZ0000000101": [hit("CAA.PA", "XPAR", "EUR")]})
        rows = [{"activityType": "BUY", "symbol": "CAA", "isin": "ZZ0000000101", "currency": "EUR",
                 "symbolName": "Company Axx", "exchangeMic": "XPAR"} for _ in range(2)]
        rows.append({"activityType": "DEPOSIT", "symbol": "$CASH-EUR", "currency": "EUR"})
        self.assertEqual(t.resolve_listings(wf, rows), [])
        self.assertEqual([(r["symbol"], r.get("exchangeMic"), r.get("quoteCcy")) for r in rows],
                         [("CAA.PA", "XPAR", "EUR"), ("CAA.PA", "XPAR", "EUR"), ("$CASH-EUR", None, None)])
        self.assertEqual(wf.calls, ["ZZ0000000101", "CAA"])

    def test_unverified_instrument_is_priced_manually(self):
        rows = [{"activityType": "BUY", "symbol": "CEE", "isin": "ZZ0000000106", "currency": "USD",
                 "symbolName": "Company Exx"}]
        manual = t.resolve_listings(FakeWf({"CEE": [hit("CEE.MI", "XMIL", "EUR", "Otherbuild S.p.A.")]}), rows)
        self.assertEqual(manual, ["Company Exx"])
        keys = ("symbol", "exchangeMic", "quoteCcy", "quoteMode", "instrumentType")
        self.assertEqual({k: rows[0].get(k) for k in keys},
                         {"symbol": "CEE", "exchangeMic": None, "quoteCcy": "USD", "quoteMode": "MANUAL",
                          "instrumentType": "EQUITY"})


class DividendCurrency(unittest.TestCase):
    ISIN = "ZZ0000000109"

    def wf(self):
        # The ISIN search finds the Nasdaq listing in USD and Xetra's in EUR.
        return FakeWf({self.ISIN: [hit("CID", "XNAS", "USD", "Company Ixx Inc"),
                                   hit("CIC.DE", "XETR", "EUR", "Company Ixx Inc")]})

    def dividend(self, ref="d1", ticker="CID_US_EQ"):
        return {"paidOn": "2020-03-01T00:00:00Z", "amount": 2.5, "currency": "EUR", "reference": ref,
                "ticker": ticker, "instrument": {"ticker": ticker, "isin": self.ISIN, "name": "Company Ixx Inc"}}

    def test_eur_dividend_lands_on_traded_listing(self):
        d = data([order(1, "BUY", "CID_US_EQ", self.ISIN, "USD", 1, "300", fx="1.5")], [self.dividend()])
        rows, _ = t.map_activities(d, "A")
        t.resolve_listings(self.wf(), rows, d)
        self.assertEqual([(r["activityType"], r["symbol"], r["exchangeMic"], r["quoteCcy"], r["currency"])
                          for r in rows],
                         [("BUY", "CID", "XNAS", "USD", "USD"), ("DIVIDEND", "CID", "XNAS", "USD", "EUR")])

    def test_dividend_alone_takes_currency_from_metadata(self):
        # Later run: the trades are known, only the dividend is new.
        d = data(dividends=[self.dividend()])
        d["currencies"] = {"CID_US_EQ": "USD"}
        rows, _ = t.map_activities(d, "A")
        t.resolve_listings(self.wf(), rows, d)
        self.assertEqual((rows[0]["symbol"], rows[0]["quoteCcy"], rows[0]["currency"]), ("CID", "USD", "EUR"))

    def test_same_currency_dividend_omits_quote_currency(self):
        d = data(dividends=[dict(self.dividend(), currency="USD")])
        d["currencies"] = {"CID_US_EQ": "USD"}
        rows, _ = t.map_activities(d, "A")
        self.assertNotIn("quoteCcy", rows[0])

    def test_pence_dividend_and_split_are_skipped(self):
        at = "2020-06-10T04:00:00Z"
        d = data([order(20, "SELL", "CLMl_EQ", "ZZ0000000111", "GBX", -3, "60", ftype="STOCK_SPLIT", at=at),
                  order(21, "BUY", "CLMl_EQ", "ZZ0000000111", "GBX", 6, "30", ftype="STOCK_SPLIT", at=at)],
                 [dict(self.dividend(ticker="CLMl_EQ"), instrument={"ticker": "CLMl_EQ", "isin": "ZZ0000000111"})])
        rows, skipped = t.map_activities(d, "A")
        self.assertEqual(rows, [])
        self.assertEqual([s.reason for s in skipped], ["pence quote (GBX) not supported yet"] * 2)


class NoIsin(unittest.TestCase):
    def test_security_without_isin_is_priced_manually(self):
        rows = [{"activityType": "DIVIDEND", "symbol": "NEWT", "currency": "EUR", "quoteCcy": "USD",
                 "exchangeMic": "XNAS"}]
        wf = FakeWf({})
        self.assertEqual(t.resolve_listings(wf, rows), ["NEWT"])
        self.assertEqual((rows[0]["quoteMode"], rows[0]["quoteCcy"], "exchangeMic" in rows[0]),
                         ("MANUAL", "USD", False))
        self.assertEqual(wf.calls, [])

    def test_order_without_isin_lends_no_currency(self):
        d = data([order(1, "BUY", "CID_US_EQ", None, "USD", 1, "300", fx="1.5")],
                 [{"paidOn": "2020-03-01T00:00:00Z", "amount": 1, "currency": "EUR", "reference": "d1",
                   "ticker": "X_EQ", "instrument": {"ticker": "X_EQ", "isin": None}}])
        rows, _ = t.map_activities(d, "A")
        self.assertNotIn("quoteCcy", rows[1])


class TransactionSign(unittest.TestCase):
    def transaction(self, kind, amount):
        tx = {"type": kind, "amount": amount, "currency": "EUR", "dateTime": "2020-01-01T00:00:00Z", "reference": "r"}
        return t.map_activities(data([], [], [tx]), "A")

    def test_expected_signs_are_mapped(self):
        for kind, amount in (("DEPOSIT", 5), ("WITHDRAW", -5), ("FEE", -5), ("INTEREST_ON_FREE_CASH", 5),
                             ("LENDING_INTEREST", 5)):
            rows, skipped = self.transaction(kind, amount)
            self.assertEqual((len(rows), rows[0]["amount"], skipped), (1, "5", []), kind)

    def test_unexpected_sign_is_skipped(self):
        for kind, amount in (("DEPOSIT", -5), ("WITHDRAW", 5), ("FEE", 5), ("INTEREST_ON_FREE_CASH", -5)):
            rows, skipped = self.transaction(kind, amount)
            self.assertEqual((rows, [s.reason for s in skipped]), ([], [f"unexpected sign of {kind}"]), kind)


class Venue(unittest.TestCase):
    def test_mapped_venues_have_yahoo_suffixes(self):
        self.assertEqual(set(t.EXCHANGE_MICS.values()) - set(t.YAHOO_SUFFIXES), set())

    def test_metadata_venue_lands_on_the_row(self):
        d = data([order(1, "BUY", "CID_US_EQ", "ZZ0000000109", "USD", 1, "300", fx="1.5"),
                  order(2, "BUY", "CEE_US_EQ", "ZZ0000000106", "USD", 1, "20", fx="1.5")])
        d["venues"] = {"CID_US_EQ": "NASDAQ", "OTHER_EQ": "Gettex"}
        rows, _ = t.map_activities(d, "A")
        self.assertEqual([r.get("exchangeMic") for r in rows], ["XNAS", None])


class Trades(unittest.TestCase):
    def test_eur_trade_has_no_fx(self):
        rows, skipped = t.map_activities(data([order(1, "BUY", "FDDd_EQ", "ZZ0000000105", "EUR", 4, "100.25")]), "A")
        self.assertEqual(skipped, [])
        r = rows[0]
        self.assertEqual((r["activityType"], r["currency"], r["quantity"], r["unitPrice"], r["isin"]),
                         ("BUY", "EUR", "4", "100.25", "ZZ0000000105"))
        self.assertNotIn("fxRate", r)
        self.assertEqual(r["comment"], "wfsync:t212:fill:1")

    def test_usd_trade_inverts_fx_converts_fee(self):
        # T212 fxRate = USD per EUR (2); the EUR fee 0.25 is 0.50 USD; Wealthfolio wants EUR per USD.
        sale = order(2, "SELL", "CID_US_EQ", "ZZ0000000109", "USD", -5, "300", fx="2", fees=["0.25"])
        rows, _ = t.map_activities(data([sale]), "A")
        r = rows[0]
        self.assertEqual((r["activityType"], r["currency"], r["quantity"]), ("SELL", "USD", "5"))
        self.assertEqual(Decimal(r["fxRate"]), Decimal("0.5"))
        self.assertEqual(Decimal(r["fee"]), Decimal("0.5"))

    def test_missing_fx_is_skipped_not_guessed(self):
        buy = order(3, "BUY", "CID_US_EQ", "ZZ0000000109", "USD", 1, "300", fx=None)
        rows, skipped = t.map_activities(data([buy]), "A")
        self.assertEqual(rows, [])
        self.assertEqual(skipped[0].reason, "FX rate missing")

    def test_pence_listing_is_skipped_visibly(self):
        buy = order(5, "BUY", "CLMl_EQ", "ZZ0000000111", "GBX", 1, "60", fx="80")
        rows, skipped = t.map_activities(data([buy]), "A")
        self.assertEqual(rows, [])
        self.assertEqual(skipped[0].reason, "pence quote (GBX) not supported yet")

    def test_transaction_without_amount_is_skipped(self):
        rows, skipped = t.map_activities(data(transactions=[
            {"type": "DEPOSIT", "currency": "EUR", "dateTime": "2020-01-01T00:00:00Z", "reference": "t1"}]), "A")
        self.assertEqual((rows, [s.reason for s in skipped]), ([], ["amount missing"]))

    def test_repeating_next_page_stops(self):
        c = t.Trading212("k", "s", pause=0)
        c.get = lambda path: {"items": [], "nextPagePath": "/api/v0/again"}
        with self.assertRaisesRegex(RuntimeError, "repeats"):
            c.pages("/api/v0/start")

    def test_next_page_outside_api_stops(self):
        for nxt in (".evil.test/api/v0/x", "//evil.test/api/v0/x", "https://evil.test/api/v0/x", 5):
            c = t.Trading212("k", "s", pause=0)
            asked = []
            c.get = lambda path: asked.append(path) or {"items": [], "nextPagePath": nxt}
            with self.assertRaisesRegex(RuntimeError, "outside its API"):
                c.pages("/api/v0/start")
            self.assertEqual(asked, ["/api/v0/start"])

    def test_cancelled_order_without_fill_is_ignored(self):
        o = order(4, "BUY", "CID_US_EQ", "ZZ0000000109", "USD", 1, "300")
        o["fill"] = None
        self.assertEqual(t.map_activities(data([o]), "A"), ([], []))


class Splits(unittest.TestCase):
    def test_sell_buy_pair_becomes_one_split(self):
        at = "2020-06-10T04:00:00Z"
        rows, skipped = t.map_activities(data([
            order(10, "SELL", "CKA_US_EQ", "ZZ0000000110", "USD", -3, "900", fx="1.5", ftype="STOCK_SPLIT", at=at),
            order(11, "BUY", "CKA_US_EQ", "ZZ0000000110", "USD", 30, "90", fx="1.5", ftype="STOCK_SPLIT", at=at),
        ]), "A")
        self.assertEqual(skipped, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["activityType"], Decimal(rows[0]["amount"])), ("SPLIT", Decimal(10)))
        self.assertEqual(rows[0]["comment"], "wfsync:t212:split:10,11")

    def test_unpaired_split_is_skipped(self):
        rows, skipped = t.map_activities(data([
            order(12, "BUY", "CKA_US_EQ", "ZZ0000000110", "USD", 30, "90", ftype="STOCK_SPLIT")]), "A")
        self.assertEqual(rows, [])
        self.assertEqual(skipped[0].reason, "split halves not paired")


class CashAndIncome(unittest.TestCase):
    def test_dividend_and_transactions(self):
        rows, skipped = t.map_activities(data(
            dividends=[{"paidOn": "2020-03-01T00:00:00Z", "amount": 2.5, "currency": "EUR", "reference": "d1",
                        "ticker": "CID_US_EQ", "instrument": {"ticker": "CID_US_EQ", "isin": "ZZ0000000109"}}],
            transactions=[
                {"type": "DEPOSIT", "amount": 1000, "currency": "EUR", "dateTime": "2020-01-01T00:00:00Z",
                 "reference": "t1"},
                {"type": "WITHDRAW", "amount": -100, "currency": "EUR", "dateTime": "2020-01-02T00:00:00Z",
                 "reference": "t2"},
                {"type": "INTEREST_ON_FREE_CASH", "amount": 0.25, "currency": "EUR", "dateTime": "2020-01-03T00:00:00Z",
                 "reference": "t3"},
                {"type": "FEE", "amount": -2, "currency": "EUR", "dateTime": "2020-01-04T00:00:00Z", "reference": "t4"},
                {"type": "TRANSFER", "amount": -10, "currency": "EUR", "dateTime": "2020-01-05T00:00:00Z",
                 "reference": "t5"},
                {"type": "SOMETHING_NEW", "amount": 1, "currency": "EUR", "dateTime": "2020-01-06T00:00:00Z",
                 "reference": "t6"},
            ]), "A")
        kinds = [(r["activityType"], r.get("amount")) for r in rows]
        self.assertEqual(kinds, [("DIVIDEND", "2.5"), ("DEPOSIT", "1000"), ("WITHDRAWAL", "100"), ("INTEREST", "0.25"),
                                 ("FEE", "2"), ("TRANSFER_OUT", "10")])
        self.assertTrue(rows[-1]["isExternal"])
        self.assertEqual([s.reason for s in skipped], ["transaction type SOMETHING_NEW"])


if __name__ == "__main__":
    unittest.main()
