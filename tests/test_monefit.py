import unittest

from wfsync.sources import monefit as m
from wfsync.sources import statement

DE = "Datum;Details;Betrag;Gebühren;Gesamtes Vermögen;Währung"
EN = "Date;Details;Amount;Fees;Total Wealth;Currency"

# Newest first, 8 decimals, balance after each row.
ROWS_DE = [
    "03.03.2020;Ausgezahlte Renditen;0.25000001;0;925.25000001;EUR",
    "02.03.2020;Abhebungen;100.00000000;0;925.00000000;EUR",
    "01.03.2020;Boni;25.00000000;0;1025.00000000;EUR",
    "01.03.2020;Einzahlungen;1000.00000000;0;1000.00000000;EUR",
]
ROWS_EN = [r.replace("Ausgezahlte Renditen", "Earnings received").replace("Abhebungen", "Withdrawals")
           .replace("Boni", "Bonuses").replace("Einzahlungen", "Funds added") for r in ROWS_DE]


def csv(header, rows, end="\n"):
    return (end.join([header] + rows) + end).encode()


class Parse(unittest.TestCase):
    def test_oldest_first_with_iso_dates(self):
        recs = m.parse(csv(DE, ROWS_DE))
        self.assertEqual([(r["date"], r["kind"]) for r in recs],
                         [("2020-03-01", "DEPOSIT"), ("2020-03-01", "BONUS"), ("2020-03-02", "WITHDRAWAL"),
                          ("2020-03-03", "INTEREST")])

    def test_refs_ignore_the_export_language(self):
        self.assertEqual([r["ref"] for r in m.parse(csv(DE, ROWS_DE))], [r["ref"] for r in m.parse(csv(EN, ROWS_EN))])

    def test_bom_and_crlf_are_accepted(self):
        self.assertEqual(len(m.parse(b"\xef\xbb\xbf" + csv(DE, ROWS_DE, end="\r\n"))), 4)

    def test_withdrawal_may_come_signed(self):
        rows = list(ROWS_DE)
        rows[1] = rows[1].replace(";100.", ";-100.")
        self.assertEqual(m.parse(csv(DE, rows))[2]["amount"], m.parse(csv(DE, ROWS_DE))[2]["amount"])

    def test_broken_balance_chain_stops_the_import(self):
        rows = list(ROWS_DE)
        rows[2] = rows[2].replace("1025.00000000", "1025.00000001")
        with self.assertRaisesRegex(m.StatementError, "Total Wealth does not match"):
            m.parse(csv(DE, rows))

    def test_unsorted_rows_stop_the_import(self):
        # Chain intact, only the dates swapped: newest-first means a later line may not be newer.
        rows = list(ROWS_DE)
        rows[2] = rows[2].replace("01.03.2020", "02.03.2020")
        rows[1] = rows[1].replace("02.03.2020", "01.03.2020")
        with self.assertRaisesRegex(m.StatementError, "order is not descending"):
            m.parse(csv(DE, rows))

    def test_wrong_direction_breaks_the_chain(self):
        # A withdrawal that raised the balance is not a withdrawal: the type table would be wrong.
        rows = list(ROWS_DE)
        rows[1] = "02.03.2020;Abhebungen;100.00000000;0;1125.00000000;EUR"
        rows[0] = "03.03.2020;Ausgezahlte Renditen;0.25000001;0;1125.25000001;EUR"
        with self.assertRaisesRegex(m.StatementError, "Line 3: Total Wealth"):
            m.parse(csv(DE, rows))

    def test_vault_rows_and_fees_stop_import(self):
        vault = ["02.03.2020;Vault-Einzahlung;100.00000000;0;925.00000000;EUR"] + ROWS_DE[2:]
        with self.assertRaisesRegex(m.StatementError, "Vault-Einzahlung\" is not supported yet"):
            m.parse(csv(DE, vault))
        fee = ["01.03.2020;Einzahlungen;400.00000000;2.5;397.50000000;EUR"]
        with self.assertRaisesRegex(m.StatementError, "non-zero fee"):
            m.parse(csv(DE, fee))

    def test_unknown_input_reveals_no_figures(self):
        with self.assertRaisesRegex(m.StatementError, "\"Intereses\" is unknown"):
            m.parse(csv(DE, ["01.03.2020;Intereses;1.00000000;0;1.00000000;EUR"]))
        for raw in (b"a,b,c\n1,2,3\n", csv(DE, []), "Datum;x".encode("utf-16")):
            with self.assertRaises(m.StatementError) as cm:
                m.parse(raw)
            self.assertFalse(any(ch.isdigit() for ch in str(cm.exception)), cm.exception)
        for amount in ("1,5", "1e2", "1_00", "+100", "NaN"):
            with self.assertRaisesRegex(m.StatementError, "Line 2: Amount is not a number"):
                m.parse(csv(DE, [f"01.03.2020;Einzahlungen;{amount};0;100.00000000;EUR"]))

    def test_zero_fee_spelling_keeps_the_reference(self):
        refs = {m.parse(csv(DE, [f"01.03.2020;Einzahlungen;1.00000000;{fee};1.00000000;EUR"]))[0]["ref"]
                for fee in ("0", "-0", "0.00000000")}
        self.assertEqual(len(refs), 1)
        with self.assertRaisesRegex(m.StatementError, "currency"):
            m.parse(csv(DE, ["01.03.2020;Einzahlungen;1.00000000;0;1.00000000;USD"]))
        for day in ("2020-03-01", "31.02.2020", "+1.03.2020"):
            with self.assertRaisesRegex(m.StatementError, "date"):
                m.parse(csv(DE, [f"{day};Einzahlungen;1.00000000;0;1.00000000;EUR"]))

    def test_unknown_label_is_shown_without_digits(self):
        with self.assertRaises(m.StatementError) as cm:
            m.parse(csv(DE, ["01.03.2020;Bonus 25 EUR;1.00000000;0;1.00000000;EUR"]))
        self.assertIn("\"Bonus ## EUR\"", str(cm.exception))
        with self.assertRaises(m.StatementError) as cm:
            m.parse(csv(DE, ["01.03.2020;Zins \uff11\u0663\u00b2\u2460;1.00000000;0;1.00000000;EUR"]))
        self.assertIn("\"Zins ####\"", str(cm.exception))

    def test_references_stay_those_already_imported(self):
        # A changed reference formula would import every existing transaction a second time.
        rows = ["01.03.2020;Boni;25.00000000;0;1025.00000000;EUR",
                "01.03.2020;Einzahlungen;1000.00000000;0;1000.00000000;EUR"]
        self.assertEqual([r["ref"] for r in m.parse(csv(DE, rows))],
                         ["wfsync:monefit:deposit:2020-03-01:b62ed4a6ca90d144",
                          "wfsync:monefit:bonus:2020-03-01:697de150fb8c1175"])

    def test_equal_bonuses_stay_distinct(self):
        rows = ["01.03.2020;Boni;20.00000000;0;60.00000000;EUR", "01.03.2020;Boni;20.00000000;0;40.00000000;EUR",
                "01.03.2020;Einzahlungen;20.00000000;0;20.00000000;EUR"]
        self.assertEqual(len({r["ref"] for r in m.parse(csv(DE, rows))}), 3)


