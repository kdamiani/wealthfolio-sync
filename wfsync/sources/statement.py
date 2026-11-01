"""Shared parts of the uploaded account statements (Monefit CSV, Go & Grow and Debitum Excel).

Monefit's and Go & Grow's files carry no transaction id: a record's reference depends on its date,
type, amount and the balance after it, and the balance chain is the guard that the rows are complete
and their direction right. Debitum's carry one (its reference and guards are in `debitum`). For all
three `check_anchor` keeps a statement from leaving a gap or double counting against what Wealthfolio
already holds.
"""

import hashlib
import re
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal

from ..rows import required_dec
from ..types import Record, StatementData

MAX_LABEL = 40
NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?")  # `0`, `5` or `123.45678901`; no exponent, no grouping
# Apache POI writes small and large doubles in scientific notation. The bound keeps Decimal in range.
SCIENTIFIC = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]{1,2})?")


class StatementError(RuntimeError):
    """The statement cannot be imported. The message is written for the /sync page, and never
    carries a figure from the statement."""


def number(text: str, line: int, column: str, source: str, exponent: bool = False) -> Decimal:
    if not (SCIENTIFIC if exponent else NUMBER).fullmatch(text):
        raise StatementError(f"Line {line}: {column} is not a number in the format of {source}")
    return Decimal(text)


def iso_date(text: str, line: int) -> str:
    """`DD.MM.YYYY` -> `YYYY-MM-DD`."""
    day, _, rest = text.partition(".")
    month, _, year = rest.partition(".")
    try:
        if not (len(day) == 2 and len(month) == 2 and len(year) == 4 and (day + month + year).isdigit()):
            raise ValueError
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        raise StatementError(f"Line {line}: date is not in the format DD.MM.YYYY") from None


def masked(label: str) -> str:
    """A label from the file as it may go to the page and status.json: short, digits masked."""
    return "".join("#" if ch.isnumeric() else ch for ch in label[:MAX_LABEL])


def fmt_key(d: Decimal) -> str:
    return format(required_dec(d), "f")


def reference(source_id: str, rec: Record, *extra: Decimal) -> str:
    """`wfsync:<source>:<kind>:<YYYY-MM-DD>:<hash>` over date, type, amount, `extra` and the balance
    after the row. The balance makes two equal transactions on one day distinct."""
    amounts = (rec["amount"], *extra, rec["balance"])
    key = "|".join((rec["date"], rec["kind"], *map(fmt_key, amounts)))
    return f"wfsync:{source_id}:{rec['kind'].lower()}:{rec['date']}:{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def own_ref(source_id: str) -> re.Pattern[str]:
    return re.compile(rf"wfsync:{source_id}:[a-z]+:([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}):[0-9a-f]{{16}}")


def check_chain(records: Iterable[Record], balance_name: str, opening: Decimal | None = None) -> None:
    """Records oldest first: each balance is the one before plus the signed change. Without an
    opening balance the first row only starts the chain."""
    prev = opening
    for rec in records:
        if prev is not None and prev + rec["change"] != rec["balance"]:
            raise StatementError(f"Line {rec['line']}: {balance_name} does not match the transaction before it")
        prev = rec["balance"]


def check_unique(records: Sequence[Record]) -> None:
    if len({r["ref"] for r in records}) != len(records):
        raise StatementError("Statement contains the same transaction twice")


def check_anchor(data: StatementData, known: set[str], pattern: re.Pattern[str], account_name: str,
                 hint: str) -> None:
    """Refuse a statement that would leave a gap or double count. Into an empty account only
    from the opening (balance zero before its first row); otherwise its first row must be
    imported already. Either way every imported transaction within its days (for a statement from
    the opening also before them) must be in it: a row that vanished means the provider changed
    history, and importing on top would double count.

    `data`: `records` oldest first, `from_start`, `days` (first and last day the statement covers);
    `pattern`: `own_ref(source_id)`; `hint`: how to export from the opening, for the message."""
    records = data["records"]
    first_day, last_day = data["days"]
    if any(not pattern.fullmatch(k) for k in known):
        # Nothing else writes `wfsync:` references into this account; one that does not parse
        # cannot be placed in time, so the checks below could not see it.
        raise StatementError(f"Account {account_name} contains an unknown wfsync transaction: import stopped")
    if not known and not data["from_start"]:
        raise StatementError(f"Statement does not begin at the account opening: for the first import {hint}")
    refs = {r["ref"] for r in records}
    # A statement from the opening that holds every imported transaction can always go in: it only
    # adds what is missing.
    if data["from_start"] and known <= refs:
        return
    if known and records[0]["ref"] not in known:
        raise StatementError(f"First transaction of the statement is not imported: {hint}; "
                             "if that fails too, Wealthfolio is missing an older transaction")
    # Before the opening nothing can exist, so a statement from the opening covers every earlier day.
    low = "" if data["from_start"] else first_day
    days = (m.group(1) for m in map(pattern.fullmatch, known - refs) if m)
    if any(low <= day <= last_day for day in days):
        raise StatementError("Statement contradicts transactions already imported")


def statement_range(data: StatementData) -> str:
    """`2020-01-01 to 2020-12-31` for the page: dates only, no figures."""
    first_day, last_day = data["days"]
    return f"{first_day} to {last_day}"
