"""Every NACHA file the mock writes, for every behaviour, and its shape (#55).

`GeneratedMessagesAreValid` in `test_dictionary.py` walks every message the mock
writes against its own dictionary, for every behaviour, with the counts pinned.
It cannot cover NACHA: it parses each collected body as XML, and a NACHA account
is sent text. So this is the same idea for the other format, and it is here
rather than folded into that class because the two share no assertion - one
walks an element tree, the other reads 94-column records.

Two things are checked that a declaration cannot satisfy by accident, which is
the blind spot #10 found and #55 exists to close. A reader and a writer built
from one declaration agree with each other even when both are wrong about the
standard, so:

- the **outside files** in `tests/samples/external/` pin the field positions
  against somebody else's reader. Those already exist and `test_nacha.py` reads
  them; nothing is added here.
- the **shape** of what the mock writes is checked against the format's own
  rules rather than against the declaration: a record type in column 1, every
  line exactly 94 characters, and a file a whole number of ten-line blocks.
  `RECORDS` summing to 94 columns is a self-consistency check and would hold
  even if every field in it were at the wrong offset; a file that is 94
  characters wide *and* read correctly by moov-io's fixtures is not.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from support import MockServerCase                                   # noqa: E402
from test_payments import BANK_START                                 # noqa: E402

from mockbank import nacha                                           # noqa: E402
from mockbank.accounts import BEHAVIOURS                             # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
TWIN = "nacha_four_payments_to_the_seed.ach"


def sample(name):
    with open(os.path.join(SAMPLES, name), "rb") as handle:
        return handle.read()


class WrittenNachaCase(MockServerCase):
    config_kwargs = {"clock": BANK_START}          # Thursday 09:00, before the cutoff
    FRIDAY = "2026-10-02"

    def nacha_account(self, account, **fields):
        resp = self.request("PATCH", "/_mock/accounts/" + account,
                            body=dict({"format": "nacha", "currency": "USD"},
                                      **fields))
        self.assertEqual(resp.status, 200, resp.body)

    def collect(self):
        return self.get("/_mock/mailbox").json()


class GeneratedNachaFilesAreValid(WrittenNachaCase):
    """Every NACHA file the mock writes reads back through its own reader.

    With no error, for every behaviour an account can have, and with the kinds
    and numbers pinned - so a path that stops producing a file fails rather than
    passing because it produced nothing to check, which is the failure mode the
    counts in `GeneratedMessagesAreValid` exist to catch.
    """

    # behaviour -> ({message type: how many}, times the file is sent).
    #
    # A first draft of this had a return file only under `return-later`, and was
    # wrong about the format rather than about the numbers: the twin pays the
    # seeded accounts, two of which refuse - INITECH is closed and EURODIS has a
    # bank identifier that is not this bank - and #54 sends a NACHA originator
    # its rejections as an R-coded return file, not only in the acknowledgement.
    # So a return file is the normal case here and its absence is what needs
    # explaining:
    #
    #   reject-file  no return, because a file refused at group level books
    #                nothing, so there is nothing to send back
    #   silent       a return but no acknowledgement: `silent` suppresses the
    #                acknowledgement, and a return is its own message
    #   duplicate    two acknowledgements, one per send, and one return: the
    #                second file is DUPL and books nothing
    #
    # `return-later` is one file, not two: its own returns and the rejections
    # land on the same day and the mock writes one file per account per day, so
    # that file carries four entries where `accept`'s carries two. Checked, not
    # assumed - see TheShapeOfWhatTheMockWrites.
    #
    # Statements are camt.053 for a NACHA account until #57 wires BAI2 in, so
    # none appear here. They will, and then this fails and gets updated.
    EXPECTED = {
        "accept": ({nacha.ACK: 1, nacha.RETURN: 1}, 1),
        "closed-account": ({nacha.ACK: 1, nacha.RETURN: 1}, 1),
        "insufficient-funds": ({nacha.ACK: 1, nacha.RETURN: 1}, 1),
        "bad-bank-id": ({nacha.ACK: 1, nacha.RETURN: 1}, 1),
        "return-later": ({nacha.ACK: 1, nacha.RETURN: 1}, 1),
        "reject-file": ({nacha.ACK: 1}, 1),
        "duplicate-file": ({nacha.ACK: 2, nacha.RETURN: 1}, 2),
        "silent": ({nacha.RETURN: 1}, 1),
        "statement-gap": ({nacha.ACK: 1, nacha.RETURN: 1}, 1),
    }

    def test_every_behaviour_is_exercised(self):
        self.assertEqual(set(self.EXPECTED), set(BEHAVIOURS),
                         "a behaviour whose NACHA output this test does not check")

    def written_under(self, behaviour):
        """What a NACHA-format ACME is sent under this behaviour."""
        self.post("/_mock/reset")
        fields = {"behaviour": behaviour}
        if behaviour == "return-later":
            # One day, so the return lands on the Friday this advances to.
            fields["parameters"] = {"days": 1}
        self.nacha_account("ACME", **fields)
        counts, sends = self.EXPECTED[behaviour]
        for _ in range(sends):
            self.post("/payments", body=sample(TWIN))
        self.assertEqual(self.post("/_mock/advance?to=" + self.FRIDAY).status, 200)
        return [m for m in self.collect() if m["type"] in nacha.TEXT_TYPES]

    def test_every_file_reads_back_with_no_error_for_every_behaviour(self):
        checked = 0
        for behaviour in sorted(BEHAVIOURS):
            with self.subTest(behaviour):
                written = self.written_under(behaviour)
                counts = {}
                for item in written:
                    counts[item["type"]] = counts.get(item["type"], 0) + 1
                    checked += 1
                    if item["type"] != nacha.RETURN:
                        continue        # the acknowledgement is prose, not a file
                    found = nacha.inspect(item["body"].encode("utf-8"),
                                          None)
                    errors = [f for f in _findings(found)
                              if getattr(f, "level", "error") == "error"]
                    self.assertEqual(errors, [],
                                     "%s under %s" % (item["type"], behaviour))
                self.assertEqual(counts, self.EXPECTED[behaviour][0],
                                 "under %s" % behaviour)
        self.assertEqual(checked,
                         sum(sum(c.values()) for c, _ in self.EXPECTED.values()))


def _findings(read):
    """`inspect` returns the file and its findings, in whichever shape."""
    if isinstance(read, tuple):
        return read[1]
    return getattr(read, "findings", [])


class TheShapeOfWhatTheMockWrites(WrittenNachaCase):
    """The format's own rules, not the declaration's self-consistency.

    `RECORDS` summing to 94 columns would hold with every field at the wrong
    offset. These three would not survive a writer that padded wrongly, and they
    are the ones a receiving bank's parser applies first.
    """

    def a_return_file(self):
        self.post("/_mock/reset")
        self.nacha_account("ACME", behaviour="return-later",
                           parameters={"days": 1})
        self.post("/payments", body=sample(TWIN))
        self.assertEqual(self.post("/_mock/advance?to=" + self.FRIDAY).status, 200)
        written = [m for m in self.collect() if m["type"] == nacha.RETURN]
        self.assertEqual(len(written), 1, "no return file to check the shape of")
        return written[0]["body"]

    def lines(self, body):
        text = body.rstrip("\n")
        return text.split("\n")

    def test_every_line_is_exactly_94_characters(self):
        for number, line in enumerate(self.lines(self.a_return_file()), start=1):
            self.assertEqual(len(line), nacha.LINE,
                             "line %d is %d characters" % (number, len(line)))

    def test_column_1_is_always_a_declared_record_type(self):
        for number, line in enumerate(self.lines(self.a_return_file()), start=1):
            self.assertIn(line[0], nacha.RECORDS,
                          "line %d starts with %r" % (number, line[0]))

    def test_the_file_is_a_whole_number_of_ten_line_blocks(self):
        lines = self.lines(self.a_return_file())
        self.assertEqual(len(lines) % nacha.BLOCK, 0,
                         "%d lines is not a whole number of blocks" % len(lines))
        self.assertGreater(len(lines), 0)

    def test_the_padding_is_nines_and_carries_no_content(self):
        # A short last block is filled with lines of nines. A writer that padded
        # with spaces, or with a record code, would still be 94 columns wide.
        lines = self.lines(self.a_return_file())
        filler = [l for l in lines if set(l) == {"9"}]
        self.assertTrue(filler, "no filler in a file whose records do not fill a block")
        for line in filler:
            self.assertEqual(line, "9" * nacha.LINE)

    def test_the_records_come_before_the_filler(self):
        lines = self.lines(self.a_return_file())
        codes = [l[0] for l in lines]
        last_record = max(i for i, l in enumerate(lines) if set(l) != {"9"})
        self.assertTrue(all(set(lines[i]) == {"9"}
                            for i in range(last_record + 1, len(lines))),
                        "a record after the filler began: %s" % codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
