"""BAI2: the same statement as the camt.053, in the format US systems read.

The test that matters is the one the issue asks for: give both writers the
**same** statement data and check the two files agree about the opening
balance, the entries and the closing balance. Not "does the BAI2 look
plausible" - does it say what the `camt.053` says.

Both writers are pure, so none of this needs a server. The balances are also
never checked against the file that reports them: the numbers come from the
arguments the test passed in, worked out here.
"""
import datetime
import unittest
import xml.etree.ElementTree as ET

# test_statements brings in support, which puts the checkout on sys.path
from test_statements import read_statement

from mockbank import bai2, messages                   # noqa: E402

UTC = datetime.timezone.utc
DAY = datetime.date(2026, 10, 5)
AT = datetime.datetime(2026, 10, 6, 0, 0, tzinfo=UTC)

ACCOUNT = {"id": "ACME", "iban": "NL41MOCK0000000001", "currency": "EUR",
           "name": "ACME Corporation"}


def payment(identifier, reference, amount, name="Globex Supplies B.V.",
            credit=False):
    """A payment row of the shape both writers are handed."""
    return {
        "id": identifier, "amount": amount, "currency": "EUR",
        "msg_id": "ACME-20261001-0001", "end_to_end_id": reference,
        "pmt_inf_id": "ACME-20261001-0001-B1",
        "instruction_id": "ACME-0001-%d" % identifier,
        "creditor_name": name, "creditor_iban": "NL14MOCK0000000002",
        "creditor_bic": "MOCKNL2A",
        "credit": credit, "return_reason": "AC04" if credit else "",
    }


THREE = [payment(1, "INV-2026-0101", 125000),
         payment(4, "INV-2026-0104", 1500000, name="Umbrella Logistics S.A."),
         payment(1, "INV-2026-0101", 125000, credit=True)]


def both(opening, closing, payments, account=None):
    """The same statement, written twice. Returns (camt.053 parsed, BAI2 parsed)."""
    account = account or ACCOUNT
    xml = messages.write_camt053(account, DAY, 7, opening, closing, payments,
                                 "MB-C053-ACME-7", AT, UTC)
    text = bai2.write_statement(account, DAY, 7, opening, closing, payments,
                                 created_at=AT)
    return read_statement(ET.fromstring(xml)), bai2.statements(text)[0], text


class TheDeclaration(unittest.TestCase):

    def test_every_record_declares_its_own_code_first(self):
        for code, (name, fields) in bai2.RECORDS.items():
            with self.subTest(record=code):
                self.assertEqual(fields[0].name, "record code", name)

    def test_the_six_records_a_statement_needs_are_declared(self):
        for code in ("01", "02", "03", "16", "49", "98", "99"):
            self.assertIn(code, bai2.RECORDS)

    def test_a_record_with_the_wrong_number_of_fields_is_refused_by_name(self):
        # The writer is built from the declaration, so a caller that forgets a
        # field is a bug here rather than a short line somebody else's parser
        # rejects days later.
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2._record("49", 100)              # a 49 takes a total and a count
        self.assertIn("account trailer", str(refused.exception))
        self.assertIn("49", str(refused.exception))

    def test_an_undeclared_record_code_is_refused(self):
        with self.assertRaises(bai2.Unreadable):
            bai2._record("88", 1, 2)


