"""NACHA: a record is 94 bytes, and a file is whole blocks of ten (#162).

Three faults, one rule each. The reader took a byte that is no NACHA character
and said nothing; it never counted the lines of nines after the file control;
and the bank wrote records of 95 and 96 bytes, because a line of 94 characters
is 94 bytes only while every character is ASCII.

The reader's half goes through `POST /_mock/validate`. The writer's half is
measured on the bytes `?raw` serves, which is what a client saves to disk, and
not on the text in the mailbox's JSON, where `é` is one character.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from support import MockServerCase  # noqa: E402
from test_payments import BANK_START  # noqa: E402

from mockbank import nacha  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
NINES = b"9" * 94


def sample(name):
    with open(os.path.join(SAMPLES, name), "rb") as handle:
        return handle.read()


def twin_lines():
    """The clean sample: 8 records and the 2 lines of nines that fill its block."""
    lines = sample("nacha_four_payments_to_the_seed.ach").split(b"\n")[:-1]
    assert len(lines) == 10 and lines[8:] == [NINES, NINES]
    return lines


def file_of(lines, end=b"\n"):
    return end.join(lines) + end


class ReaderCase(MockServerCase):
    config_kwargs = {"clock": "2026-09-30T09:00"}

    def findings(self, body):
        resp = self.post("/_mock/validate", body=body,
                         headers={"Accept": "application/json"})
        found = resp.json()["findings"]
        self.assertEqual(resp.status, 422 if found else 200)
        return found

    def the_one(self, body):
        found = self.findings(body)
        self.assertEqual(len(found), 1, found)
        self.assertEqual((found[0]["level"], found[0]["code"]), ("error", "FF01"))
        return found[0]


class AByteThatIsNotACharacter(ReaderCase):

    def with_name(self, name: bytes):
        """The sample with line 4's individual name, columns 55-76, replaced."""
        lines = twin_lines()
        entry = lines[3]
        self.assertEqual(entry[54:61], b"Initech")
        lines[3] = entry[:54] + name + entry[54 + 22:]
        return file_of(lines)

    def test_a_latin_1_letter_names_the_line_the_column_and_the_field(self):
        body = self.with_name(b"Init\xe9ch Services N.V. ")
        finding = self.the_one(body)
        self.assertEqual(finding["path"], "/line 4 (entry detail)")
        self.assertIn("byte 0xE9 at column 59 (individual name)", finding["text"])

    def test_what_was_read_holds_a_question_mark_not_a_guess(self):
        # Nothing in the file says which encoding 0xE9 is in, so the reading
        # does not say it was Latin-1's e-acute.
        read, _ = nacha.inspect(self.with_name(b"Init\xe9ch Services N.V. "))
        self.assertEqual(read.payments[1].creditor_name, "Init?ch Services N.V.")

    def test_the_same_letter_in_utf_8_is_two_bytes_and_a_line_too_long(self):
        # 22 characters, 23 bytes: what a client gets by padding the name as text
        # and encoding the file afterwards.
        found = self.findings(self.with_name("Initéch Services N.V. ".encode("utf-8")))
        # ...and every field after the name is a column to the right, which is
        # a third finding and why the first one is worth having.
        self.assertEqual([f["path"] for f in found[:2]], ["/line 4 (entry detail)"] * 2)
        self.assertIn("byte 0xC3 at column 59 (individual name), "
                      "byte 0xA9 at column 60 (individual name)", found[0]["text"])
        self.assertIn("95 characters", found[1]["text"])

    def test_a_control_character_is_one_too(self):
        finding = self.the_one(self.with_name(b"Initech\tServices N.V. "))
        self.assertIn("byte 0x09 at column 62 (individual name)", finding["text"])

    def test_tilde_and_space_are_the_ends_of_what_is_allowed(self):
        self.assertEqual(self.findings(self.with_name(b"~ Initech ~ Services ~")), [])
        finding = self.the_one(self.with_name(b"Initech\x7fServices N.V. "))
        self.assertIn("byte 0x7F at column 62", finding["text"])

    def test_a_line_full_of_them_shows_five_and_counts_the_rest(self):
        finding = self.the_one(self.with_name(b"\xff" * 22))
        self.assertEqual(finding["text"].count("byte 0xFF"), 5)
        self.assertIn(", 17 more:", finding["text"])

    def test_the_payments_door_refuses_the_file(self):
        self.post("/_mock/reset")
        resp = self.post("/payments", body=self.with_name(b"Init\xe9ch Services N.V. "))
        self.assertEqual((resp.status, resp.json()["status"], resp.json()["reason"]),
                         (422, "RJCT", "FF01"))


