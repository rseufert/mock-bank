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
import re
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from mockbank import bai2                                            # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "external", "bai2-sample1.txt")
TWO = os.path.join(HERE, "samples", "external", "bai2-sample2.txt")
THREE = os.path.join(HERE, "samples", "external", "bai2-sample3.txt")
FOUR = os.path.join(HERE, "samples", "external", "bai2-sample4.txt")
FIVE = os.path.join(HERE, "samples", "external", "bai2-sample5.txt")

# The digest in SOURCES.md. Not a checksum for its own sake: it is what makes
# "byte-for-byte from that commit" a claim a test can fail on, and it is how a
# re-pin becomes a visible change rather than a quiet one.
DIGESTS = {
    SAMPLE: "0150331e6118e9fc6a1a10871f739b2d317c5cca5159c007622cffbbb64fe00c",
    TWO: "34ccf04a37e44353e5aac16981201239ae90102c12806aaa739a2e13ae3aee6b",
    THREE: "8a13ec611352000fbab9a880858e8349b50380fab9754bc237391738cfd9ada4",
    FOUR: "5a11cde54c9c8266b34d9980ee66237c1311f56b87d9eb1d28e5f02bafebaa9f",
    FIVE: "0391a0999e718ee84048f1b9642be5b08f963c41f3cd628f3f8ac677fb9a2e5c",
}

# The five, and what each was vendored to settle. Kept as data so a test cannot
# quietly stop covering one.
ALL = (SAMPLE, TWO, THREE, FOUR, FIVE)

# Three of the five state trailers that match their own contents. `sample4` and
# `sample5` do not, and that is a fact about those files rather than about the
# reader - see `TheTwoFixturesWhoseOwnArithmeticIsWrong`.
RECONCILING = (SAMPLE, TWO, THREE)
DIGEST = DIGESTS[SAMPLE]

DAY = datetime.date(2026, 10, 5)
AT = datetime.datetime(2026, 10, 6, 9, 0)
NACHA_ACCOUNT = {"id": "ACME", "name": "ACME Corporation", "currency": "USD",
                 "iban": "NL41MOCK0000000001", "format": "nacha",
                 "account_number": "10200123456"}
IBAN_ACCOUNT = {"id": "ACME", "name": "ACME Corporation", "currency": "USD",
                "iban": "NL41MOCK0000000001"}


def text(path=SAMPLE):
    with open(path, "r", encoding="utf-8") as handle:
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
        for path, digest in sorted(DIGESTS.items()):
            with self.subTest(os.path.basename(path)):
                with open(path, "rb") as handle:
                    self.assertEqual(hashlib.sha256(handle.read()).hexdigest(),
                                     digest)

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
        # Deduplicated: during an unresolved merge `git ls-files` lists a
        # conflicted path once per stage, so a run in the middle of one counted
        # 26 names for 24 files and failed for a reason that had nothing to do
        # with line endings.
        files = sorted({name for name in tracked.stdout.split("\0") if name})
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
        self.assertIn("test/testdata/sample2.txt", sources)
        for digest in DIGESTS.values():
            self.assertIn(digest, sources)

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


