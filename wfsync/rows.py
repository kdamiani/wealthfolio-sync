"""Build Wealthfolio `ActivityImport` rows.

Every row carries `comment = <ref>`, a stable source reference (`wfsync:<source>:...`). It is the
deduplication key (`sync.sync_source`).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Final, cast

from .types import Row

Number = Decimal | int | float | str | None

# Bitget reports up to 11 decimals, and a coin worth under 0.001 EUR needs more than 8 in its unit
# price. 18 places keep both; the wide context keeps large quantities exact.
PLACES = Decimal("1e-18")
# Rows of one source record that only make sense together (both legs of a trade) share this key.
# `sync.sync_source` strips it before Wealthfolio sees the row, keeps the group in one batch and,
# if Wealthfolio rejects one leg, holds the others back.
GROUP_KEY: Final = "_group"


def dec(value: Number) -> Decimal | None:
    if value is None or value == "":
        return None
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    with localcontext() as ctx:
        ctx.prec = 60
        return d.quantize(PLACES).normalize()


def required_dec(value: Number) -> Decimal:
    d = dec(value)
    if d is None:
        raise ValueError("number missing")
    return d


def fmt(value: Number) -> str | None:
    d = dec(value)
    return None if d is None else format(d, "f")


def row(account_id: str, date: str, activity_type: str, currency: str, ref: str, *, symbol: str = "",
        isin: str | None = None, symbol_name: str | None = None, quantity: Number = None, unit_price: Number = None,
        amount: Number = None, fee: Number = None, fx_rate: Number = None, subtype: str | None = None,
        instrument_type: str | None = None, is_external: bool | None = None, exchange_mic: str | None = None,
        quote_ccy: str | None = None) -> Row:
    r = {
        "date": date,
        "symbol": symbol,
        "activityType": activity_type,
        "currency": currency,
        "accountId": account_id,
        "comment": ref,
        "isDraft": False,
        "isValid": True,
    }
    optional = {
        "isin": isin,
        "symbolName": symbol_name,
        "quantity": fmt(quantity),
        "unitPrice": fmt(unit_price),
        "amount": fmt(amount),
        "fee": fmt(fee),
        "fxRate": fmt(fx_rate),
        "subtype": subtype,
        "instrumentType": instrument_type,
        "isExternal": is_external,
        "exchangeMic": exchange_mic,
        "quoteCcy": quote_ccy,
    }
    r.update({k: v for k, v in optional.items() if v is not None})
    return cast(Row, r)


PRICE_WAIT = timedelta(days=7)


class Skipped:
    """A source record that is not imported, with a reason that carries no figures. `transient`:
    a later run should import it, so the source stays `partial` until then."""

    def __init__(self, ref: str, reason: str, transient: bool = False) -> None:
        self.ref = ref
        self.reason = reason
        self.transient = transient

    def __repr__(self) -> str:
        return f"Skipped({self.ref!r}, {self.reason!r})"


def missing_price(ref: str, reason: str, date: str) -> Skipped:
    """A recent record waits for its price. An older one will not get one (a delisted pair, say):
    it stays listed but no longer holds the source at `partial`. Every run tries it again."""
    age = datetime.now(UTC) - datetime.fromisoformat(date.replace("Z", "+00:00"))
    return Skipped(ref, reason, transient=age < PRICE_WAIT)
