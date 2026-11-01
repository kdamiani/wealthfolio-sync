import hashlib
import io
import re
import unittest
import zipfile
from decimal import Decimal
from xml.sax.saxutils import escape

from wfsync.sources import debitum as d
from wfsync.sources import statement

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PNS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"

TX = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 7)]
# (date, id, type, turnover, interest repaid), oldest first.
ROWS = [
    ("2020-03-01", TX[0], "DEPOSIT", "1000.0", None),
    ("2020-03-01", TX[1], "SUBSCRIPTION", "-400.0", None),
    ("2020-03-02", TX[2], "INVESTMENT", "-400.0", None),
    ("2020-03-20", TX[3], "INTEREST_REPAYMENT", "2.5", "2.5"),
    ("2020-03-20", TX[4], "TAX", "-0.5", None),
    ("2020-03-25", TX[5], "WITHDRAWAL", "-2.0", None),
]


class Cells:
    """Shared strings as Apache POI writes them: every text cell is an index."""

    def __init__(self):
        self.strings = []

    def s(self, ref, value, style=9):
        if value not in self.strings:
            self.strings.append(value)
        return f'<c r="{ref}" s="{style}" t="s"><v>{self.strings.index(value)}</v></c>'

    @staticmethod
    def n(ref, value):
        return f'<c r="{ref}" s="10" t="n"><v>{value}</v></c>'

    @staticmethod
    def empty(ref):
        return f'<c r="{ref}" s="9"/>'


def overview_xml(b, opening, closing, deposits, start="2019-01-01", end="2020-03-29", withdrawals="0.0",
                 invested=("0.0", "0.0")):
    rows = [(5, b.s("B5", "Account identifier:") + b.s("C5", "00000000-0000-4000-8000-000000000000")),
            (6, b.s("B6", d.FROM) + b.s("C6", start)), (7, b.s("B7", d.TO) + b.s("C7", end)),
            (9, b.s("A9", f"Balance on {start}:") + b.s("B9", "") + b.n("C9", opening)),
            (10, b.s("A10", d.DEPOSITS) + b.s("B10", "") + b.n("C10", deposits)),
            (11, b.s("A11", "Other balance increases during reporting period:") + b.n("C11", "0.0")),
            (13, b.s("A13", f"Opening Investments as at {start}") + b.s("B13", "") + b.n("C13", invested[0])),
            (14, b.s("A14", "") + b.s("B14", "New investments") + b.n("C14", "0.0")),
            (16, b.s("A16", f"Closing Investments as at {end}") + b.s("B16", "") + b.n("C16", invested[1])),
            (29, b.s("A29", "Other balance decreases during reporting period:") + b.n("C29", "0.0")),
            (30, b.s("A30", d.WITHDRAWALS) + b.n("C30", withdrawals)),
            (31, b.s("A31", f"Balance on {end}:") + b.s("B31", "") + b.n("C31", closing))]
    return sheet("".join(f'<row r="{r}">{cells}</row>' for r, cells in rows))


def detail_xml(b, rows, total=None, extra=""):
    out = ['<row r="1">' + "".join(b.s(f"{c}1", h, 12) for c, h in zip(d.COLUMNS, d.HEADER, strict=True)) + "</row>"]
    sums = {"E": 0, "G": 0, "H": 0}
    for i, (day, txid, kind, turnover, interest, *principal) in enumerate(rows, 2):
        parts = {"H": interest, "G": principal[0] if principal else None}
        cells = [b.s(f"A{i}", day), b.s(f"B{i}", txid), b.s(f"C{i}", "asset"), b.s(f"D{i}", kind),
                 b.n(f"E{i}", turnover)]
        cells += [b.n(f"{c}{i}", parts[c]) if parts.get(c) else b.empty(f"{c}{i}") for c in "FGHIJKLMNOPQRS"]
        cells.append(b.s(f"T{i}", "EUR"))
        out.append(f'<row r="{i}">{"".join(cells)}</row>')
        sums["E"] += float(turnover)
        sums["G"] += float(parts["G"] or 0)
        sums["H"] += float(interest or 0)
    t = len(rows) + 2
    total = total if total is not None else {c: f"{v:.2f}" for c, v in sums.items()}
    out.append(f'<row r="{t}">{b.s(f"A{t}", "TOTAL", 13)}'
               + "".join(b.n(f"{c}{t}", total.get(c, "0.0")) for c in d.SUMMED) + "</row>")
    return sheet("".join(out) + extra)


