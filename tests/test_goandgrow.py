import io
import re
import unittest
import zipfile
from decimal import Decimal
from unittest import mock
from xml.sax.saxutils import escape

from wfsync.sources import goandgrow as g
from wfsync.sources import statement
from wfsync.sources import xlsx as xlsx_reader

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# Oldest first, 10 decimals, balance after each row.
ROWS = [
    ("01.03.2020", "SEPA-Banküberweisung", "10.0000000000", None, "10.0000000000"),
    ("01.03.2020", "SEPA-Banküberweisung", "1000.0000000000", None, "1010.0000000000"),
    ("02.03.2020", "Go & Grow Zinsen", "0.0500000001", None, "1010.0500000001"),
    ("03.03.2020", "SEPA-Banküberweisung", None, "200.0000000000", "810.0500000001"),
]


def s(ref, text):
    return f'<x:c r="{ref}" t="inlineStr"><x:is><x:t>{escape(text)}</x:t></x:is></x:c>'


def n(ref, value):
    return f'<x:c r="{ref}" t="n"><x:v>{value}</x:v></x:c>'


def sheet_xml(rows, opening="0", closing=None, period="01.03.2000-27.03.2020", goal="Alle Ziele", extra=""):
    closing = closing if closing is not None else (rows[-1][4] if rows else opening)
    out = [f'<x:row r="1">{s("A1", "Max Muster")}{s("D1", "Zusammenfassung")}</x:row>',
           f'<x:row r="2">{s("A2", period)}{s("D2", "Zu Beginn")}{n("E2", opening)}</x:row>',
           f'<x:row r="3">{s("A3", "")}{s("D3", "Einzahlungen")}{n("E3", "1")}</x:row>',
           f'<x:row r="4">{s("D4", "Auszahlungen")}{n("E4", "0")}</x:row>',
           f'<x:row r="5">{s("D5", "Ergebnis")}{n("E5", closing)}</x:row>',
           f'<x:row r="7">{s("A7", goal)}</x:row>',
           '<x:row r="8">' + "".join(s(f"{c}8", h) for c, h in zip("ABCDE", g.HEADER, strict=True)) + "</x:row>"]
    for i, (day, label, inc, outg, bal) in enumerate(rows, 9):
        cells = [s(f"A{i}", day), s(f"B{i}", label),
                 n(f"C{i}", inc) if inc is not None else s(f"C{i}", ""),
                 n(f"D{i}", outg) if outg is not None else s(f"D{i}", ""), n(f"E{i}", bal)]
        out.append(f'<x:row r="{i}">{"".join(cells)}</x:row>')
    return (f'<?xml version="1.0" encoding="utf-8"?><x:worksheet xmlns:x="{NS}"><x:sheetData>'
            + "".join(out) + extra + "</x:sheetData></x:worksheet>")


def xlsx(rows=ROWS, sheet=None, sheets='<x:sheet name="Data" sheetId="1" r:id="R1" />',
         target="/xl/worksheets/sheet1.xml",
         **kw):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="utf-8"?><x:workbook xmlns:x="{NS}" '
                                      f'xmlns:r="{RNS}"><x:sheets>{sheets}</x:sheets></x:workbook>')
        z.writestr("xl/worksheets/sheet1.xml", sheet if sheet is not None else sheet_xml(rows, **kw))
        z.writestr("_rels/.rels", "<Relationships/>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="utf-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Type="http://schemas.openxmlformats.org/officeDocument/'
                   f'2006/relationships/worksheet" Target="{target}" Id="R1" /></Relationships>')
        z.writestr("[Content_Types].xml", "<Types/>")
    return buf.getvalue()


def parse(**kw):
    return g.parse(xlsx(**kw))


