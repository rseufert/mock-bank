"""POST /_mock/validate: the reader and the validator, over the wire."""
import os
import random
import re

from support import MockServerCase

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
BODY = "/Document/CstmrCdtTrfInitn"
PAYMENT = BODY + "/PmtInf[1]/CdtTrfTxInf[%d]"

# The samples ask for execution on 2026-10-01; ValidateCase pins bank time
# the day before, so that date is never in the past.
SAMPLE_DATE = "2026-10-01"



def sample(name):
    with open(os.path.join(SAMPLES, name), encoding="utf-8") as handle:
        return handle.read()


# Each broken sample and the one finding its name promises:
# (level, code, path, a phrase the prose must contain).
BROKEN = {
    "pain001_broken_ctrlsum.xml": ("error", "AM10", BODY + "/GrpHdr/CtrlSum", "20630.75"),
    "pain001_broken_nboftxs.xml": ("error", "AM18", BODY + "/GrpHdr/NbOfTxs", "holds 4"),
    "pain001_broken_iban.xml": ("error", "AC01", PAYMENT % 2 + "/CdtrAcct/Id/IBAN", "check digits"),
    "pain001_broken_currency.xml": ("error", "AM03", PAYMENT % 3 + "/Amt/InstdAmt", "USD"),
    "pain001_broken_past_date.xml": ("warning", "DT01", BODY + "/PmtInf[1]/ReqdExctnDt", "2020-01-02"),
    "pain001_broken_duplicate_end_to_end_id.xml": (
        "error", "AM05", PAYMENT % 4 + "/PmtId/EndToEndId", "INV-2026-0101"),
    "pain001_broken_empty_batch.xml": ("error", "FF01", BODY + "/PmtInf[2]/CdtTrfTxInf", "CdtTrfTxInf"),
    "pain001_broken_order.xml": ("error", "FF01", BODY + "/GrpHdr/MsgId", "out of order"),
    "pain001_broken_namespace.xml": ("error", "FF01", PAYMENT % 3 + "/Amt/InstdAmt", "urn:example:other"),
    "pain001_broken_signed.xml": ("error", "FF01", "/", "signature"),
    "pain001_broken_unknown_message.xml": ("error", "FF01", "/", "pain.008.001.08"),
    "pain001_broken_not_xml.xml": ("error", "FF01", "/", "not well-formed XML"),
}


class ValidateCase(MockServerCase):
    # Bank time pinned the day before the samples' execution date, so the
    # clean files are clean whatever day the suite runs.
    config_kwargs = {"clock": "2026-09-30T09:00"}

    def validate(self, body, **headers):
        return self.post("/_mock/validate", body=body, headers=headers or None)

    def findings(self, body, **headers):
        headers["Accept"] = "application/json"
        resp = self.validate(body, **headers)
        return resp, resp.json()["findings"]

    def reading(self, body):
        return self.validate(body, Accept="application/json").json()["file"]


