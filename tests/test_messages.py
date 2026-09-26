"""What the bank sends back: pain.002 and camt.054, through the mailbox.

Every message collected here is also walked against the dictionary; a
message the mock writes that its own reader would reject is a failed test.
"""
import datetime
from xml.etree import ElementTree as ET

# test_payments brings in support, which puts the checkout on sys.path
from test_payments import (ACME, UMBRELLA, TODAY, PipelineCase, amounts,
                           pain001, sample)

from mockbank import schema

PAIN002 = schema.MESSAGES["pain.002.001.10"]
CAMT054 = schema.MESSAGES["camt.054.001.08"]


def ns(message):
    return {"m": message.namespace}


class MessageCase(PipelineCase):

    def mailbox(self):
        """Collect the mailbox, and walk every message in it."""
        resp = self.get("/_mock/mailbox")
        self.assertEqual(resp.status, 200)
        collected = resp.json()
        for item in collected:
            root = ET.fromstring(item["body"].encode("utf-8"))
            message = schema.identify(root)
            self.assertIsNotNone(message, item["type"])
            self.assertEqual(message.name, item["type"])
            self.assertEqual(schema.check(message, root), [], item["type"])
        return collected

    def of_type(self, collected, message):
        return [ET.fromstring(item["body"].encode("utf-8"))
                for item in collected if item["type"] == message.name]


class StatusReport(MessageCase):

    def test_the_sample_gets_one_pain002_reporting_every_payment(self):
        text = sample("pain001_four_payments.xml")
        answer = self.send(text).json()
        self.assertEqual([(q["type"], q["released"]) for q in answer["queued"]
                          if q["type"] == PAIN002.name], [(PAIN002.name, True)])
        reports = self.of_type(self.mailbox(), PAIN002)
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report.tag, "{%s}Document" % PAIN002.namespace)
        # GrpHdr first, then the original group, then the batches
        body = report[0]
        self.assertEqual([schema.split_tag(e.tag)[1] for e in body],
                         ["GrpHdr", "OrgnlGrpInfAndSts", "OrgnlPmtInfAndSts"])
        m = ns(PAIN002)
        group = body.find("m:OrgnlGrpInfAndSts", m)
        self.assertEqual(group.findtext("m:OrgnlMsgId", namespaces=m), "ACME-20261001-0001")
        self.assertEqual(group.findtext("m:OrgnlMsgNmId", namespaces=m), "pain.001.001.09")
        self.assertEqual(group.findtext("m:GrpSts", namespaces=m), "PART")
        self.assertEqual(group.findtext("m:OrgnlCtrlSum", namespaces=m), "20630.75")
        transactions = [(tx.findtext("m:OrgnlEndToEndId", namespaces=m),
                         tx.findtext("m:TxSts", namespaces=m),
                         tx.findtext("m:StsRsnInf/m:Rsn/m:Cd", namespaces=m))
                        for tx in body.iterfind("m:OrgnlPmtInfAndSts/m:TxInfAndSts", m)]
        self.assertEqual(transactions, [
            ("INV-2026-0101", "ACCP", None), ("INV-2026-0102", "RJCT", "AC04"),
            ("INV-2026-0103", "RJCT", "RC01"), ("INV-2026-0104", "ACCP", None)])
        self.assertEqual(body.findtext("m:OrgnlPmtInfAndSts/m:PmtInfSts", namespaces=m), "PART")
        self.assertTrue(body.findtext("m:GrpHdr/m:CreDtTm", namespaces=m).endswith("+00:00"))

    def test_the_mailbox_is_collected_once(self):
        self.send(sample("pain001_four_payments.xml"))
        self.assertTrue(self.mailbox())
        self.assertEqual(self.mailbox(), [])
        self.assertEqual(self.get("/_mock/state").json()["messages"]["waiting"], 0)

    def test_a_file_rejected_outright_has_group_status_only(self):
        text = sample("pain001_four_payments.xml")
        self.send(text)
        self.mailbox()
        self.send(text)                                      # DUPL
        reports = self.of_type(self.mailbox(), PAIN002)
        self.assertEqual(len(reports), 1)
        m = ns(PAIN002)
        body = reports[0][0]
        self.assertEqual(body.findtext("m:OrgnlGrpInfAndSts/m:GrpSts", namespaces=m), "RJCT")
        self.assertEqual(body.findtext("m:OrgnlGrpInfAndSts/m:StsRsnInf/m:Rsn/m:Cd",
                                       namespaces=m), "DUPL")
        self.assertIsNone(body.find("m:OrgnlPmtInfAndSts", m))

    def test_a_silent_account_gets_no_pain002_and_still_books(self):
        self.patch_account("ACME", behaviour="silent")
        before = self.balance("ACME")
        answer = self.send(pain001("SIL-1", ACME, [("S1", 700, UMBRELLA)])).json()
        self.assertFalse([q for q in answer["queued"] if q["type"] == PAIN002.name])
        collected = self.mailbox()
        self.assertEqual(self.of_type(collected, PAIN002), [])
        self.assertEqual(len(self.of_type(collected, CAMT054)), 1)
        self.assertEqual(before - self.balance("ACME"), 700)

    def test_a_body_with_no_msgid_gets_no_pain002(self):
        answer = self.send("MsgId,Amount\n").json()
        self.assertEqual(answer["queued"], [])
        self.assertEqual(self.mailbox(), [])


