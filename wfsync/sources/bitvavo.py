"""Bitvavo via the read-only REST API v2 (`GET /account/history`).

Field semantics:
- Without `fromDate` the history covers only the current year; `fromDate=0` returns all of it.
- `buy`: `priceAmount` is the unit price in `priceCurrency`, `receivedAmount` the coins
  received, `sentAmount` == receivedAmount * priceAmount, and `feesAmount` is charged on top.
- Crypto `deposit` (coins sent in from elsewhere) carries no price. Wealthfolio would record a cost
  basis of 0 and show the whole value as gain, so the coin is valued at Bitvavo's own EUR price when
  it arrived (`Bitvavo.price_at`).
The mapping of `sell`, crypto withdrawals and staking follows the API documentation and is unconfirmed.
"""

import hashlib
import hmac
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from urllib.request import OpenerDirector

from ..httpjson import NO_DATA, HttpError, default_opener, request_json
from ..rows import Number, Skipped, dec, missing_price, required_dec, row
from ..types import Json, MarketData, Row
from .crypto import resolve_coins

SOURCE_ID = "bitvavo"
ACCOUNT_NAME = "Bitvavo"
BASE_URL = "https://api.bitvavo.com/v2"
FIAT = {"EUR"}

REWARD_TYPES = {"staking", "fixed_staking", "distribution", "affiliate", "rebate"}
HOUR_MS = 3_600_000
MAX_PRICE_AGE_H = 24
FEE_ON_TRANSFER = "fee on a crypto transfer not supported yet"


