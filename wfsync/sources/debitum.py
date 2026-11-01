"""Debitum via its Excel account statement (my balance -> full period, all transactions ->
download selected statement -> XLSX; Debitum sends the file by email).

Debitum has no investor API; the statement is uploaded on the /sync page and imported at once, never
stored, like Monefit's and Go & Grow's.

Format (written by Apache POI, English whatever the app's language):
- an .xlsx with the sheets `Overview`, `Detailed Balance` and `Outstanding Investments`; texts in
  shared strings, numbers as decimals (`100.0`, `-50.0`), very small or large ones with an exponent,
  styled empty cells without a value.
- `Overview`: in B/C `Report date from (inclusive):` and `Report date to (inclusive):` as
  `YYYY-MM-DD` (from defaults to 2018-09-01, before any account); in A/C `Balance on <from>:`,
  `DEPOSITS during reporting period:`, `Other balance increases ...`, `Opening Investments as at <from>`,
  `Closing Investments as at <to>`, `WITHDRAWALS ...`, `Other balance decreases ...` and
  `Balance on <to>:`. The balance is the free cash, not the total value: a SUBSCRIPTION (an order)
  leaves it unchanged, an executed INVESTMENT lowers it by what `Closing Investments` gains. The
  account's total value (the app's total balance) is balance plus investments.
- Interest and tax rows carry 8 and 9 decimals; the balance takes them truncated to the cent, so each
  imported row is truncated to the cent.
- `Detailed Balance`: header `Date|Transaction ID|Asset ID|Transaction Type|Total Turnover|...|Currency`
  (20 columns), one row per transaction with a UUID, oldest first and within a day in transaction
  order (the deposit before the subscriptions made with it), then `TOTAL` with the column sums (in
  floating point, see TOTAL_TOLERANCE). `Total Turnover` is the signed amount of the transaction
  (DEPOSIT positive, SUBSCRIPTION negative, though the balance only falls with the INVESTMENT that
  executes the order).

Confirmed types: DEPOSIT, SUBSCRIPTION, INVESTMENT, TAX and REPAYMENT. REPAYMENT is not in the app's
catalogue; it carries its parts in `Principal Repaid`, `Interest Repaid`, `Penalty Repaid` and
`Interest Boost Repaid`: the earnings are imported as interest, the principal stays a move inside the
account, and the parts must add up to Total Turnover. The other types come from the app's transaction
type catalogue, each with the sign its meaning implies. The guard is the total value: opening balance
and investments plus the imported rows must give the closing ones, the deposits the overview's
DEPOSITS and the withdrawals its WITHDRAWALS (as amounts: the overview's sign is unknown), so a type
wrongly taken for an internal move, or the other way round, stops the import. Types whose meaning or
currency the catalogue leaves open (DEB loyalty points, coupons, exchanges, donations, most reversals)
stop it as well.
"""

import hashlib
import re
from decimal import ROUND_DOWN, Decimal
from typing import TypedDict

from ..rows import Skipped, row
from ..types import Record, Row, StatementData
from . import statement, xlsx
from .statement import StatementError, check_anchor, check_unique, fmt_key, masked, own_ref
from .xlsx import Grid, text

SOURCE_ID = "debitum"
ACCOUNT_NAME = "Debitum"
ACCOUNT_TYPE = "CASH"
CURRENCY = "EUR"

SHEETS = ["Overview", "Detailed Balance", "Outstanding Investments"]
HEADER = ("Date", "Transaction ID", "Asset ID", "Transaction Type", "Total Turnover", "Principal Invested",
          "Principal Repaid", "Interest Repaid", "Penalty Repaid", "Interest Boost Repaid", "Loan Originator",
          "Interest Rate", "Penalty Rate", "Loan Term", "Industry", "Under Asset", "Number of purchased securities",
          "Number of sold securities", "Nominal value of one security", "Currency")