class CleanFiles(ValidateCase):

    def test_the_clean_sample_has_no_findings(self):
        resp = self.validate(sample("pain001_four_payments.xml"))
        self.assertEqual(resp.status, 200)
        self.assertIn("text/plain", resp.headers["Content-Type"])
        self.assertEqual(resp.body.decode("utf-8").splitlines(), [
            "pain.001.001.09 ACME-20261001-0001: 1 batch, 4 payments, 0 findings"])

    def test_it_reads_into_four_payments_that_sum_to_the_control_sum(self):
        text = sample("pain001_four_payments.xml")
        reading = self.reading(text)
        payments = reading["batches"][0]["payments"]
        self.assertEqual(len(payments), 4)
        self.assertEqual([p["end_to_end_id"] for p in payments],
                         ["INV-2026-0101", "INV-2026-0102", "INV-2026-0103", "INV-2026-0104"])
        self.assertTrue(all(isinstance(p["amount"], int) for p in payments))
        # the control sum as the file states it, against the amounts as read
        control = re.search(r"<CtrlSum>([0-9.]+)</CtrlSum>", text).group(1)
        whole, cents = control.split(".")
        self.assertEqual(sum(p["amount"] for p in payments), int(whole) * 100 + int(cents))
        self.assertEqual(payments[1]["remittance_references"], ["INV-2026-0102"])
        self.assertEqual(payments[0]["creditor_account"], "NL14MOCK0000000002")
        self.assertEqual(payments[0]["creditor_bic"], "MOCKNL2A")
        batch = reading["batches"][0]
        self.assertEqual(batch["requested_execution_date"], SAMPLE_DATE)
        self.assertEqual((batch["debtor_account"], batch["debtor_account_currency"]),
                         ("NL41MOCK0000000001", "EUR"))

    def test_the_2009_twin_reads_into_the_same_model(self):
        new = self.reading(sample("pain001_four_payments.xml"))
        resp, findings = self.findings(sample("pain001_four_payments_001_03.xml"))
        self.assertEqual((resp.status, findings), (200, []))
        old = self.reading(sample("pain001_four_payments_001_03.xml"))
        self.assertEqual(old["message"], "pain.001.001.03")
        old["message"] = new["message"]
        self.assertEqual(old, new)

    def test_a_prefixed_namespace_reads_the_same(self):
        text = sample("pain001_four_payments.xml").replace('xmlns="', 'xmlns:p="')
        text = re.sub(r"<(/?)([A-Z])", r"<\1p:\2", text)
        self.assertIn("<p:CstmrCdtTrfInitn>", text)
        resp, findings = self.findings(text)
        self.assertEqual((resp.status, findings), (200, []))
        self.assertEqual(self.reading(text), self.reading(sample("pain001_four_payments.xml")))

    def test_non_ascii_survives_as_utf8_and_as_declared_latin1(self):
        text = sample("pain001_four_payments.xml")
        name = self.reading(text)["batches"][0]["payments"][2]["creditor_name"]
        self.assertEqual(name, "Eurodis Handels GmbH Düsseldorf")
        latin1 = text.replace('encoding="UTF-8"', 'encoding="ISO-8859-1"').encode("latin-1")
        self.assertNotIn("Düsseldorf".encode("utf-8"), latin1)
        reading = self.validate(latin1, Accept="application/json").json()["file"]
        self.assertEqual(reading["batches"][0]["payments"][2]["creditor_name"], name)
        self.assertEqual(reading["batches"][0]["payments"][2]["remittance"],
                         ["Rechnung INV-2026-0103, Lieferung Straße 5"])

    def test_nothing_is_stored(self):
        before = self.get("/_mock/state").json()
        self.validate(sample("pain001_four_payments.xml"))
        after = self.get("/_mock/state").json()
        self.assertEqual({k: v for k, v in after.items() if k != "requests"},
                         {k: v for k, v in before.items() if k != "requests"})


