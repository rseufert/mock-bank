"""payment_run's hand-written readers, held to the mock's own writers (#57).

`examples/payment_run.py` imports neither mock, so it reads a `camt.053`, a
BAI2 statement and a NACHA return file by hand. Its own tests run it against a
live mock-bank, which can only show that the reader agrees with whichever format
that build sends. Here the two statement readers are given the same statement
written both ways by `mockbank`, and must come back with the same numbers: a
BAI2 reader that got a sign or a scale wrong would disagree with the camt one.
The return file is moov-io/ach's, from outside the project.
"""
import datetime
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "examples"))

import payment_run                                                   # noqa: E402

from mockbank import bai2, messages                                  # noqa: E402

# The rows test_bai2 hands both writers, rather than rows typed again here.
from test_bai2 import ACCOUNT, AT, DAY, THREE                       # noqa: E402

UTC = datetime.timezone.utc


def both(opening, closing, payments, day=DAY, number=7):
    """The same statement read back from its camt.053 and from its BAI2 file."""
    camt = messages.write_camt053(ACCOUNT, day, number, opening, closing, payments,
                                  "MB-C053-ACME-%d" % number, AT, UTC).decode("utf-8")
    text = bai2.write_statement(ACCOUNT, day, number, opening, closing, payments,
                                created_at=AT)
    [from_camt] = payment_run.camt_statements(camt)
    [from_bai2] = payment_run.bai2_statements(text)
    return from_camt, from_bai2, text


def movements(statement):
    return [(Decimal(e["amount"]), e["side"], e["end_to_end_id"]) for e in statement["lines"]]


class TheTwoStatementReadersAgree(unittest.TestCase):

    def test_the_same_statement_reads_the_same_either_way(self):
        opening = 10875000
        closing = opening - 125000 - 1500000 + 125000
        from_camt, from_bai2, _ = both(opening, closing, THREE)
        for key in ("number", "day", "opening", "closing"):
            self.assertEqual(from_bai2[key], from_camt[key], key)
        self.assertEqual(movements(from_bai2), movements(from_camt))
        # Worked out by hand, not only against the other reader.
        self.assertEqual((from_bai2["day"], from_bai2["number"]), ("2026-10-05", "7"))
        self.assertEqual((from_bai2["opening"], from_bai2["closing"]),
                         (Decimal("108750.00"), Decimal("93750.00")))
        self.assertEqual(movements(from_bai2),
                         [(Decimal("1250.00"), "DBIT", "INV-2026-0101"),
                          (Decimal("15000.00"), "DBIT", "INV-2026-0104"),
                          (Decimal("1250.00"), "CRDT", "INV-2026-0101")])

    def test_an_overdrawn_balance_keeps_its_sign(self):
        from_camt, from_bai2, _ = both(100000, -25000, THREE[:1])
        self.assertEqual((from_bai2["opening"], from_bai2["closing"]),
                         (Decimal("1000.00"), Decimal("-250.00")))
        self.assertEqual(from_bai2["closing"], from_camt["closing"])

    def test_the_account_is_named_as_payment_run_knows_it(self):
        _, from_bai2, _ = both(0, 0, [])
        run = payment_run.PaymentRun("http://sap", "http://bank",
                                     {"iban": ACCOUNT["iban"], "company_id": "0000000001",
                                      "name": "ACME", "bic": "MOCKNL2A",
                                      "routing": "999999992"}, "nacha")
        self.assertIn(from_bai2["account"], run.own_account())
        self.assertEqual(from_bai2["lines"], [])

    def test_a_record_continued_on_an_88_is_read_as_one(self):
        _, _, text = both(1000000, 875000, THREE[:1])
        detail = next(l for l in text.splitlines() if l.startswith("16,"))
        cut = detail.index(",INV-2026-0101")
        continued = text.replace(detail, detail[:cut] + "\n88" + detail[cut:])
        [statement] = payment_run.bai2_statements(continued)
        self.assertEqual(movements(statement),
                         [(Decimal("1250.00"), "DBIT", "INV-2026-0101")])

    def test_a_funds_type_with_availability_fields_is_refused_not_misread(self):
        _, _, text = both(1000000, 875000, THREE[:1])
        self.assertIn(",125000,Z,", text)
        with self.assertRaises(ValueError):
            payment_run.bai2_statements(text.replace(",125000,Z,", ",125000,S,"))

    def test_several_files_in_one_mailbox_answer_are_several_statements(self):
        _, _, first = both(1000000, 875000, THREE[:1])
        second = bai2.write_statement(ACCOUNT, DAY + datetime.timedelta(days=1), 8,
                                      875000, 875000, [], created_at=AT)
        found = payment_run.bai2_statements(first.strip() + "\n" + second)
        self.assertEqual([(s["number"], s["day"]) for s in found],
                         [("7", "2026-10-05"), ("8", "2026-10-06")])


class TheReturnFileReader(unittest.TestCase):

    def test_each_return_gives_its_r_code_by_identification_number(self):
        # moov-io/ach's return-WEB.ach: a debit returned R01 and a credit R03.
        with open(os.path.join(HERE, "samples", "external", "nacha-return-WEB.ach"),
                  encoding="utf-8") as handle:
            reasons = payment_run.nacha_return_reasons(handle.read())
        self.assertEqual(reasons, {"MjMxNDAwMjAtOGQ": "R01", "NmRjZTJmMzItMGN": "R03"})


if __name__ == "__main__":
    unittest.main()