class Bitvavo:
    def __init__(self, key: str, secret: str, opener: OpenerDirector | None = None) -> None:
        self.key = key
        self.secret = secret.encode()
        self.opener = opener or default_opener()

    def signature(self, path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        sig = hmac.new(self.secret, (ts + "GET" + "/v2" + path).encode(), hashlib.sha256).hexdigest()
        return {"Bitvavo-Access-Key": self.key, "Bitvavo-Access-Signature": sig,
                "Bitvavo-Access-Timestamp": ts, "Bitvavo-Access-Window": "10000"}

    def get(self, path: str) -> Any:
        return request_json(self.opener, "GET", BASE_URL + path, sign=lambda: self.signature(path))

    def fetch(self) -> Json:
        items: list[Json] = []
        page = 1
        while True:
            data = self.get(f"/account/history?page={page}&maxItems=100&fromDate=0")
            items.extend(data.get("items") or [])
            if page >= int(data.get("totalPages") or 1):
                break
            page += 1
            time.sleep(0.3)
        tickers = {t["market"][:-4]: t["price"] for t in self.get("/ticker/price")
                   if t.get("market", "").endswith("-EUR") and t.get("price")}
        return {"currency": "EUR", "items": items, "prices": {}, "tickers": tickers}

    def complete(self, data: Json, known: set[str]) -> None:
        """Price the coins that arrived without one, only for records not imported yet."""
        for it in data["items"]:
            if needs_price(it) and ref(it) not in known:
                data["prices"][it["transactionId"]] = self.price_at(it["receivedCurrency"], it["executedAt"])
                time.sleep(0.2)

    def price_at(self, coin: str, executed_at: str) -> Number:
        """Bitvavo's EUR price when `executed_at` happened: the open of that hour's candle or,
        if nothing traded in that hour, the close of the last candle within
        `MAX_PRICE_AGE_H` before. None if Bitvavo has neither."""
        ms = int(datetime.fromisoformat(executed_at.replace("Z", "+00:00")).timestamp() * 1000)
        hour = ms - ms % HOUR_MS
        # Newest candle up to that hour; `end` must sit on an interval boundary (error 205).
        try:
            candles = self.get(f"/{coin}-EUR/candles?interval=1h&end={hour + HOUR_MS}&limit=1")
        except HttpError as e:
            if e.status not in NO_DATA:
                raise
            return None
        if not candles:
            return None
        candle: list[str] = candles[0][:5]
        start, open_, _high, _low, close = candle
        if int(start) == hour:
            return open_
        return close if hour - int(start) <= MAX_PRICE_AGE_H * HOUR_MS else None


def ref(it: Json) -> str:
    return f"wfsync:bitvavo:{it.get('type')}:{it['transactionId']}"


def needs_price(it: Json) -> bool:
    """Coins arriving without a price: deposits from elsewhere and rewards."""
    coin = it.get("receivedCurrency")
    return bool((it.get("type") == "deposit" or it.get("type") in REWARD_TYPES) and coin and coin not in FIAT
                and not dec(it.get("priceAmount")))


def map_activities(data: Json, account_id: str) -> tuple[list[Row], list[Skipped]]:
    rows: list[Row] = []
    skipped: list[Skipped] = []
    for it in data["items"]:
        kind = it.get("type")
        rid = ref(it)
        date = it["executedAt"]
        sent_ccy, recv_ccy = it.get("sentCurrency"), it.get("receivedCurrency")
        sent, recv = dec(it.get("sentAmount")), dec(it.get("receivedAmount"))
        price, price_ccy = dec(it.get("priceAmount")), it.get("priceCurrency")
        if not price and needs_price(it):
            price, price_ccy = dec((data.get("prices") or {}).get(it["transactionId"])), "EUR"
        fee, fee_ccy = dec(it.get("feesAmount")), it.get("feesCurrency")

        if kind in ("buy", "convert_buy", "sell", "convert_sell"):
            buying = kind.endswith("buy")
            coin, qty = (recv_ccy, recv) if buying else (sent_ccy, sent)
            if not (coin and qty and price and price_ccy):
                skipped.append(Skipped(rid, "trade without price or quantity"))
                continue
            if fee and fee_ccy == coin:
                # Whether the amount is before or after a fee in the coin is unknown; a guess would
                # shift quantity and cost basis.
                skipped.append(Skipped(rid, "fee in the coin not supported yet"))
                continue
            if fee and fee_ccy != price_ccy:
                skipped.append(Skipped(rid, "fee in a third currency"))
                continue
            trade_fee = fee or None
            rows.append(row(account_id, date, "BUY" if buying else "SELL", price_ccy, rid,
                            symbol=coin, instrument_type="CRYPTO", quantity=qty, unit_price=price, fee=trade_fee))
        elif kind == "deposit" and recv_ccy in FIAT:
            rows.append(row(account_id, date, "DEPOSIT", recv_ccy, rid, amount=recv, fee=fee or None))
        elif kind == "withdrawal" and sent_ccy in FIAT:
            rows.append(row(account_id, date, "WITHDRAWAL", sent_ccy, rid, amount=sent, fee=fee or None))
        elif kind == "deposit":
            if not price:
                skipped.append(missing_price(rid, "crypto deposit without a price", date))
                continue
            if fee:
                skipped.append(Skipped(rid, FEE_ON_TRANSFER))
                continue
            rows.append(row(account_id, date, "TRANSFER_IN", price_ccy, rid, symbol=recv_ccy,
                            instrument_type="CRYPTO", quantity=recv, unit_price=price, is_external=True))
        elif kind == "withdrawal":
            if fee:
                skipped.append(Skipped(rid, FEE_ON_TRANSFER))
                continue
            rows.append(row(account_id, date, "TRANSFER_OUT", price_ccy or "EUR", rid, symbol=sent_ccy,
                            instrument_type="CRYPTO", quantity=sent, unit_price=price or 0, is_external=True))
        elif kind in REWARD_TYPES and recv_ccy and recv_ccy not in FIAT:
            if not price:
                skipped.append(missing_price(rid, "reward without a price", date))
                continue
            rows.append(row(account_id, date, "INTEREST", price_ccy, rid, symbol=recv_ccy, instrument_type="CRYPTO",
                            quantity=recv, unit_price=price, amount=required_dec(recv) * price,
                            subtype="STAKING_REWARD"))
        elif kind in REWARD_TYPES and recv_ccy in FIAT:
            rows.append(row(account_id, date, "CREDIT", recv_ccy, rid, amount=recv,
                            subtype="REBATE" if kind == "rebate" else "BONUS"))
        else:
            skipped.append(Skipped(rid, f"transaction type {kind}"))
    return rows, skipped


def resolve_listings(wf: MarketData, rows: list[Row], data: Json, existing: Iterable[Json] = ()) -> list[str]:
    """See `crypto.resolve_coins`; Bitvavo's EUR ticker is the reference price."""
    return resolve_coins(wf, rows, data.get("tickers") or {}, existing, "wfsync:bitvavo:")
