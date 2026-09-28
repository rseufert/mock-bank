"""NACHA files, read into the same payments as a pain.001 (#52).

Everything goes through `POST /_mock/validate`, the one door that takes a
NACHA file so far. Two kinds of ground truth: a pain.001 written by hand as
the twin of the clean sample, which the NACHA reading must equal payment for
payment, and files from moov-io/ach, written by somebody else, which pin the
field positions against a reader that is not this one.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from support import MockServerCase  # noqa: E402

from mockbank import nacha  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
EXTERNAL = os.path.join(SAMPLES, "external")


def sample(*parts):
    with open(os.path.join(SAMPLES, *parts), "rb") as handle:
        return handle.read()


class NachaCase(MockServerCase):
    config_kwargs = {"clock": "2026-09-30T09:00"}

    def reading(self, body):
        resp = self.post("/_mock/validate", body=body,
                         headers={"Accept": "application/json"})
        return resp.status, resp.json()


class TheDeclaration(unittest.TestCase):

    def test_every_record_type_is_declared_across_all_94_columns(self):
        self.assertEqual(sorted(nacha.RECORDS), ["1", "5", "6", "7", "8", "9"])
        for kind, (label, fields) in nacha.RECORDS.items():
            with self.subTest(label):
                columns = [c for f in fields for c in range(f.start, f.start + f.width)]
                self.assertEqual(columns, list(range(1, 95)))

    def test_the_check_digit_is_the_aba_one(self):
        # Published routing numbers, each with its own check digit last.
        for routing in ("021000021", "121000248", "011000138", "111000025"):
            self.assertEqual(nacha.check_digit(routing[:8]), int(routing[8]), routing)


class TheCleanFile(NachaCase):

    def test_it_reads_into_the_same_payments_as_its_pain001_twin(self):
        status, ach = self.reading(sample("nacha_four_payments.ach"))
        self.assertEqual((status, ach["findings"]), (200, []))
        _, twin = self.reading(sample("pain001_four_payments_usd.xml"))
        self.assertEqual(twin["findings"], [])
        self.assertEqual(ach["file"]["message"], "NACHA")
        for key in ("msg_id", "creation_time", "initiating_party", "batches"):
            self.assertEqual(ach["file"][key], twin["file"][key], key)

    def test_the_end_to_end_id_is_the_individual_identification_number(self):
        _, ach = self.reading(sample("nacha_four_payments.ach"))
        payments = ach["file"]["batches"][0]["payments"]
        self.assertEqual([p["end_to_end_id"] for p in payments],
                         ["INV-2026-001", "INV-2026-002", "INV-2026-003", "INV-2026-000004"])
        # The last one fills its fields to the last column (17, 15 and 22), so
        # a boundary off by one would show here rather than hide in padding.
        self.assertEqual((payments[3]["creditor_account"], payments[3]["creditor_name"]),
                         ("77009988110000123", "Hooli Savings Accounts"))
        # the trace number is the ODFI's, and is the InstrId instead
        self.assertEqual(payments[0]["instruction_id"], "091000010000001")
        self.assertEqual(payments[2]["remittance"], ["Invoice 2026-003, April services"])

    def test_the_prose_says_which_format_it_saw(self):
        resp = self.post("/_mock/validate", body=sample("nacha_four_payments.ach"))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body.decode().splitlines()[0],
                         "NACHA 1234567890-2610010900A: 1 batch, 4 payments, 0 findings")


class FilesWrittenElsewhere(NachaCase):
    """moov-io/ach's test files; tests/samples/external/SOURCES.md says where from."""

    def errors(self, reading):
        return [f for f in reading["findings"] if f["level"] == "error"]

    def test_a_credit_file_reads_clean_with_its_fields_where_moov_put_them(self):
        status, reading = self.reading(sample("external", "nacha-loan-credit.ach"))
        self.assertEqual((status, self.errors(reading)), (200, []))
        # Dated 2019, so a past effective date: a warning, as for a pain.001.
        self.assertEqual([(f["level"], f["code"]) for f in reading["findings"]],
                         [("warning", "DT01")])
        batch = reading["file"]["batches"][0]
        self.assertEqual((batch["debtor_name"], batch["requested_execution_date"]),
                         ("Name on Account", "2019-06-25"))
        payment = batch["payments"][0]
        self.assertEqual(payment["amount"], 100000000)          # $1,000,000.00
        self.assertEqual(payment["creditor_account"], "12345678")
        self.assertEqual(payment["creditor_name"], "Receiver Account Name")
        self.assertEqual(payment["instruction_id"], "121042880000001")
        # moov left the individual identification number blank
        self.assertEqual(payment["end_to_end_id"], "NOTPROVIDED")
        # the receiving bank's routing number, check digit included
        self.assertEqual(payment["creditor_clearing_id"], "231380104")

    def test_a_debit_in_a_mixed_file_is_the_one_finding(self):
        status, reading = self.reading(sample("external", "nacha-ppd-mixedDebitCredit.ach"))
        self.assertEqual(status, 422)
        self.assertEqual([(f["code"], f["path"]) for f in self.errors(reading)],
                         [("FF01", "/line 3 (entry detail)/transaction code (columns 2-3)")])
        self.assertEqual(len(reading["file"]["batches"][0]["payments"]), 2)


