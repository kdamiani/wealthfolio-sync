"""Trading 212 (Invest account) via the official read-only API v0.

Field semantics:
- `fill.walletImpact.fxRate` is instrument currency per account currency, i.e.
  account value = quantity * price / fxRate. Wealthfolio's `fxRate` is the inverse.
- `walletImpact.taxes` are in the account currency and are charged on top of the trade value.
- A stock split arrives as a SELL fill of the old quantity plus a BUY fill of the new quantity
  with `fill.type == STOCK_SPLIT` and the same `filledAt`.
- The instrument metadata names the exchange Trading 212 trades on (via `workingScheduleId`);
  instruments Trading 212 no longer lists are missing there.
"""

import base64
import time
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from typing import Any
from urllib.request import OpenerDirector

from ..httpjson import default_opener, request_json
from ..rows import Skipped, dec, required_dec, row
from ..types import Json, MarketData, Row

SOURCE_ID = "trading212"
ACCOUNT_NAME = "Trading 212"
BASE_URL = "https://live.trading212.com"
PAGE_PAUSE_S = 3.2  # 20 requests per minute per endpoint

# Transaction type -> (Wealthfolio type, sign of the amount). The row carries the amount without its
# sign, so a refund or correction with the other one would be imported in the wrong direction.
TX_TYPES = {
    "DEPOSIT": ("DEPOSIT", 1),
    "WITHDRAW": ("WITHDRAWAL", -1),
    "FEE": ("FEE", -1),
    "INTEREST_ON_FREE_CASH": ("INTEREST", 1),
    "LENDING_INTEREST": ("INTEREST", 1),
}


class Trading212:
    def __init__(self, key: str, secret: str, opener: OpenerDirector | None = None,
                 pause: float = PAGE_PAUSE_S) -> None:
        token = base64.b64encode(f"{key}:{secret}".encode()).decode()
        self.headers = {"Authorization": "Basic " + token}
        self.opener = opener or default_opener()
        self.pause = pause

    def get(self, path: str) -> Any:
        return request_json(self.opener, "GET", BASE_URL + path, headers=self.headers)

    def pages(self, path: str) -> list[Json]:
        items: list[Json] = []
        nxt, seen = path, set()
        while nxt:
            if nxt in seen:
                raise RuntimeError("Trading 212 paging repeats itself")
            seen.add(nxt)
            data = self.get(nxt)
            items.extend(data.get("items") or [])
            nxt = data.get("nextPagePath")
            # Joined to BASE_URL with the credentials: anything but an API path could change the host.
            if nxt and not (isinstance(nxt, str) and nxt.startswith("/api/v0/")):
                raise RuntimeError("Trading 212 paging points outside its API")
            if nxt:
                time.sleep(self.pause)
        return items

    def fetch(self) -> Json:
        info = self.get("/api/v0/equity/account/info")
        orders = self.pages("/api/v0/equity/history/orders?limit=50")
        time.sleep(self.pause)
        dividends = self.pages("/api/v0/equity/history/dividends?limit=50")
        time.sleep(self.pause)
        transactions = self.pages("/api/v0/equity/history/transactions?limit=50")
        time.sleep(self.pause)
        return {"currency": info["currencyCode"], "orders": orders, "dividends": dividends,
                "transactions": transactions, **self.instruments()}

    def instruments(self) -> Json:
        """Ticker -> exchange name (`venues`) and -> trading currency (`currencies`), for every
        instrument Trading 212 currently lists."""
        instruments = self.get("/api/v0/equity/metadata/instruments")
        time.sleep(self.pause)
        schedules = {s["id"]: e["name"] for e in self.get("/api/v0/equity/metadata/exchanges")
                     for s in e.get("workingSchedules") or []}
        return {"venues": {i["ticker"]: schedules.get(i.get("workingScheduleId")) for i in instruments},
                "currencies": {i["ticker"]: i.get("currencyCode") for i in instruments}}


def symbol_hint(ticker: str | None) -> str:
    """`ABC_US_EQ` -> `ABC`, `ABCd_EQ` -> `ABC`. Only a hint: the ISIN decides."""
    base = (ticker or "").split("_", 1)[0]
    return base.rstrip("abcdefghijklmnopqrstuvwxyz") or base


