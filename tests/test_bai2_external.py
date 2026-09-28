"""The mock's BAI2 against a file it did not write (#57).

`tests/test_bai2.py` holds the writer to the reader and both to `RECORDS`. That
can only catch a writer that disagrees with the declaration. A reader and a
writer derived from one declaration agree with each other even when both are
wrong about the format - the blind spot #10 found in the dictionary and #55
closed for NACHA - and only a third party's file catches it.

`tests/samples/external/bai2-sample1.txt` is that file: a statement from
moov-io/bai2, byte-for-byte from a pinned commit, listed in `SOURCES.md`.

Two kinds of test are here and they are deliberately different.

**What the sample settles** is asserted by *recomputing* it, with the arithmetic
written out in this file rather than borrowed from `mockbank.bai2`. Reusing the
module's own `_control_total` to check the module's reading of a control total
would be the same circularity in a new place.

**What the sample refuses to be read by** is pinned as it stands. `bai2.read`
cannot parse this file, in three ways, and each way has a test that asserts the
current failure. They are not xfails or skips: they pass today, describe a real
defect, and #114 must change them to close it. A gap nobody can see is a gap
that stays.
"""
import datetime
import hashlib
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from mockbank import bai2                                            # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "external", "bai2-sample1.txt")

# The digest in SOURCES.md. Not a checksum for its own sake: it is what makes
# "byte-for-byte from that commit" a claim a test can fail on, and it is how a
# re-pin becomes a visible change rather than a quiet one.
DIGEST = "0150331e6118e9fc6a1a10871f739b2d317c5cca5159c007622cffbbb64fe00c"

DAY = datetime.date(2026, 10, 5)
AT = datetime.datetime(2026, 10, 6, 9, 0)
NACHA_ACCOUNT = {"id": "ACME", "name": "ACME Corporation", "currency": "USD",
                 "iban": "NL41MOCK0000000001", "format": "nacha",
                 "account_number": "10200123456"}
IBAN_ACCOUNT = {"id": "ACME", "name": "ACME Corporation", "currency": "USD",
                "iban": "NL41MOCK0000000001"}


def text():
    with open(SAMPLE, "r", encoding="utf-8") as handle:
        return handle.read()


def records():
    """The sample as [(code, [fields after the code])], parsed here.

    Eleven lines of parser, so that nothing below asks `mockbank.bai2` whether
    `mockbank.bai2` is right.
    """
    out = []
    for line in text().split("\n"):
        if not line:
            continue
        body = line[:-1] if line.endswith("/") else line
        fields = body.split(",")
        out.append((fields[0], fields[1:]))
    return out


def sections():
    """(index of each 03, index of its 49) for every account in the sample."""
    rows = records()
    out, start = [], None
    for index, (code, _) in enumerate(rows):
        if code == "03":
            start = index
        elif code == "49":
            out.append((start, index))
    return out


class TheSampleIsWhatSourcesMdSaysItIs(unittest.TestCase):
    """Every claim below is about *this* file, so first, that it is that file."""

    def test_the_bytes_are_the_ones_recorded_in_sources_md(self):
        with open(SAMPLE, "rb") as handle:
            self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), DIGEST)

    @unittest.skipIf(shutil.which("git") is None, "no git to ask")
    def test_git_is_told_not_to_rewrite_these_files_line_endings(self):
        """The guard that makes the digest test fail on every platform, not one.

        A Windows checkout with the default `core.autocrlf=true` rewrites every
        LF to CRLF, so the file on disk is not the file the commit holds: this
        sample hashed to `0258766c...` there and `0150331e...` everywhere else,
        and the test above failed on `windows-latest` alone. `.gitattributes`
        declares the directory `-text`.

        Asking `git check-attr` rather than reading `.gitattributes` is the
        point: a pattern that does not match is a file this will not protect,
        and only git knows which it is. And the NACHA samples were being
        rewritten the same way with nothing to catch it, because
        `nacha.inspect` strips a line before reading it - they passed while
        reading a file moov never published.
        """
        root = os.path.dirname(HERE)
        directory = os.path.join(HERE, "samples", "external")
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--", directory],
            cwd=root, capture_output=True, text=True)
        if tracked.returncode != 0:
            self.skipTest("not a git work tree")
        files = [name for name in tracked.stdout.split("\0") if name]
        self.assertGreaterEqual(len(files), 15, "no external samples found")
        self.assertIn("tests/samples/external/bai2-sample1.txt", files)
        asked = subprocess.run(
            ["git", "check-attr", "-z", "text", "--"] + files,
            cwd=root, capture_output=True, text=True)
        self.assertEqual(asked.returncode, 0, asked.stderr)
        # -z gives path, attribute, value, repeated.
        parts = [p for p in asked.stdout.split("\0")][:-1]
        answers = dict(zip(parts[0::3], parts[2::3]))
        self.assertEqual(len(answers), len(files))
        for name, value in sorted(answers.items()):
            self.assertEqual(value, "unset",
                             "%s may be rewritten on checkout" % name)

    def test_sources_md_names_the_commit_and_the_path(self):
        here = os.path.join(HERE, "samples", "external", "SOURCES.md")
        with open(here, "r", encoding="utf-8") as handle:
            sources = handle.read()
        self.assertIn("d3e11b628d3d59fd6911836b9ca328cb8b7621f2", sources)
        self.assertIn("test/testdata/sample1.txt", sources)
        self.assertIn(DIGEST, sources)

    def test_it_is_the_shape_the_rest_of_this_file_assumes(self):
        rows = records()
        self.assertEqual(len(rows), 27)
        self.assertEqual([code for code, _ in rows][:3], ["01", "02", "03"])
        self.assertEqual([code for code, _ in rows][-2:], ["98", "99"])
        self.assertEqual(len(sections()), 2, "two account sections")
        self.assertTrue(any(code == "88" for code, _ in rows),
                        "the continuation record this sample was chosen for")


