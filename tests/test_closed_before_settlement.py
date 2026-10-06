"""An account closed while a payment waits to settle (#158).

A payment accepted for a later day used to book on that day whatever had
happened to the account since. A closed account gets no statement, so the money
left an account the bank called closed and no statement ever showed it.

The rule, chosen on the issue: the bank looks again on the settlement day and
rejects the payment, `AC04`, in a further `pain.002` for the file. Nothing
books and there is no `camt.054`. A NACHA account is told by a return entry,
`R02`. Money due *back* to a closed account - a `return-later` payment - is put
off a business day at a time until the account reopens, as a credit is.
"""
import datetime
from xml.etree import ElementTree as ET

from support import MockServerCase
from test_nacha_returns import twin
from test_payments import sample

from mockbank import nacha

PAIN002, CAMT054, PACS004 = "pain.002.001.10", "camt.054.001.08", "pacs.004"
THURSDAY = "2026-10-01"                 # the day the samples' payments settle
OPENING = 12500000


class ClosedCase(MockServerCase):
    """Monday, with the sample's two accepted payments waiting for Thursday."""

    config_kwargs = {"clock": "2026-09-28T09:00"}
    file = staticmethod(lambda: sample("pain001_four_payments.xml"))

    def setUp(self):
        self.post("/_mock/reset")
        self.prepare()
        self.sent = self.post("/payments", body=self.file()).json()
        self.assertEqual(self.sent["accepted"], 2, self.sent)
        self.get("/_mock/mailbox")       # the file's own status report

    def prepare(self):
        pass

    def patch_acme(self, **fields):
        resp = self.patch("/_mock/accounts/ACME", body=fields)
        self.assertEqual(resp.status, 200, resp.body)

    def close(self):
        self.patch_acme(closed=True)

    def advance(self, query):
        resp = self.post("/_mock/advance?" + query)
        self.assertEqual(resp.status, 200, resp.body)

    def balance(self):
        return self.get("/_mock/accounts/ACME").json()["balance"]

    def mail(self, kind=""):
        return [m for m in self.get("/_mock/mailbox?type=" + kind).json()
                if m["account"] == "ACME"]

    def queue(self):
        # Whether it is about one file, and not which.
        return [(e["type"], e["reports"], e["dueAt"], e["fileId"] is not None)
                for e in self.get("/_mock/queue").json() if e["account"] == "ACME"]

    def statuses(self):
        return sorted((p["end_to_end_id"], p["status"], p["reason"])
                      for p in self.get("/_mock/payments").json())


def rejections(body):
    """(OrgnlMsgId, agent element, [(batch, EndToEndId, TxSts, reason)])."""
    root = ET.fromstring(body)
    ns = {"m": root.tag[1:].split("}")[0]}
    report = root.find("m:CstmrPmtStsRpt", ns)
    agent = [child.tag.split("}")[1] for child in report.find("m:GrpHdr", ns)][-1]
    out = []
    for batch in report.iterfind("m:OrgnlPmtInfAndSts", ns):
        for tx in batch.iterfind("m:TxInfAndSts", ns):
            out.append((batch.findtext("m:OrgnlPmtInfId", namespaces=ns),
                        tx.findtext("m:OrgnlEndToEndId", namespaces=ns),
                        tx.findtext("m:TxSts", namespaces=ns),
                        tx.findtext("m:StsRsnInf/m:Rsn/m:Cd", namespaces=ns)))
    return (report.findtext("m:OrgnlGrpInfAndSts/m:OrgnlMsgId", namespaces=ns), agent,
            report.findtext("m:OrgnlGrpInfAndSts/m:GrpSts", namespaces=ns), out)


