"""The ISO 20022 dictionary: what it serves, how it builds a message, and
GeneratedMessagesAreValid - everything the mock writes, checked against the
dictionary and the dictionary against samples from outside the project."""
import datetime
import glob
import json
import os
import re
import unittest
from xml.etree import ElementTree as ET

from support import MockServerCase

from mockbank import schema
from mockbank.accounts import BEHAVIOURS

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




# -- the mock's own output, and samples from outside the project -----------------

PAIN002 = schema.MESSAGES["pain.002.001.10"]
CAMT054 = schema.MESSAGES["camt.054.001.08"]
CAMT053 = schema.MESSAGES["camt.053.001.08"]
PACS004 = schema.MESSAGES["pacs.004.001.09"]
EXTERNAL = os.path.join(SAMPLES, "external")
ZONED = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


class GeneratedMessagesAreValid(MockServerCase):
    """Every message the mock writes walks against its own dictionary with no
    finding at all - not even a warning, since the mock only writes what it
    declares - for every behaviour an account can have.

    For each behaviour the sample file is sent, the clock is advanced through
    its settlement and a statement, and the mailbox is collected. The kinds and
    numbers of message each behaviour must produce are pinned in EXPECTED, so a
    path that stops producing messages fails rather than passing because it
    produced nothing to check.
    """

    config_kwargs = {"clock": "2026-10-01T09:00"}       # a Thursday, before the cutoff
    FRIDAY = "2026-10-02"

    # behaviour -> (the account it is set on, other fields to PATCH, how many
    # times the sample is sent, {message: how many the bank sends}). The sample
    # pays GLOBEX, INITECH (closed), EURODIS (bad-bank-id) and a creditor at
    # another bank; three open accounts each get Thursday's statement.
    EXPECTED = {
        "accept": ("ACME", {}, 1, {PAIN002: 1, CAMT054: 1, CAMT053: 3}),
        "closed-account": ("GLOBEX", {}, 1, {PAIN002: 1, CAMT054: 1, CAMT053: 3}),
        "insufficient-funds": ("ACME", {"balance": 200000}, 1,
                               {PAIN002: 1, CAMT054: 1, CAMT053: 3}),
        "bad-bank-id": ("GLOBEX", {}, 1, {PAIN002: 1, CAMT054: 1, CAMT053: 3}),
        # a day, not the default three, so the return lands on the Friday
        # this test advances to: both accepted payments come back, in one
        # pacs.004 (one original file) and one camt.054 credit beside the debit
        "return-later": ("ACME", {"parameters": {"days": 1}}, 1,
                         {PAIN002: 1, CAMT054: 2, CAMT053: 3, PACS004: 1}),
        "reject-file": ("ACME", {}, 1, {PAIN002: 1, CAMT053: 3}),
        "duplicate-file": ("ACME", {}, 2, {PAIN002: 2, CAMT054: 1, CAMT053: 3}),
        "silent": ("ACME", {}, 1, {CAMT054: 1, CAMT053: 3}),
        "statement-gap": ("ACME", {}, 1, {PAIN002: 1, CAMT054: 1, CAMT053: 3}),
    }

    def collect_for(self, behaviour):
        account, fields, sends, _ = self.EXPECTED[behaviour]
        self.post("/_mock/reset")
        resp = self.request("PATCH", "/_mock/accounts/" + account,
                            body=dict(fields, behaviour=behaviour))
        self.assertEqual(resp.status, 200, resp.body)
        for _ in range(sends):
            self.post("/payments", body=sample_text())
        self.assertEqual(self.post("/_mock/advance?to=" + self.FRIDAY).status, 200)
        return self.get("/_mock/mailbox").json()

    def test_every_behaviour_is_exercised(self):
        self.assertEqual(set(self.EXPECTED), set(BEHAVIOURS),
                         "a behaviour whose output this test does not check")

    def test_every_message_walks_clean_for_every_behaviour(self):
        checked = 0
        for behaviour in sorted(BEHAVIOURS):
            with self.subTest(behaviour):
                collected = self.collect_for(behaviour)
                counts = {}
                for item in collected:
                    root = ET.fromstring(item["body"].encode("utf-8"))
                    message = schema.identify(root)
                    self.assertIsNotNone(message, item["type"])
                    self.assertEqual(schema.check(message, root), [],
                                     "%s under %s" % (item["type"], behaviour))
                    counts[message] = counts.get(message, 0) + 1
                    checked += 1
                self.assertEqual(counts, self.EXPECTED[behaviour][3])
        self.assertEqual(checked, sum(sum(e[3].values()) for e in self.EXPECTED.values()))

    def test_every_seeded_account_as_a_debtor(self):
        from test_payments import UMBRELLA, pain001
        accounts = self.get("/_mock/accounts").json()
        self.assertEqual(len(accounts), 4)
        for account in accounts:
            with self.subTest(account["id"]):
                self.post("/_mock/reset")
                self.post("/payments", body=pain001(
                    "DEBTOR-" + account["id"], account["iban"], [("E1", 100, UMBRELLA)],
                    when=datetime.date(2026, 10, 1)))
                self.post("/_mock/advance?to=" + self.FRIDAY)
                collected = self.get("/_mock/mailbox").json()
                self.assertIn(PAIN002.name, [m["type"] for m in collected])
                for item in collected:
                    root = ET.fromstring(item["body"].encode("utf-8"))
                    self.assertEqual(schema.check(schema.identify(root), root), [])

    def test_elements_sit_where_the_standard_puts_them(self):
        """Checked by position, which a shared dictionary cannot satisfy by
        accident: these are the standard's rules, not the declaration's."""
        collected = self.collect_for("accept")
        seen = set()
        for item in collected:
            root = ET.fromstring(item["body"].encode("utf-8"))
            message = schema.identify(root)
            seen.add(message.name)
            body = root[0]
            names = [schema.split_tag(e.tag)[1] for e in body]
            self.assertEqual(names[0], "GrpHdr", message.name)
            if message is PAIN002:
                self.assertLess(names.index("OrgnlGrpInfAndSts"),
                                names.index("OrgnlPmtInfAndSts"))
            for report in body[1:]:
                parts = [schema.split_tag(e.tag)[1] for e in report]
                if "Bal" in parts and "Ntry" in parts:
                    self.assertLess(max(i for i, n in enumerate(parts) if n == "Bal"),
                                    min(i for i, n in enumerate(parts) if n == "Ntry"))
            for path, decl, elem in schema.walk(message, root):
                if decl.type == "amount":
                    self.assertTrue(elem.get("Ccy"), path)
                if decl.type == "datetime":
                    self.assertRegex(elem.text, ZONED, path)
        self.assertEqual(seen, {PAIN002.name, CAMT054.name, CAMT053.name})


