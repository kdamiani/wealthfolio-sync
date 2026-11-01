"""Go & Grow via its Excel account statement (statements -> download: type account statement,
goal `Alle Ziele`, format Excel).

Go & Grow closed its API on 2025-09-30; the statement is uploaded on the /sync page and imported at
once, never stored, like Monefit's.

Format (German export):
- an .xlsx with one sheet `Data`; cells are inline strings and numbers, no shared strings.
- Summary in D/E: `Zusammenfassung`, `Zu Beginn` (balance before the period), `Einzahlungen`,
  `Auszahlungen`, `Ergebnis` (balance at its end); A2 the period `DD.MM.YYYY-DD.MM.YYYY`, A1 the
  holder's name (not used).
- Then the goal row `Alle Ziele` and the header `Datum|Zahlungsart|Eingänge|Ausgänge|Guthaben`;
  rows oldest first, `DD.MM.YYYY`, one of `Eingänge`/`Ausgänge` filled with 10 decimals, `Guthaben`
  the balance after the row; no transaction id.
- `Zahlungsart`: deposits are `SEPA-Banküberweisung`. The interest label `Go & Grow Zinsen` comes from
  the descriptions of the former API, and zero returns are missing from the export.
Deposits are confirmed. A return and an outgoing transfer follow the API's descriptions; any other
description stops the import, and the balance chain stops a row whose column was misread.
`Einzahlungen`/`Auszahlungen` are not checked: whether they count returns is unknown.
"""

import re
from decimal import Decimal

from ..rows import Skipped, row
from ..types import Record, Row, StatementData
from . import statement, xlsx
from .statement import StatementError, check_anchor, check_chain, check_unique, iso_date, masked, own_ref, reference
from .xlsx import Grid, text

SOURCE_ID = "goandgrow"
ACCOUNT_NAME = "Go & Grow"
ACCOUNT_TYPE = "CASH"
CURRENCY = "EUR"  # Go & Grow only holds euros; the file names no currency

SHEET = "Data"
GOAL = "Alle Ziele"
HEADER = ("Datum", "Zahlungsart", "Eingänge", "Ausgänge", "Guthaben")
COLUMNS = tuple("ABCDE")
PERIOD = re.compile(r"([0-9]{2}\.[0-9]{2}\.[0-9]{4})-([0-9]{2}\.[0-9]{2}\.[0-9]{4})")
# (Zahlungsart, column) -> type. Direction from the column: `Eingänge` raise the balance, `Ausgänge` lower it.
KINDS = {
    ("SEPA-Banküberweisung", "in"): "DEPOSIT",
    ("SEPA-Banküberweisung", "out"): "WITHDRAWAL",
    ("Go & Grow Zinsen", "in"): "INTEREST",
}
OWN_REF = own_ref(SOURCE_ID)
FROM_OPENING = "choose a start date before the account opening"
UPLOAD_ACCEPT = ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
UPLOAD_HELP = ("Go & Grow has no API any more. Under statements choose download, type account statement, all goals "
               "and format Excel (.xlsx), a start date before the account opening and today as the end date, and "
               "upload the file here (only the German export is supported)")
NOT_OURS = "File is not a Go & Grow Excel statement"


def cells(raw: bytes) -> Grid:
    """{(row, column): ("s" | "n", text)} of the sheet `Data`, the workbook's only one."""
    with xlsx.Workbook(raw, NOT_OURS, shared=False) as wb:
        if wb.names() != [SHEET]:
            raise StatementError(f"{NOT_OURS} (sheets)")
        return wb.grid(SHEET)


def amount(grid: Grid, r: int, col: str, column: str) -> Decimal | None:
    """Number in a cell, None for an empty one."""
    kind, value = grid.get((r, col), ("s", ""))
    if kind == "s" and not value:
        return None
    if kind != "n":
        raise StatementError(f"Line {r}: {column} is not a number in the format of Go & Grow")
    # Always written with 10 decimals, so an exponent would mean the format changed.
    return statement.number(value, r, column, "Go & Grow")