class ItIsRejectedOnTheDay(ClosedCase):

    def test_nothing_books_and_a_further_status_report_says_why(self):
        self.close()
        self.advance("days=5")
        self.assertEqual(self.balance(), OPENING, "the issue's case: it was 10875000")
        [report] = self.mail(PAIN002)
        original, agent, group_status, rejected = rejections(report["body"])
        self.assertEqual((original, agent, group_status),
                         (self.sent["msg_id"], "DbtrAgt", None))
        self.assertEqual(rejected, [
            ("ACME-20261001-0001-B1", "INV-2026-0101", "RJCT", "AC04"),
            ("ACME-20261001-0001-B1", "INV-2026-0104", "RJCT", "AC04")])
        self.assertIsNotNone(report["fileId"])
        self.assertEqual(self.mail(CAMT054), [], "no debit, so no debit notification")
        self.assertEqual(self.get("/_mock/unsent").json(), [])

    def test_the_payments_say_so(self):
        self.close()
        self.advance("to=" + THURSDAY)
        self.assertEqual([s for s in self.statuses() if s[0] in ("INV-2026-0101",
                                                                 "INV-2026-0104")],
                         [("INV-2026-0101", "rejected", "AC04"),
                          ("INV-2026-0104", "rejected", "AC04")])
        one = self.get("/_mock/payments/INV-2026-0101").json()
        one = one[0] if isinstance(one, list) else one
        self.assertIn("closed before the payment settled", one["reason_text"])
        self.assertIsNone(one["settlement_date"])
        self.assertIsNone(one["booked_at"])

    def test_not_before_the_day(self):
        self.close()
        self.advance("to=2026-09-30")
        self.assertEqual(self.mail(PAIN002), [])
        self.assertEqual([s[1] for s in self.statuses()].count("accepted"), 2)

    def test_it_is_said_once_and_reopening_does_not_bring_the_payment_back(self):
        self.close()
        self.advance("to=" + THURSDAY)
        self.assertEqual(len(self.mail(PAIN002)), 1)
        self.patch_acme(closed=False)
        self.advance("days=4")
        self.assertEqual(self.mail(PAIN002) + self.mail(CAMT054), [])
        self.assertEqual(self.balance(), OPENING)
        for statement in self.get("/_mock/accounts/ACME/statements").json():
            self.assertEqual((statement["opening"], statement["closing"],
                              statement["entries"]), (OPENING, OPENING, 0))

    def test_reopened_before_the_day_it_settles_as_if_nothing_had_happened(self):
        self.close()
        self.advance("to=2026-09-30")
        self.patch_acme(closed=False)
        self.advance("to=2026-10-02")
        self.assertEqual(self.balance(), 10875000)
        self.assertEqual(self.mail(PAIN002), [])
        self.assertEqual(len(self.mail(CAMT054)), 1)
        thursday = [s for s in self.get("/_mock/accounts/ACME/statements").json()
                    if s["day"] == THURSDAY]
        self.assertEqual([(s["closing"], s["entries"]) for s in thursday], [(10875000, 2)])

    def test_closed_after_the_day_what_booked_stays_booked(self):
        self.advance("to=" + THURSDAY)
        self.close()
        self.advance("days=4")
        self.assertEqual(self.balance(), 10875000)
        self.assertEqual(self.mail(PAIN002), [])

    def test_each_file_gets_its_own_report(self):
        second = self.post("/payments", body=self.file().replace(
            "ACME-20261001-0001", "ACME-20261001-0002")).json()
        self.assertEqual(second["accepted"], 2, second)
        self.get("/_mock/mailbox")
        self.close()
        self.advance("to=" + THURSDAY)
        reports = [rejections(m["body"]) for m in self.mail(PAIN002)]
        self.assertEqual([r[0] for r in reports], [self.sent["msg_id"], second["msg_id"]])
        self.assertEqual([len(r[3]) for r in reports], [2, 2])

    def test_another_accounts_payments_are_not_touched(self):
        self.patch("/_mock/accounts/GLOBEX", body={"closed": True})
        self.advance("to=" + THURSDAY)
        self.assertEqual(self.balance(), 10875000)
        self.assertEqual(self.mail(PAIN002), [])