class OneStatementWrittenTwice(unittest.TestCase):
    """The issue's first "Done when": the two renderings agree."""

    def test_the_opening_and_closing_balances_are_the_same(self):
        camt, ba, _ = both(10875000, 12500000, THREE)
        self.assertEqual((camt["opening"], camt["closing"]), (10875000, 12500000))
        self.assertEqual((ba.opening, ba.closing), (camt["opening"], camt["closing"]))

    def test_the_entries_are_the_same_references_and_amounts_in_order(self):
        camt, ba, _ = both(10875000, 12500000, THREE)
        self.assertEqual([(e.reference, e.amount) for e in ba.entries],
                         camt["entries"])
        self.assertEqual(len(ba.entries), 3)

    def test_the_account_is_named_by_the_same_iban(self):
        camt, ba, _ = both(1000, 875, THREE[:1])
        self.assertEqual(ba.account, camt["iban"])

    def test_an_overdrawn_closing_balance_survives_both(self):
        # camt.053 carries the sign as CdtDbtInd; BAI2 writes a leading minus.
        camt, ba, text = both(1000, -50000, THREE[:1])
        self.assertEqual(camt["closing"], -50000)
        self.assertEqual(ba.closing, -50000)
        self.assertIn("015,-50000", text)

    def test_a_statement_with_no_entries_agrees_too(self):
        camt, ba, _ = both(500, 500, [])
        self.assertEqual((ba.opening, ba.closing), (500, 500))
        self.assertEqual(ba.entries, [])
        self.assertEqual(camt["entries"], [])

    def test_a_gap_is_a_gap_in_both(self):
        # Under statement-gap one payment is left off, and the balances then do
        # not reconcile. Neither writer is allowed to notice: the point of the
        # behaviour is a statement that does not add up.
        shown = THREE[:2]
        opening, closing = 10875000, 10875000 - 125000      # short by INV-0104
        camt, ba, _ = both(opening, closing, shown)
        self.assertEqual(len(ba.entries), 2)
        self.assertEqual([(e.reference, e.amount) for e in ba.entries],
                         camt["entries"])
        self.assertNotEqual(ba.opening - sum(e.amount for e in ba.entries),
                            ba.closing)


class TheTypeCodes(unittest.TestCase):

    def codes(self, text):
        return [e.type_code for e in bai2.statements(text)[0].entries]

    def test_a_debit_and_a_return_get_the_codes_the_module_names(self):
        # Asserted against the constants, not against literals: both are
        # placeholders until #57 settles them, and a test hard-coding them would
        # have to be edited in lockstep for no gain. What is worth pinning is
        # that a debit and a return differ and that the return is the credit.
        _, _, text = both(10875000, 12500000, THREE)
        self.assertEqual(self.codes(text),
                         [bai2.DEBIT, bai2.DEBIT, bai2.RETURNED_CREDIT])
        self.assertNotEqual(bai2.DEBIT, bai2.RETURNED_CREDIT)
        self.assertEqual(set(bai2.PLACEHOLDER_CODES),
                         {bai2.DEBIT, bai2.RETURNED_CREDIT, bai2.RECEIVED_CREDIT})

    def test_money_arriving_is_a_received_credit_not_a_return(self):
        # The row `credits.booked_on` gives the statement, as the outbox marks it.
        arriving = {"id": 3, "amount": 125000, "currency": "EUR", "credit": True,
                    "incoming": True, "end_to_end_id": "CUST-77",
                    "reference": "RF18539007547034", "debtor_name": "Customer, Ltd",
                    "value_date": "2026-10-01", "note": ["INV-1001"]}
        text = bai2.write_statement(ACCOUNT, DAY, 7, 0, 125000, [arriving],
                                    created_at=AT)
        self.assertEqual(self.codes(text), [bai2.RECEIVED_CREDIT])
        self.assertEqual(len({bai2.DEBIT, bai2.RETURNED_CREDIT, bai2.RECEIVED_CREDIT}), 3)
        [detail] = [l for l in text.splitlines() if l.startswith("16,")]
        self.assertEqual(detail.split(",")[3:],
                         ["Z", "CUST-77", "RF18539007547034", "Customer  Ltd/"])
        self.assertEqual(bai2.trailers_agree(text), [])

    def test_a_credit_that_is_not_a_return_is_refused_rather_than_mislabelled(self):
        # #96 adds money arriving, whose rows are credits too. Coding a
        # customer's payment as a return would be a wrong statement rather than
        # a cosmetic slip, so an unexplained credit stops here.
        arriving = payment(9, "CUSTOMER-1", 50000, credit=True)
        arriving["return_reason"] = ""
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2.write_statement(ACCOUNT, DAY, 7, 0, 50000, [arriving],
                                 created_at=AT)
        self.assertIn("CUSTOMER-1", str(refused.exception))
        self.assertIn("came back", str(refused.exception))
        self.assertIn("not money arriving", str(refused.exception))

    def test_the_account_record_carries_the_two_balances_and_no_movement_totals(self):
        # Movement summaries on the 03 are legal BAI2 and were in a first draft.
        # They double-count: the control total sums every amount field, so each
        # payment would appear in its summary and again in its own 16.
        _, _, text = both(10875000, 12500000, THREE)
        account_record = [l for l in text.splitlines() if l.startswith("03,")][0]
        self.assertIn("010,10875000", account_record)
        self.assertIn("015,12500000", account_record)
        # Against the constants: an earlier version asserted "455" was absent,
        # which was the first draft's debit code, so after it changed to 495 the
        # assertion could no longer fail for the reason it was written for.
        self.assertNotIn(bai2.DEBIT, account_record)
        self.assertNotIn(bai2.RETURNED_CREDIT, account_record)

    def test_the_end_to_end_id_is_the_bank_reference(self):
        # The field a treasury system reconciles on, which is the whole point of
        # writing this format at all.
        _, _, text = both(1000, 875, THREE[:1])
        detail = [l for l in text.splitlines() if l.startswith("16,")][0]
        self.assertEqual(detail.rstrip("/").split(",")[4], "INV-2026-0101")