# Trading 212 exchange names (from its metadata) -> MIC. Gettex has no listing in
# Wealthfolio's search and stays unmapped, so those instruments resolve by search alone.
EXCHANGE_MICS = {
    "NASDAQ": "XNAS", "NYSE": "XNYS", "OTC Markets": "OTCM",
    "Deutsche Börse Xetra": "XETR", "Euronext Paris": "XPAR", "Euronext Amsterdam": "XAMS",
    "Euronext Brussels": "XBRU", "Euronext Lisbon": "XLIS", "Borsa Italiana": "XMIL",
    "Bolsa de Madrid": "XMAD", "London Stock Exchange": "XLON", "London Stock Exchange AIM": "XLON",
    "London Stock Exchange NON-ISA": "XLON", "SIX Swiss Exchange": "XSWX",
    "Toronto Stock Exchange": "XTSE", "Wiener Börse": "XWBO",
}
# Yahoo suffix per MIC, from Wealthfolio's `exchanges.json` (3.11.0). The import check takes
# `ABC.DE` + XETR literally but re-resolves a bare `ABC` + XETR by its own search, which can land
# on an unrelated instrument, so a pinned listing carries the suffix.
YAHOO_SUFFIXES = {
    "XNAS": "", "XNYS": "", "OTCM": "", "XETR": ".DE", "XPAR": ".PA", "XAMS": ".AS", "XBRU": ".BR",
    "XLIS": ".LS", "XMIL": ".MI", "XMAD": ".MC", "XLON": ".L", "XSWX": ".SW", "XTSE": ".TO", "XWBO": ".VI",
}
SECURITY_TYPES = {"BUY", "SELL", "DIVIDEND", "SPLIT"}
# Regional German venues, multilateral feeds and OTC: real but thin listings, used only when
# nothing better exists.
SECONDARY_MICS = {"XSTU", "XHAM", "XMUN", "XHAN", "XDUS", "XBER", "CCXE", "BCXE", "OTCM"}
NAME_NOISE = {"inc", "corp", "corporation", "co", "company", "the", "class", "plc", "sa", "ag", "se", "nv",
              "spa", "group", "holding", "holdings", "ltd", "limited", "adr", "etf", "ucits", "acc", "dist"}


def name_tokens(name: str | None) -> set[str]:
    words = "".join(c if c.isalnum() else " " for c in (name or "").lower().replace("'", "")).split()
    return {w for w in words if len(w) >= 3 and w not in NAME_NOISE}


def same_name(hit: Json, name: str | None) -> bool:
    return bool(name_tokens(hit.get("longName") or hit.get("shortName")) & name_tokens(name))


def pick_listing(wf: MarketData, isin: str, hint: str, currency: str, name: str | None,
                 venue: str | None) -> Json | None:
    """Choose the listing Wealthfolio should price, from its own symbol search.

    No single query is reliable. The ticker hint can find an unrelated company with the same
    ticker; the ISIN finds Stuttgart listings of US stocks (also as pseudo symbols named after the
    ISIN) and for some large companies nothing at all. So only hits in the currency Trading 212
    trades in count: any ISIN hit, a hint hit with the exact symbol and a matching name
    (Wealthfolio's `canonicalSymbol` drops the venue suffix: `ABC.DE` -> `ABC`), or a name-search
    hit with a matching name. The exchange Trading 212 names (`venue`) wins, then an ISIN hit on a
    main venue, then the hint pinned to `venue` once Wealthfolio confirms a quote there in that
    currency, then other main venues, then secondary ones; within a class ISIN beats hint beats name.
    Returns a search hit (or a pinned stand-in) or None.
    """
    def usable(hit: Json) -> bool:
        return hit.get("currency") == currency and bool(hit.get("exchangeMic"))

    def rank(c: tuple[int, Json]) -> tuple[int, int]:
        step, hit = c
        mic = hit["exchangeMic"]
        return (0 if mic == venue else 3 if mic in SECONDARY_MICS else 1, step)

    def best() -> tuple[int, Json]:
        return min(cands, key=rank)

    cands = [(0, h) for h in wf.search(isin) if usable(h)]
    cands += [(1, h) for h in wf.search(hint)
              if usable(h) and (h.get("canonicalSymbol") or h.get("symbol")) == hint and same_name(h, name)]
    if name and (not cands or rank(best())[0]):
        cands += [(2, h) for h in wf.search(name) if usable(h) and same_name(h, name)]
    # Below an ISIN hit on a main venue (renamed tickers) and above the rest.
    if venue and (not cands or rank(best()) > (1, 0)) and wf.quote(hint, venue).get("currency") == currency:
        return {"symbol": hint + YAHOO_SUFFIXES[venue], "exchangeMic": venue, "currency": currency}
    return best()[1] if cands else None