class ExternalSamples(unittest.TestCase):
    """Files written outside this project, which the published XSD accepts (or,
    in invalid/, rejects). tests/samples/external/SOURCES.md says where each
    came from; tools/check_xsd.py confirms the XSD's verdict."""

    def walk(self, path):
        root = ET.parse(path).getroot()
        message = schema.identify(root)
        self.assertIsNotNone(message, path)
        return message, schema.check(message, root)

    def test_every_valid_sample_walks_without_an_error(self):
        paths = sorted(glob.glob(os.path.join(EXTERNAL, "*.xml")))
        spoken = set()
        for path in paths:
            with self.subTest(os.path.basename(path)):
                message, findings = self.walk(path)
                spoken.add(message.name)
                self.assertEqual([f for f in findings if f.level == "error"], [])
        # at least one outside sample for every message the mock writes, and
        # for the pain.001 it reads
        self.assertLessEqual({"pain.001.001.09", PAIN002.name, CAMT054.name, CAMT053.name},
                             spoken)

    def test_every_invalid_sample_is_rejected(self):
        paths = sorted(glob.glob(os.path.join(EXTERNAL, "invalid", "*.xml")))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(os.path.basename(path)):
                _, findings = self.walk(path)
                self.assertTrue([f for f in findings if f.level == "error"])

    def test_the_xsd_sources_agree_with_the_tool_that_fetches_them(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "tools", "check_xsd.py")
        spec = importlib.util.spec_from_file_location("check_xsd", path)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        with open(os.path.join(EXTERNAL, "SOURCES.md"), encoding="utf-8") as handle:
            sources = handle.read()
        for name, (url, digest) in tool.XSDS.items():
            with self.subTest(name):
                commit = url.split("/")[5]
                self.assertIn("`%s.xsd`" % name, sources)
                self.assertIn(commit, sources)
                self.assertIn(digest, sources)

    def test_every_sample_is_accounted_for(self):
        with open(os.path.join(EXTERNAL, "SOURCES.md"), encoding="utf-8") as handle:
            sources = handle.read()
        for path in glob.glob(os.path.join(EXTERNAL, "**", "*.xml"), recursive=True):
            relative = os.path.relpath(path, EXTERNAL).replace(os.sep, "/")
            self.assertIn("`%s`" % relative, sources, "no source recorded for " + relative)


if __name__ == "__main__":
    unittest.main()