class TheLinesOfNines(ReaderCase):

    def blocking(self, lines, **kwargs):
        finding = self.the_one(file_of(lines, **kwargs))
        self.assertEqual(finding["path"], "/")
        return finding["text"]

    def test_none_where_two_belong(self):
        text = self.blocking(twin_lines()[:8])
        self.assertIn("the file is 8 lines, 8 record(s) and 0 line(s) of nines", text)
        self.assertIn("followed by 2 line(s) of nines to make 10 lines", text)

    def test_one_short(self):
        self.assertIn("9 lines, 8 record(s) and 1 line(s)", self.blocking(twin_lines()[:9]))

    def test_seven_too_many(self):
        text = self.blocking(twin_lines() + [NINES] * 7)
        self.assertIn("the file is 17 lines, 8 record(s) and 9 line(s) of nines", text)

    def test_a_whole_block_of_nines_is_ten_too_many(self):
        # Twenty lines is a multiple of ten, and still not what 8 records need.
        text = self.blocking(twin_lines() + [NINES] * 10)
        self.assertIn("20 lines, 8 record(s) and 12 line(s)", text)

    def test_a_block_count_that_agrees_with_the_padding_does_not_excuse_it(self):
        lines = twin_lines() + [NINES] * 10
        control = lines[7]
        self.assertEqual(control[7:13], b"000001")
        lines[7] = control[:7] + b"000002" + control[13:]
        found = self.findings(file_of(lines))
        self.assertEqual([f["path"] for f in found],
                         ["/line 8 (file control)/block count (columns 8-13)", "/"])

    def test_line_ends_and_blank_lines_are_not_lines(self):
        self.assertEqual(self.findings(file_of(twin_lines(), end=b"\r\n")), [])
        self.assertEqual(self.findings(file_of(twin_lines()) + b"\n\n"), [])

    def test_ten_records_need_no_nines(self):
        # A file that is exactly one block: two more addenda-free entries would
        # change every control, so check the rule where it is computed.
        self.assertEqual(nacha._blocking(10, 10, 0), [])
        self.assertEqual(nacha._blocking(20, 11, 9), [])
        self.assertEqual(len(nacha._blocking(11, 10, 1)), 1)


class WhatTheBankCanWrite(unittest.TestCase):

    def test_an_accent_is_dropped_and_anything_else_is_a_question_mark(self):
        for text, written in (("Müller", "Muller"), ("Initéch", "Initech"),
                              ("Ångström Ñandú", "Angstrom Nandu"),
                              ("Straße", "Stra?e"), ("ﬁn", "?n"), ("日本", "??"),
                              ("a\tb", "a?b"), ("plain ~ ASCII", "plain ~ ASCII")):
            with self.subTest(text):
                self.assertEqual(nacha.writable(text), written)
                self.assertEqual(len(written), len(text), "one for one")

    def test_a_record_is_94_bytes_whatever_it_is_given(self):
        record = nacha.line(nacha.RECORDS["6"], **{
            "record type code": 6, "transaction code": "21",
            "receiving DFI identification": "99999999", "check digit": 2,
            "DFI account number": "0000000003", "amount": 1,
            "individual identification number": "RÉF-1",
            "individual name": "Zoë Ødegård & Søn", "addenda record indicator": 1,
            "trace number": "999999990000001"})
        self.assertEqual(len(record.encode("utf-8")), 94)
        self.assertEqual(record[54:76], "Zoe ?degard & S?n     ")


class NothingTheBankWritesIsNot94Bytes(MockServerCase):
    config_kwargs = {"clock": BANK_START}

    def setUp(self):
        self.post("/_mock/reset")

    def nacha_acme(self, **more):
        resp = self.request("PATCH", "/_mock/accounts/ACME",
                            body=dict({"format": "nacha", "currency": "USD"}, **more))
        self.assertEqual(resp.status, 200, resp.body)

    def the_return_file(self, reads_back=True) -> bytes:
        self.assertEqual(self.post("/_mock/advance?to=2026-10-02").status, 200)
        files = self.get("/_mock/mailbox?type=" + nacha.RETURN).json()
        self.assertEqual(len(files), 1)
        raw = self.get("/_mock/mailbox/%s?raw" % files[0]["id"]).body
        lines = raw.split(b"\n")
        self.assertEqual(lines[-1], b"")
        self.assertEqual([len(line) for line in lines[:-1]], [94] * 10)
        if reads_back:
            self.assertEqual(nacha.inspect(raw)[1], [], "and it reads back with no finding")
        return raw

    def test_an_account_whose_name_is_not_ascii(self):
        self.nacha_acme(name="Müller Maschinenbau")
        self.post("/payments", body=sample("nacha_four_payments_to_the_seed.ach"))
        raw = self.the_return_file()
        self.assertIn(b"Muller Maschinenbau", raw.split(b"\n")[0])
        self.assertIn(b"Muller Maschinen ", raw.split(b"\n")[1])

    def test_a_pain001_whose_creditor_is_not_ascii(self):
        # A NACHA account may send a pain.001, where any name is allowed, and
        # the rejections come back as NACHA return entries with that name.
        self.nacha_acme()
        xml = sample("pain001_four_payments.xml").decode("utf-8")
        self.assertIn("Initech", xml)
        xml = xml.replace("Initech", "Initéch").replace('Ccy="EUR"', 'Ccy="USD"') \
                 .replace("<Ccy>EUR</Ccy>", "<Ccy>USD</Ccy>")
        resp = self.post("/payments", body=xml.encode("utf-8"),
                         headers={"Content-Type": "application/xml"})
        self.assertEqual((resp.status, resp.json()["rejected"]), (202, 2))
        # Not read back: the sample's InstrId is not a number, and the return
        # addenda's original entry trace number is. That is its own fault.
        self.assertIn(b"Initech Services N.V.", self.the_return_file(reads_back=False))


if __name__ == "__main__":
    unittest.main()
