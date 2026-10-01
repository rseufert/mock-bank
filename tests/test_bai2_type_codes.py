"""The BAI2 transaction type codes, held to where they came from (#127).

Until #127 the three codes the mock writes on its `16` records were placeholders:
no licensed list of BAI2 codes existed when #57 looked. moov-io/bai2 has since
published one, and its test files include a bank's own exports. Both are
vendored under `tests/samples/external/` and listed in `SOURCES.md`:

* `bai2-type-codes.go`, moov's transcription of the specification's type code
  table, which gives each code a direction and a description;
* `bai2-sample3.txt` and `bai2-sample4.txt`, which show what a bank writes for
  which movement - the text of each `16` says what the movement was.

Each code is asserted against both where both have something to say, and the
file is parsed here rather than by `mockbank.bai2`, so that nothing asks the
module whether the module is right. The codes it replaced are checked too: the
same files show each of them meant something else.
"""
import hashlib
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from mockbank import bai2                                            # noqa: E402

EXTERNAL = os.path.join(HERE, "samples", "external")
TABLE = os.path.join(EXTERNAL, "bai2-type-codes.go")
THREE = os.path.join(EXTERNAL, "bai2-sample3.txt")
FOUR = os.path.join(EXTERNAL, "bai2-sample4.txt")

# The digests in SOURCES.md, so "byte-for-byte from that commit" can fail.
DIGESTS = {
    TABLE: "2efbcb0cd05620f4e2c2bdb10c528f70571ad9cba6285cd657ebb0b7eebe66ee",
    THREE: "8a13ec611352000fbab9a880858e8349b50380fab9754bc237391738cfd9ada4",
    FOUR: "5a11cde54c9c8266b34d9980ee66237c1311f56b87d9eb1d28e5f02bafebaa9f",
}

ENTRY = re.compile(r'"(\d{3})": \{Code: "\1", Transaction: Transaction(CR|DB|NA), '
                   r'Level: TypeLevel(\w+), Description: "([^"]*)"\}')
# A 16 whose funds type is blank or Z, the only two these files use on the
# records asserted here: code, amount, funds type, two references, then the
# text, which runs to the end of the record - sample4's texts carry commas.
DETAIL = re.compile(r"(?:^|/ )16,(\d{3}),\d+,Z?,[^,]*,[^,]*,([^/\n]*)", re.M)


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def table():
    """{code: (direction, level, description)} from moov's transcription."""
    return {code: (kind, level, description)
            for code, kind, level, description in ENTRY.findall(read(TABLE))}


def texts(path, code):
    """The text of every 16 in `path` written with `code`."""
    return [text.strip() for found, text in DETAIL.findall(read(path)) if found == code]


class TheSourcesAreTheOnesPinned(unittest.TestCase):

    def test_each_file_is_byte_for_byte_what_was_fetched(self):
        for path, digest in DIGESTS.items():
            with self.subTest(file=os.path.basename(path)):
                with open(path, "rb") as handle:
                    self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), digest)

    def test_the_table_is_read_whole(self):
        # A parser that silently skipped rows would make every lookup below a
        # KeyError at best and a wrong row at worst; the file has 469 of them.
        codes = table()
        self.assertEqual(len(codes), 469)
        self.assertEqual(codes["010"], ("NA", "Status", "Opening Ledger"))