class TheTrailers(unittest.TestCase):
    """The issue's second "Done when": recomputed from the records they cover."""

    def file(self):
        return both(10875000, 12500000, THREE)[2]

    def test_every_trailer_agrees_with_its_contents(self):
        self.assertEqual(bai2.trailers_agree(self.file()), [])

    def test_the_account_control_total_is_the_balances_plus_the_entries(self):
        # Worked out here rather than read back: 010 + 015 + every 16 amount.
        text = self.file()
        want = 10875000 + 12500000 + 125000 + 1500000 + 125000
        stated = int([l for l in text.splitlines()
                      if l.startswith("49,")][0].rstrip("/").split(",")[1])
        self.assertEqual(stated, want)

    def test_a_negative_balance_subtracts_from_the_control_total(self):
        # CONTROL_TOTALS_ARE_SIGNED, stated in the module and asserted here so
        # that changing the reading breaks a test rather than a bank's parser.
        self.assertTrue(bai2.CONTROL_TOTALS_ARE_SIGNED)
        text = both(1000, -500, [payment(1, "E1", 200)])[2]
        stated = int([l for l in text.splitlines()
                      if l.startswith("49,")][0].rstrip("/").split(",")[1])
        self.assertEqual(stated, 1000 - 500 + 200)

    def test_the_counts_include_the_trailer_that_carries_them(self):
        self.assertTrue(bai2.COUNTS_INCLUDE_THE_TRAILER)
        text = self.file()
        lines = text.splitlines()
        # 03 + three 16s + the 49 itself.
        self.assertEqual(int(lines[-3].rstrip("/").split(",")[2]), 5)
        # 02 + those five + the 98 itself.
        self.assertEqual(int(lines[-2].rstrip("/").split(",")[3]), 7)
        # 01 + those seven + the 99 itself.
        self.assertEqual(int(lines[-1].rstrip("/").split(",")[3]), 9)
        self.assertEqual(len(lines), 9)

    def test_a_wrong_total_or_count_in_any_trailer_is_caught_by_name(self):
        text = self.file()
        for original, broken, expect in (
                ("49,25125000,5/", "49,999999,5/", "account control total"),
                ("49,25125000,5/", "49,25125000,9/", "number of records"),
                ("98,25125000,1,7/", "98,1,1,7/", "group control total"),
                ("99,25125000,1,9/", "99,25125000,1,99/", "number of records")):
            with self.subTest(expect=expect, broken=broken):
                self.assertIn(original, text, "the file's shape moved")
                problems = bai2.trailers_agree(text.replace(original, broken))
                self.assertTrue(problems, "not caught")
                self.assertIn(expect, problems[0])


