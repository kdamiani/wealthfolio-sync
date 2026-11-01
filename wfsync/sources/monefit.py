"""Monefit SmartSaver via its CSV account statement (account -> statement -> download).

Monefit has no public API; the statement is uploaded on the /sync page and imported at once, never
stored (it would sit unencrypted next to Wealthfolio's SQLCipher database).

Format (German and English exports):
- `;`-separated, header `Datum;Details;Betrag;Gebühren;Gesamtes Vermögen;Währung`
  (English `Date;Details;Amount;Fees;Total Wealth;Currency`), dates `DD.MM.YYYY`, decimal point with
  8 places, newest row first, no transaction id.
- `Details` is the app's label of the transaction type in the account's language.
- `Gesamtes Vermögen` is the balance after the row: previous balance + amount - fee, gapless and
  starting at zero for an all-time export.
Deposits and bonuses are confirmed. Withdrawals and earnings follow the app's message catalogue;
their sign is checked by the balance chain, so a wrong guess stops the import. Vault and card-fee
rows and non-zero fees stop it as well.
"""

import csv
import io
from decimal import Decimal
from itertools import pairwise

from ..rows import Skipped, row
from ..types import Record, Row, StatementData
from . import statement
from .statement import StatementError, check_anchor, check_chain, check_unique, iso_date, masked, own_ref, reference

SOURCE_ID = "monefit"
ACCOUNT_NAME = "Monefit SmartSaver"
ACCOUNT_TYPE = "CASH"
CURRENCY = "EUR"

HEADERS = (
    ("Datum", "Details", "Betrag", "Gebühren", "Gesamtes Vermögen", "Währung"),
    ("Date", "Details", "Amount", "Fees", "Total Wealth", "Currency"),
)
# Label -> (type, direction of the balance). German and English labels of the app.
KINDS = {
    "Einzahlungen": ("DEPOSIT", 1), "Funds added": ("DEPOSIT", 1),
    "Abhebungen": ("WITHDRAWAL", -1), "Withdrawals": ("WITHDRAWAL", -1),
    "Ausgezahlte Renditen": ("INTEREST", 1), "Earnings received": ("INTEREST", 1),
    "Boni": ("BONUS", 1), "Bonuses": ("BONUS", 1),
}
UNCHECKED = {
    "Kartengebühr (Einzahlung)", "Card fee (Investment)", "Kartengebühr (Auszahlung)", "Card fee (Withdrawal)",
    "Vault-Einzahlung", "Transfer to Vaults", "Vault-Auszahlung", "Transfer from Vaults",
    "Vault-Erträge", "Vault earnings",
}
OWN_REF = own_ref(SOURCE_ID)
ALL_TIME = "choose the all-time period"
UPLOAD_ACCEPT = ".csv,text/csv"
UPLOAD_HELP = ("Monefit has no API. In SmartSaver open the account, choose the all-time period, apply it, "
               "download the CSV file and upload it here")


def number(text: str, line: int, column: str) -> Decimal:
    return statement.number(text, line, column, "Monefit")


def ref(rec: Record) -> str:
    """Stable reference without an id from Monefit (`statement.reference`, the fee included). The
    type keeps it the same whichever language the statement was exported in."""
    return reference(SOURCE_ID, rec, rec.get("fee", Decimal(0)))


def parse(raw: bytes) -> list[Record]:
    """Records oldest first, each with its signed balance change checked against the balance chain."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise StatementError("File is not a Monefit CSV statement (encoding)") from None
    lines = list(csv.reader(io.StringIO(text), delimiter=";"))
    while lines and not any(cell.strip() for cell in lines[-1]):
        lines.pop()
    if not lines or tuple(c.strip() for c in lines[0]) not in HEADERS:
        raise StatementError("File is not a Monefit CSV statement (unknown header)")
    records: list[Record] = []
    for line, cells in enumerate(lines[1:], 2):
        if len(cells) != 6:
            raise StatementError(f"Line {line}: {len(cells)} columns instead of 6")
        date_s, label, amount_s, fee_s, balance_s, currency = (c.strip() for c in cells)
        if currency != CURRENCY:
            raise StatementError(f"Line {line}: currency is not EUR")
        if label in UNCHECKED:
            raise StatementError(f"Line {line}: transaction type \"{label}\" is not supported yet")
        if label not in KINDS:
            raise StatementError(f"Line {line}: transaction type \"{masked(label)}\" is unknown")
        kind, direction = KINDS[label]
        fee = number(fee_s, line, "Fees")
        if fee:
            raise StatementError(f"Line {line}: a non-zero fee is not supported yet")
        fee = Decimal(0)  # `-0` and `0.00000000` give the same reference as `0`
        amount = number(amount_s, line, "Amount").copy_abs()
        records.append({"line": line, "date": iso_date(date_s, line), "kind": kind, "amount": amount, "fee": fee,
                        "change": direction * amount, "balance": number(balance_s, line, "Total Wealth")})
    if not records:
        raise StatementError("Statement contains no transactions")
    records.reverse()
    for prev, rec in pairwise(records):
        if prev["date"] > rec["date"]:
            raise StatementError(f"Line {rec['line']}: order is not descending by date")
    check_chain(records, "Total Wealth")
    for rec in records:
        rec["ref"] = ref(rec)
    check_unique(records)
    return records


class Statement:
    """The uploaded statement as a source client: `fetch` parses, `complete` checks it against what
    Wealthfolio already holds (`statement.check_anchor`), before anything is mapped."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.data: StatementData | None = None

    def fetch(self) -> StatementData:
        if self.data is None:
            records = parse(self.raw)
            first = records[0]
            self.data = {"currency": CURRENCY, "records": records, "days": (first["date"], records[-1]["date"]),
                         "from_start": first.get("balance") == first["change"]}
        return self.data

    def complete(self, data: StatementData, known: set[str]) -> None:
        check_anchor(data, known, OWN_REF, ACCOUNT_NAME, ALL_TIME)


def map_activities(data: StatementData, account_id: str) -> tuple[list[Row], list[Skipped]]:
    rows: list[Row] = []
    for rec in data["records"]:
        kind, amount = rec["kind"], rec["amount"]
        if kind == "BONUS":
            # A bonus is a gain, as in Monefit's own total earnings, not new capital (Wealthfolio's
            # default for BONUS).
            rows.append(row(account_id, rec["date"], "CREDIT", CURRENCY, rec["ref"], amount=amount,
                            subtype="BONUS", is_external=False))
        else:
            rows.append(row(account_id, rec["date"], kind, CURRENCY, rec["ref"], amount=amount))
    return rows, []