class Parse(unittest.TestCase):
    def test_parse_gives_kinds_dates_and_changes(self):
        records, opening, days = parse()
        self.assertEqual([(r["date"], r["kind"], r["change"]) for r in records],
                         [("2020-03-01", "DEPOSIT", Decimal(10)), ("2020-03-01", "DEPOSIT", Decimal(1000)),
                          ("2020-03-02", "INTEREST", Decimal("0.0500000001")),
                          ("2020-03-03", "WITHDRAWAL", Decimal(-200))])
        self.assertEqual((opening, days), (Decimal(0), ("2000-03-01", "2020-03-27")))
        self.assertTrue(all(r["ref"].startswith("wfsync:goandgrow:") and g.OWN_REF.fullmatch(r["ref"])
                            for r in records))

    def test_relative_sheet_target_is_accepted(self):
        self.assertEqual(len(parse(target="worksheets/sheet1.xml")[0]), 4)

    def test_broken_chain_or_closing_stops_import(self):
        rows = list(ROWS)
        rows[1] = rows[1][:4] + ("1010.0000000001",)
        with self.assertRaisesRegex(statement.StatementError, "Line 10: Guthaben does not match"):
            parse(rows=rows)
        with self.assertRaisesRegex(statement.StatementError, "Line 9: Guthaben does not match"):
            parse(opening="1")  # the first row is checked against the opening balance too
        with self.assertRaisesRegex(statement.StatementError, "Ergebnis of the summary"):
            parse(closing="810.0500000002")

    def test_misread_column_breaks_the_chain(self):
        # An outgoing row read as incoming (or the other way) cannot match the balance after it.
        rows = list(ROWS)
        rows[3] = ("03.03.2020", "SEPA-Banküberweisung", "200.0000000000", None, "810.0500000001")
        with self.assertRaisesRegex(statement.StatementError, "Line 12: Guthaben does not match"):
            parse(rows=rows)

    def test_unknown_description_stops_without_figures(self):
        for row in (("02.03.2020", "Bonus 25 EUR", "1.0000000000", None, "1011.0000000000"),
                    ("02.03.2020", "Go & Grow Zinsen", None, "1.0000000000", "1009.0000000000")):
            with self.assertRaises(statement.StatementError) as cm:
                parse(rows=ROWS[:2] + [row])
            self.assertRegex(str(cm.exception), "Line 11: Zahlungsart .* is unknown")
        with self.assertRaises(statement.StatementError) as cm:
            parse(rows=ROWS[:2] + [("02.03.2020", "Bonus 25 EUR", "1.0000000000", None, "1011.0000000000")])
        self.assertIn("\"Bonus ## EUR\"", str(cm.exception))

    def test_wrong_goal_period_or_order_refused(self):
        with self.assertRaisesRegex(statement.StatementError, "goal \"Alle Ziele\" when downloading"):
            parse(goal="Notgroschen")
        with self.assertRaisesRegex(statement.StatementError, "date outside the period"):
            parse(period="02.03.2020-27.03.2020", opening="0")
        with self.assertRaisesRegex(statement.StatementError, "ends before it begins"):
            parse(period="27.03.2020-01.03.2020")
        rows = [ROWS[0], ("29.02.2020",) + ROWS[1][1:]]
        with self.assertRaisesRegex(statement.StatementError, "rder is not ascending"):
            parse(rows=rows)

    def test_row_needs_one_side_and_balance(self):
        for row in (("02.03.2020", "SEPA-Banküberweisung", "1", "1", "1010.0000000000"),
                    ("02.03.2020", "SEPA-Banküberweisung", None, None, "1010.0000000000")):
            with self.assertRaisesRegex(statement.StatementError, "expected exactly one of Eingänge and Ausgänge"):
                parse(rows=ROWS[:2] + [row])
        with self.assertRaisesRegex(statement.StatementError, "negative amount"):
            parse(rows=ROWS[:2] + [("02.03.2020", "SEPA-Banküberweisung", "-1", None, "1009.0000000000")])
        for value in ("1E-05", "1,5", "NaN"):
            with self.assertRaisesRegex(statement.StatementError, "Line 11: Eingänge is not a number"):
                parse(rows=ROWS[:2] + [("02.03.2020", "SEPA-Banküberweisung", value, None, "1010.0000000000")])
        # a number cell without a value counts as empty
        with self.assertRaisesRegex(statement.StatementError, "Line 11: expected exactly one of Eingänge and Ausgänge"):
            parse(rows=ROWS[:2] + [("02.03.2020", "SEPA-Banküberweisung", "", None, "1010.0000000000")])

    def test_zero_and_empty_rows_are_skipped(self):
        rows = ROWS[:2] + [("02.03.2020", "Go & Grow Zinsen", "0", None, "1010.0000000000")]
        self.assertEqual(len(parse(rows=rows)[0]), 2)
        empty = '<x:row r="20"><x:c r="A20" t="inlineStr"><x:is><x:t></x:t></x:is></x:c></x:row>'
        self.assertEqual(len(parse(extra=empty)[0]), 4)
        with self.assertRaisesRegex(statement.StatementError, "no transactions"):
            parse(rows=[])

    def test_equal_deposits_stay_distinct(self):
        rows = [("01.03.2020", "SEPA-Banküberweisung", "20", None, "20"),
                ("01.03.2020", "SEPA-Banküberweisung", "20", None, "40")]
        self.assertEqual(len({r["ref"] for r in parse(rows=rows)[0]}), 2)

    def test_same_transaction_twice_is_refused(self):
        # In, out and in again on one day: the third row repeats the first in every field the reference uses.
        rows = [("01.03.2020", "SEPA-Banküberweisung", "20", None, "20"),
                ("01.03.2020", "SEPA-Banküberweisung", None, "20", "0"),
                ("01.03.2020", "SEPA-Banküberweisung", "20", None, "20")]
        with self.assertRaisesRegex(statement.StatementError, "same transaction twice"):
            parse(rows=rows)

    def test_reference_ignores_number_spelling(self):
        a = parse(rows=[("01.03.2020", "SEPA-Banküberweisung", "20", None, "20")])[0][0]["ref"]
        b = parse(rows=[("01.03.2020", "SEPA-Banküberweisung", "20.0000000000", None, "20.0000000000")])[0][0]["ref"]
        self.assertEqual(a, b)

    def test_unrelated_files_are_refused_without_figures(self):
        bad = [b"Datum;Details\n1;2\n", xlsx(sheets='<x:sheet name="Data" sheetId="1" r:id="R1" />'
                                                 '<x:sheet name="Mehr" sheetId="2" r:id="R2" />'),
               xlsx(sheets='<x:sheet name="Andere" sheetId="1" r:id="R1" />'), xlsx(target="/xl/worksheets/sheet9.xml"),
               xlsx(sheet="<x:worksheet"), xlsx(sheet=sheet_xml(ROWS).replace("Guthaben", "Saldo")),
               xlsx(sheet=sheet_xml(ROWS).replace('t="n"', 't="s"', 1)),
               xlsx(sheet=sheet_xml(ROWS).replace("</x:row>", f'{s("F9", "x")}</x:row>', 9)),
               xlsx(sheet=sheet_xml(ROWS).replace('r="A9"', 'r="9A"'))]
        for raw in bad:
            with self.assertRaises(statement.StatementError) as cm:
                g.parse(raw)
            self.assertNotRegex(re.sub(r"^Line [0-9]+: ", "", str(cm.exception)), "[0-9]")

    def test_value_beyond_column_e_is_refused(self):
        for col in ("F", "AB", "CD"):
            with self.assertRaisesRegex(statement.StatementError, r"\(columns\)"):
                g.parse(xlsx(extra=f'<x:row r="30">{s(col + "30", "x")}</x:row>'))

    def test_shared_strings_are_not_read(self):
        # Go & Grow files carry inline strings only; the shared reader must not change that
        with self.assertRaisesRegex(statement.StatementError, "Line 2: cell format \"s\" is not supported yet"):
            g.parse(xlsx(sheet=sheet_xml(ROWS).replace('t="n"', 't="s"', 1)))

    def test_entity_declarations_are_refused_unparsed(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                f'<x:worksheet xmlns:x="{NS}"><x:sheetData><x:row r="1">{s("A1", "&b;")}</x:row></x:sheetData>'
                '</x:worksheet>')
        with self.assertRaisesRegex(statement.StatementError, r"\(content\)"):
            g.parse(xlsx(sheet=bomb))
        with self.assertRaisesRegex(statement.StatementError, r"\(XML\)"):  # not XML at all
            g.parse(xlsx(sheet=bomb.replace("DOCTYPE", "doctype")))
        with self.assertRaisesRegex(statement.StatementError, r"\(content\)"):
            g.parse(xlsx(sheet=bomb.replace(' [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]', "")
                         .replace("&b;", "x")))

    def test_utf16_entity_declarations_are_refused(self):
        bomb = ('<?xml version="1.0" encoding="utf-16"?><!DOCTYPE x [<!ENTITY a "SEPA-Banküberweisung">]>'
                + sheet_xml(ROWS).split("?>", 1)[1].replace("SEPA-Banküberweisung", "&a;")).encode("utf-16")
        with self.assertRaisesRegex(statement.StatementError, r"\(content\)"):
            g.parse(xlsx(sheet=bomb))
        plain = ('<?xml version="1.0" encoding="utf-16"?>' + sheet_xml(ROWS).split("?>", 1)[1]).encode("utf-16")
        self.assertEqual(len(g.parse(xlsx(sheet=plain))[0]), 4)

    def test_broken_or_encrypted_archive_is_named(self):
        raw = bytearray(xlsx())
        i = raw.index(b"xl/worksheets/sheet1.xml") + len(b"xl/worksheets/sheet1.xml")
        raw[i + 2:i + 12] = b"\xff" * 10  # inside the deflate stream of the sheet
        with self.assertRaisesRegex(statement.StatementError, r"\(archive damaged\)|\(XML\)"):
            g.parse(bytes(raw))
        enc = bytearray(xlsx())
        for sig in (b"PK\x03\x04", b"PK\x01\x02"):  # set the encryption flag in every header
            off = 6 if sig == b"PK\x03\x04" else 8
            start = 0
            while (start := enc.find(sig, start)) >= 0:
                enc[start + off] |= 1
                start += 4
        with self.assertRaisesRegex(statement.StatementError, r"\(archive damaged\)"):
            g.parse(bytes(enc))

    def test_unsupported_encoding_or_zip_is_named(self):
        with self.assertRaisesRegex(statement.StatementError, r"\(XML\)"):
            g.parse(xlsx(sheet='<?xml version="1.0" encoding="utf-7"?><x:worksheet/>'))
        raw = bytearray(xlsx())
        end = raw.rindex(b"PK\x05\x06")  # end of central directory: point it at a newer zip version
        cd = int.from_bytes(raw[end + 16:end + 20], "little")
        raw[cd + 6] = 76
        with self.assertRaises(statement.StatementError):
            g.parse(bytes(raw))

    def test_missing_summary_parts_are_named(self):
        for sheet in (sheet_xml(ROWS).replace(n("E2", "0"), s("E2", "")),
                      sheet_xml(ROWS).replace("01.03.2000-27.03.2020", "Zeitraum"),
                      sheet_xml(ROWS).replace(">Ergebnis<", ">Ende<"),
                      sheet_xml(ROWS).replace(">Zu Beginn<", ">Start<")):
            with self.assertRaisesRegex(statement.StatementError, r"\(summary\)"):
                g.parse(xlsx(sheet=sheet))

    def test_row_after_period_is_refused(self):
        with self.assertRaisesRegex(statement.StatementError, "Line 12: date outside the period"):
            parse(period="01.03.2000-02.03.2020")

    def test_numeric_date_or_description_is_named(self):
        sheet = sheet_xml(ROWS).replace(s("B10", "SEPA-Banküberweisung"), n("B10", "1"))
        with self.assertRaisesRegex(statement.StatementError, "Line 10: date or Zahlungsart is not text"):
            g.parse(xlsx(sheet=sheet))

    def test_missing_sheet_relationship_is_named(self):
        with self.assertRaisesRegex(statement.StatementError, r"\(sheets\)"):
            g.parse(xlsx(sheets='<x:sheet name="Data" sheetId="1" r:id="R7" />'))

    def test_oversized_part_is_refused_unread(self):
        with mock.patch.object(xlsx_reader, "MAX_PART", 100), \
             self.assertRaisesRegex(statement.StatementError, r"\(too large\)"):
            parse()


