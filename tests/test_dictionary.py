"""The ISO 20022 dictionary: what it serves, and how it builds a message."""
import datetime
import json
import os
import unittest
from xml.etree import ElementTree as ET

from support import MockServerCase

from mockbank import schema

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
CLEAN = os.path.join(SAMPLES, "pain001_four_payments.xml")
PAIN001 = schema.MESSAGES["pain.001.001.09"]
UTC = datetime.timezone.utc


def sample_text():
    with open(CLEAN, encoding="utf-8") as handle:
        return handle.read()


def parse(text):
    return ET.fromstring(text.encode("utf-8"))


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


class Builder(unittest.TestCase):
    """The builder on its own; the writers (#7) put it on the wire, and the
    walker's cases are in test_validate.py, over POST /_mock/validate."""

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
