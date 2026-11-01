"""Pricing of coins in Wealthfolio, shared by the crypto exchanges."""

from collections.abc import Iterable
from decimal import Decimal

from ..config import yahoo_aliases
from ..rows import Number, dec
from ..types import Json, MarketData, Row

PRICE_TOLERANCE = Decimal("0.05")


def verified_pair(wf: MarketData, coin: str, eur_price: Number, usd_price: Number = None) -> str | None:
    """The Yahoo pair `<COIN>-EUR` if Wealthfolio's quote for it matches the exchange's EUR price,
    else `<COIN>-USD` against its USD (USDT) price: Yahoo lists some coins only in USD, and
    Wealthfolio converts the quote to EUR.

    A bare coin symbol is not safe: it can resolve to another coin. Different coins differ wildly in
    price, so a match within `PRICE_TOLERANCE` confirms the identity.
    """
    base = yahoo_aliases().get(coin, coin)
    for ccy, price in (("EUR", eur_price), ("USD", usd_price)):
        exchange = dec(price)
        if not exchange:
            continue
        pair = f"{base}-{ccy}"
        quote = wf.quote(pair)
        yahoo = dec(quote.get("price"))
        if quote.get("currency") == ccy and yahoo and abs(yahoo / exchange - 1) <= PRICE_TOLERANCE:
            return pair
    return None


def resolve_coins(wf: MarketData, rows: list[Row], eur_prices: dict[str, Number], existing: Iterable[Json],
                  prefix: str, usd_prices: dict[str, Number] | None = None) -> list[str]:
    """Pin every coin row to a price-confirmed Yahoo pair (EUR, else USD), else price it from its
    own trades (MANUAL) instead of leaving the symbol to Wealthfolio's guess. Returns the coins
    priced manually.

    A coin already held in the account keeps the pricing of its asset (`existing` activities;
    Wealthfolio stores the pair `BTC-EUR` as asset `BTC`): coins have no ISIN, so a price check
    failing on a later night would otherwise put a sale on a second, empty asset.
    """
    held = {a.get("assetSymbol"): (a.get("assetPricingMode"), a.get("assetQuoteCcy") or "EUR") for a in existing
            if a.get("assetId") and a.get("instrumentType") == "CRYPTO"
            and (a.get("comment") or "").startswith(prefix)}
    aliases = yahoo_aliases()
    picks: dict[str, str | None] = {}
    manual: set[str] = set()
    for r in rows:
        if r.get("instrumentType") != "CRYPTO":
            continue
        coin = r["symbol"]
        if coin not in picks:
            base = aliases.get(coin, coin)
            # A MANUAL asset carries the coin itself, a priced one the Yahoo base.
            asset = next((s for s in (coin, base) if s in held), None)
            if asset:
                mode, ccy = held[asset]
                picks[coin] = None if mode == "MANUAL" else f"{asset}-{ccy}"
            else:
                picks[coin] = verified_pair(wf, coin, eur_prices.get(coin), (usd_prices or {}).get(coin))
        pick = picks[coin]
        if pick:
            r["symbol"], r["quoteCcy"] = pick, pick.rsplit("-", 1)[1]
        else:
            r["quoteCcy"], r["quoteMode"] = "EUR", "MANUAL"
            manual.add(coin)
    return sorted(manual)