def sheet(rows):
    return f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{NS}"><sheetData>{rows}</sheetData></worksheet>'


def xlsx(rows=ROWS, opening="0.0", closing=None, deposits=None, names=d.SHEETS, detail=None, start="2019-01-01",
         end="2020-03-29", shared=True, edit_detail=None, edit_overview=None, withdrawals=None, invested=("0.0", "0.0"),
         **kw):
    """`edit_detail`/`edit_overview(xml, cells)` change a sheet with the same shared strings."""
    b = Cells()
    imported = [r for r in rows if d.KINDS.get(r[2], (None,))[0] is not None]
    closing = closing if closing is not None else f"{float(opening) + sum(float(r[3]) for r in imported):.2f}"
    deposits = deposits if deposits is not None else f"{sum(float(r[3]) for r in rows if r[2] == 'DEPOSIT'):.1f}"
    withdrawals = (withdrawals if withdrawals is not None
                   else f"{-sum(float(r[3]) for r in rows if r[2] == 'WITHDRAWAL'):.1f}")
    ov = overview_xml(b, opening, closing, deposits, start, end, withdrawals, invested)
    det = detail if detail is not None else detail_xml(b, rows, **kw)
    parts = {"xl/worksheets/sheet1.xml": edit_overview(ov, b) if edit_overview else ov,
             "xl/worksheets/sheet2.xml": edit_detail(det, b) if edit_detail else det,
             "xl/worksheets/sheet3.xml": sheet(b.s("A12", "Asset type"))}
    sst = "".join(f"<si><t>{escape(v)}</t></si>" for v in b.strings)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{NS}" xmlns:r="{RNS}">'
                   "<sheets>" + "".join(f'<sheet name="{escape(n)}" r:id="rId{i}" sheetId="{i}"/>'
                                        for i, n in enumerate(names, 3)) + "</sheets></workbook>")
        rels = "".join(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i - 2}.xml" Type="{REL_TYPE}worksheet"/>'
                       for i in range(3, 3 + len(names)))
        if shared:
            rels += f'<Relationship Id="rId1" Target="sharedStrings.xml" Type="{REL_TYPE}sharedStrings"/>'
            z.writestr("xl/sharedStrings.xml", f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{NS}">{sst}</sst>')
        z.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PNS}">'
                                                 f"{rels}</Relationships>")
        for name, xml in parts.items():
            z.writestr(name, xml)
    return buf.getvalue()


def kinds(records):
    return [(r["kind"], format(r["amount"], "f")) for r in records]