class TheReaderReadsARealFile(unittest.TestCase):
    """What `WhatTheSampleShowsTheReaderCannotDo` used to pin, now inverted (#114).

    Those four tests asserted the three ways `read` failed on this file: an
    undeclared `88`, a value-dated `16` two fields wider than declared, and a
    value-dated summary group that passed the "repeats in fours" check and then
    raised a bare `ValueError` out of `_amounts_in`. They were written to be
    changed by the work that fixed them rather than deleted quietly, so here is
    the same ground from the other side.

    The strongest assertion in this class is not that the file reads. It is that
    `trailers_agree` finds nothing: every control total and every record count in
    both samples, recomputed from the records they cover. Get the fold wrong, or
    any funds-type width wrong, and a total or a count comes out different - so
    one call checks the whole design at once, against arithmetic the project did
    not write.
    """

    def test_both_samples_read(self):
        self.assertEqual(len(bai2.read(text())), 25)
        self.assertEqual(len(bai2.read(text(TWO))), 24)

    def test_every_trailer_in_both_samples_agrees(self):
        for name in (SAMPLE, TWO):
            with self.subTest(os.path.basename(name)):
                self.assertEqual(bai2.trailers_agree(text(name)), [])

    def test_a_continuation_is_folded_into_the_record_it_continues(self):
        folded = bai2._fold(text())
        self.assertEqual(len(folded), 25, "25 logical records")
        self.assertEqual(sum(r.lines for r in folded), 27, "from 27 lines")
        self.assertNotIn(bai2.CONTINUATION, [r.code for r in folded])
        # The 03 that an 88 continues carries both records' fields and counts as
        # two, which is what makes the 49's count of 14 come out right.
        first = [r for r in folded if r.code == "03"][0]
        self.assertEqual(first.lines, 2)
        self.assertIn("100", first.values, "the 88's summary type code")

    def test_a_group_split_across_the_boundary_is_read_as_one(self):
        # sample2's hardest record, and the one that proves a continuation is a
        # continuation of the *field stream* rather than a record of its own: the
        # type code 110 ends the 03 and its amount begins the 88.
        account = [r for r in bai2._fold(text(TWO))
                   if r.code == "03" and "110" in r.values
                   and any(v == "D" for v in r.values)][0]
        groups = bai2.summary_groups(account.values)
        self.assertEqual([g.type_code for g in groups], ["010", "190", "110"])
        distributed = groups[-1]
        self.assertEqual(distributed.type_code, "110")
        self.assertEqual(distributed.amount, 70000000)
        self.assertEqual(distributed.funds, "D")

    def test_every_funds_type_in_both_samples_is_accounted_for(self):
        # The widths, checked the way they were derived: a 16 must end with
        # nought to three trailing fields, never a negative number of them.
        seen = set()
        for name in (SAMPLE, TWO):
            for record in bai2._fold(text(name)):
                if record.code != "16":
                    continue
                one = bai2.detail(record.values)
                seen.add(one.funds)
                trailing = len(record.values) - 2 - len(one.availability) - 1
                self.assertGreaterEqual(trailing, 0, record.values)
                self.assertLessEqual(trailing, 3, record.values)
        self.assertEqual(seen, {"V", "S", "1"},
                         "the funds types these two files exercise")

    def test_a_value_dated_detail_reads_its_reference_from_the_right_field(self):
        # The bug this replaces was not only a refusal. With `V` the bank
        # reference is at index 5, not 3, so the old positional read would have
        # taken the availability date `060316` for the reference.
        record = [r for r in bai2._fold(text()) if r.code == "16"][0]
        one = bai2.detail(record.values)
        self.assertEqual(one.funds, "V")
        self.assertEqual(one.availability, ("060316", ""))
        self.assertEqual(one.reference, "", "blank here, and not the date")
        self.assertNotEqual(one.reference, "060316")
        self.assertEqual(one.text, "RETURNED CHEQUE     ")
        self.assertEqual(one.amount, 2500)

    def test_the_value_error_is_gone_and_a_refusal_is_in_its_place(self):
        # `_amounts_in` raised `ValueError: invalid literal for int()` on a real
        # 03. It reads it now; and where a record genuinely does not fit, the
        # refusal is an `Unreadable`, which is a `Wrong`.
        group = [line for line in text().split("\n")
                 if line.startswith("88,")][0]
        as_an_03 = "03,10200123456,CAD," + group.split(",", 1)[1]
        self.assertEqual(bai2._amounts_in(as_an_03), [208500, 208500])
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2.read("03,1,CAD,010,100,,D,4/\n")
        self.assertIsInstance(refused.exception, bai2.Wrong)

    def test_an_amount_may_be_signed_or_bare(self):
        # sample1 signs every control total; sample2 writes them bare and puts
        # `010,+4350000` and `040,2830000` in one record. A reader that required
        # either would refuse half of the files in existence.
        signs = set()
        for name in (SAMPLE, TWO):
            for record in bai2._fold(text(name)):
                if record.code in ("49", "98", "99"):
                    signs.add("signed" if record.values[0][:1] in "+-" else "bare")
                if record.code == "03":
                    for group in bai2.summary_groups(record.values):
                        self.assertIsInstance(group.amount, int)
        self.assertEqual(signs, {"signed", "bare"},
                         "both forms must be present for this to prove anything")
        self.assertEqual(bai2._int("+4350000"), 4350000)
        self.assertEqual(bai2._int("2830000"), 2830000)
        self.assertEqual(bai2._int("-500000"), -500000)

    def test_a_continuation_with_nothing_before_it_is_still_refused(self):
        # Folding is not the same as accepting anything. A file that opens with a
        # continuation has no record for it to continue.
        with self.assertRaises(bai2.Unreadable) as refused:
            bai2.read("88,100,200/\n")
        self.assertIn("no record before it", str(refused.exception))

    def test_what_reads_cannot_always_be_reduced_to_a_statement(self):
        # `read` takes any BAI2 file; `statements` needs the two ledger balances.
        # Neither sample states both, and saying which is missing beats a KeyError.
        for name, missing in ((SAMPLE, "opening"), (TWO, "closing")):
            with self.subTest(os.path.basename(name)):
                with self.assertRaises(bai2.Unreadable) as refused:
                    bai2.statements(text(name))
                self.assertIn(missing, str(refused.exception))
                self.assertIn("camt.053", str(refused.exception))


