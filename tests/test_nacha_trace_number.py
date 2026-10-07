"""An `InstrId` a NACHA return cannot carry is refused at the door (#218).

A NACHA account may send a `pain.001` or a `pain.008`, where an `InstrId` is
free text. What comes back to that account comes back in a NACHA return file,
whose addenda carries the original entry's trace number: 15 digits. #166 held
a payment's `InstrId` to the 15 and not to the digits, so `ACME-0001-2` was
zero-filled into a numeric field and the bank sent a return file its own reader
refuses. The collection door held it to neither, so a long one lost the account
its whole return file for the day.

Both doors now refuse both, `FF01`, when the file arrives. The test that holds
the fix to the fault is the read-back: every return file here goes through
`nacha.inspect` and has no finding.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from support import MockServerCase  # noqa: E402
from test_unwritable_inputs import GLOBEX, PINNED, pain001  # noqa: E402

from mockbank import accounts, nacha, schema  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
WIDTH = accounts.NACHA_TRACE_WIDTH
FULLWIDTH = "１２３"       # digits to `str.isdigit`, and not ASCII


class TraceCase(MockServerCase):
    config_kwargs = {"clock": PINNED}

    def setUp(self):
        self.post("/_mock/reset")

    def nacha_acme(self, **more):
        answer = self.patch("/_mock/accounts/ACME",
                            body=dict({"format": "nacha", "currency": "USD"}, **more))
        self.assertEqual(answer.status, 200, answer.body)

    def returned(self):
        """Every return waiting after the next business day, each file read
        back with no finding: {EndToEndId: Return}."""
        self.assertEqual(self.post("/_mock/advance?days=1").status, 200)
        out = {}
        for message in self.get("/_mock/mailbox?type=" + nacha.RETURN).json():
            read, findings = nacha.inspect(message["body"].encode("utf-8"))
            self.assertEqual(findings, [], message["body"])
            out.update((r.end_to_end_id, r) for r in read.returns)
        self.assertEqual(self.get("/_mock/unsent").json(), [])
        return out


class APayment(TraceCase):
    """`insufficient-funds`, so the payment is rejected `AM04` and comes back as
    an `R01` return entry the next business day - when its `InstrId` lets it."""

    def decided(self, instr_id, **account):
        self.nacha_acme(behaviour="insufficient-funds", **account)
        answer = self.post("/payments", body=pain001(
            "T1", [("T-1", 100000000, GLOBEX)], instr_id=instr_id, ccy="USD"))
        self.assertEqual(answer.status, 202, answer.body)
        [decided] = answer.json()["payments"]
        return decided

    def test_one_that_is_not_digits_is_refused_and_says_what_to_send(self):
        decided = self.decided("ACME-0001-2")
        self.assertEqual((decided["outcome"], decided["reason"]),
                         (accounts.REJECTED, schema.STRUCTURAL))
        for said in ("InstrId 'ACME-0001-2' is not a number", "%d digits" % WIDTH,
                     "this payment", "Send digits, or no InstrId"):
            self.assertIn(said, decided["reason_text"])
        # Refused for its format, which has no R code: nothing comes back.
        self.assertEqual(self.returned(), {})

    def test_fifteen_digits_come_back_as_the_original_trace_number(self):
        trace = "123456789012345"
        self.assertEqual(self.decided(trace)["reason"], "AM04")
        back = self.returned()["T-1"]
        self.assertEqual((back.reason, back.original_trace), ("R01", trace))

    def test_fewer_digits_are_zero_filled(self):
        self.assertEqual(self.decided("42")["reason"], "AM04")
        self.assertEqual(self.returned()["T-1"].original_trace, "42".rjust(WIDTH, "0"))

    def test_none_at_all_is_fine_and_the_return_carries_zeros(self):
        self.assertEqual(self.decided(None)["reason"], "AM04")
        self.assertEqual(self.returned()["T-1"].original_trace, "0" * WIDTH)

    def test_a_digit_that_is_not_ascii_is_not_a_digit_here(self):
        self.assertTrue(FULLWIDTH.isdigit(), "or this test asks nothing")
        decided = self.decided(FULLWIDTH)
        self.assertEqual(decided["reason"], schema.STRUCTURAL)
        self.assertIn("is not a number", decided["reason_text"])

    def test_sixteen_digits_are_still_refused_for_their_length(self):
        decided = self.decided("1" * (WIDTH + 1))
        self.assertEqual(decided["reason"], schema.STRUCTURAL)
        self.assertIn("is %d characters" % (WIDTH + 1), decided["reason_text"])

    def test_an_iso20022_account_may_call_its_payment_anything(self):
        # Nothing carries a trace number there; a `pain.002` echoes the text.
        answer = self.post("/payments", body=pain001(
            "T2", [("T-2", 1000, GLOBEX)], instr_id="ACME-0001-2"))
        [decided] = answer.json()["payments"]
        self.assertEqual(decided["outcome"], accounts.ACCEPTED, decided)


class ACollection(TraceCase):
    """The stock `pain.008`, in dollars. On the seed its second collection is
    rejected `AM04` and its third `AC04`, which a NACHA account gets back as
    `R01` and `R02` - when their `InstrId`s let them."""

    def collect(self, instr_prefix):
        with open(os.path.join(SAMPLES, "pain008_four_collections.xml"),
                  encoding="utf-8") as handle:
            xml = handle.read()
        self.assertEqual(xml.count("<InstrId>ACME-DD-"), 4)
        xml = (xml.replace('Ccy="EUR"', 'Ccy="USD"').replace("<Ccy>EUR</Ccy>", "<Ccy>USD</Ccy>")
               .replace("<InstrId>ACME-DD-", "<InstrId>" + instr_prefix))
        answer = self.post("/payments", body=xml.encode("utf-8"),
                           headers={"Content-Type": "application/xml"})
        self.assertEqual(answer.status, 202, answer.body)
        return [(c["reason"], c["reason_text"] or "") for c in answer.json()["collections"]]

    def test_the_stock_ids_are_refused_every_one(self):
        self.nacha_acme()
        decided = self.collect("ACME-DD-")
        self.assertEqual([reason for reason, _ in decided], [schema.STRUCTURAL] * 4)
        self.assertIn("InstrId 'ACME-DD-1' is not a number", decided[0][1])
        self.assertIn("this collection", decided[0][1])
        self.assertEqual(self.returned(), {})

    def test_one_too_long_is_refused_where_it_used_to_cost_the_return_file(self):
        self.nacha_acme()
        decided = self.collect("1" * WIDTH)                 # and the sample's own digit
        self.assertEqual([reason for reason, _ in decided], [schema.STRUCTURAL] * 4)
        self.assertIn("is %d characters" % (WIDTH + 1), decided[0][1])
        self.assertEqual(self.returned(), {})

    def test_digits_are_decided_on_their_merits_and_come_back_readable(self):
        self.nacha_acme()
        decided = self.collect("900")
        self.assertEqual([reason for reason, _ in decided], [None, "AM04", "AC04", None])
        back = self.returned()
        self.assertEqual({e2e: (r.reason, r.original_trace) for e2e, r in back.items()},
                         {"DD-2026-0102": ("R01", "9002".rjust(WIDTH, "0")),
                          "DD-2026-0103": ("R02", "9003".rjust(WIDTH, "0"))})

    def test_an_iso20022_account_may_call_its_collection_anything(self):
        self.patch("/_mock/accounts/ACME", body={"currency": "USD"})
        decided = self.collect("ACME-DD-")
        self.assertEqual([reason for reason, _ in decided], [None, "AM04", "AC04", None])


class TheRule(unittest.TestCase):

    def test_what_is_and_is_not_carried(self):
        for fine in (None, "", "0", "7" * WIDTH):
            self.assertIsNone(accounts.untraceable(fine, "payment"), fine)
        for refused in ("A", "12 3", "-1", "1.0", FULLWIDTH, "7" * (WIDTH + 1)):
            self.assertIsNotNone(accounts.untraceable(refused, "payment"), refused)


if __name__ == "__main__":
    unittest.main()