class WhichSideACodeCountsOn(unittest.TestCase):
    """`side` used to put a blank or one-character code on the credit side.

    `code[1:2] in "1234"` is a substring test, and `"" in "1234"` is True, so a
    code with no second digit came back a credit. It landed on the side the
    mock's own writer computes the same way, which is the side where a wrong
    answer cannot be caught by the two disagreeing (#102).
    """

    def test_a_blank_code_is_not_a_credit(self):
        self.assertEqual(nacha.side(""), "debit")

    def test_a_one_character_code_is_not_a_credit(self):
        # It has no second digit at all: `"2"[1:2]` is `""`.
        self.assertEqual(nacha.side("2"), "debit")

    def test_the_real_codes_still_land_where_they_did(self):
        for code, expected in (("22", "credit"), ("32", "credit"),
                               ("42", "credit"), ("52", "credit"),
                               ("21", "credit"), ("31", "credit"),
                               ("27", "debit"), ("37", "debit"),
                               ("26", "debit"), ("36", "debit"),
                               ("46", "debit"), ("56", "debit")):
            with self.subTest(code=code):
                self.assertEqual(nacha.side(code), expected)

    def test_the_credit_digits_are_a_tuple_not_a_string(self):
        # The fix is the type: `in` on a string is a substring test, so any
        # future edit back to a string reintroduces the bug for a blank.
        self.assertIsInstance(nacha.CREDIT_DIGITS, tuple)


class EveryReturnedDebitIsRead(unittest.TestCase):

    def test_the_four_returned_debits_are_all_there(self):
        # 26, 36, 46 and 56 are the automated returns of debits to checking,
        # savings, the general ledger and a loan, as 21, 31, 41 and 51 are of
        # the credits. Three of the four were there, which was an asymmetry
        # rather than a decision (#102).
        for code in ("26", "36", "46", "56"):
            with self.subTest(code=code):
                self.assertIn(code, nacha.RETURNS)

    def test_every_credit_has_its_return_and_every_return_a_side(self):
        for credit, returned in nacha.RETURN_OF.items():
            with self.subTest(credit=credit):
                self.assertIn(returned, nacha.RETURNS)
                self.assertEqual(nacha.side(credit), "credit")
        for code in nacha.RETURNS:
            with self.subTest(code=code):
                self.assertIn(nacha.side(code), ("credit", "debit"))