class FieldsThatCouldEndARecordEarly(unittest.TestCase):
    """BAI2 has no escape, so a comma in a name would end the field."""

    def test_a_comma_in_a_creditor_name_does_not_shift_the_fields(self):
        rows = [payment(1, "INV-1", 1000, name="Umbrella, Logistics, S.A.")]
        _, ba, text = both(2000, 1000, rows)
        self.assertEqual([(e.reference, e.amount) for e in ba.entries],
                         [("INV-1", 1000)])
        self.assertNotIn(",,", text.splitlines()[3])
        self.assertEqual(ba.entries[0].text, "Umbrella  Logistics  S.A.")

    def test_a_slash_in_a_name_does_not_end_the_record(self):
        rows = [payment(1, "INV-1", 1000, name="A/S Nordisk")]
        _, ba, text = both(2000, 1000, rows)
        # 01, 02, 03, one 16, 49, 98, 99: a terminator inside a field must not
        # split a line into two records.
        self.assertEqual(len(bai2.read(text)), 7)
        self.assertEqual(ba.entries[0].text, "A S Nordisk")

    def test_a_reference_with_a_comma_still_reconciles(self):
        _, ba, _ = both(2000, 1000, [payment(1, "INV,1", 1000)])
        self.assertEqual(ba.entries[0].reference, "INV 1")


class FreeTextInEveryRecord(unittest.TestCase):
    """Not only the 16. The account's name is free text on the control plane.

    `_safe` used to be applied at three call sites, all in the 16, so the 01 and
    the 02 were open: an account named `ACME, Inc.` wrote a nine-field 02 with
    every field after the name shifted, and `A/S Nordisk` ended the record at
    `02,A/`. Neither showed up as a failing test, because `read` did not count a
    record's fields against its declaration and accepted the nine-field 02.
    """

    def with_name(self, name):
        account = dict(ACCOUNT, name=name)
        return bai2.write_statement(account, DAY, 7, 2000, 1000, THREE[:1],
                                    created_at=AT)

    def group_header(self, text):
        return [l for l in text.splitlines() if l.startswith("02,")][0]

    def test_a_comma_in_the_account_name_does_not_shift_the_group_header(self):
        text = self.with_name("ACME, Inc.")
        self.assertEqual(self.group_header(text),
                         "02,ACME  Inc.,ACME,1,261005,0000,EUR,/")
        self.assertEqual(len(bai2.read(text)), 7)

    def test_a_slash_in_the_account_name_does_not_end_the_group_header(self):
        text = self.with_name("A/S Nordisk")
        self.assertIn("A S Nordisk", self.group_header(text))
        self.assertEqual(len(bai2.read(text)), 7)

    def test_a_line_break_in_the_account_name_does_not_split_the_record(self):
        # The trailers count the record once however many lines it occupies, so
        # a split record is a wrong file rather than merely an unreadable one.
        for breaker in ("\n", "\r", "\r\n", "\u2028", "\u2029", "\u0085"):
            with self.subTest(breaker=repr(breaker)):
                text = self.with_name("ACME" + breaker + "Inc.")
                self.assertEqual(len(text.splitlines()), 7)
                self.assertEqual(len(bai2.read(text)), 7)
                self.assertEqual(bai2.trailers_agree(text), [])

    def test_the_file_header_is_made_safe_too(self):
        text = bai2.write_statement(ACCOUNT, DAY, 7, 2000, 1000, THREE[:1],
                                    created_at=AT, sender="MOCK, BANK/1")
        header = [l for l in text.splitlines() if l.startswith("01,")][0]
        self.assertEqual(header, "01,MOCK  BANK 1,ACME,261006,0000,7,,,2/")
        self.assertEqual(len(bai2.read(text)), 7)

    def test_none_is_written_as_nothing_rather_than_as_the_word(self):
        row = dict(THREE[0], creditor_name=None, end_to_end_id=None)
        text = bai2.write_statement(ACCOUNT, DAY, 7, 2000, 1000, [row],
                                    created_at=AT)
        detail = [l for l in text.splitlines() if l.startswith("16,")][0]
        self.assertNotIn("None", detail)
        self.assertEqual(bai2.statements(text)[0].entries[0].reference, "")


