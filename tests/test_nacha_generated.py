"""Every NACHA file the mock writes, held to more than its own reader (#55).

A reader and a writer built from one declaration agree with each other even
when both are wrong - the blind spot #10 found for ISO 20022. So three kinds of
check here, none of which the declaration can satisfy by accident:

- **By position**, straight off the text: every line 94 characters, the record
  type in column 1 and one NACHA defines, the file a whole number of blocks
  of ten, padded with nines.
- **Against files written elsewhere**: moov-io/ach's return file, whose
  addenda 99 and controls pin the positions and the debit/credit sides the
  mock's return writer has to match.
- **For every behaviour**, with the counts pinned, so a path that stops
  writing a file fails rather than passing because it wrote nothing to check.
"""
import datetime
import os

from support import MockServerCase
from test_payments import BANK_START

from mockbank.accounts import BEHAVIOURS
from mockbank import nacha

HERE = os.path.dirname(os.path.abspath(__file__))
TWIN = os.path.join(HERE, "samples", "nacha_four_payments_to_the_seed.ach")
EXTERNAL = os.path.join(HERE, "samples", "external")
FRIDAY = "2026-10-02"


def by_position(case, text, label):
    """What NACHA says of any file, read off the characters, not the declaration.

    A line is measured without its terminator, which NACHA leaves to the
    platform: a file with CRLF line ends is as good as one with LF, and a
    Windows checkout of the sample has them.
    """
    lines = text.splitlines()
    case.assertTrue(lines, label)
    for number, line in enumerate(lines, start=1):
        case.assertEqual(len(line), 94, "%s line %d" % (label, number))
        case.assertIn(line[0], "156789", "%s line %d" % (label, number))
    case.assertEqual(len(lines) % 10, 0, "%s is not a whole number of blocks" % label)
    case.assertEqual((lines[0][0], lines[0][:3]), ("1", "101"), label)
    # After the file control, only lines of nines, to fill the last block.
    control = max(i for i, line in enumerate(lines) if line[0] == "9" and line != "9" * 94)
    case.assertTrue(all(line == "9" * 94 for line in lines[control + 1:]), label)


class FilesWrittenElsewhere(MockServerCase):
    """moov-io/ach's return file: the layout the mock's return writer must match."""

    def read(self, name):
        with open(os.path.join(EXTERNAL, name), "rb") as handle:
            text = handle.read()
        return text, nacha.inspect(text, datetime.date(2026, 9, 28))

    def test_a_return_file_reads_without_an_error(self):
        text, (read, findings) = self.read("nacha-return-WEB.ach")
        by_position(self, text.decode("ascii"), "moov return-WEB.ach")
        self.assertEqual([f for f in findings if f.level == "error"], [])
        # Each addenda 99 field where moov put it: the reason in columns 4-6,
        # the original trace in 7-21, the original receiving bank in 28-35.
        self.assertEqual([(r.transaction_code, r.reason, r.original_trace,
                           r.original_receiving_dfi, r.amount) for r in read.returns],
                         [("26", "R01", "091400600000001", "09100001", 12354),
                          ("21", "R03", "091400600000003", "02100002", 4565)])

    def test_crlf_line_ends_are_as_good_as_lf(self):
        # A Windows checkout of the sample has CRLF, and so do many real files:
        # a line is 94 characters without its terminator, and the reader agrees.
        text, _ = self.read("nacha-return-WEB.ach")
        crlf = text.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        by_position(self, crlf.decode("ascii"), "return-WEB.ach with CRLF")
        read, findings = nacha.inspect(crlf, datetime.date(2026, 9, 28))
        self.assertEqual([f for f in findings if f.level == "error"], [])
        self.assertEqual(len(read.returns), 2)

    def test_a_return_of_a_debit_counts_as_a_debit(self):
        # moov's first batch returns a debit (26) and totals it as a debit; the
        # second returns a credit (21) and totals it as a credit. The reader
        # once counted every return as a credit, and its own writer agreed.
        self.assertEqual((nacha.side("26"), nacha.side("21")), ("debit", "credit"))
        text, (read, findings) = self.read("nacha-return-WEB.ach")
        self.assertNotIn("AM10", [f.code for f in findings])


