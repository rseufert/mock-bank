"""The ISO 20022 dictionary: what it serves, how it walks a file, how it builds one."""
import datetime
import json
import os
import re
import unittest
from xml.etree import ElementTree as ET

from support import MockServerCase

from mockbank import schema

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
CLEAN = os.path.join(SAMPLES, "pain001_four_payments.xml")
PAIN001 = schema.MESSAGES["pain.001.001.09"]
BODY = "/Document/CstmrCdtTrfInitn"
UTC = datetime.timezone.utc


def sample_text():
    with open(CLEAN, encoding="utf-8") as handle:
        return handle.read()


def parse(text):
    return ET.fromstring(text.encode("utf-8"))


def errors(findings):
    return [f for f in findings if f.level == "error"]


class DictionaryEndpoint(MockServerCase):

    def test_index_names_every_message_and_the_choices_made(self):
        body = self.get("/_mock/dictionary").json()
        self.assertEqual(set(body["messages"]), set(schema.MESSAGES))
        self.assertEqual(body["messages"]["pain.001.001.09"]["namespace"],
                         "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09")
        for code in ("AC04", "AM04", "RC01", "FF01", "DUPL", "MD07"):
            self.assertIn(code, body["code_sets"]["ExternalStatusReason1Code"])
        for code in ("ACCP", "RJCT", "PART", "ACSP", "ACSC"):
            self.assertIn(code, body["code_sets"]["ExternalPaymentGroupStatus1Code"])
        self.assertIn("OPBD", body["code_sets"]["ExternalBalanceType1Code"])
        self.assertIn("CLBD", body["code_sets"]["ExternalBalanceType1Code"])
        self.assertIn("PMNT/ICDT/ESCT", body["bank_transaction_codes"])
        self.assertIn("bank_transaction_code", body["choices"])

    def test_a_message_is_served_as_its_declaration(self):
        resp = self.get("/_mock/dictionary/pain.001.001.09")
        self.assertEqual(resp.status, 200)
        body = resp.json()
        self.assertEqual(body, json.loads(json.dumps(PAIN001.to_json())))
        document = body["document"]
        self.assertEqual(document["name"], "Document")
        initiation = document["children"][0]
        self.assertEqual(initiation["name"], "CstmrCdtTrfInitn")
        self.assertEqual([c["name"] for c in initiation["children"]], ["GrpHdr", "PmtInf"])
        self.assertEqual(initiation["children"][1]["max"], "unbounded")

    def test_every_message_is_served(self):
        for name in schema.MESSAGES:
            with self.subTest(name):
                self.assertEqual(self.get("/_mock/dictionary/" + name).status, 200)

    def test_an_unknown_message_names_the_ones_spoken(self):
        resp = self.get("/_mock/dictionary/pacs.008.001.08")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.json()["messages"], sorted(schema.MESSAGES))

    def test_the_readme_documents_the_endpoint(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        self.assertTrue("GET /_mock/dictionary/<message>" in readme,
                        "the README's endpoint table has no row for the dictionary")
        self.assertIn("GET /_mock/dictionary", self.get("/").body.decode("utf-8"))


class Walker(unittest.TestCase):
    """The walker on its own; POST /_mock/validate (#5) puts it on the wire."""

    def test_the_clean_sample_walks_without_a_finding(self):
        root = parse(sample_text())
        self.assertIs(schema.identify(root), PAIN001)
        findings = []
        visited = list(schema.walk(PAIN001, root, findings))
        self.assertEqual(findings, [])
        # every element of the file was visited, so none was skipped unchecked
        self.assertEqual(len(visited), sum(1 for _ in root.iter()))
        self.assertIn(BODY + "/PmtInf[1]/CdtTrfTxInf[4]/Amt/InstdAmt",
                      [path for path, _, _ in visited])

    def test_a_prefixed_namespace_walks_the_same(self):
        text = sample_text().replace('xmlns="', 'xmlns:p="')
        text = re.sub(r"<(/?)([A-Z])", r"<\1p:\2", text)
        self.assertIn("<p:CstmrCdtTrfInitn>", text)
        root = parse(text)
        self.assertEqual(schema.check(PAIN001, root), [])
        self.assertEqual(schema.read(PAIN001, root), schema.read(PAIN001, parse(sample_text())))

    def test_an_element_out_of_order_is_named_by_its_path(self):
        text = sample_text()
        msg_id = "<MsgId>ACME-20261001-0001</MsgId>"
        created = "<CreDtTm>2026-09-30T09:30:00+02:00</CreDtTm>"
        text = text.replace(msg_id + "\n      " + created, created + "\n      " + msg_id)
        findings = schema.check(PAIN001, parse(text))
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].path, BODY + "/GrpHdr/MsgId")
        self.assertEqual(findings[0].code, "FF01")
        self.assertIn("out of order", findings[0].text)

    def test_an_element_in_the_wrong_namespace_is_named_by_its_path(self):
        text = sample_text().replace('<InstdAmt Ccy="EUR">980.25',
                                     '<InstdAmt xmlns="urn:example:other" Ccy="EUR">980.25')
        findings = schema.check(PAIN001, parse(text))
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].path, BODY + "/PmtInf[1]/CdtTrfTxInf[3]/Amt/InstdAmt")
        self.assertIn("urn:example:other", findings[0].text)

    def test_a_missing_required_element_is_named(self):
        text = sample_text().replace("<EndToEndId>INV-2026-0102</EndToEndId>", "")
        findings = schema.check(PAIN001, parse(text))
        self.assertEqual([(f.path, f.code) for f in findings],
                         [(BODY + "/PmtInf[1]/CdtTrfTxInf[2]/PmtId/EndToEndId", "FF01")])

    def test_a_choice_holds_exactly_one(self):
        text = sample_text().replace("<IBAN>NL14MOCK0000000002</IBAN>",
                                     "<IBAN>NL14MOCK0000000002</IBAN><Othr><Id>2</Id></Othr>")
        findings = schema.check(PAIN001, parse(text))
        self.assertEqual([f.path for f in findings],
                         [BODY + "/PmtInf[1]/CdtTrfTxInf[1]/CdtrAcct/Id"])

    def test_values_are_checked_against_their_type(self):
        cases = {
            "<IBAN>NL14MOCK0000000002</IBAN>": "<IBAN>not an iban</IBAN>",
            '<InstdAmt Ccy="EUR">1250.00</InstdAmt>': '<InstdAmt Ccy="EUR">1250.001</InstdAmt>',
            "<Dt>2026-10-01</Dt>": "<Dt>2026-02-30</Dt>",
            "<PmtMtd>TRF</PmtMtd>": "<PmtMtd>WIRE</PmtMtd>",
            "<NbOfTxs>4</NbOfTxs>": "<NbOfTxs>four</NbOfTxs>",
            "<CreDtTm>2026-09-30T09:30:00+02:00</CreDtTm>": "<CreDtTm>2026-09-30 09:30</CreDtTm>",
        }
        for good, bad in cases.items():
            with self.subTest(bad):
                findings = schema.check(PAIN001, parse(sample_text().replace(good, bad, 1)))
                self.assertEqual(len(errors(findings)), 1, findings)

    def test_an_undeclared_element_is_a_warning_not_silence(self):
        text = sample_text().replace("<ChrgBr>SLEV</ChrgBr>",
                                     "<ChrgBr>SLEV</ChrgBr><ChrgsAcct><Id><IBAN>X</IBAN></Id></ChrgsAcct>")
        findings = schema.check(PAIN001, parse(text))
        self.assertEqual([(f.level, f.path) for f in findings],
                         [("warning", BODY + "/PmtInf[1]/ChrgsAcct")])

    def test_the_2009_version_reads_into_the_same_mapping(self):
        text = (sample_text().replace("pain.001.001.09", "pain.001.001.03")
                .replace("BICFI>", "BIC>")
                .replace("<ReqdExctnDt>\n        <Dt>2026-10-01</Dt>\n      </ReqdExctnDt>",
                         "<ReqdExctnDt>2026-10-01</ReqdExctnDt>"))
        root = parse(text)
        old = schema.identify(root)
        self.assertEqual(old.name, "pain.001.001.03")
        self.assertEqual(schema.check(old, root), [])
        then = schema.read(old, root)["CstmrCdtTrfInitn"]["PmtInf"][0]
        now = schema.read(PAIN001, parse(sample_text()))["CstmrCdtTrfInitn"]["PmtInf"][0]
        self.assertEqual(then["DbtrAgt"], now["DbtrAgt"])
        self.assertEqual(then["CdtTrfTxInf"], now["CdtTrfTxInf"])
        # the .09 BICFI is not a .03 element: read past, and said so, once per agent
        findings = schema.check(old, parse(text.replace("BIC>", "BICFI>")))
        self.assertEqual({(f.level, f.path.rsplit("/", 1)[1]) for f in findings},
                         {("warning", "BICFI")})
        self.assertEqual(len(findings), 5)

    def test_non_ascii_survives_reading(self):
        payments = schema.read(PAIN001, parse(sample_text()))["CstmrCdtTrfInitn"]["PmtInf"][0]
        self.assertEqual(payments["CdtTrfTxInf"][2]["Cdtr"]["Nm"], "Eurodis Handels GmbH Düsseldorf")