COLUMNS = tuple("ABCDEFGHIJKLMNOPQRST")
SUMMED = tuple("EFGHIJ")  # the columns the TOTAL row sums
# Debitum adds TOTAL up in binary floating point: far below a cent, far above that noise.
TOTAL_TOLERANCE = Decimal("1e-9")
FROM, TO = "Report date from (inclusive):", "Report date to (inclusive):"
DEPOSITS = "DEPOSITS during reporting period:"
WITHDRAWALS = "WITHDRAWALS during reporting period:"
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
CENT = Decimal("0.01")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Transaction type -> (Wealthfolio type or None for a move inside the account, sign of Total Turnover).
INTERNAL = None
# A repayment of both kinds: the principal goes back inside the account, the rest is earnings.
SPLIT = "REPAYMENT"
PRINCIPAL = "G"
EARNINGS = tuple("HIJ")  # Interest, Penalty and Interest Boost Repaid
KINDS = {
    "DEPOSIT": ("DEPOSIT", 1),
    "WITHDRAWAL": ("WITHDRAWAL", -1),
    # cash into or out of an investment: the total value stays
    "SUBSCRIPTION": (INTERNAL, -1), "UNSUBSCRIBE": (INTERNAL, 1), "REVERT_SUBSCRIPTION": (INTERNAL, 1),
    "INVESTMENT": (INTERNAL, -1), "REVERT_INVESTMENT": (INTERNAL, 1),
    "PRINCIPAL_REPAYMENT": (INTERNAL, 1), "PRINCIPAL_BUYBACK_REPAYMENT": (INTERNAL, 1),
    "PRINCIPAL_LIQUIDATE_REPAYMENT": (INTERNAL, 1),
    # earnings
    "INTEREST_REPAYMENT": ("INTEREST", 1), "INTEREST_BUYBACK_REPAYMENT": ("INTEREST", 1),
    "INTEREST_LIQUIDATE_REPAYMENT": ("INTEREST", 1), "INTEREST_BOOST_REPAYMENT": ("INTEREST", 1),
    "PENALTY_REPAYMENT": ("INTEREST", 1), "PENALTY_BUYBACK_REPAYMENT": ("INTEREST", 1),
    "PENALTY_LIQUIDATE_REPAYMENT": ("INTEREST", 1), SPLIT: ("INTEREST", 1),
    "CASHBACK": ("BONUS", 1), "SIGNUP_CASHBACK_BONUS": ("BONUS", 1), "TOP_UP_BONUS": ("BONUS", 1),
    "REFERRAL_REWARD": ("BONUS", 1), "INVITED_REFERRAL_REWARD": ("BONUS", 1), "REFERRAL_ELITE_YIELD": ("BONUS", 1),
    "REFERRAL_REWARD_INVITED_TWO_SIDED_BONUS": ("BONUS", 1), "REFERRAL_REWARD_INVITER_TWO_SIDED_BONUS": ("BONUS", 1),
    "AFFILIATE": ("BONUS", 1),
    # costs
    "TAX": ("TAX", -1), "INVITED_REFERRAL_REWARD_TAX": ("TAX", -1), "REFERRAL_ELITE_YIELD_TAX": ("TAX", -1),
    "REFERRAL_REWARD_TAX": ("TAX", -1), "REFERRAL_REWARD_INVITED_TWO_SIDED_BONUS_TAX": ("TAX", -1),
    "REFERRAL_REWARD_INVITER_TWO_SIDED_BONUS_TAX": ("TAX", -1), "SIGNUP_CASHBACK_BONUS_TAX": ("TAX", -1),
    "TOP_UP_BONUS_TAX": ("TAX", -1),
    "FEE": ("FEE", -1), "PREMIUM_SERVICE_FEE": ("FEE", -1),
}
UNCHECKED = {
    "LOYALTY_DEB", "COUPON", "COUPON_REMOVAL", "DONATION", "EXCHANGE", "EXCHANGE_SELL", "TAX_RETURN",
    "WITHDRAWAL_CANCELLED", "REVERT_AFFILIATE", "REVERT_CASHBACK", "REVERT_INTEREST", "REVERT_PENALTY",
    "REVERT_TAX", "REVERT_INVITED_REFERRAL_REWARD", "REVERT_INVITER_REFERRAL_REWARD",
}
OWN_REF = own_ref(SOURCE_ID)
FROM_OPENING = "choose the full period"
UPLOAD_ACCEPT = ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
UPLOAD_HELP = ("Debitum has no investor API. Under my balance choose the full period and all transactions, download "
               "the selected statement as XLSX; Debitum sends the file by email, upload it here")
NOT_OURS = "File is not a Debitum Excel statement"


class Move(TypedDict):
    """A row of `Detailed Balance`; `kind` is None for a move inside the account."""

    line: int
    date: str
    type: str
    txid: str
    kind: str | None
    change: Decimal


class Overview(TypedDict):
    days: tuple[str, str]
    opening: Decimal
    closing: Decimal
    deposits: Decimal
    withdrawals: Decimal


def amount(grid: Grid, r: int, col: str, column: str) -> Decimal | None:
    """Number in a cell, None for an empty one."""
    kind, value = grid.get((r, col), ("s", ""))
    if kind == "s" and not value:
        return None
    if kind != "n":
        raise StatementError(f"Line {r}: {column} is not a number in the format of Debitum")
    return statement.number(value, r, column, "Debitum", exponent=True)