class EveryFileTheMockWrites(MockServerCase):
    """For every behaviour, a NACHA account sends the twin of the sample, and
    every acknowledgement and return file that comes back is checked."""

    config_kwargs = {"clock": BANK_START}

    # behaviour -> (the account it is set on, other fields, how many times the
    # file is sent, {message type: how many}). The twin pays GLOBEX, INITECH
    # (closed: R02), EURODIS (bad bank: R03) and Umbrella at another bank.
    EXPECTED = {
        "accept": ("ACME", {}, 1, {nacha.ACK: 1, nacha.RETURN: 1}),
        "closed-account": ("GLOBEX", {}, 1, {nacha.ACK: 1, nacha.RETURN: 1}),
        "insufficient-funds": ("ACME", {"balance": 200000}, 1,
                               {nacha.ACK: 1, nacha.RETURN: 1}),
        "bad-bank-id": ("GLOBEX", {}, 1, {nacha.ACK: 1, nacha.RETURN: 1}),
        # returned and rejected come back on the same day, from one file
        "return-later": ("ACME", {"parameters": {"days": 1}}, 1,
                         {nacha.ACK: 1, nacha.RETURN: 1}),
        # refused whole: acknowledged, but no payment to return
        "reject-file": ("ACME", {}, 1, {nacha.ACK: 1}),
        "duplicate-file": ("ACME", {}, 2, {nacha.ACK: 2, nacha.RETURN: 1}),
        # silent sends no status, but what comes back still comes back
        "silent": ("ACME", {}, 1, {nacha.RETURN: 1}),
        "statement-gap": ("ACME", {}, 1, {nacha.ACK: 1, nacha.RETURN: 1}),
    }

    def collect_for(self, behaviour):
        account, fields, sends, _ = self.EXPECTED[behaviour]
        self.post("/_mock/reset")
        self.request("PATCH", "/_mock/accounts/ACME",
                     body={"format": "nacha", "currency": "USD"})
        resp = self.request("PATCH", "/_mock/accounts/" + account,
                            body=dict(fields, behaviour=behaviour))
        self.assertEqual(resp.status, 200, resp.body)
        with open(TWIN, "rb") as handle:
            body = handle.read()
        for _ in range(sends):
            self.post("/payments", body=body)
        self.assertEqual(self.post("/_mock/advance?to=" + FRIDAY).status, 200)
        return [m for m in self.get("/_mock/mailbox").json()
                if m["type"] in nacha.TEXT_TYPES]

    def test_every_behaviour_is_exercised(self):
        self.assertEqual(set(self.EXPECTED), set(BEHAVIOURS))

    def test_every_nacha_file_is_well_formed_for_every_behaviour(self):
        for behaviour in sorted(BEHAVIOURS):
            with self.subTest(behaviour):
                counts = {}
                for message in self.collect_for(behaviour):
                    counts[message["type"]] = counts.get(message["type"], 0) + 1
                    label = "%s %s" % (behaviour, message["type"])
                    if message["type"] == nacha.RETURN:
                        by_position(self, message["body"], label)
                        read, findings = nacha.inspect(message["body"].encode("ascii"),
                                                       datetime.date(2026, 10, 2))
                        self.assertEqual(findings, [], label)
                        self.assertTrue(read.returns, label)
                    else:
                        # An acknowledgement is the mock's own plain shape, not
                        # a NACHA file: each line starts with what it is.
                        for line in message["body"].splitlines():
                            self.assertIn(line.split(" ", 1)[0], (
                                "ACKNOWLEDGEMENT", "CREATED", "FILE", "NAME",
                                "STATUS", "ENTRY"), label)
                self.assertEqual(counts, self.EXPECTED[behaviour][3])