class Anchor(unittest.TestCase):
    def data(self, rows):
        return m.Statement(csv(DE, rows)).fetch()

    def complete(self, data, known):
        return m.Statement(b"").complete(data, known)

    def test_all_time_statement_fills_empty_account(self):
        data = self.data(ROWS_DE)
        self.assertTrue(data["from_start"])
        self.complete(data, set())

    def test_partial_statement_needs_first_row_imported(self):
        full = self.data(ROWS_DE)
        part = self.data(ROWS_DE[:2])  # starts with the withdrawal, opening balance 1025
        self.assertFalse(part["from_start"])
        with self.assertRaisesRegex(m.StatementError, "First transaction of the statement is not imported"):
            self.complete(part, {r["ref"] for r in full["records"][:2]})
        self.complete(part, {r["ref"] for r in full["records"][:3]})

    def test_partial_statement_refused_on_empty_account(self):
        with self.assertRaisesRegex(m.StatementError, "does not begin at the account opening"):
            self.complete(self.data(ROWS_DE[:2]), set())

    def test_partial_statement_with_changed_row_refused(self):
        full = self.data(ROWS_DE)
        known = {r["ref"] for r in full["records"]}
        changed = ["03.03.2020;Ausgezahlte Renditen;0.35000001;0;925.35000001;EUR", ROWS_DE[1], ROWS_DE[2]]
        with self.assertRaisesRegex(m.StatementError, "contradicts"):
            self.complete(self.data(changed), known)
        self.complete(self.data(ROWS_DE[1:]), known)  # same rows, shorter period: fine

    def test_older_statement_is_accepted(self):
        known = {r["ref"] for r in self.data(ROWS_DE)["records"]}
        self.complete(self.data(ROWS_DE[2:]), known)  # ends on 01.03., later rows outside

    def test_unknown_reference_stops_every_statement(self):
        known = {r["ref"] for r in self.data(ROWS_DE)["records"]}
        for odd in ("wfsync:a:b:zzzz:c", "wfsync:t212:order:1", "wfsync:monefit:bonus:2020-03-01:xyz"):
            for rows in (ROWS_DE, ROWS_DE[1:]):
                with self.assertRaisesRegex(m.StatementError, "unknown wfsync transaction"):
                    self.complete(self.data(rows), known | {odd})

    def test_missing_imported_row_is_refused(self):
        data = self.data(ROWS_DE)
        known = {r["ref"] for r in data["records"]}
        self.complete(data, known)
        with self.assertRaisesRegex(m.StatementError, "contradicts"):
            self.complete(data, known | {"wfsync:monefit:bonus:2020-02-29:0000000000000000"})


class Map(unittest.TestCase):
    def test_bonus_is_gain_earnings_are_income(self):
        rows, skipped = m.map_activities(m.Statement(csv(DE, ROWS_DE)).fetch(), "A")
        self.assertEqual(skipped, [])
        self.assertEqual([(r["activityType"], r.get("subtype"), r.get("isExternal"), r["amount"]) for r in rows],
                         [("DEPOSIT", None, None, "1000"), ("CREDIT", "BONUS", False, "25"),
                          ("WITHDRAWAL", None, None, "100"), ("INTEREST", None, None, "0.25000001")])
        self.assertTrue(all(r["symbol"] == "" and r["currency"] == "EUR" and r["accountId"] == "A" for r in rows))
        self.assertTrue(all(r["comment"].startswith("wfsync:monefit:") for r in rows))

    def test_statement_range_has_dates_only(self):
        self.assertEqual(statement.statement_range(m.Statement(csv(DE, ROWS_DE)).fetch()), "2020-03-01 to 2020-03-03")