def parse(raw: bytes) -> tuple[list[Record], Decimal, tuple[str, str]]:
    """Records oldest first with the statement's opening balance, period and closing balance."""
    grid = cells(raw)
    rows = sorted({r for r, _ in grid})
    headers = [r for r in rows if tuple(text(grid, r, c) for c in COLUMNS) == HEADER]
    if len(headers) != 1:
        raise StatementError(f"{NOT_OURS} (unknown header)")
    head = headers[0]
    if text(grid, head - 1, "A") != GOAL:
        raise StatementError("Statement is not for \"Alle Ziele\": choose the goal \"Alle Ziele\" when downloading")
    summary = {text(grid, r, "D"): r for r in rows if r < head and text(grid, r, "D")}
    if "Zu Beginn" not in summary or "Ergebnis" not in summary:
        raise StatementError(f"{NOT_OURS} (summary)")
    opening = amount(grid, summary["Zu Beginn"], "E", "Zu Beginn")
    closing = amount(grid, summary["Ergebnis"], "E", "Ergebnis")
    periods = [p for r in rows if r < head and (p := PERIOD.fullmatch(text(grid, r, "A") or ""))]
    if opening is None or closing is None or len(periods) != 1:
        raise StatementError(f"{NOT_OURS} (summary)")
    line = summary["Zu Beginn"]
    days = (iso_date(periods[0].group(1), line), iso_date(periods[0].group(2), line))
    if days[0] > days[1]:
        raise StatementError("Period of the statement ends before it begins")
    if any(c not in COLUMNS and value for (r, c), (_, value) in grid.items() if r > head):
        raise StatementError(f"{NOT_OURS} (columns)")
    records: list[Record] = []
    for r in (r for r in rows if r > head):
        if not any(grid.get((r, c), ("s", ""))[1] for c in COLUMNS):
            continue
        day, label = text(grid, r, "A"), text(grid, r, "B")
        if day is None or label is None:
            raise StatementError(f"Line {r}: date or Zahlungsart is not text")
        when = iso_date(day, r)
        incoming, outgoing = amount(grid, r, "C", "Eingänge"), amount(grid, r, "D", "Ausgänge")
        balance = amount(grid, r, "E", "Guthaben")
        if balance is None or (incoming is None) == (outgoing is None):
            raise StatementError(f"Line {r}: expected exactly one of Eingänge and Ausgänge, and Guthaben")
        value, side = (incoming, "in") if incoming is not None else (outgoing or Decimal(0), "out")
        if value < 0:
            raise StatementError(f"Line {r}: a negative amount is not supported yet")
        if not days[0] <= when <= days[1]:
            raise StatementError(f"Line {r}: date outside the period")
        if records and records[-1]["date"] > when:
            raise StatementError(f"Line {r}: order is not ascending by date")
        if (label, side) not in KINDS:
            column = "Eingänge" if side == "in" else "Ausgänge"
            raise StatementError(f"Line {r}: Zahlungsart \"{masked(label)}\" in {column} is unknown")
        if not value:
            continue  # nothing to import
        records.append({"line": r, "date": when, "kind": KINDS[label, side], "amount": value,
                        "change": value if side == "in" else -value, "balance": balance})
    if not records:
        raise StatementError("Statement contains no transactions")
    check_chain(records, "Guthaben", opening)
    if records[-1]["balance"] != closing:
        raise StatementError("Ergebnis of the summary does not match the Guthaben of the last transaction")
    for rec in records:
        rec["ref"] = reference(SOURCE_ID, rec)
    check_unique(records)
    return records, opening, days


class Statement:
    """The uploaded statement as a source client, as `monefit.Statement`."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.data: StatementData | None = None

    def fetch(self) -> StatementData:
        if self.data is None:
            records, opening, days = parse(self.raw)
            self.data = {"currency": CURRENCY, "records": records, "days": days,
                         "from_start": opening == Decimal(0)}
        return self.data

    def complete(self, data: StatementData, known: set[str]) -> None:
        check_anchor(data, known, OWN_REF, ACCOUNT_NAME, FROM_OPENING)


def map_activities(data: StatementData, account_id: str) -> tuple[list[Row], list[Skipped]]:
    return [row(account_id, rec["date"], rec["kind"], CURRENCY, rec["ref"], amount=rec["amount"])
            for rec in data["records"]], []