class BrokenFiles(NachaCase):
    # sample -> (code, path) of the one finding its name promises
    PROMISED = {
        "nacha_broken_line_length.ach": ("FF01", "/line 1 (file header)"),
        "nacha_broken_check_digit.ach":
            ("RC01", "/line 4 (entry detail)/check digit (columns 12-12)"),
        "nacha_broken_entry_hash.ach":
            ("FF01", "/line 8 (batch control)/entry hash (columns 11-20)"),
        "nacha_broken_batch_total.ach":
            ("AM10", "/line 8 (batch control)/total credit entry dollar amount (columns 33-44)"),
        "nacha_broken_file_total.ach":
            ("AM10", "/line 9 (file control)/total credit entry dollar amount (columns 44-55)"),
        # The debit pair. Before #102 the debit totals were never compared, so
        # deleting either check left every NACHA test green; these are the
        # samples that make that a failure. The clean file is all credits, so
        # its debit totals are zero and claiming one cent of debits is a total
        # no entry accounts for - which needs no debit entry and no outside
        # file. A first attempt derived them from moov-io/ach's return-WEB.ach,
        # which has a real returned debit and is dated 2017, so each sample
        # carried a second finding about a past effective date and could not
        # promise exactly one.
        "nacha_broken_batch_debit_total.ach":
            ("AM10", "/line 8 (batch control)/total debit entry dollar amount (columns 21-32)"),
        "nacha_broken_file_debit_total.ach":
            ("AM10", "/line 9 (file control)/total debit entry dollar amount (columns 32-43)"),
        "nacha_broken_block_count.ach":
            ("FF01", "/line 9 (file control)/block count (columns 8-13)"),
    }

    def test_every_broken_sample_is_here(self):
        found = {n for n in os.listdir(SAMPLES) if n.startswith("nacha_broken_")}
        self.assertEqual(found, set(self.PROMISED))

    def test_each_produces_exactly_the_finding_its_name_promises(self):
        for name, promised in sorted(self.PROMISED.items()):
            with self.subTest(name):
                status, reading = self.reading(sample(name))
                self.assertEqual(status, 422)
                self.assertEqual([(f["code"], f["path"]) for f in reading["findings"]],
                                 [promised])

    def test_a_wrong_entry_hash_names_the_field_and_both_numbers(self):
        _, reading = self.reading(sample("nacha_broken_entry_hash.ach"))
        text = reading["findings"][0]["text"]
        self.assertIn("265100431", text)
        self.assertIn("26400041", text)


class AsAPain001Would(NachaCase):
    """The checks a pain.001 gets that NACHA has no rule of its own for (#71)."""

    def clean(self):
        return sample("nacha_four_payments.ach").decode("ascii")

    def test_a_past_effective_date_is_a_dt01_warning(self):
        status, reading = self.reading(self.clean().replace(
            "SUPPLIERS       261001", "SUPPLIERS       260901"))
        self.assertEqual(status, 200)
        self.assertEqual([(f["level"], f["code"], f["path"]) for f in reading["findings"]],
                         [("warning", "DT01",
                           "/line 2 (batch header)/effective entry date (columns 70-75)")])

    def test_a_repeated_identification_number_is_am05(self):
        status, reading = self.reading(self.clean().replace("INV-2026-002  ", "INV-2026-001  "))
        self.assertEqual(status, 422)
        self.assertEqual([(f["code"], f["path"]) for f in reading["findings"]],
                         [("AM05", "/line 4 (entry detail)/individual identification "
                                   "number (columns 40-54)")])

    def test_a_blank_file_creation_time_is_allowed(self):
        text = self.clean()
        header = text.splitlines()[0]
        blanked = header[:29] + "    " + header[33:]
        status, reading = self.reading(text.replace(header, blanked))
        self.assertEqual((status, reading["findings"]), (200, []))

    def test_both_readers_keep_the_creditors_routing_number(self):
        _, ach = self.reading(sample("nacha_four_payments.ach"))
        _, twin = self.reading(sample("pain001_four_payments_usd.xml"))
        routing = [p["creditor_clearing_id"] for p in ach["file"]["batches"][0]["payments"]]
        self.assertEqual(routing, ["021000021", "121000248", "011000138", "111000025"])
        self.assertEqual(routing, [p["creditor_clearing_id"]
                                   for p in twin["file"]["batches"][0]["payments"]])
