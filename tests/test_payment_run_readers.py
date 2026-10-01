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

    def test_a_funds_type_with_availability_fields_is_read_past(self):
        # #130: `V`, `S` and `D` carry fields of their own before the bank
        # reference, so the reference moves. Refused until then.
        _, _, text = both(1000000, 875000, THREE[:1])
        self.assertIn(",125000,Z,", text)
        for funds in ("V,261005,1200", "S,100,200,300", "D,2,1,100,2,200", "0"):
            with self.subTest(funds=funds):
                [statement] = payment_run.bai2_statements(
                    text.replace(",125000,Z,", ",125000,%s," % funds))
                self.assertEqual(movements(statement),
                                 [(Decimal("1250.00"), "DBIT", "INV-2026-0101")])

    def test_a_funds_type_bai2_does_not_define_is_refused(self):
        _, _, text = both(1000000, 875000, THREE[:1])
        with self.assertRaises(ValueError) as refused:
            payment_run.bai2_statements(text.replace(",125000,Z,", ",125000,X,"))
        self.assertIn("'X'", str(refused.exception))

    def test_several_files_in_one_mailbox_answer_are_several_statements(self):
        _, _, first = both(1000000, 875000, THREE[:1])
        second = bai2.write_statement(ACCOUNT, DAY + datetime.timedelta(days=1), 8,
                                      875000, 875000, [], created_at=AT)
        found = payment_run.bai2_statements(first.strip() + "\n" + second)
        self.assertEqual([(s["number"], s["day"]) for s in found],
                         [("7", "2026-10-05"), ("8", "2026-10-06")])


EXTERNAL = os.path.join(HERE, "samples", "external")
SAMPLES = ["bai2-sample%d.txt" % n for n in range(1, 6)]


def sample(name):
    with open(os.path.join(EXTERNAL, name), encoding="utf-8") as handle:
        return handle.read()