class Anchor(unittest.TestCase):
    def data(self, **kw):
        return g.Statement(xlsx(**kw)).fetch()

    def complete(self, data, known):
        return g.Statement(b"").complete(data, known)

    def test_opening_statement_fills_empty_account(self):
        data = self.data()
        self.assertTrue(data["from_start"])
        self.complete(data, set())

    def test_later_statement_needs_first_row_imported(self):
        full = {r["ref"] for r in self.data()["records"]}
        part = self.data(rows=ROWS[2:], opening="1010.0000000000", period="02.03.2020-27.03.2020")
        self.assertFalse(part["from_start"])
        with self.assertRaisesRegex(statement.StatementError,
                                    "does not begin at the account opening: .* before the account opening"):
            self.complete(part, set())
        self.complete(part, full)
        with self.assertRaisesRegex(statement.StatementError, "First transaction of the statement is not imported"):
            self.complete(part, {r for r in full if ":interest:" not in r})

    def test_imported_row_missing_in_period_refused(self):
        data = self.data()
        known = {r["ref"] for r in data["records"]}
        with self.assertRaisesRegex(statement.StatementError, "contradicts"):
            self.complete(data, known | {"wfsync:goandgrow:interest:2020-03-26:0000000000000000"})
        # outside the period of a later statement: fine
        part = self.data(rows=ROWS[2:], opening="1010.0000000000", period="02.03.2020-03.03.2020")
        self.complete(part, known | {"wfsync:goandgrow:interest:2020-03-26:0000000000000000"})

    def test_mistaken_opening_statement_can_be_repaired(self):
        # Everything withdrawn on 03.03., exported from 04.03.: the balance before is zero, so it passes as
        # starting at the opening while the earlier history is missing. A full statement must fill it in.
        rows = ROWS + [("05.03.2020", "SEPA-Banküberweisung", "20", None, "830.0500000001")]
        rows[3] = ("03.03.2020", "SEPA-Banküberweisung", None, "1010.0500000001", "0")
        rows[4] = ("05.03.2020", "SEPA-Banküberweisung", "20", None, "20")
        late = self.data(rows=rows[4:], opening="0", period="04.03.2020-27.03.2020")
        self.assertTrue(late["from_start"])
        self.complete(late, set())
        full = self.data(rows=rows)
        self.complete(full, {r["ref"] for r in late["records"]})
        # without that one transaction in it, the full statement still contradicts
        with self.assertRaisesRegex(statement.StatementError, "contradicts|not imported"):
            self.complete(self.data(rows=rows[:4]), {r["ref"] for r in late["records"]})

    def test_monefit_reference_stops_the_import(self):
        with self.assertRaisesRegex(statement.StatementError,
                                    "Account Go & Grow contains an unknown wfsync transaction"):
            self.complete(self.data(), {"wfsync:monefit:deposit:2020-03-01:0000000000000000"})


class Map(unittest.TestCase):
    def test_rows_are_referenced_cash_activities(self):
        data = g.Statement(xlsx()).fetch()
        rows, skipped = g.map_activities(data, "A")
        self.assertEqual(skipped, [])
        self.assertEqual([(r["activityType"], r["amount"]) for r in rows],
                         [("DEPOSIT", "10"), ("DEPOSIT", "1000"), ("INTEREST", "0.0500000001"), ("WITHDRAWAL", "200")])
        self.assertTrue(all(r["symbol"] == "" and r["currency"] == "EUR" and r["accountId"] == "A" for r in rows))
        self.assertEqual(statement.statement_range(data), "2000-03-01 to 2020-03-27")


if __name__ == "__main__":
    unittest.main()