class Parse(unittest.TestCase):
    def test_parse_omits_internal_moves(self):
        records, head = d.parse(xlsx())
        self.assertEqual(kinds(records), [("DEPOSIT", "1000.0"), ("INTEREST", "2.5"), ("TAX", "0.5"),
                                          ("WITHDRAWAL", "2.0")])
        self.assertEqual(head["days"], ("2019-01-01", "2020-03-29"))
        self.assertEqual([r["date"] for r in records], ["2020-03-01", "2020-03-20", "2020-03-20", "2020-03-25"])

    def test_subscriptions_leave_the_balance_alone(self):
        rows = [("2020-03-29", TX[0], "DEPOSIT", "1000.0", None), ("2020-03-29", TX[1], "SUBSCRIPTION", "-500.0", None),
                ("2020-03-29", TX[2], "SUBSCRIPTION", "-500.0", None)]
        records, _ = d.parse(xlsx(rows=rows, closing="1000.0"))
        self.assertEqual(kinds(records), [("DEPOSIT", "1000.0")])

    def test_balance_guard_catches_misjudged_internal_move(self):
        # Overview says the balance fell by the subscription: then a subscription is not internal, and
        # nothing may be imported.
        with self.assertRaisesRegex(statement.StatementError, "Balance of the overview does not match"):
            d.parse(xlsx(closing="600.0"))
        with self.assertRaisesRegex(statement.StatementError, "Balance of the overview does not match"):
            d.parse(xlsx(opening="1.0", closing="600.0"))

    def test_deposits_guard(self):
        with self.assertRaisesRegex(statement.StatementError, "Deposits of the overview"):
            d.parse(xlsx(deposits="800.0"))

    def test_total_row_must_match_each_column(self):
        with self.assertRaisesRegex(statement.StatementError, "Total row does not match the column Total Turnover"):
            d.parse(xlsx(total={"E": "1.0", "H": "2.5"}))
        with self.assertRaisesRegex(statement.StatementError, "column Interest Repaid"):
            d.parse(xlsx(total={"E": "200.00", "H": "0.0"}))
        with self.assertRaisesRegex(statement.StatementError, r"\(total row\)"):
            d.parse(xlsx(edit_detail=lambda x, b: x.replace(b.s("A8", "TOTAL", 13), b.s("A8", "Total", 13))))

    def test_total_row_accepts_floating_point_noise(self):
        # Rows carry 8 and 9 decimals, TOTAL is binary floating point.
        rows = ROWS[:1] + [("2020-03-20", TX[3], "INTEREST_REPAYMENT", "0.25000001", "0.25000001")]
        records, _ = d.parse(xlsx(rows=rows, closing="1000.25", total={"E": "1000.25000001000004", "H": "0.25000001"}))
        self.assertEqual(kinds(records), [("DEPOSIT", "1000.0"), ("INTEREST", "0.25")])
        with self.assertRaisesRegex(statement.StatementError, "column Total Turnover"):
            d.parse(xlsx(rows=rows, closing="1000.25", total={"E": "1000.2500001", "H": "0.25000001"}))

    def test_wrong_sign_stops_the_import(self):
        rows = ROWS[:1] + [("2020-03-20", TX[3], "INTEREST_REPAYMENT", "-1.67", None)]
        with self.assertRaisesRegex(statement.StatementError, "Line 3: sign of Total Turnover does not match"):
            d.parse(xlsx(rows=rows, closing="997.5"))
        rows = ROWS[:1] + [("2020-03-20", TX[3], "SUBSCRIPTION", "800.0", None)]
        with self.assertRaisesRegex(statement.StatementError, "sign"):
            d.parse(xlsx(rows=rows))

    def test_unknown_types_stop_without_figures(self):
        for kind, message in (("LOYALTY_DEB", "not supported yet"), ("REVERT_INTEREST", "not supported yet"),
                              ("MYSTERY_42", "unknown")):
            rows = ROWS[:1] + [("2020-03-20", TX[3], kind, "1.0", None)]
            with self.assertRaisesRegex(statement.StatementError, f"Line 3: Transaction Type .* is {message}") as cm:
                d.parse(xlsx(rows=rows))
            self.assertNotRegex(re.sub(r"^Line [0-9]+: ", "", str(cm.exception)), "[0-9]")

    def test_earnings_are_truncated_to_the_cent(self):
        # `Balance on` is the free cash, an executed INVESTMENT lowers it and moves to
        # `Closing Investments`, a SUBSCRIPTION does not; interest and tax carry 8 and 9 decimals and the
        # balance takes them truncated to the cent, not rounded half up.
        rows = [("2020-03-29", TX[0], "DEPOSIT", "1000.0", None),
                ("2020-03-29", TX[1], "SUBSCRIPTION", "-500.0", None),
                ("2020-03-29", TX[2], "SUBSCRIPTION", "-500.0", None),
                ("2020-03-30", TX[3], "INVESTMENT", "-1000.0", None),
                ("2020-03-30", TX[4], "REPAYMENT", "2.50956789", "2.50956789"),
                ("2020-03-30", TX[5], "TAX", "-0.501913578", None)]
        total = {"E": "-997.992345688", "H": "2.50956789"}
        records, _ = d.parse(xlsx(rows=rows, closing="2.00", invested=("0.0", "1000.0"), end="2020-03-30", total=total))
        self.assertEqual(kinds(records), [("DEPOSIT", "1000.0"), ("INTEREST", "2.50"), ("TAX", "0.50")])
        for closing in ("2.0077", "2.01"):  # the untruncated sum, and rounding half up
            with self.assertRaisesRegex(statement.StatementError, "Balance of the overview does not match"):
                d.parse(xlsx(rows=rows, closing=closing, invested=("0.0", "1000.0"), end="2020-03-30", total=total))
        with self.assertRaisesRegex(statement.StatementError, "Balance of the overview does not match"):
            d.parse(xlsx(rows=rows, closing="2.00", invested=("0.0", "0.0"), end="2020-03-30", total=total))

    def test_investments_rows_must_be_there_once(self):
        def drop(x, b):
            return x.replace(b.s("A16", "Closing Investments as at 2020-03-29"), b.s("A16", "Closing"))

        with self.assertRaisesRegex(statement.StatementError, r"\(overview\)"):
            d.parse(xlsx(edit_overview=drop))

    def test_repayment_imports_only_its_earnings(self):
        # REPAYMENT rows may set only Interest Repaid, with Total Turnover equal to it.
        rows = [("2020-03-01", TX[0], "DEPOSIT", "1000.0", None), ("2020-03-01", TX[1], "SUBSCRIPTION", "-500.0", None),
                ("2020-03-20", TX[2], "REPAYMENT", "2.0", "2.0"),
                ("2020-03-21", TX[3], "REPAYMENT", "100.5", "0.5", "100.0")]
        records, _ = d.parse(xlsx(rows=rows, closing="1002.5"))
        self.assertEqual(kinds(records), [("DEPOSIT", "1000.0"), ("INTEREST", "2.0"), ("INTEREST", "0.5")])
        # the whole turnover taken for interest breaks the balance, so the split is what the guard checks
        with self.assertRaisesRegex(statement.StatementError, "Balance of the overview does not match"):
            d.parse(xlsx(rows=rows, closing="1102.5"))

    def test_principal_repayment_imports_nothing(self):
        rows = [("2020-03-01", TX[0], "DEPOSIT", "1000.0", None), ("2020-03-01", TX[1], "SUBSCRIPTION", "-500.0", None),
                ("2020-03-20", TX[2], "REPAYMENT", "100.0", None, "100.0")]
        records, _ = d.parse(xlsx(rows=rows, closing="1000.0"))
        self.assertEqual(kinds(records), [("DEPOSIT", "1000.0")])

    def test_exponents_are_read_exactly(self):
        rows = [("2020-03-01", TX[0], "DEPOSIT", "1.0E3", None),
                ("2020-03-20", TX[1], "INTEREST_REPAYMENT", "2.5E-1", "2.5E-1")]
        records, _ = d.parse(xlsx(rows=rows, closing="1000.25", deposits="1000.0"))
        self.assertEqual([r["credited"] for r in records], [Decimal(1000), Decimal("0.25")])

    def test_reference_keeps_the_exact_amount(self):
        rows = [("2020-03-01", TX[0], "DEPOSIT", "1000.0", None),
                ("2020-03-20", TX[1], "INTEREST_REPAYMENT", "2.509", "2.509")]
        records, _ = d.parse(xlsx(rows=rows, closing="1002.5", deposits="1000.0",
                                  total={"E": "1002.509", "H": "2.509"}))
        key = f"{TX[1]}|INTEREST_REPAYMENT|2.509".encode()
        self.assertEqual(records[1]["ref"],
                         f"wfsync:debitum:interest:2020-03-20:{hashlib.sha256(key).hexdigest()[:16]}")
        self.assertEqual(records[1]["amount"], Decimal("2.50"))

    def test_absurd_exponent_is_refused(self):
        rows = [("2020-03-01", TX[0], "DEPOSIT", "1E999", None)]
        with self.assertRaisesRegex(statement.StatementError, "Total Turnover is not a number"):
            d.parse(xlsx(rows=rows, closing="1000.0", deposits="1000.0", total={}))

    def test_repayment_parts_must_match_turnover(self):
        rows = ROWS[:1] + [("2020-03-20", TX[3], "REPAYMENT", "4.0", "2.0")]
        with self.assertRaisesRegex(statement.StatementError,
                                    "Line 3: parts of REPAYMENT do not add up to Total Turnover") as cm:
            d.parse(xlsx(rows=rows, closing="1002.0"))
        self.assertNotRegex(re.sub(r"^Line [0-9]+: ", "", str(cm.exception)), "[0-9]")
        rows = ROWS[:1] + [("2020-03-20", TX[3], "REPAYMENT", "-2.0", "-2.0")]
        with self.assertRaisesRegex(statement.StatementError, "Line 3: sign"):
            d.parse(xlsx(rows=rows, closing="998.0"))

    def test_catalogue_is_complete_and_disjoint(self):
        # The app's 51 types plus REPAYMENT, which it does not list: each one is mapped or stops.
        self.assertEqual(len(d.KINDS) + len(d.UNCHECKED), 52)
        self.assertFalse(set(d.KINDS) & d.UNCHECKED)

    def test_zero_rows_are_left_out(self):
        rows = ROWS[:1] + [("2020-03-20", TX[3], "INTEREST_REPAYMENT", "0.0", None)]
        self.assertEqual(kinds(d.parse(xlsx(rows=rows))[0]), [("DEPOSIT", "1000.0")])

    def test_rows_must_be_oldest_first(self):
        # newest first is unsupported and stops
        for rows in (list(reversed(ROWS)), [ROWS[3], ROWS[0], ROWS[5]]):
            with self.assertRaisesRegex(statement.StatementError, "rder is not ascending"):
                d.parse(xlsx(rows=rows))

    def test_withdrawals_guard_compares_amounts(self):
        d.parse(xlsx(withdrawals="-2.0"))  # either sign of the overview's figure
        with self.assertRaisesRegex(statement.StatementError, "Withdrawals of the overview"):
            d.parse(xlsx(withdrawals="0.0"))

    def test_period_end_before_start_is_refused(self):
        with self.assertRaisesRegex(statement.StatementError, "ends before it begins"):
            d.parse(xlsx(start="2020-03-30", end="2020-03-29"))

    def test_row_before_period_is_refused(self):
        with self.assertRaisesRegex(statement.StatementError, "Line 2: date outside the period"):
            d.parse(xlsx(start="2020-03-02", opening="0.0"))

    def test_one_day_statement_reads_both_balances(self):
        rows = [("2020-03-29", TX[0], "DEPOSIT", "1000.0", None)]
        records, head = d.parse(xlsx(rows=rows, start="2020-03-29", end="2020-03-29"))
        self.assertEqual((head["opening"], head["closing"], len(records)), (0, 1000, 1))

    def test_missing_cells_are_named(self):
        with self.assertRaisesRegex(statement.StatementError, "Line 3: Total Turnover is missing"):
            d.parse(xlsx(rows=ROWS[:1] + [("2020-03-02", TX[2], "INVESTMENT", "0", None)],
                         edit_detail=lambda x, b: x.replace(b.n("E3", "0"), b.empty("E3"))))
        for col, value in (("A", "2020-03-02"), ("B", TX[2]), ("D", "INVESTMENT")):
            with self.assertRaisesRegex(statement.StatementError,
                                        "Line 3: Date, Transaction ID or Transaction Type is not text"):
                d.parse(xlsx(rows=ROWS[:1] + [("2020-03-02", TX[2], "INVESTMENT", "-1.0", None)],
                             edit_detail=lambda x, b, col=col, value=value: x.replace(b.s(f"{col}3", value),
                                                                                      b.n(f"{col}3", "1"))))

    def test_statement_without_transactions_is_refused(self):
        with self.assertRaisesRegex(statement.StatementError, "contains no transactions"):
            d.parse(xlsx(rows=[("2020-03-02", TX[2], "INVESTMENT", "0.0", None)]))

    def test_extra_rows_and_columns_are_refused(self):
        extra = '<row r="20"><c r="A20" t="n"><v>1</v></c></row>'
        with self.assertRaisesRegex(statement.StatementError, r"\(total row\)"):
            d.parse(xlsx(edit_detail=lambda x, b: x.replace("</sheetData>", extra + "</sheetData>")))
        for col in ("U", "AB", "ST"):
            with self.assertRaisesRegex(statement.StatementError, r"\(columns\)"):
                d.parse(xlsx(edit_detail=lambda x, b, col=col: x.replace('<row r="2">',
                                                                         f'<row r="2">{b.n(col + "2", "9")}', 1)))

    def test_overview_label_twice_is_refused(self):
        with self.assertRaisesRegex(statement.StatementError, r"\(overview\)"):
            d.parse(xlsx(edit_overview=lambda x, b: x.replace("</sheetData>", f'<row r="40">{b.s("A40", d.DEPOSITS)}'
                                                               f'{b.n("C40", "1.0")}</row></sheetData>')))

    def test_row_currency_id_and_date_checked(self):
        with self.assertRaisesRegex(statement.StatementError, "Line 2: currency is not EUR"):
            d.parse(xlsx(edit_detail=lambda x, b: x.replace(b.s("T2", "EUR"), b.s("T2", "USD"))))
        rows = [("2020-03-01", "not-a-uuid", "DEPOSIT", "1000.0", None)]
        with self.assertRaisesRegex(statement.StatementError, "Line 2: Transaction ID"):
            d.parse(xlsx(rows=rows))
        with self.assertRaisesRegex(statement.StatementError, "date outside the period"):
            d.parse(xlsx(end="2020-03-24"))
        for day in ("01.03.2020", "2020-13-01"):
            with self.assertRaisesRegex(statement.StatementError, "Line 2: date"):
                d.parse(xlsx(rows=[(day, TX[0], "DEPOSIT", "1000.0", None)]))

    def test_same_transaction_twice_is_refused(self):
        with self.assertRaisesRegex(statement.StatementError, "same transaction twice"):
            d.parse(xlsx(rows=ROWS[:1] + ROWS[:1]))

    def test_reference_depends_on_id_and_type(self):
        a = d.parse(xlsx(rows=ROWS[:1]))[0][0]["ref"]
        b = d.parse(xlsx(rows=[ROWS[0][:3] + ("1000.00", None)], deposits="1000.0", closing="1000.0"))[0][0]["ref"]
        self.assertEqual(a, b)
        self.assertRegex(a, r"^wfsync:debitum:deposit:2020-03-01:[0-9a-f]{16}$")
        self.assertTrue(d.OWN_REF.fullmatch(a))
        other = d.parse(xlsx(rows=[(ROWS[0][0], TX[5]) + ROWS[0][2:]]))[0][0]["ref"]
        self.assertNotEqual(a, other)
        changed = d.parse(xlsx(rows=[ROWS[0][:3] + ("800.0", None)]))[0][0]["ref"]
        self.assertNotEqual(a, changed)
        # one transaction id under two types of the same Wealthfolio kind (interest and penalty, should
        # Debitum share ids) stays two transactions
        rows = [ROWS[0], ("2020-03-20", TX[3], "INTEREST_REPAYMENT", "2.0", "2.0"),
                ("2020-03-20", TX[3], "PENALTY_REPAYMENT", "0.5", None)]
        self.assertEqual(len(d.parse(xlsx(rows=rows))[0]), 3)

    def test_unrelated_files_are_refused_without_figures(self):
        bad = [b"Datum;Details\n1;2\n", xlsx(names=["Overview", "Detailed Balance"]),
               xlsx(names=["Overview", "Detailed Balance", "Outstanding Investments", "More"]),
               xlsx(shared=False),
               xlsx(detail=sheet('<row r="1"><c r="A1" t="inlineStr"><is><t>Date</t></is></c></row>'))]
        for raw in bad:
            with self.assertRaises(statement.StatementError) as cm:
                d.parse(raw)
            self.assertNotRegex(re.sub(r"^Line [0-9]+: ", "", str(cm.exception)), "[0-9]")

    def test_overview_labels_must_be_there_once(self):
        for edit in (lambda x, b: x.replace(b.s("A10", d.DEPOSITS), ""),
                     lambda x, b: x.replace(b.n("C31", "1000.0"), b.empty("C31")),
                     lambda x, b: x.replace(b.s("C6", "2019-01-01"), b.s("C6", "01.01.2019")),
                     lambda x, b: x.replace(b.s("A9", "Balance on 2019-01-01:"), b.s("A9", "Balance at start:"))):
            raw = xlsx(rows=ROWS[:1], closing="1000.0", edit_overview=edit)
            with self.assertRaisesRegex(statement.StatementError, r"\(overview\)|Line 6: date"):
                d.parse(raw)
        d.parse(xlsx(rows=ROWS[:1], closing="1000.0", edit_overview=lambda x, b: x))

    def test_same_cell_twice_is_refused(self):
        # a second A2 could otherwise silently replace the first
        with self.assertRaisesRegex(statement.StatementError, r"\(cell reference\)"):
            d.parse(xlsx(edit_detail=lambda x, b: x.replace('<row r="2">', '<row r="2">' + b.s("A2", "2020-03-02"), 1)))

    def test_bad_shared_string_index_is_named(self):
        b = Cells()
        detail = detail_xml(b, ROWS).replace('t="s"><v>0</v>', 't="s"><v>9999</v>', 1)
        with self.assertRaisesRegex(statement.StatementError, r"\(strings\)"):
            d.parse(xlsx(detail=detail))
        def edit(index):
            def go(x, b):
                assert 't="s"><v>0</v>' in x  # the edit must hit a cell
                return x.replace('t="s"><v>0</v>', f't="s"><v>{index}</v>', 1)
            return go
        d.parse(xlsx(edit_overview=edit("0")))
        for index in ("²", "-1", "1.0"):  # isdigit() but not int(), a sign, a decimal point
            with self.assertRaisesRegex(statement.StatementError, r"\(strings\)"):
                d.parse(xlsx(edit_overview=edit(index)))