class WhatTheSampleSettles(unittest.TestCase):
    """The two readings `mockbank/bai2.py` flagged, and three it got wrong."""

    def test_a_record_count_includes_the_trailer_that_states_it(self):
        # bai2.COUNTS_INCLUDE_THE_TRAILER, against somebody else's file.
        self.assertTrue(bai2.COUNTS_INCLUDE_THE_TRAILER)
        rows = records()
        for start, end in sections():
            self.assertEqual(int(rows[end][1][1]), end - start + 1,
                             "the 49 on record %d" % (end + 1))
        self.assertEqual(int(rows[25][1][2]), 25, "the 98, for 02 through 98")
        self.assertEqual(int(rows[26][1][2]), len(rows), "the 99, for the file")

    def test_a_continuation_record_is_counted_like_any_other(self):
        # Not a separate reading so much as the thing that makes the count above
        # unambiguous: 14 records is only 14 if the 88 is one of them.
        rows = records()
        start, end = sections()[0]
        span = rows[start:end + 1]
        stated = int(rows[end][1][1])
        continuations = sum(1 for code, _ in span if code == "88")
        self.assertEqual(continuations, 1, "nothing to prove without one")
        self.assertEqual(stated, len(span))
        # The assertion that would fail if a continuation did not count: the
        # records that are not continuations are fewer than the stated total.
        self.assertEqual(len(span) - continuations, stated - continuations)
        self.assertLess(len(span) - continuations, stated)

    def test_every_balance_and_control_total_states_its_sign(self):
        # bai2._signed: what states a position states its sign. The sample writes
        # `+` on all six of them and on none of the movement amounts.
        rows = records()
        totals = [f[0] for code, f in rows if code in ("49", "98", "99")]
        self.assertEqual(len(totals), 4)
        for value in totals:
            self.assertIn(value[0], "+-", value)
        summaries = [f[i + 1] for code, f in rows if code == "03"
                     for i in range(2, len(f), 4) if f[i]]
        self.assertEqual(len(summaries), 4)
        for value in summaries:
            self.assertIn(value[0], "+-", value)

    def test_a_movement_amount_states_no_sign(self):
        # The other half, and the reason `_movement` is not `_amount`: direction
        # lives in the type code, so there is no sign to write.
        amounts = [f[1] for code, f in records() if code == "16"]
        self.assertEqual(len(amounts), 17)
        for value in amounts:
            self.assertNotIn(value[0], "+-", value)
            self.assertTrue(value.isdigit(), value)

    def test_the_mock_writes_the_sign_the_sample_does(self):
        # The point of the two tests above: the writer now agrees with the file.
        written = bai2.write_statement(NACHA_ACCOUNT, DAY, 7,
                                       10875000, 12500000, [], created_at=AT)
        for line in written.splitlines():
            if line.startswith(("49,", "98,", "99,")):
                self.assertIn(line.split(",")[1][0], "+-", line)
            if line.startswith("03,"):
                self.assertIn("010,+10875000", line)
                self.assertIn("015,+12500000", line)

    def test_a_control_total_sums_the_summaries_as_well_as_the_details(self):
        # The reading `_account`'s comment had backwards. Each 49 here is exactly
        # twice the sum of its own 16s, because the 88's totals count too.
        rows = records()
        for start, end in sections():
            details = sum(int(f[1]) for code, f in rows[start:end]
                          if code == "16")
            summary = 0
            for code, f in rows[start:end]:
                if code == "03":
                    summary += sum(int(f[i + 1].lstrip("+"))
                                   for i in range(2, len(f), 4) if f[i])
                if code == "88":
                    summary += sum(int(f[i + 1].lstrip("+"))
                                   for i in range(0, len(f), 6) if f[i])
            stated = int(rows[end][1][0].lstrip("+"))
            self.assertEqual(stated, details + summary)
            self.assertNotEqual(stated, details,
                                "if these were equal the sample would not say "
                                "anything about summaries")

    def test_a_group_trailer_sums_its_accounts_and_a_file_trailer_its_groups(self):
        rows = records()
        accounts = [int(rows[end][1][0].lstrip("+")) for _, end in sections()]
        self.assertEqual(int(rows[25][1][0].lstrip("+")), sum(accounts))
        self.assertEqual(int(rows[26][1][0].lstrip("+")),
                         int(rows[25][1][0].lstrip("+")))

    def test_the_bank_originates_the_group_and_the_customer_receives_it(self):
        # `_parties`. The 01's sender and the 02's originator are one party; the
        # 01's receiver and the 02's ultimate receiver are the other.
        rows = records()
        sender, receiver = rows[0][1][0], rows[0][1][1]
        ultimate_receiver, originator = rows[1][1][0], rows[1][1][1]
        self.assertEqual(originator, sender, "the 02 originates from the 01's sender")
        self.assertEqual(ultimate_receiver, receiver, "and is addressed as the 01 is")
        self.assertNotEqual(sender, receiver, "otherwise this proves nothing")

    def test_the_mock_names_the_two_parties_the_way_the_sample_does(self):
        written = bai2.write_statement(IBAN_ACCOUNT, DAY, 7, 2000, 1000, [],
                                       created_at=AT, sender="MOCKBANK",
                                       receiver="ACME")
        lines = written.splitlines()
        first = lines[0].rstrip("/").split(",")
        group = lines[1].rstrip("/").split(",")
        self.assertEqual((first[1], first[2]), ("MOCKBANK", "ACME"))
        self.assertEqual((group[1], group[2]), ("ACME", "MOCKBANK"))

    def test_the_account_is_named_by_number_not_by_iban(self):
        # The change #110 made, against the file rather than against reasoning.
        rows = records()
        for code, f in rows:
            if code == "03":
                self.assertTrue(f[0].isdigit(), f[0])
                self.assertEqual(f[0], "10200123456")

    def test_the_record_length_field_is_populated_and_respected(self):
        # #95 said blank was right because the field is optional. It is stated
        # here, and it is a real bound: nothing in the file exceeds it.
        rows = records()
        stated = int(rows[0][1][5])
        self.assertEqual(stated, 80)
        longest = max(len(line) for line in text().split("\n") if line)
        self.assertLessEqual(longest, stated)
        self.assertEqual(longest, 75)