def resolve_listings(wf: MarketData, rows: list[Row], data: Json | None = None,
                     existing: Iterable[Json] = ()) -> list[str]:
    """Pin every security row to one listing (symbol, exchange MIC, quote currency).

    Rows arrive with the hint as `symbol`, Trading 212's exchange as `exchangeMic` and, where the
    payment currency differs from the instrument's (a EUR dividend on a USD stock), the
    instrument currency as `quoteCcy`; the listing is chosen in that currency, so trades and
    dividends of one ISIN land on one asset. Without a verified listing a row is priced from its
    own trades (MANUAL), never left to Wealthfolio's guess, which can pick an unrelated company.
    Returns the names priced manually.

    `data` and `existing` are unused (common signature with Bitvavo). Earlier runs need no memory
    here: when Wealthfolio holds exactly one asset for an ISIN, its import takes that one before
    looking at symbol or venue (`find_for_import_input`, 3.11.0). A row without ISIN cannot be
    verified and is priced manually.
    """
    picks: dict[tuple[str, str, str], Json | None] = {}
    manual: set[str] = set()
    for r in rows:
        isin = r.get("isin")
        if r.get("activityType") not in SECURITY_TYPES:
            continue  # cash rows
        if not isin:
            r.pop("exchangeMic", None)
            r["quoteCcy"], r["quoteMode"] = r.get("quoteCcy") or r["currency"], "MANUAL"
            r["instrumentType"] = "EQUITY"
            manual.add(r.get("symbolName") or r["symbol"])
            continue
        ccy = r.get("quoteCcy") or r["currency"]
        key = (isin, r["symbol"], ccy)
        if key not in picks:
            picks[key] = pick_listing(wf, isin, r["symbol"], ccy, r.get("symbolName"), r.get("exchangeMic"))
        hit = picks[key]
        if hit:
            r["symbol"], r["exchangeMic"], r["quoteCcy"] = hit["symbol"], hit["exchangeMic"], hit["currency"]
        else:
            r.pop("exchangeMic", None)
            r["quoteCcy"], r["quoteMode"], r["instrumentType"] = ccy, "MANUAL", "EQUITY"
            manual.add(r.get("symbolName") or r["symbol"])
    return sorted(manual)


def quote_currency(code: str | None) -> str:
    return "GBp" if code == "GBX" else code or ""


