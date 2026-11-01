"""Bitget spot account via the read-only REST API v2, from the tax records (`/api/v2/tax/spot-record`).

Field semantics:
- Every balance change of the spot account is a record (`spotTaxType`, `coin`, signed `amount`,
  `fee`, running `balance`): the last `balance` per coin equals the current spot holding, and
  `balance` follows `previous + amount - |fee|`. So `amount` is gross, the fee comes off.
- A trade is two records with the same `bizOrderId`: `Buy` (+ received coin, fee in that coin)
  and `Sell` (- paid coin, no fee). Buying a coin pays USDT; selling a coin receives USDT, fee in USDT.
- EUR reaches USDT in two ways: `fiat_balance_user_out` (- EUR) with `fiat_balance_success_user_in`
  (+ USDT) under one `bizOrderId`, or `FIAT_BALANCE_BUY_OR_SELL_USER_OUT` (- EUR) followed within
  minutes by a `Deposit` (+ USDT) under another id. EUR arrives as `FIAT_BANK_TRANSFER_DEPOSIT_*`
  or `fiat_recharge_in`.
- `Transfer in`/`Transfer out` move USDT between spot and futures; futures stay outside (this importer
  covers spot only), so they count as external transfers.
- Queries span at most 30 days each.

USDT is held as a coin priced in EUR (Yahoo `USDT-EUR`), so performance stays in EUR: a coin
bought for USDT is imported as a USDT sale and a coin purchase of the same EUR value, at Bitget's
USDT/EUR rate of that hour (`Bitget.price_at`).
"""

import base64
import hashlib
import hmac
import time
import urllib.parse
from collections.abc import Collection, Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.request import OpenerDirector

from ..httpjson import NO_DATA, NUMERIC_CODE, HttpError, default_opener, request_json
from ..rows import GROUP_KEY, Number, Skipped, dec, missing_price, required_dec, row
from ..types import Json, MarketData, Row
from .crypto import resolve_coins

SOURCE_ID = "bitget"
ACCOUNT_NAME = "Bitget"
BASE_URL = "https://api.bitget.com"
FIAT = "EUR"
STABLE = "USDT"
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
WINDOW_MS = 30 * DAY_MS
HISTORY_START_MS = 1_514_764_800_000  # 2018-01-01, before Bitget opened
PAGE_LIMIT = 500
PAUSE_S = 1.2  # the tax records return 429 at 0.15 s between calls, not at 1.2 s
FIAT_MATCH_MS = 15 * 60_000

EUR_IN = {"FIAT_BANK_TRANSFER_DEPOSIT_SUCCESS_USER_IN", "fiat_recharge_in"}
FIAT_BUY_OUT = "FIAT_BALANCE_BUY_OR_SELL_USER_OUT"
CONVERT = {"fiat_balance_user_out", "fiat_balance_success_user_in"}
TRANSFER = {"Transfer in": "TRANSFER_IN", "Transfer out": "TRANSFER_OUT"}
REWARD = {"Airdrop Reward-A"}
CREDITS = {"Deposit", "Transfer in"} | REWARD

Event = tuple[Any, ...]