class ARealBanksFile(unittest.TestCase):
    """#130: moov-io/bai2's five files, which the mock's own reader reads (#128)."""

    def test_every_movement_reads_as_the_mocks_reader_reads_it(self):
        # Two readers written apart - this one by hand, `mockbank.bai2` from its
        # declaration - over every 16 in five files the project did not write:
        # funds types V, S, D and blank, records packed onto a line, a record
        # wrapped onto the next, texts full of commas and slashes.
        for name in SAMPLES:
            with self.subTest(sample=name):
                text = sample(name)
                theirs = [bai2.detail(r.values) for r in bai2.read(text) if r.code == "16"]
                ours = [line for s in payment_run.bai2_statements(text) for line in s["lines"]]
                self.assertEqual(len(ours), len(theirs))
                self.assertEqual(
                    [(Decimal(l["amount"]), l["side"], l["end_to_end_id"], l["msg_id"])
                     for l in ours],
                    [(Decimal(d.amount).scaleb(-2), "CRDT" if int(d.type_code) < 400
                      else "DBIT", d.reference, d.customer_reference) for d in theirs])

    def test_both_readers_split_every_file_into_the_same_fields(self):
        """The agreement, one level below the movements - and it had a hole.

        The test above compares each movement's amount, side and two references.
        It does **not** compare the text, and both wraps in the corpus -
        `sample3` line 18 and `sample5` line 62 - fall in a text field, so the two
        readers could disagree about how a wrap joins and nothing here noticed:
        reverting `payment_run`'s join alone, while `mockbank.bai2` kept #142's,
        passed this whole suite.

        Comparing the field lists closes it. It is also the stronger statement of
        what "two readers agree" should mean - not that they agree about the four
        values one caller happens to use, but that they cut the same file into the
        same fields.
        """
        for name in SAMPLES:
            with self.subTest(sample=name):
                text = sample(name)
                theirs = [[r.code] + list(r.values) for r in bai2._fold(text)]
                self.assertEqual(payment_run.bai2_records(text), theirs)

    def test_a_wrap_inside_a_reference_reads_as_one_value_here_too(self):
        """`payment_run` on the case #142 exists for, held on its own.

        No vendored file wraps outside a `16`'s text, so the agreement test above
        can only hold the readers together where a sample happens to wrap - and
        the field that matters to this reader is the one it reconciles on. A
        newline left in `end_to_end_id` is a payment that stops matching its
        invoice, and that is this reader's job, not `mockbank.bai2`'s.
        """
        wrapped = ("01,BANK,CUST,261001,0000,1,,,2/\n"
                   "02,ACME,BANK,1,261001,0000,USD,/\n"
                   "03,0000000001,USD,010,+125000,,Z,015,+0,,Z/\n"
                   "16,495,125000,Z,INV-2026-\n"
                   "0101,MSG-1,Globex Supplies B.V./\n"
                   "49,+125000,4/\n98,+125000,1,6/\n99,+125000,1,8/\n")
        [statement] = payment_run.bai2_statements(wrapped)
        [line] = statement["lines"]
        self.assertEqual(line["end_to_end_id"], "INV-2026-0101")
        self.assertEqual(line["msg_id"], "MSG-1")
        self.assertEqual((Decimal(line["amount"]), line["side"]),
                         (Decimal("1250.00"), "DBIT"))
        # And the same text through the other reader, which is the claim the
        # agreement test cannot make about a field no sample wraps.
        self.assertEqual(payment_run.bai2_records(wrapped),
                         [[r.code] + list(r.values) for r in bai2._fold(wrapped)])

    def test_a_slash_at_a_wrap_is_content_in_both_readers(self):
        """#150, held across the two readers rather than in one of them.

        No vendored file has this shape - five samples, two wrapped lines, and
        neither ends in `/` - so the corpus cannot hold the readers together here
        and a constructed file has to. Both lose the slash before #150, so a
        reference read `ABGS/RP0001` and the invoice behind it stopped matching.
        """
        wrapped = ("01,BANK,CUST,261001,0000,1,,,2/\n"
                   "02,ACME,BANK,1,261001,0000,USD,/\n"
                   "03,0000000001,USD,010,+125000,,Z,015,+0,,Z/\n"
                   "16,495,125000,Z,AB/\n"
                   "GS/RP0001,MSG-1,Globex Supplies B.V./\n"
                   "49,+125000,4/\n98,+125000,1,6/\n99,+125000,1,8/\n")
        # Field list by field list, which is the statement of agreement that
        # caught the one-sided revert on #142.
        theirs = [[r.code] + list(r.values) for r in bai2._fold(wrapped)]
        self.assertEqual(payment_run.bai2_records(wrapped), theirs)
        # And the value each of them arrives at.
        [statement] = payment_run.bai2_statements(wrapped)
        [line] = statement["lines"]
        self.assertEqual(line["end_to_end_id"], "AB/GS/RP0001")
        self.assertEqual(line["msg_id"], "MSG-1")

    def test_a_slash_at_each_of_two_wraps_in_one_field_is_kept(self):
        """The case a one-line wrap cannot reach, found by mutation.

        Reverting `payment_run`'s *continuation* branch to strip every trailing
        slash passed the whole suite, because in a two-line wrap that branch only
        ever sees the closing line, where the slash really is a terminator and
        stripping it is right. The branch is only wrong when a continuation line is
        itself continued - a field wrapped across three lines - which nothing
        exercised. #142's own hole had this shape, and so did `_continues` on #146.
        """
        twice = ("01,BANK,CUST,261001,0000,1,,,2/\n"
                 "02,ACME,BANK,1,261001,0000,USD,/\n"
                 "03,0000000001,USD,010,+125000,,Z,015,+0,,Z/\n"
                 "16,495,125000,Z,AB/\n"
                 "CD/\n"
                 "EF,MSG-1,Globex Supplies B.V./\n"
                 "49,+125000,4/\n98,+125000,1,6/\n99,+125000,1,8/\n")
        theirs = [[r.code] + list(r.values) for r in bai2._fold(twice)]
        self.assertEqual(payment_run.bai2_records(twice), theirs)
        [statement] = payment_run.bai2_statements(twice)
        [line] = statement["lines"]
        # Both slashes content, and the one closing the record still a terminator.
        self.assertEqual(line["end_to_end_id"], "AB/CD/EF")
        self.assertEqual(line["msg_id"], "MSG-1")

    def test_a_space_on_either_side_of_a_wrap_is_kept_by_both(self):
        """Joining with nothing means nothing is inserted - and nothing removed.

        Both readers used to `rstrip()` every line before joining, so a producer
        that wrapped *after* a space lost it: `PAYMENT FOR ` + `INVOICE 12` read
        `PAYMENT FORINVOICE 12`. Until #142 the line break kept the words apart,
        so the rule traded one defect for another. moov trims only a copy, for the
        record-code test, and appends to its buffer untouched.

        Whitespace after a terminator is a different thing - it is outside the
        record - and still comes off.
        """
        def read(text):
            fields = [[r.code] + list(r.values) for r in bai2._fold(text)]
            self.assertEqual(payment_run.bai2_records(text), fields, text)
            return fields[0][-1]

        # A space before the break, which the producer wrote and meant.
        self.assertEqual(read("16,495,125000,Z,REF1,MSG-1,PAYMENT FOR \n"
                              "INVOICE 12/\n"),
                         "PAYMENT FOR INVOICE 12")
        # One on each side: both are content, so both survive.
        self.assertEqual(read("16,495,125000,Z,REF1,MSG-1,PAYMENT FOR \n"
                              " INVOICE 12/\n"),
                         "PAYMENT FOR  INVOICE 12")
        # A fixed-width field padded to its column, which is what wraps in the
        # first place. sample1 writes `RETURNED CHEQUE     ` this way.
        self.assertEqual(read("16,495,125000,Z,REF1,MSG-1,ACME      \n"
                              "LTD       /\n"),
                         "ACME      LTD       ")
        # Outside the record: the terminator's trailing whitespace still goes.
        self.assertEqual(read("16,495,125000,Z,REF1,MSG-1,DONE/   \n"), "DONE")
        # And the end of the file continues nothing, so an unterminated last
        # record's trailing whitespace is padding around it, not content. With
        # no terminator and no line after it, this is the one case where the
        # lookahead has nothing to look at.
        self.assertEqual(read("16,495,125000,Z,REF1,MSG-1,TAIL   \n"), "TAIL")
        self.assertEqual(read("16,495,125000,Z,REF1,MSG-1,TAIL   "), "TAIL")

    def test_padding_every_line_changes_only_the_two_wrapped_records(self):
        """Trailing whitespace is content only where a wrap joins to it.

        The space before a wrap has to survive, because the join inserts nothing.
        Everywhere else trailing whitespace is padding *around* a record and must
        come off, as it did before #142 - a `49,+125000,2   ` that ends its record
        states 2, not `2   `. A first draft of this kept it on every line with no
        terminator, which read 17 of `sample4`'s 31 records differently once its
        lines were padded; v0.5.0 read none differently. moov draws the line in
        the same place, trimming at its `fullLine` label and leaving the buffer
        alone on the continuation path.

        So: pad every line of all five files and the fields must not move, except
        in the one record per file that is actually wrapped, where the padding now
        falls inside the field by design.
        """
        wrapped_at = {"bai2-sample3.txt": 18, "bai2-sample5.txt": 62}
        for name in SAMPLES:
            with self.subTest(sample=name):
                raw = sample(name)
                padded = "".join(line.rstrip("\r\n") + "   " + "\n"
                                 for line in raw.splitlines(True))
                before = list(bai2._fold(raw))
                after = list(bai2._fold(padded))
                self.assertEqual(len(before), len(after))
                moved = [b.line for b, a in zip(before, after)
                         if list(b.values) != list(a.values)]
                self.assertEqual(
                    moved, [wrapped_at[name]] if name in wrapped_at else [],
                    "%s: padding moved fields in records that are not wrapped" % name)
                # Both readers, on both texts: a rule only one of them follows is
                # the defect this file exists to catch.
                self.assertEqual(payment_run.bai2_records(raw),
                                 [[r.code] + list(r.values) for r in before])
                self.assertEqual(payment_run.bai2_records(padded),
                                 [[r.code] + list(r.values) for r in after])

    def test_sample3_adds_up_account_by_account(self):
        # Packed records and a wrapped one: if either were read wrong, a
        # movement would be lost or invented and an account would not add up.
        statements = payment_run.bai2_statements(sample("bai2-sample3.txt"))
        self.assertEqual((len(statements), sum(len(s["lines"]) for s in statements)),
                         (15, 26))
        for s in statements:
            moved = sum((Decimal(l["amount"]) * (1 if l["side"] == "CRDT" else -1)
                         for l in s["lines"]), Decimal("0"))
            self.assertEqual(s["opening"] + moved, s["closing"], s["account"])

    def test_a_text_with_commas_and_slashes_leaves_the_references_alone(self):
        [payment] = [l for s in payment_run.bai2_statements(sample("bai2-sample4.txt"))
                     for l in s["lines"] if l["end_to_end_id"] == "SPB2322684598521"]
        self.assertEqual((payment["amount"], payment["side"], payment["msg_id"]),
                         ("9286.50", "DBIT", "AB-GS-RPFILERP0001-RPBA0001"))

    def test_a_code_after_a_slash_is_text_unless_a_comma_follows(self):
        # The rule's second half, which no sample exercises: `12/16` at the end
        # of a text and `4/88 Harbour St` inside one are text, not records.
        _, _, text = both(1000000, 875000, THREE[:1])
        detail = next(l for l in text.splitlines() if l.startswith("16,"))
        written = text.replace(detail, detail[:-1] + " Unit 4/88 Harbour St 12/16/")
        [statement] = payment_run.bai2_statements(written)
        self.assertEqual(movements(statement),
                         [(Decimal("1250.00"), "DBIT", "INV-2026-0101")])

    def test_an_account_without_both_ledger_balances_is_not_a_statement(self):
        # sample4's first account reports only a current balance (060): an
        # intraday position. It comes back without an opening or closing
        # rather than stopping the accounts beside it from being read.
        statements = payment_run.bai2_statements(sample("bai2-sample4.txt"))
        by_account = {s["account"]: s for s in statements}
        self.assertEqual((by_account["107049924"]["opening"],
                          by_account["107049924"]["closing"]), (None, None))
        self.assertEqual((by_account["104108339"]["opening"],
                          by_account["104108339"]["closing"]),
                         (Decimal("1595811.94"), Decimal("1593811.94")))