class WhatTheQueueShows(ClosedCase):

    SETTLING = [(CAMT054, "payments settling", "2026-10-01T00:00:00Z", False)]
    REJECTING = [(PAIN002, "payments rejected at settlement", "2026-10-01T00:00:00Z", True)]

    def test_the_debits_entry_becomes_the_rejections_and_goes_back_on_reopening(self):
        self.assertEqual(self.queue(), self.SETTLING)
        self.close()
        self.assertEqual(self.queue(), self.REJECTING)
        self.patch_acme(closed=False)
        self.assertEqual(self.queue(), self.SETTLING)

    def test_the_message_is_the_entry_it_was_promised_as(self):
        self.close()
        [entry] = [e for e in self.get("/_mock/queue").json() if e["account"] == "ACME"]
        self.assertEqual((entry["written"], entry["msgId"]), (False, self.sent["msg_id"]))
        self.advance("to=" + THURSDAY)
        self.assertEqual(self.queue(), [])
        [report] = self.mail(PAIN002)
        self.assertEqual((report["key"], report["queuedAt"], report["fileId"]),
                         (entry["key"], entry["queuedAt"], entry["fileId"]))
        self.assertEqual(report["queuedAt"], "2026-09-28T09:00:00Z", "as the file arrived")


class ANachaAccountIsToldByAReturnEntry(ClosedCase):

    file = staticmethod(twin)

    def prepare(self):
        self.patch_acme(format="nacha", currency="USD")

    def test_r02_for_each_and_nothing_books(self):
        before = self.balance()
        self.close()
        [entry] = [e for e in self.get("/_mock/queue").json()
                   if e["account"] == "ACME" and e["dueAt"].startswith(THURSDAY)]
        self.assertEqual((entry["type"], entry["reports"]),
                         (nacha.RETURN, "payments returned"))
        self.advance("to=" + THURSDAY)
        self.assertEqual(self.balance(), before)
        accepted = sorted(p["end_to_end_id"] for p in self.sent["payments"]
                          if p["outcome"] == "accepted")
        sent = [m for m in self.mail(nacha.RETURN) if m["key"] == entry["key"]]
        self.assertEqual(len(sent), 1)
        returned, findings = nacha.inspect(sent[0]["body"].encode("ascii"),
                                           datetime.date(2026, 10, 1))
        self.assertEqual(findings, [])
        self.assertEqual(sorted((r.end_to_end_id, r.reason) for r in returned.returns),
                         [(name, "R02") for name in accepted])
        self.assertEqual(self.mail(CAMT054) + self.mail(PAIN002), [])
        self.advance("days=4")
        self.assertEqual(self.mail(nacha.RETURN), [], "said once")


class MoneyDueBackIsPutOff(ClosedCase):
    """`return-later`: the payments settle on Thursday and are due back on
    Monday, and the account closes in between."""

    def prepare(self):
        self.patch_acme(behaviour="return-later", parameters={"days": 2})

    def test_it_waits_for_the_account_to_reopen_and_books_on_a_day_with_a_statement(self):
        self.advance("to=" + THURSDAY)
        self.assertEqual(self.balance(), 10875000)
        self.get("/_mock/mailbox")
        self.assertIn(PACS004, [e[0][:8] for e in self.queue()])
        self.close()
        self.assertEqual(self.queue(), [], "put off, so not promised")
        self.advance("to=2026-10-06")                 # past Monday the 5th
        self.assertEqual(self.balance(), 10875000, "the issue's other half: it was credited")
        self.assertEqual(self.mail(), [])
        self.patch_acme(closed=False)
        self.assertEqual({e[2] for e in self.queue()}, {"2026-10-07T00:00:00Z"})
        self.advance("to=2026-10-08")
        self.assertEqual(self.balance(), OPENING)
        kinds = sorted(m["type"][:8] for m in self.mail() if not m["type"].startswith("camt.053"))
        self.assertEqual(kinds, ["camt.054", "pacs.004"])
        wednesday = [s for s in self.get("/_mock/accounts/ACME/statements").json()
                     if s["day"] == "2026-10-07"]
        self.assertEqual([(s["opening"], s["closing"], s["entries"]) for s in wednesday],
                         [(10875000, OPENING, 2)])