class EachCodeIsTheOneABankWrites(unittest.TestCase):

    def test_the_debit_for_a_payment_sent_is_447(self):
        self.assertEqual(bai2.DEBIT, "447")
        self.assertEqual(table()["447"], ("DB", "Detail", "ACH Disbursement Funding Debit"))
        # What a NACHA account pays with: an ACH credit it originated, CCD or CTX.
        written = texts(FOUR, "447")
        self.assertEqual(len(written), 2)
        for text in written:
            self.assertTrue(text.startswith("ACH Credit Payment,"), text)
        self.assertEqual({re.search(r"SEC: (\w+)", t).group(1) for t in written},
                         {"CCD", "CTX"})

    def test_the_credit_for_a_payment_returned_is_257(self):
        self.assertEqual(bai2.RETURNED_CREDIT, "257")
        # The table is the evidence for our case: an individual return item,
        # credited. The sample's only 257 is a returned *debit* - money the
        # account holder collected, coming back - not a credit it sent, so it
        # shows the code in use for an ACH return and no more than that.
        self.assertEqual(table()["257"], ("CR", "Detail", "Individual ACH Return Item"))
        [written] = texts(FOUR, "257")
        self.assertTrue(written.startswith("ACH Debit Payment Return,"), written)
        # The other return code on the credit side is a settlement total, not
        # one item, which is why it is not the one.
        self.assertEqual(table()["168"][2], "ACH Return Item or Adjustment Settlement")

    def test_money_arriving_is_142(self):
        self.assertEqual(bai2.RECEIVED_CREDIT, "142")
        self.assertEqual(table()["142"], ("CR", "Detail", "ACH Credit Received"))
        written = texts(THREE, "142")
        self.assertTrue(any(t.endswith("PPD") for t in written), written)

    def test_a_collection_that_settled_is_165(self):
        # The code the bank writes for the proceeds of the account holder's own
        # collection (#131). Until #127 the mock wrote it for a return, which
        # is the other thing below.
        self.assertEqual(bai2.COLLECTED_CREDIT, "165")
        self.assertEqual(table()["165"], ("CR", "Detail", "Preauthorized ACH Credit"))
        [written] = texts(FOUR, "165")
        self.assertTrue(written.startswith("ACH Debit Collection,"), written)

    def test_a_collection_that_went_back_is_557(self):
        # The table is the evidence, as for 257, its twin on the credit side:
        # the sample's only 557 is a credit *received* going back, not a debit
        # collected. No sample shows a collection returned.
        self.assertEqual(bai2.RETURNED_COLLECTION, "557")
        self.assertEqual(table()["557"], ("DB", "Detail", "Individual ACH Return Item"))
        [written] = texts(FOUR, "557")
        self.assertTrue(written.startswith("ACH Credit Receipt Return,"), written)

    def test_none_is_left_a_placeholder(self):
        self.assertEqual(bai2.PLACEHOLDER_CODES, ())

    def test_each_is_in_the_range_a_reader_takes_its_direction_from(self):
        # 100-399 a credit and 400-699 a debit, which is all
        # examples/payment_run.py knows: the change must not move one across.
        codes = table()
        for code, kind in ((bai2.DEBIT, "DB"), (bai2.RETURNED_CREDIT, "CR"),
                           (bai2.RECEIVED_CREDIT, "CR"), (bai2.COLLECTED_CREDIT, "CR"),
                           (bai2.RETURNED_COLLECTION, "DB")):
            with self.subTest(code=code):
                self.assertEqual(codes[code][0], kind)
                self.assertEqual("CR" if 100 <= int(code) <= 399 else "DB", kind)


class TheCodesTheyReplacedMeantSomethingElse(unittest.TestCase):
    """495, 165 and 195 until #127: the same bank writes each for another thing."""

    def test_495_is_an_outgoing_wire(self):
        self.assertEqual(table()["495"][2], "Outgoing Money Transfer")
        self.assertTrue(all(t.startswith("Outgoing Wire,") for t in texts(FOUR, "495")))

    def test_165_is_the_proceeds_of_a_collection_not_a_return(self):
        self.assertEqual(table()["165"][2], "Preauthorized ACH Credit")
        [written] = texts(FOUR, "165")
        self.assertTrue(written.startswith("ACH Debit Collection,"), written)

    def test_195_is_an_incoming_wire(self):
        self.assertEqual(table()["195"][2], "Incoming Money Transfer")
        self.assertIn("Incoming Wire,-", texts(FOUR, "195"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