class WhatWasStillNotRead(unittest.TestCase):
    """`WhatIsStillNotRead` inverted (#128). Same contract, other side.

    Those three tests asserted that `read` could not take a `16` whose text
    contains commas, records packed several to a line, or a record with no
    terminator. Each was written to be changed by the work that fixed it rather
    than deleted, so here is each one from the other direction, against the files
    that prompted it rather than against a line I made up.
    """

    def test_a_text_field_containing_commas_is_one_field(self):
        # sample4's 16s carry free text with commas in it. The text is the last
        # field of a record and BAI2 has no escape, so it runs to the terminator.
        one = [bai2.detail(r.values) for r in bai2._fold(text(FOUR))
               if r.code == "16"][0]
        self.assertEqual(one.type_code, "447")
        self.assertEqual(one.amount, 60000)
        self.assertEqual(one.reference, "SPB2322984714570")
        self.assertIn(",", one.text, "the commas are inside one field")
        self.assertTrue(one.text.startswith("ACH Credit Payment,"), one.text)

    def test_records_packed_onto_one_line_are_all_read(self):
        # sample3 puts as many as three records on a line, separated by `/`.
        packed = [line for line in text(THREE).splitlines()
                  if line.rstrip().rstrip("/").count("/") > 1]
        self.assertTrue(packed, "no packed line to read")
        found = [r for r in bai2._records(text(THREE))]
        self.assertGreater(len(found), len(text(THREE).strip().splitlines()),
                           "more records than lines, which is the point")
        self.assertEqual(len(bai2.read(text(THREE))), 60)

    def test_a_record_with_no_terminator_is_read(self):
        # sample4 leaves 102 of its 116 lines unterminated; the newline ends them.
        lines = [l for l in text(FOUR).splitlines() if l.strip()]
        unterminated = [l for l in lines if not l.rstrip().endswith("/")]
        self.assertEqual(len(unterminated), 102)
        self.assertEqual(len(bai2.read(text(FOUR))), 31)

    def test_every_vendored_sample_reads(self):
        # The flat statement, so a regression in any one of them is one failure
        # with a name rather than five scattered ones.
        for path in ALL:
            with self.subTest(os.path.basename(path)):
                self.assertTrue(bai2.read(text(path)))