class Builder(unittest.TestCase):

    def test_the_sample_round_trips(self):
        mapping = schema.read(PAIN001, parse(sample_text()))
        written = schema.serialize(PAIN001, mapping)
        self.assertTrue(written.startswith(b"<?xml"))
        root = ET.fromstring(written)
        self.assertEqual(root.tag, "{%s}Document" % PAIN001.namespace)
        self.assertEqual(schema.check(PAIN001, root), [])
        self.assertEqual(schema.read(PAIN001, root), mapping)

    def test_elements_come_out_in_declared_order_whatever_the_mapping_order(self):
        mapping = schema.read(PAIN001, parse(sample_text()))
        header = mapping["CstmrCdtTrfInitn"]["GrpHdr"]
        mapping["CstmrCdtTrfInitn"]["GrpHdr"] = dict(reversed(list(header.items())))
        root = ET.fromstring(schema.serialize(PAIN001, mapping))
        names = [schema.split_tag(e.tag)[1] for e in root[0][0]]
        self.assertEqual(names, ["MsgId", "CreDtTm", "NbOfTxs", "CtrlSum", "InitgPty"])

    def test_a_mapping_that_does_not_fit_is_the_writers_bug(self):
        mapping = schema.read(PAIN001, parse(sample_text()))
        header = mapping["CstmrCdtTrfInitn"]["GrpHdr"]
        with self.assertRaisesRegex(ValueError, "GrpHdr/MsgId is required"):
            schema.build(PAIN001, _replace(mapping, header, MsgId=None))
        with self.assertRaisesRegex(ValueError, "declares no MsgIdent"):
            schema.build(PAIN001, _replace(mapping, header, MsgIdent="x"))
        with self.assertRaisesRegex(ValueError, "carries its zone"):
            schema.build(PAIN001, _replace(mapping, header,
                                           CreDtTm=datetime.datetime(2026, 10, 1, 9, 0)))

    def test_a_statement_builds_with_balances_before_entries(self):
        camt053 = schema.MESSAGES["camt.053.001.08"]
        day = datetime.date(2026, 10, 1)
        entry = {"Amt": schema.Amount(125000, "EUR"), "CdtDbtInd": "DBIT",
                 "Sts": {"Cd": "BOOK"}, "BookgDt": {"Dt": day}, "ValDt": {"Dt": day},
                 "BkTxCd": {"Domn": {"Cd": "PMNT", "Fmly": {"Cd": "ICDT", "SubFmlyCd": "ESCT"}}},
                 "NtryDtls": [{"TxDtls": [{"Refs": {"EndToEndId": "INV-2026-0101"}}]}]}
        balance = {"Tp": {"CdOrPrtry": {"Cd": "OPBD"}}, "Amt": schema.Amount(1000000, "EUR"),
                   "CdtDbtInd": "CRDT", "Dt": {"Dt": day}}
        statement = {"Ntry": [entry], "Bal": [balance], "Id": "STMT-1", "ElctrncSeqNb": 1,
                     "CreDtTm": datetime.datetime(2026, 10, 1, 23, 59, tzinfo=UTC),
                     "Acct": {"Id": {"IBAN": "NL41MOCK0000000001"}, "Ccy": "EUR"}}
        mapping = {"BkToCstmrStmt": {
            "GrpHdr": {"MsgId": "STMT-1", "CreDtTm": datetime.datetime(2026, 10, 1, 23, 59, tzinfo=UTC)},
            "Stmt": [statement]}}
        root = ET.fromstring(schema.serialize(camt053, mapping))
        self.assertEqual(schema.check(camt053, root), [])
        names = [schema.split_tag(e.tag)[1] for e in root[0][1]]
        self.assertLess(names.index("Bal"), names.index("Ntry"))
        amount = root.find(".//{%s}Ntry/{%s}Amt" % (camt053.namespace, camt053.namespace))
        self.assertEqual((amount.text, amount.get("Ccy")), ("1250.00", "EUR"))
        self.assertTrue(root.find(".//{%s}CreDtTm" % camt053.namespace).text.endswith("+00:00"))

    def test_amounts_use_the_currencys_minor_units(self):
        self.assertEqual(schema.format_amount(125000, "EUR"), "1250.00")
        self.assertEqual(schema.format_amount(5, "EUR"), "0.05")
        self.assertEqual(schema.format_amount(1250, "JPY"), "1250")
        self.assertEqual(schema.format_amount(1250, "KWD"), "1.250")
        self.assertEqual(schema.parse_amount("1250.5", "EUR"), 125050)
        self.assertEqual(schema.parse_amount("1250", "EUR"), 125000)
        self.assertIsNone(schema.parse_amount("1250.001", "EUR"))
        self.assertIsNone(schema.parse_amount("1250.5", "JPY"))
        self.assertIsNone(schema.parse_amount("-1", "EUR"))
        with self.assertRaises(ValueError):
            schema.format_amount(12.5, "EUR")


def _replace(mapping, header, **changes):
    """The mapping with its group header changed; the original is untouched."""
    changed = dict(header)
    for key, value in changes.items():
        if value is None:
            changed.pop(key, None)
        else:
            changed[key] = value
    body = dict(mapping["CstmrCdtTrfInitn"], GrpHdr=changed)
    return {"CstmrCdtTrfInitn": body}


if __name__ == "__main__":
    unittest.main()