def map_activities(data: Json, account_id: str) -> tuple[list[Row], list[Skipped]]:
    acct_ccy = data["currency"]
    venues = data.get("venues") or {}
    currencies = data.get("currencies") or {}
    # Instrument currency per ISIN from the trades, for instruments missing in the metadata.
    instruments = [(i.get("order") or {}).get("instrument") or {} for i in data["orders"]]
    traded_ccy = {x["isin"]: x.get("currency") for x in instruments if x.get("isin")}
    rows: list[Row] = []
    skipped: list[Skipped] = []

    def venue(ticker: str | None) -> str | None:
        return EXCHANGE_MICS.get(venues.get(ticker) or "")

    def instrument_ccy(ticker: str | None, isin: str | None, fallback: str) -> str:
        return quote_currency(currencies.get(ticker) or traded_ccy.get(isin) or fallback)

    splits: defaultdict[tuple[Any, Any], list[Json]] = defaultdict(list)

    for item in data["orders"]:
        order, fill = item.get("order") or {}, item.get("fill")
        if not fill:
            continue  # cancelled or rejected, nothing happened
        instr = order.get("instrument") or {}
        ref = f"wfsync:t212:fill:{fill['id']}"
        if fill.get("type") == "STOCK_SPLIT":
            splits[(instr.get("ticker") or order.get("ticker"), fill.get("filledAt"))].append(item)
            continue
        if fill.get("type") != "TRADE":
            skipped.append(Skipped(ref, f"fill type {fill.get('type')}"))
            continue
        ccy = quote_currency(instr.get("currency") or order.get("currency"))
        if ccy == "GBp":
            # Whether walletImpact.fxRate is per pound or per pence is unknown.
            skipped.append(Skipped(ref, "pence quote (GBX) not supported yet"))
            continue
        wallet = fill.get("walletImpact") or {}
        taxes = wallet.get("taxes") or []
        fee_acct = sum(((dec(t.get("quantity")) or Decimal(0)).copy_abs() for t in taxes), Decimal(0))
        fx_rate = fee = None
        if ccy == acct_ccy:
            fee = fee_acct
        else:
            t212_fx = dec(wallet.get("fxRate"))
            if not t212_fx:
                skipped.append(Skipped(ref, "FX rate missing"))
                continue
            fx_rate = Decimal(1) / t212_fx
            fee = fee_acct * t212_fx
        rows.append(row(
            account_id, fill["filledAt"], order["side"], ccy, ref,
            symbol=symbol_hint(instr.get("ticker")), isin=instr.get("isin"), symbol_name=instr.get("name"),
            quantity=required_dec(fill["quantity"]).copy_abs(), unit_price=fill["price"], fee=fee, fx_rate=fx_rate,
            exchange_mic=venue(instr.get("ticker")),
        ))

    for (ticker, filled_at), items in splits.items():
        sells = [i for i in items if i["order"]["side"] == "SELL"]
        buys = [i for i in items if i["order"]["side"] == "BUY"]
        refs = ",".join(str(i["fill"]["id"]) for i in sorted(items, key=lambda i: i["fill"]["id"]))
        ref = f"wfsync:t212:split:{refs}"
        if len(sells) != 1 or len(buys) != 1:
            skipped.append(Skipped(ref, "split halves not paired"))
            continue
        old_q = required_dec(sells[0]["fill"]["quantity"]).copy_abs()
        new_q = required_dec(buys[0]["fill"]["quantity"]).copy_abs()
        if not old_q:
            skipped.append(Skipped(ref, "split without old quantity"))
            continue
        instr = buys[0]["order"].get("instrument") or {}
        if quote_currency(instr.get("currency")) == "GBp":
            skipped.append(Skipped(ref, "pence quote (GBX) not supported yet"))
            continue
        rows.append(row(
            account_id, filled_at, "SPLIT", quote_currency(instr.get("currency")), ref,
            symbol=symbol_hint(ticker), isin=instr.get("isin"), symbol_name=instr.get("name"),
            amount=new_q / old_q, exchange_mic=venue(ticker),
        ))

    for d in data["dividends"]:
        instr = d.get("instrument") or {}
        ticker = instr.get("ticker") or d.get("ticker")
        ref = f"wfsync:t212:div:{d['reference']}"
        pay_ccy = d.get("currency") or acct_ccy
        listing_ccy = instrument_ccy(ticker, instr.get("isin"), pay_ccy)
        if listing_ccy == "GBp":
            skipped.append(Skipped(ref, "pence quote (GBX) not supported yet"))
            continue
        rows.append(row(
            account_id, d["paidOn"], "DIVIDEND", pay_ccy, ref,
            symbol=symbol_hint(ticker), isin=instr.get("isin"),
            symbol_name=instr.get("name"), amount=required_dec(d["amount"]).copy_abs(),
            exchange_mic=venue(ticker), quote_ccy=listing_ccy if listing_ccy != pay_ccy else None,
        ))

    for t in data["transactions"]:
        ref = f"wfsync:t212:tx:{t['reference']}"
        kind = t.get("type")
        amount = dec(t.get("amount"))
        if amount is None:
            skipped.append(Skipped(ref, "amount missing"))
            continue
        ccy = t.get("currency") or acct_ccy
        if kind == "TRANSFER":
            act = "TRANSFER_IN" if amount >= 0 else "TRANSFER_OUT"
            rows.append(row(account_id, t["dateTime"], act, ccy, ref, amount=amount.copy_abs(), is_external=True))
        elif kind in TX_TYPES:
            act, sign = TX_TYPES[kind]
            if amount * sign < 0:
                skipped.append(Skipped(ref, f"unexpected sign of {kind}"))
                continue
            rows.append(row(account_id, t["dateTime"], act, ccy, ref, amount=amount.copy_abs()))
        else:
            skipped.append(Skipped(ref, f"transaction type {kind}"))

    return rows, skipped