class DelayedStatus(MessageCase):
    config_kwargs = {"status_delay_ms": 60000}

    def test_the_pain002_is_queued_not_released(self):
        answer = self.send(pain001("DEL-1", ACME, [("D1", 100, UMBRELLA)])).json()
        status = [q for q in answer["queued"] if q["type"] == PAIN002.name]
        self.assertEqual(len(status), 1)
        self.assertIs(status[0]["released"], False)
        self.assertEqual(self.of_type(self.mailbox(), PAIN002), [])
        self.assertEqual(self.get("/_mock/state").json()["messages"]["queued"], 1)


class DebitNotification(MessageCase):

    def test_one_camt054_per_account_and_booking_with_an_entry_per_payment(self):
        text = sample("pain001_four_payments.xml")
        before = self.balance("ACME")
        self.send(text)
        notices = self.of_type(self.mailbox(), CAMT054)
        self.assertEqual(len(notices), 1)
        m = ns(CAMT054)
        notification = notices[0].find("m:BkToCstmrDbtCdtNtfctn/m:Ntfctn", m)
        self.assertEqual(notification.findtext("m:Acct/m:Id/m:IBAN", namespaces=m), ACME)
        entries = notification.findall("m:Ntry", m)
        self.assertEqual(
            [e.findtext("m:NtryDtls/m:TxDtls/m:Refs/m:EndToEndId", namespaces=m) for e in entries],
            ["INV-2026-0101", "INV-2026-0104"])
        for entry in entries:
            self.assertEqual(entry.findtext("m:CdtDbtInd", namespaces=m), "DBIT")
            self.assertEqual(entry.findtext("m:BookgDt/m:Dt", namespaces=m), TODAY.isoformat())
            self.assertEqual(entry.find("m:Amt", m).get("Ccy"), "EUR")
            self.assertEqual(
                (entry.findtext("m:BkTxCd/m:Domn/m:Cd", namespaces=m),
                 entry.findtext("m:BkTxCd/m:Domn/m:Fmly/m:Cd", namespaces=m),
                 entry.findtext("m:BkTxCd/m:Domn/m:Fmly/m:SubFmlyCd", namespaces=m)),
                schema.BOOKED_DEBIT)
        # the entries, read off the wire as decimals, are what left the account
        on_wire = [int(e.findtext("m:Amt", namespaces=m).replace(".", "")) for e in entries]
        paid = amounts(text)
        self.assertEqual(on_wire, [paid[0], paid[3]])
        self.assertEqual(before - self.balance("ACME"), sum(on_wire))

    def test_a_later_settlement_date_brings_no_camt054_yet(self):
        later = TODAY + datetime.timedelta(days=4)
        answer = self.send(pain001("LATE-1", ACME, [("L1", 100, UMBRELLA)], when=later)).json()
        self.assertIn({"type": CAMT054.name, "account": "ACME", "due_on": later.isoformat()},
                      answer["queued"])
        self.assertEqual(self.of_type(self.mailbox(), CAMT054), [])

    def test_two_files_on_one_day_are_two_notifications(self):
        self.send(pain001("TWO-1", ACME, [("A1", 100, UMBRELLA)]))
        self.send(pain001("TWO-2", ACME, [("A2", 200, UMBRELLA)]))
        notices = self.of_type(self.mailbox(), CAMT054)
        m = ns(CAMT054)
        ids = [n.findtext("m:BkToCstmrDbtCdtNtfctn/m:GrpHdr/m:MsgId", namespaces=m)
               for n in notices]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2, "each notification has its own MsgId")