def iso_date(value: str | None, line: int) -> str:
    """`YYYY-MM-DD`, checked as a real date."""
    if value is None or not ISO_DATE.fullmatch(value):
        raise StatementError(f"Line {line}: date is not in the format YYYY-MM-DD")
    return statement.iso_date(".".join(reversed(value.split("-"))), line)


def overview(grid: Grid) -> Overview:
    """Period and balances of the sheet `Overview`: each label exactly once, its value in column C."""
    def find(col: str, label: str, times: int = 1) -> list[int]:
        rows = sorted(r for (r, c), (kind, value) in grid.items() if c == col and kind == "s" and value == label)
        if len(rows) != times:
            raise StatementError(f"{NOT_OURS} (overview)")
        return rows

    def value(r: int) -> Decimal:
        v = amount(grid, r, "C", "Overview")
        if v is None:
            raise StatementError(f"{NOT_OURS} (overview)")
        return v

    (r_from,), (r_to,) = find("B", FROM), find("B", TO)
    days = iso_date(text(grid, r_from, "C"), r_from), iso_date(text(grid, r_to, "C"), r_to)
    if days[0] > days[1]:
        raise StatementError("Period of the statement ends before it begins")
    if days[0] == days[1]:
        # one day: both balance rows carry the same label, the opening one comes first
        r_open, r_close = find("A", f"Balance on {days[0]}:", times=2)
    else:
        (r_open,), (r_close,) = find("A", f"Balance on {days[0]}:"), find("A", f"Balance on {days[1]}:")
    (r_dep,), (r_wd,) = find("A", DEPOSITS), find("A", WITHDRAWALS)
    (r_inv_open,), (r_inv_close,) = (find("A", f"Opening Investments as at {days[0]}"),
                                     find("A", f"Closing Investments as at {days[1]}"))
    return {"days": days, "opening": value(r_open) + value(r_inv_open), "closing": value(r_close) + value(r_inv_close),
            "deposits": value(r_dep), "withdrawals": value(r_wd)}


def transactions(grid: Grid) -> list[Move]:
    """Rows of the sheet `Detailed Balance` between the header and `TOTAL`, each checked against the
    catalogue, and the TOTAL row checked against the column sums."""
    rows = sorted({r for r, _ in grid})
    if not rows or rows[0] != 1 or tuple(text(grid, 1, c) for c in COLUMNS) != HEADER:
        raise StatementError(f"{NOT_OURS} (unknown header)")
    if any(c not in COLUMNS and value for (_, c), (_, value) in grid.items()):
        raise StatementError(f"{NOT_OURS} (columns)")
    totals = [r for r in rows if text(grid, r, "A") == "TOTAL"]
    if len(totals) != 1 or totals[0] != rows[-1]:
        raise StatementError(f"{NOT_OURS} (total row)")
    total = totals[0]
    out: list[Move] = []
    sums = dict.fromkeys(SUMMED, Decimal(0))
    for r in rows[1:-1]:
        if not any(grid.get((r, c), ("s", ""))[1] for c in COLUMNS):
            continue
        day, txid, kind = text(grid, r, "A"), text(grid, r, "B"), text(grid, r, "D")
        if day is None or txid is None or kind is None:
            raise StatementError(f"Line {r}: Date, Transaction ID or Transaction Type is not text")
        if text(grid, r, "T") != CURRENCY:
            raise StatementError(f"Line {r}: currency is not EUR")
        if not UUID.fullmatch(txid):
            raise StatementError(f"Line {r}: Transaction ID does not have the form of Debitum")
        for c in SUMMED:
            sums[c] += amount(grid, r, c, HEADER[COLUMNS.index(c)]) or 0
        turnover = amount(grid, r, "E", "Total Turnover")
        if turnover is None:
            raise StatementError(f"Line {r}: Total Turnover is missing")
        if kind in UNCHECKED:
            raise StatementError(f"Line {r}: Transaction Type \"{kind}\" is not supported yet")
        if kind not in KINDS:
            raise StatementError(f"Line {r}: Transaction Type \"{masked(kind)}\" is unknown")
        target, sign = KINDS[kind]
        if turnover * sign < 0:
            raise StatementError(f"Line {r}: sign of Total Turnover does not match \"{kind}\"")
        if kind == SPLIT:
            parts = {c: amount(grid, r, c, HEADER[COLUMNS.index(c)]) or Decimal(0) for c in (PRINCIPAL, *EARNINGS)}
            if any(v < 0 for v in parts.values()) or sum(parts.values()) != turnover:
                raise StatementError(f"Line {r}: parts of {SPLIT} do not add up to Total Turnover")
            turnover = sum((parts[c] for c in EARNINGS), Decimal(0))
        out.append({"line": r, "date": iso_date(day, r), "type": kind, "txid": txid, "kind": target,
                    "change": turnover})
    for c in SUMMED:
        if abs((amount(grid, total, c, f"TOTAL {HEADER[COLUMNS.index(c)]}") or 0) - sums[c]) > TOTAL_TOLERANCE:
            raise StatementError(f"Total row does not match the column {HEADER[COLUMNS.index(c)]}")
    dates = [t["date"] for t in out]
    if dates != sorted(dates):
        raise StatementError("Order is not ascending by date")
    return out