class Bitget:
    def __init__(self, key: str, secret: str, passphrase: str, opener: OpenerDirector | None = None,
                 pause: float = PAUSE_S) -> None:
        self.key = key
        self.secret = secret.encode()
        self.passphrase = passphrase
        self.opener = opener or default_opener()
        self.pause = pause

    def signature(self, target: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(hmac.new(self.secret, (ts + "GET" + target).encode(), hashlib.sha256).digest())
        return {"ACCESS-KEY": self.key, "ACCESS-SIGN": sig.decode(), "ACCESS-TIMESTAMP": ts,
                "ACCESS-PASSPHRASE": self.passphrase}

    def get(self, path: str, signed: bool = True, **query: object) -> Any:
        qs = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        target = path + ("?" + qs if qs else "")
        res = request_json(self.opener, "GET", BASE_URL + target, headers={"locale": "en-US"}, numeric_code="code",
                           sign=(lambda: self.signature(target)) if signed else None)
        time.sleep(self.pause)
        code = str((res or {}).get("code"))
        if code != "00000":
            raise HttpError(200, code if NUMERIC_CODE.fullmatch(code) else "error")
        return res.get("data")

    def fetch(self, now_ms: int | None = None) -> Json:
        by_id: dict[str, Json] = {}
        end = now_ms or int(time.time() * 1000)
        while end > HISTORY_START_MS:
            start = max(HISTORY_START_MS, end - WINDOW_MS + 1)
            by_id.update((r["id"], r) for r in self.window(start, end))
            end = start - 1
        records = list(by_id.values())
        broken = chain_breaks(records)
        if broken:
            raise RuntimeError("Bitget records incomplete for " + ", ".join(broken))
        last = {t["symbol"]: t["lastPr"] for t in self.get("/api/v2/spot/market/tickers", signed=False) or []
                if t.get("symbol") and t.get("lastPr")}
        usd = {s[:-len(STABLE)]: dec(p) for s, p in last.items() if s.endswith(STABLE)}
        usd[STABLE] = Decimal(1)
        return {"currency": FIAT, "records": records, "rates": {}, "eur_prices": eur_prices(last), "usd_prices": usd}

    def window(self, start: int, end: int) -> list[Json]:
        """Records from `start` to `end` (ms, both included). A full page can hide more, and paging
        does not reach them: Bitget sorts a page by ascending id, and `idLessThan` does not continue
        it. So a full response halves the span until every part fits."""
        page: list[Json] = self.get("/api/v2/tax/spot-record", startTime=start, endTime=end, limit=PAGE_LIMIT) or []
        if len(page) < PAGE_LIMIT:
            return page
        if end - start < 1000:
            raise RuntimeError("Bitget: more records within a second than one page holds")
        mid = (start + end) // 2
        return self.window(start, mid) + self.window(mid + 1, end)

    def complete(self, data: Json, known: set[str]) -> None:
        """Fetch the hourly prices that new records need, and only those. Keeps `known` for the
        pairing in `map_activities`."""
        data["known"] = set(known)
        for symbol, hour in needed_prices(plan(data["records"], known), known):
            if (symbol, hour) not in data["rates"]:
                data["rates"][(symbol, hour)] = self.price_at(symbol, hour)

    def price_at(self, symbol: str, hour: int) -> Number:
        """Open of the hourly candle starting at `hour`; `endTime` one hour later returns exactly
        that candle. None if Bitget has none."""
        try:
            candles = self.get("/api/v2/spot/market/history-candles", signed=False, symbol=symbol,
                               granularity="1h", endTime=hour + HOUR_MS, limit=1)
        except HttpError as e:
            if e.status not in NO_DATA:
                raise
            return None
        if not candles or int(candles[0][0]) != hour:
            return None
        open_: str = candles[0][1]
        return open_


def chain_breaks(records: Iterable[Json]) -> list[str]:
    """Coins whose running `balance` does not follow `previous + amount - |fee|` from zero: a
    missing record shows here, whatever the paging did."""
    per: dict[str, list[Json]] = {}
    for r in records:
        per.setdefault(r["coin"], []).append(r)
    broken = []
    for coin, rs in sorted(per.items()):
        balance = Decimal(0)
        for r in sorted(rs, key=lambda r: (int(r["ts"]), int(r["id"]))):
            balance += required_dec(r["amount"]) - abs(dec(r.get("fee")) or Decimal(0))
            if balance != dec(r["balance"]):
                broken.append(coin)
                break
    return broken


def eur_prices(last: dict[str, str]) -> dict[str, Number]:
    """Coin -> current EUR price from Bitget's tickers: direct `<COIN>EUR` or via USDT."""
    usdt_eur = dec(last.get(STABLE + FIAT))
    out: dict[str, Number] = {STABLE: usdt_eur} if usdt_eur else {}
    for symbol, price in last.items():
        if symbol.endswith(FIAT) and symbol != STABLE + FIAT:
            out[symbol[:-len(FIAT)]] = dec(price)
        elif symbol.endswith(STABLE) and usdt_eur and symbol[:-len(STABLE)] not in out:
            out[symbol[:-len(STABLE)]] = required_dec(price) * usdt_eur
    return out


def ref(record: Json) -> str:
    return f"wfsync:bitget:spot:{record['id']}"


def hour_of(record: Json) -> int:
    ms = int(record["ts"])
    return ms - ms % HOUR_MS


def iso(record: Json) -> str:
    moment = datetime.fromtimestamp(int(record["ts"]) / 1000, UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def plan(records: list[Json], known: Collection[str] = frozenset()) -> list[Event]:
    """Group the records into events: ("trade", coin side, quote side), ("convert", EUR side,
    USDT side), ("single", record) or ("unmatched", record, reason). Each event is imported under
    the refs of the records it turns into activities (`event_refs`).

    A fiat purchase pairs with the nearest USDT `Deposit` up to `FIAT_MATCH_MS` after it and is
    imported under the ref of its EUR record. A deposit already imported on its own (its ref is in
    `known`) is never paired later, so an imported record keeps its meaning."""
    events: list[Event] = []
    used: set[str] = set()
    groups: dict[str, list[Json]] = {}
    for r in records:
        groups.setdefault(r.get("bizOrderId") or r["id"], []).append(r)
    for g in groups.values():
        kinds = {r["spotTaxType"] for r in g}
        if kinds == {"Buy", "Sell"}:
            used.update(r["id"] for r in g)
            if len(g) != 2:
                events.extend(("unmatched", r, "order with partial fills not supported yet") for r in g)
                continue
            buy, sell = (g[0], g[1]) if g[0]["spotTaxType"] == "Buy" else (g[1], g[0])
            if buy["coin"] in (STABLE, FIAT) and sell["coin"] not in (STABLE, FIAT):
                events.append(("trade", sell, buy))
            elif sell["coin"] in (STABLE, FIAT) and buy["coin"] not in (STABLE, FIAT):
                events.append(("trade", buy, sell))
            else:
                events.append(("unmatched", buy, "trading pair not supported yet"))
        elif kinds == CONVERT and len(g) == 2:
            used.update(r["id"] for r in g)
            eur, usdt = sorted(g, key=lambda r: r["coin"] != FIAT)
            events.append(("convert", eur, usdt) if (eur["coin"], usdt["coin"]) == (FIAT, STABLE)
                          else ("unmatched", eur, "conversion not supported yet"))
    # A fiat purchase credits its USDT as a `Deposit` under another id, minutes later.
    deposits = [r for r in records if r["spotTaxType"] == "Deposit" and r["coin"] == STABLE and r["id"] not in used]
    for out in sorted((r for r in records if r["spotTaxType"] == FIAT_BUY_OUT and r["id"] not in used),
                      key=lambda r: int(r["ts"])):
        used.add(out["id"])
        after = [d for d in deposits if d["id"] not in used and 0 <= int(d["ts"]) - int(out["ts"]) <= FIAT_MATCH_MS]
        free = [d for d in after if ref(d) not in known]
        if free:
            match = min(free, key=lambda d: int(d["ts"]))
            used.add(match["id"])
            events.append(("convert", out, match))
        elif after:
            events.append(("unmatched", out, "fiat purchase: USDT credit already imported on its own"))
        else:
            events.append(("unmatched", out, "fiat purchase without USDT credit"))
    events.extend(("single", r) for r in records if r["id"] not in used)
    return events


def event_refs(event: Event) -> list[str]:
    """Refs of the records an event imports activities under."""
    if event[0] == "trade":
        return [ref(event[1])] + ([ref(event[2])] if event[2]["coin"] == STABLE else [])
    if event[0] == "convert":
        return [convert_ref(event)]
    return [ref(event[1])]


def convert_ref(event: Event) -> str:
    """A conversion under one order is imported under its USDT record, a fiat purchase under its
    EUR record: the pairing with a separate deposit is the part that could change, not the purchase."""
    eur, usdt = event[1], event[2]
    return ref(eur) if eur["spotTaxType"] == FIAT_BUY_OUT else ref(usdt)


def needed_prices(events: Iterable[Event], known: Collection[str]) -> list[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for e in events:
        if all(r in known for r in event_refs(e)):
            continue
        if e[0] == "trade" and e[2]["coin"] == STABLE:
            out.add((STABLE + FIAT, hour_of(e[1])))
        elif e[0] == "single" and e[1]["coin"] != FIAT and e[1]["spotTaxType"] in CREDITS:
            out.add((STABLE + FIAT, hour_of(e[1])))
            if e[1]["coin"] != STABLE:
                out.add((e[1]["coin"] + STABLE, hour_of(e[1])))
    return sorted(out)


def eur_price(data: Json, coin: str, record: Json) -> Decimal | None:
    """EUR price of `coin` in the hour of `record`, from the prices `complete` fetched."""
    rates = data.get("rates") or {}
    usdt = dec(rates.get((STABLE + FIAT, hour_of(record))))
    if coin == STABLE or not usdt:
        return usdt
    in_usdt = dec(rates.get((coin + STABLE, hour_of(record))))
    return in_usdt * usdt if in_usdt else None


def map_activities(data: Json, account_id: str) -> tuple[list[Row], list[Skipped]]:
    rows: list[Row] = []
    skipped: list[Skipped] = []
    for e in plan(data["records"], data.get("known") or ()):
        kind, first = e[0], e[1]
        rid, date = ref(first), iso(first)
        if kind == "unmatched":
            skipped.append(Skipped(rid, e[2]))
        elif kind == "trade":
            coin_side, quote_side = e[1], e[2]
            coin, quote = coin_side["coin"], quote_side["coin"]
            amount, fee = abs(required_dec(coin_side["amount"])), abs(dec(coin_side.get("fee")) or Decimal(0))
            q_amount, q_fee = abs(required_dec(quote_side["amount"])), abs(dec(quote_side.get("fee")) or Decimal(0))
            rate = Decimal(1) if quote == FIAT else eur_price(data, STABLE, coin_side)
            if not rate:
                skipped.append(missing_price(rid, "USDT price missing", date))
                continue
            if coin_side["spotTaxType"] == "Buy":
                if q_fee:
                    skipped.append(Skipped(rid, "fee on the paying side not supported yet"))
                    continue
                price = q_amount * rate / amount
                legs = [row(account_id, date, "BUY", FIAT, rid, symbol=coin, instrument_type="CRYPTO",
                            quantity=amount - fee, unit_price=price, fee=fee * price or None)]
                if quote == STABLE:
                    legs.append(row(account_id, date, "SELL", FIAT, ref(quote_side), symbol=STABLE,
                                    instrument_type="CRYPTO", quantity=q_amount, unit_price=rate))
            else:
                if fee:
                    skipped.append(Skipped(rid, "fee on the sold side not supported yet"))
                    continue
                legs = [row(account_id, date, "SELL", FIAT, rid, symbol=coin, instrument_type="CRYPTO",
                            quantity=amount, unit_price=q_amount * rate / amount, fee=q_fee * rate or None)]
                if quote == STABLE:
                    legs.append(row(account_id, date, "BUY", FIAT, ref(quote_side), symbol=STABLE,
                                    instrument_type="CRYPTO", quantity=q_amount - q_fee, unit_price=rate))
            for leg in legs:
                # One leg alone would leave EUR cash and the USDT holding off: they share a batch, and
                # a rejected leg holds the other back.
                leg[GROUP_KEY] = rid
            rows.extend(legs)
        elif kind == "convert":
            eur, usdt = e[1], e[2]
            paid, got = abs(required_dec(eur["amount"])), required_dec(usdt["amount"])
            if dec(eur.get("fee")) or dec(usdt.get("fee")):
                skipped.append(Skipped(convert_ref(e), "fee on a fiat conversion not supported yet"))
                continue
            rows.append(row(account_id, iso(usdt), "BUY", FIAT, convert_ref(e), symbol=STABLE,
                            instrument_type="CRYPTO", quantity=got, unit_price=paid / got))
        else:
            result = single(data, account_id, first)
            if isinstance(result, Skipped):
                skipped.append(result)
            else:
                rows.append(result)
    return rows, skipped


def single(data: Json, account_id: str, r: Json) -> Row | Skipped:
    kind, coin, rid, date = r["spotTaxType"], r["coin"], ref(r), iso(r)
    amount, fee = abs(required_dec(r["amount"])), abs(dec(r.get("fee")) or Decimal(0))
    if kind in EUR_IN and coin == FIAT:
        return row(account_id, date, "DEPOSIT", FIAT, rid, amount=amount, fee=fee or None)
    if coin == FIAT:
        return Skipped(rid, f"EUR transaction {kind}")
    if kind in ("Withdrawal", "Transfer out"):
        # The fee leaves the account with the coins (`balance` drops by amount + fee).
        return row(account_id, date, "TRANSFER_OUT", FIAT, rid, symbol=coin, instrument_type="CRYPTO",
                   quantity=amount + fee, unit_price=0, is_external=True)
    if kind in ("Deposit", "Transfer in") or kind in REWARD:
        if fee:
            return Skipped(rid, "credit with a fee not supported yet")
        price = eur_price(data, coin, r)
        if not price:
            return missing_price(rid, "price missing", date)
        if kind in REWARD:
            # Tokens received as income: STAKING_REWARD is Wealthfolio's subtype for that
            # (expands to INTEREST + BUY); there is none for airdrops.
            return row(account_id, date, "INTEREST", FIAT, rid, symbol=coin, instrument_type="CRYPTO",
                       quantity=amount, unit_price=price, amount=amount * price, subtype="STAKING_REWARD")
        return row(account_id, date, "TRANSFER_IN", FIAT, rid, symbol=coin, instrument_type="CRYPTO",
                   quantity=amount, unit_price=price, is_external=True)
    return Skipped(rid, f"transaction type {kind}")


def resolve_listings(wf: MarketData, rows: list[Row], data: Json, existing: Iterable[Json] = ()) -> list[str]:
    """See `crypto.resolve_coins`; Bitget's EUR price (direct or via USDT) is the reference, its USDT
    price stands in for USD."""
    eur = data.get("eur_prices") or {}
    return resolve_coins(wf, rows, eur, existing, "wfsync:bitget:", data.get("usd_prices"))