class WhatTheSampleShowsTheReaderCannotDo(unittest.TestCase):
    """`bai2.read` refuses or breaks on this file in three ways. That is #114.

    Each test pins the failure as it is today. They pass, and they are meant to
    be *deleted or inverted* by the change that adds continuation records and a
    variable-width funds type - which is the point: the alternative is a gap
    recorded only in prose, and prose does not fail.

    None of this reaches a user of the mock. `read` exists for the tests, and
    what the mock writes it reads. It matters because a reader that cannot open
    a real file has never been held to one, so every agreement it reports is an
    agreement with itself.
    """

    def test_the_whole_sample_is_refused_for_the_continuation_record(self):
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2.read(text())
        self.assertIn("'88'", str(refused.exception))
        self.assertIn("not declared", str(refused.exception))
        self.assertNotIn("88", bai2.RECORDS)

    def test_a_value_dated_detail_carries_two_fields_more_than_declared(self):
        # `V` is followed by an availability date and time, so a funds type is
        # one field or three. The declaration allows one.
        detail = [line for line in text().split("\n") if line.startswith("16,")][0]
        self.assertIn(",V,", detail)
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2.read(detail + "\n")
        self.assertIn("transaction detail", str(refused.exception))
        self.assertIn("6 field", str(refused.exception))
        self.assertIn("has 8", str(refused.exception))

    def test_a_value_dated_summary_group_raises_a_bare_value_error(self):
        # The worst of the three, because it is not a refusal. A summary group is
        # four fields for a blank funds type and six for `V`; six passes the
        # "repeats in fours" check when there are two of them, and then the
        # amount is read out of the wrong slot.
        group = [line for line in text().split("\n") if line.startswith("88,")][0]
        as_an_03 = "03,10200123456,CAD," + group.split(",", 1)[1]
        bai2.read(as_an_03 + "\n")          # accepted, which is the problem
        with self.assertRaises(ValueError) as broken:
            bai2._amounts_in(as_an_03)
        self.assertNotIsInstance(broken.exception, bai2.Wrong)
        # And the shape of why: a V group is six fields, not four.
        fields = as_an_03.rstrip("/").split(",")[3:]
        self.assertEqual(len(fields), 12)
        self.assertEqual(fields[4], "060316", "the date that makes it six")

    def test_the_three_failures_are_the_whole_list(self):
        # So that #114 cannot fix two of them and look finished. Dropping the
        # 88s and widening nothing else must still fail on the funds type, and
        # that is the only thing left.
        without_88 = "\n".join(line for line in text().split("\n")
                               if not line.startswith("88")) + "\n"
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2.read(without_88)
        self.assertIn("transaction detail", str(refused.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