def cut(value: Decimal) -> Decimal:
    """A value truncated to the cent, unchanged when it has no more decimals (`100.0` stays)."""
    short = value.quantize(CENT, rounding=ROUND_DOWN)
    return value if short == value else short


def reference(rec: Record) -> str:
    """`wfsync:debitum:<kind>:<YYYY-MM-DD>:<hash>` over Debitum's transaction id, type and amount, in
    the form `statement.check_anchor` places in time. The amount is normalised (`100.0` = `100.00`);
    it is in the key so that a changed amount of an imported transaction stops the import as a
    contradiction instead of being skipped as known."""
    key = f"{rec.get('txid')}|{rec.get('type')}|{fmt_key(rec['change'])}"
    return f"wfsync:{SOURCE_ID}:{rec['kind'].lower()}:{rec['date']}:{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def parse(raw: bytes) -> tuple[list[Record], Overview]:
    """Imported records oldest first and the overview."""
    with xlsx.Workbook(raw, NOT_OURS) as wb:
        if wb.names() != SHEETS:
            raise StatementError(f"{NOT_OURS} (sheets)")
        head = overview(wb.grid(SHEETS[0]))
        moves = transactions(wb.grid(SHEETS[1]))
    first_day, last_day = head["days"]
    for t in moves:
        if not first_day <= t["date"] <= last_day:
            raise StatementError(f"Line {t['line']}: date outside the period")
    # what Debitum credits: each row truncated to the cent
    records: list[Record] = []
    for t in moves:
        credited = cut(t["change"])
        if t["kind"] is not INTERNAL and credited:
            records.append({"line": t["line"], "date": t["date"], "type": t["type"], "txid": t["txid"],
                            "kind": t["kind"], "change": t["change"], "credited": credited,
                            "amount": credited.copy_abs()})
    if sum((t["change"] for t in moves if t["type"] == "DEPOSIT"), Decimal(0)) != head["deposits"]:
        raise StatementError("Deposits of the overview do not match the transactions")
    # the overview's sign of withdrawals is unknown: compared as amounts
    withdrawn = sum((t["change"] for t in moves if t["type"] == "WITHDRAWAL"), Decimal(0))
    if withdrawn.copy_abs() != head["withdrawals"].copy_abs():
        raise StatementError("Withdrawals of the overview do not match the transactions")
    if head["opening"] + sum((r["credited"] for r in records), Decimal(0)) != head["closing"]:
        raise StatementError("Balance of the overview does not match the transactions")
    if not records:
        raise StatementError("Statement contains no transactions")
    for rec in records:
        rec["ref"] = reference(rec)
    check_unique(records)
    return records, head


class Statement:
    """The uploaded statement as a source client, as `monefit.Statement`."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.data: StatementData | None = None

    def fetch(self) -> StatementData:
        if self.data is None:
            records, head = parse(self.raw)
            self.data = {"currency": CURRENCY, "records": records, "days": head["days"],
                         "from_start": head["opening"] == Decimal(0)}
        return self.data

    def complete(self, data: StatementData, known: set[str]) -> None:
        check_anchor(data, known, OWN_REF, ACCOUNT_NAME, FROM_OPENING)


def map_activities(data: StatementData, account_id: str) -> tuple[list[Row], list[Skipped]]:
    rows: list[Row] = []
    for rec in data["records"]:
        if rec["kind"] == "BONUS":
            # As for Monefit: a bonus is a gain, not new capital.
            rows.append(row(account_id, rec["date"], "CREDIT", CURRENCY, rec["ref"], amount=rec["amount"],
                            subtype="BONUS", is_external=False))
        else:
            rows.append(row(account_id, rec["date"], rec["kind"], CURRENCY, rec["ref"], amount=rec["amount"]))
    return rows, []