class BrokenFiles(ValidateCase):

    def test_each_broken_sample_produces_exactly_the_finding_its_name_promises(self):
        on_disk = {n for n in os.listdir(SAMPLES) if n.startswith("pain001_broken_")}
        self.assertEqual(on_disk, set(BROKEN), "a broken sample without an expectation, or the reverse")
        for name, (level, code, path, phrase) in sorted(BROKEN.items()):
            with self.subTest(name):
                resp, findings = self.findings(sample(name))
                self.assertEqual([(f["level"], f["code"], f["path"]) for f in findings],
                                 [(level, code, path)])
                self.assertIn(phrase, findings[0]["text"])
                self.assertEqual(resp.status, 422 if level == "error" else 200)

    def test_a_finding_renders_as_one_line_of_prose(self):
        resp = self.validate(sample("pain001_broken_iban.xml"))
        self.assertEqual(resp.status, 422)
        lines = resp.body.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "pain.001.001.09 ACME-20261001-0001: 1 batch, 4 payments, 1 finding")
        self.assertEqual(lines[1], "error AC01 at %s/CdtrAcct/Id/IBAN: NL85MOCK0000000003 fails "
                                   "its check digits" % (PAYMENT % 2))

    def test_a_file_the_mock_cannot_read_is_one_line_naming_what_it_reads(self):
        for name in ("pain001_broken_not_xml.xml", "pain001_broken_unknown_message.xml"):
            with self.subTest(name):
                lines = self.validate(sample(name)).body.decode("utf-8").splitlines()
                self.assertEqual(len(lines), 1)
                self.assertIn("pain.001.001.09, pain.001.001.03", lines[0])

    def test_a_message_the_mock_writes_is_not_one_it_reads(self):
        text = sample("pain001_four_payments.xml").replace("pain.001.001.09", "camt.053.001.08")
        _, findings = self.findings(text)
        self.assertEqual(len(findings), 1)
        self.assertIn("the mock writes, it does not read", findings[0]["text"])

    def test_an_encrypted_body_is_refused_by_its_content_type(self):
        resp, findings = self.findings(b"\x30\x82\x01\x0a", **{"Content-Type": "application/pkcs7-mime"})
        self.assertEqual(resp.status, 422)
        self.assertEqual(len(findings), 1)
        self.assertIn("application/pkcs7-mime", findings[0]["text"])

    def test_a_dtd_is_refused_not_expanded(self):
        text = sample("pain001_four_payments.xml").replace(
            "<Document", '<!DOCTYPE Document [<!ENTITY a "aaaaaaaaaa">]>\n<Document', 1)
        _, findings = self.findings(text)
        self.assertEqual([(f["path"], f["code"]) for f in findings], [("/", "FF01")])
        self.assertIn("DTD", findings[0]["text"])

    def test_a_missing_required_element_is_named(self):
        text = sample("pain001_four_payments.xml").replace("<EndToEndId>INV-2026-0102</EndToEndId>", "")
        _, findings = self.findings(text)
        self.assertEqual([(f["path"], f["code"]) for f in findings],
                         [(PAYMENT % 2 + "/PmtId/EndToEndId", "FF01")])

    def test_a_choice_holds_exactly_one(self):
        text = sample("pain001_four_payments.xml").replace(
            "<IBAN>NL14MOCK0000000002</IBAN>", "<IBAN>NL14MOCK0000000002</IBAN><Othr><Id>2</Id></Othr>")
        _, findings = self.findings(text)
        self.assertEqual([f["path"] for f in findings], [PAYMENT % 1 + "/CdtrAcct/Id"])

    def test_values_are_checked_against_their_type(self):
        cases = {
            "<IBAN>NL14MOCK0000000002</IBAN>": "<IBAN>not an iban</IBAN>",
            '<InstdAmt Ccy="EUR">1250.00</InstdAmt>': '<InstdAmt Ccy="EUR">1250.001</InstdAmt>',
            "<Dt>%s</Dt>" % SAMPLE_DATE: "<Dt>2026-02-30</Dt>",
            "<PmtMtd>TRF</PmtMtd>": "<PmtMtd>WIRE</PmtMtd>",
            "<CreDtTm>2026-09-30T09:30:00+02:00</CreDtTm>": "<CreDtTm>2026-09-30 09:30</CreDtTm>",
        }
        for good, bad in cases.items():
            with self.subTest(bad):
                resp, findings = self.findings(sample("pain001_four_payments.xml").replace(good, bad, 1))
                self.assertEqual(resp.status, 422)
                self.assertEqual([f["level"] for f in findings], ["error"], findings)

    def test_an_undeclared_element_is_a_warning_not_silence(self):
        text = sample("pain001_four_payments.xml").replace(
            "<ChrgBr>SLEV</ChrgBr>", "<ChrgBr>SLEV</ChrgBr><ChrgsAcct><Id><IBAN>X</IBAN></Id></ChrgsAcct>")
        resp, findings = self.findings(text)
        self.assertEqual(resp.status, 200)
        self.assertEqual([(f["level"], f["path"]) for f in findings],
                         [("warning", BODY + "/PmtInf[1]/ChrgsAcct")])


class NeverRaises(ValidateCase):
    """Whatever arrives, the answer is findings - never a 500, never 'a bug'."""

    def assert_findings(self, body, **headers):
        resp, findings = self.findings(body, **headers)
        self.assertIn(resp.status, (200, 422))
        for finding in findings:
            self.assertNotIn("bug in the mock", finding["text"])
        return findings

    def test_an_empty_body(self):
        findings = self.assert_findings(b"")
        self.assertEqual(len(findings), 1)
        self.assertIn("empty", findings[0]["text"])

    def test_random_bytes(self):
        rng = random.Random(20261001)
        for n in range(40):
            with self.subTest(n):
                self.assertTrue(self.assert_findings(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 2048)))))

    def test_mangled_versions_of_the_clean_file(self):
        rng = random.Random(9)
        text = sample("pain001_four_payments.xml").encode("utf-8")
        for n in range(40):
            with self.subTest(n):
                cut = rng.randrange(len(text))
                self.assert_findings(text[:cut] + text[cut + rng.randrange(1, 40):])

    def test_a_ten_megabyte_file(self):
        text = sample("pain001_four_payments.xml")
        start = text.index("<CdtTrfTxInf>")
        end = text.rindex("</CdtTrfTxInf>") + len("</CdtTrfTxInf>")
        payments = text[start:end]
        copies = 10 * 1024 * 1024 // len(payments) + 1
        big = text[:start] + payments * copies + text[end:]
        self.assertGreater(len(big.encode("utf-8")), 10 * 1024 * 1024)
        findings = self.assert_findings(big)
        codes = {f["code"] for f in findings}
        # the counts, the sums and the repeated EndToEndIds are all noticed
        self.assertEqual(codes, {"AM18", "AM10", "AM05"})