class TheReaderCountsFields(unittest.TestCase):
    """Without this, a file whose fields had all shifted read back clean."""

    def clean(self):
        return both(2000, 1000, THREE[:1])[2]

    def test_a_record_with_a_field_too_many_is_refused_by_line_and_name(self):
        text = self.clean().replace("02,ACME Corporation,",
                                    "02,ACME,Corporation,")
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2.read(text)
        self.assertIn("line 2", str(refused.exception))
        self.assertIn("group header", str(refused.exception))

    def test_a_record_with_a_field_too_few_is_refused(self):
        text = self.clean().replace("49,", "49x,").replace("49x,", "49,", 0)
        lines = self.clean().splitlines()
        trailer = [i for i, l in enumerate(lines) if l.startswith("49,")][0]
        lines[trailer] = "49,100/"                     # a total and no count
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2.read("\n".join(lines) + "\n")
        self.assertIn("account trailer", str(refused.exception))

    def test_the_summary_must_repeat_in_fours(self):
        lines = self.clean().splitlines()
        account = [i for i, l in enumerate(lines) if l.startswith("03,")][0]
        lines[account] = lines[account].rstrip("/")[:-2] + "/"      # drop one field
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2.read("\n".join(lines) + "\n")
        self.assertIn("fours", str(refused.exception))

    def test_a_clean_file_still_reads(self):
        self.assertEqual(len(bai2.read(self.clean())), 7)


class OnlyABalanceMayBeNegative(unittest.TestCase):

    def test_a_negative_movement_is_refused_rather_than_written(self):
        # It would contradict its own type code and lower the control total.
        row = dict(THREE[0], amount=-1000)
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2.write_statement(ACCOUNT, DAY, 7, 2000, 1000, [row],
                                 created_at=AT)
        self.assertIn("direction", str(refused.exception))

    def test_a_negative_balance_is_still_written(self):
        text = both(1000, -500, THREE[:1])[2]
        self.assertIn("015,-500", text)


class TheFileHeaderDate(unittest.TestCase):

    def test_it_is_the_creation_date_not_the_statement_day(self):
        # The time beside it was already the creation time, so the two halves of
        # one timestamp disagreed: the statement day with the creation clock.
        text = bai2.write_statement(ACCOUNT, DAY, 7, 2000, 1000, THREE[:1],
                                    created_at=AT)
        header = [l for l in text.splitlines() if l.startswith("01,")][0]
        self.assertEqual(header.split(",")[3], "261006")        # AT, not DAY
        self.assertNotEqual(header.split(",")[3], "261005")


class ReadingBackWhatIsNotBai2(unittest.TestCase):

    def test_a_line_without_the_terminator_is_refused_by_line_number(self):
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2.read("01,MOCKBANK,ACME,261005,0000,7,,,2/\n02,x,y,1\n")
        self.assertIn("line 2", str(refused.exception))

    def test_an_unknown_record_code_is_refused_by_line_number(self):
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2.read("77,nothing/\n")
        self.assertIn("77", str(refused.exception))
        self.assertIn("line 1", str(refused.exception))

    def test_blank_lines_are_ignored_rather_than_refused(self):
        text = both(1000, 875, THREE[:1])[2]
        self.assertEqual(len(bai2.read(text + "\n\n")), len(bai2.read(text)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