class HowARecordEnds(unittest.TestCase):
    """The rule #128 settled, and the two readings it had to rule out.

    A `/` is not a delimiter to split on and a newline is not one either. A line
    starts a record when it begins with a declared code and a separator, and
    otherwise continues the record above; within a line, a `/` ends a record when
    a declared code and a separator follow it.
    """

    def test_a_slash_inside_a_field_is_not_a_terminator(self):
        # sample5's customer references and remittance text carry slashes. Split
        # on `/` and twenty-two fields in that one file are shattered.
        inside = [r for r in bai2._fold(text(FIVE))
                  if r.code == "16" and "/" in ",".join(r.values)]
        self.assertTrue(inside, "no slash-bearing field to check")
        one = bai2.detail(inside[0].values)
        self.assertIn("/", one.customer_reference + one.text)
        self.assertEqual(len(bai2.read(text(FIVE))), 34)

    def test_a_slash_before_a_record_code_is_a_terminator(self):
        line = "16,142,2500,Z,,,FIRST/ 16,142,500,Z,,,SECOND/"
        got = [piece for _, piece in bai2._records(line + "\n")]
        self.assertEqual(len(got), 2, got)
        self.assertTrue(got[0].endswith("FIRST"), got[0])
        self.assertTrue(got[1].endswith("SECOND"), got[1])

    def test_a_slash_before_two_digits_that_are_not_a_code_is_content(self):
        """The near miss, and it has to carry a comma to be one.

        A first version of this used `08/18/23 Invoice`, taken from sample5. That
        does not distinguish anything: `/18` is followed by `/`, not a separator,
        so even a rule of "any two digits then a comma" leaves it alone - and a
        mutation weakening the codes to `\\d\\d` passed the whole suite.

        The case that separates them needs a slash, two digits **and** a comma:
        `1/23,456` in free text. `23,` is a separator-terminated pair of digits
        and is not a declared code, so this splits under the weaker rule and not
        under the real one.
        """
        line = "16,495,30000000,,GI23,3785726,Wire,Payment for 1/23,456 units/"
        got = [piece for _, piece in bai2._records(line + "\n")]
        self.assertEqual(len(got), 1, got)
        self.assertIn("1/23,456 units", got[0])
        # And sample5's own, which is why the file is vendored even though it
        # cannot tell the two rules apart on its own.
        real = "16,495,30000000,,GI2323300009168,3785726,Wire,\"08/18/23 Invoice\"/"
        self.assertEqual(len([p for _, p in bai2._records(real + "\n")]), 1)

    def test_a_line_that_does_not_start_with_a_code_continues_the_one_above(self):
        # sample3 wraps a 16's text onto a second line that carries the
        # terminator; sample5 writes a continuation as `88:EREF: ...`, with a
        # colon where the separator should be. Treating every newline as a
        # terminator makes those records whose codes are `111111111111111` and
        # `88:EREF: 07370568132`.
        for path, orphan in ((THREE, "111111111111111"),
                             (FIVE, "88:EREF: 07370568132")):
            with self.subTest(os.path.basename(path)):
                self.assertIn(orphan, text(path), "the sample changed")
                codes = {r.code for r in bai2._fold(text(path))}
                self.assertTrue(codes <= set(bai2.RECORDS), sorted(codes))
                self.assertNotIn(orphan.split(":")[0] + ":", codes)

    def test_field_padding_is_content_and_is_kept(self):
        # `RETURNED CHEQUE     ` is twenty characters in a fixed-width field.
        # Stripping whitespace off a record's end took five of them.
        one = bai2.detail([r for r in bai2._fold(text()) if r.code == "16"][0].values)
        self.assertEqual(one.text, "RETURNED CHEQUE     ")

    def test_no_record_the_file_contains_is_dropped(self):
        """The guard on the whole rule, counted a second way.

        Every place a declared code begins a record - at the start of a line or
        after a `/` - is accounted for, so a splitter that quietly swallowed one
        fails here rather than producing a smaller, tidier, wrong answer.

        Counted with its own expression rather than by calling `_records`, which
        would be asking the splitter whether the splitter is right. An earlier
        version counted coded *lines*, which undercounts `sample3` by the eleven
        records it packs onto lines that already had one.
        """
        starts = re.compile(r"(?:^|/[ \t]*)(?:%s)," % "|".join(
            sorted(set(bai2.RECORDS) | {bai2.CONTINUATION})), re.M)
        for path in ALL:
            with self.subTest(os.path.basename(path)):
                self.assertEqual(sum(r.lines for r in bai2._fold(text(path))),
                                 len(starts.findall(text(path))))


class TheTwoFixturesWhoseOwnArithmeticIsWrong(unittest.TestCase):
    """`sample4` and `sample5` read and do not reconcile, and that is the file.

    Pinned because the tempting conclusion is that the reader is wrong. It is
    not: every coded line in both is accounted for (the test above), and the
    files state totals that contradict themselves. They are moov-io's parser
    fixtures - one is named for a bug report - so they exercise reading rather
    than arithmetic.
    """

    def test_three_of_the_five_reconcile(self):
        for path in RECONCILING:
            with self.subTest(os.path.basename(path)):
                self.assertEqual(bai2.trailers_agree(text(path)), [])

    def test_the_other_two_do_not_and_here_is_why(self):
        for path in (FOUR, FIVE):
            with self.subTest(os.path.basename(path)):
                folded = bai2._fold(text(path))
                totals = [bai2._int(r.values[0]) for r in folded if r.code == "49"]
                stated = bai2._int([r for r in folded if r.code == "98"][0].values[0])
                # One account states a large negative total and two state nought;
                # the group trailer is the sum of the positive ones alone, which
                # no consistent file would be.
                self.assertIn(-1260161341762, totals)
                self.assertNotEqual(sum(totals), stated)
                self.assertEqual(sum(t for t in totals if t > 0), stated)
                self.assertTrue(bai2.trailers_agree(text(path)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