class WhatAStatementCannotCarryBack(unittest.TestCase):
    """`payment_run.UNCARRIABLE` against the writer it is about (#171).

    The example imports no mock, so it cannot read `bai2.UNSAFE` and has to write
    the set out. That is a copy, and a copy drifts: if `_safe` ever starts
    replacing another character, the run would keep paying invoices whose
    reference it mangles and nothing would say so. This holds the two equal, which
    is the only thing making "from `bai2.UNSAFE` and `CONTROLS`, not a list typed
    by hand" true rather than true on the day it was written.
    """

    def test_the_examples_set_is_exactly_the_writers(self):
        self.assertEqual(set(payment_run.UNCARRIABLE),
                         set(bai2.UNSAFE) | set(bai2.CONTROLS))

    def test_a_reference_holding_one_really_does_come_back_changed(self):
        # Not a tautology about two constants: the reason the guard exists is that
        # `_safe` changes the value, so one of them is put through it.
        for bad in (",", "/"):
            reference = "INV%s1" % bad
            self.assertNotEqual(bai2._safe(reference), reference, bad)
            self.assertEqual(bai2._safe(reference), "INV 1", bad)


class AnAccountThatIsNotAStatement(unittest.TestCase):

    def test_if_it_is_ours_it_is_a_problem_and_nothing_is_posted(self):
        # The paying account reported without 010 and 015: named in
        # run.problems, and SAP is never asked to post it.
        text = sample("bai2-sample4.txt")
        answers = {"camt.053": b"", "bai2": text.encode("utf-8"), "nacha.return": b""}
        run = payment_run.PaymentRun("http://sap", "http://bank",
                                     {"iban": ACCOUNT["iban"], "company_id": "107049924",
                                      "name": "ACME", "bic": "MOCKNL2A",
                                      "routing": "999999992"}, "nacha")
        posted = []
        run.post_statement = lambda r, s: posted.append(s)
        original = payment_run.call
        payment_run.call = lambda base, method, path: (200, answers[path.rsplit("=", 1)[1]])
        try:
            outcome = payment_run.Run(DAY, "R1")
            run.reconcile(outcome)
        finally:
            payment_run.call = original
        self.assertEqual(posted, [])
        self.assertEqual(outcome.problems, [
            "the BAI2 statement for 2023-09-06 gives no opening and closing ledger "
            "balance (010 and 015), so it was not posted"])


class TheReturnFileReader(unittest.TestCase):

    def test_each_return_gives_its_r_code_by_identification_number(self):
        # moov-io/ach's return-WEB.ach: a debit returned R01 and a credit R03.
        with open(os.path.join(HERE, "samples", "external", "nacha-return-WEB.ach"),
                  encoding="utf-8") as handle:
            reasons = payment_run.nacha_return_reasons(handle.read())
        self.assertEqual(reasons, {"MjMxNDAwMjAtOGQ": "R01", "NmRjZTJmMzItMGN": "R03"})


if __name__ == "__main__":
    unittest.main()