class Anchor(unittest.TestCase):
    def data(self, **kw):
        return d.Statement(xlsx(**kw)).fetch()

    def complete(self, data, known):
        return d.Statement(b"").complete(data, known)

    def test_opening_statement_fills_empty_account(self):
        data = self.data()
        self.assertTrue(data["from_start"])
        self.complete(data, set())

    def test_later_statement_needs_first_row_imported(self):
        full = {r["ref"] for r in self.data()["records"]}
        part = self.data(rows=ROWS[3:], opening="1000.0", start="2020-03-20", deposits="0.0")
        self.assertFalse(part["from_start"])
        with self.assertRaisesRegex(statement.StatementError, "does not begin at the account opening: .*full period"):
            self.complete(part, set())
        self.complete(part, full)
        with self.assertRaisesRegex(statement.StatementError, "First transaction of the statement is not imported"):
            self.complete(part, {r for r in full if ":interest:" not in r})

    def test_imported_row_missing_is_refused(self):
        data = self.data()
        known = {r["ref"] for r in data["records"]}
        with self.assertRaisesRegex(statement.StatementError, "contradicts"):
            self.complete(self.data(rows=ROWS[:5]), known)

    def test_changed_imported_amount_is_a_contradiction(self):
        known = {r["ref"] for r in self.data()["records"]}
        rows = [ROWS[0][:3] + ("800.0", None)] + ROWS[1:]
        with self.assertRaisesRegex(statement.StatementError,
                                    "contradicts|First transaction of the statement is not imported"):
            self.complete(self.data(rows=rows), known)
        # a later withdrawal changed, the first row unchanged: the range check names the contradiction
        rows = ROWS[:5] + [ROWS[5][:3] + ("-4.0", None)]
        with self.assertRaisesRegex(statement.StatementError, "contradicts"):
            self.complete(self.data(rows=rows), known)

    def test_second_statement_adds_only_new_deposit(self):
        first = self.data(rows=ROWS[:2], closing="1000.0")
        known = {r["ref"] for r in first["records"]}
        rows = ROWS[:2] + [("2020-03-29", TX[5], "DEPOSIT", "500.0", None)]
        second = self.data(rows=rows)
        self.complete(second, known)
        self.assertEqual(len({r["ref"] for r in second["records"]} - known), 1)

    def test_unknown_reference_stops_the_import(self):
        with self.assertRaisesRegex(statement.StatementError, "Account Debitum contains an unknown wfsync transaction"):
            self.complete(self.data(), {"wfsync:goandgrow:deposit:2020-03-01:0000000000000000"})


class Map(unittest.TestCase):
    def test_rows_are_referenced_cash_activities(self):
        rows = ROWS + [("2020-03-26", "00000000-0000-4000-8000-000000000007", "CASHBACK", "10.0", None),
                       ("2020-03-27", "00000000-0000-4000-8000-000000000008", "PREMIUM_SERVICE_FEE", "-4.0", None)]
        data = d.Statement(xlsx(rows=rows)).fetch()
        out, skipped = d.map_activities(data, "A")
        self.assertEqual(skipped, [])
        self.assertEqual([(r["activityType"], r["amount"], r.get("subtype"), r.get("isExternal")) for r in out],
                         [("DEPOSIT", "1000", None, None), ("INTEREST", "2.5", None, None), ("TAX", "0.5", None, None),
                          ("WITHDRAWAL", "2", None, None), ("CREDIT", "10", "BONUS", False), ("FEE", "4", None, None)])
        self.assertTrue(all(r["symbol"] == "" and r["currency"] == "EUR" and r["accountId"] == "A" for r in out))
        self.assertEqual(statement.statement_range(data), "2019-01-01 to 2020-03-29")


if __name__ == "__main__":
    unittest.main()
