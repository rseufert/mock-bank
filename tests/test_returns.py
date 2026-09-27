"""pacs.004 returns and the return-later behaviour: a settled payment comes back.

Bank time is pinned to Monday 2026-10-05, 09:00, before the cutoff, so a
payment dated Monday settles on receipt and three business days later is
Thursday. Every message collected is walked against the dictionary.
"""
import datetime
from xml.etree import ElementTree as ET

# test_messages brings in support, which puts the checkout on sys.path
from test_messages import MessageCase
from test_payments import ACME, UMBRELLA, amounts, pain001, sample

from mockbank import schema

PACS004 = schema.MESSAGES["pacs.004.001.09"]
CAMT054 = schema.MESSAGES["camt.054.001.08"]
CAMT053 = schema.MESSAGES["camt.053.001.08"]
MONDAY = datetime.date(2026, 10, 5)
assert MONDAY.weekday() == 0
THURSDAY = MONDAY + datetime.timedelta(days=3)


def ns(message):
    return {"m": message.namespace}


class ReturnCase(MessageCase):
    config_kwargs = {"clock": "2026-10-05T09:00"}

    def return_later(self, **parameters):
        resp = self.request("PATCH", "/_mock/accounts/ACME",
                            body={"behaviour": "return-later", "parameters": parameters})
        self.assertEqual(resp.status, 200, resp.body)

    def advance(self, **query):
        resp = self.post("/_mock/advance?" + "&".join("%s=%s" % kv for kv in query.items()))
        self.assertEqual(resp.status, 200, resp.body)

    def returns_in(self, collected):
        """(OrgnlEndToEndId, reason, returned minor units) for each pacs.004 TxInf."""
        m = ns(PACS004)
        out = []
        for root in self.of_type(collected, PACS004):
            for tx in root.iterfind("m:PmtRtr/m:TxInf", m):
                amount = tx.find("m:RtrdIntrBkSttlmAmt", m).text
                out.append((tx.findtext("m:OrgnlEndToEndId", namespaces=m),
                            tx.findtext("m:RtrRsnInf/m:Rsn/m:Cd", namespaces=m),
                            int(amount.replace(".", ""))))
        return out


class ReturnLater(ReturnCase):

    def test_it_comes_back_three_business_days_later_and_not_before(self):
        self.return_later()
        before = self.balance("ACME")
        self.send(pain001("RL-1", ACME, [("R1", 4200, UMBRELLA)], when=MONDAY))
        self.mailbox()
        self.assertEqual(before - self.balance("ACME"), 4200)
        self.assertEqual(self.get("/_mock/payments/R1").json()["return_due"],
                         THURSDAY.isoformat())

        self.advance(days=2)                                   # Wednesday
        self.assertEqual(self.returns_in(self.mailbox()), [])
        self.assertEqual(self.balance("ACME"), before - 4200)

        self.advance(days=1)                                   # Thursday
        self.assertEqual(self.returns_in(self.mailbox()), [("R1", "AC04", 4200)])
        self.assertEqual(self.balance("ACME"), before)
        stored = self.get("/_mock/payments/R1").json()
        self.assertEqual((stored["status"], stored["return_reason"]), ("returned", "AC04"))
        self.assertEqual(self.get("/_mock/state").json()["payments"]["returned"], 1)

    def test_business_days_skip_the_weekend(self):
        # Settled on Thursday, three business days on is Tuesday - not Sunday,
        # which three calendar days would give.
        self.return_later()
        self.send(pain001("RL-W", ACME, [("W1", 100, UMBRELLA)], when=THURSDAY))
        self.advance(to=THURSDAY.isoformat())
        self.assertEqual(self.get("/_mock/payments/W1").json()["return_due"],
                         (THURSDAY + datetime.timedelta(days=5)).isoformat())
        self.advance(to=(THURSDAY + datetime.timedelta(days=4)).isoformat())   # Monday
        self.assertEqual(self.returns_in(self.mailbox()), [])
        self.advance(to=(THURSDAY + datetime.timedelta(days=5)).isoformat())   # Tuesday
        self.assertEqual(self.returns_in(self.mailbox()), [("W1", "AC04", 100)])

    def test_days_and_reason_are_parameters(self):
        self.return_later(days=1, reason="MD07")
        self.send(pain001("RL-2", ACME, [("R2", 300, UMBRELLA)], when=MONDAY))
        self.advance(days=1)
        self.assertEqual(self.returns_in(self.mailbox()), [("R2", "MD07", 300)])

    def test_every_payment_comes_back_unless_one_is_named(self):
        self.return_later(days=1, end_to_end_id="ONLY-THIS")
        self.send(pain001("RL-3", ACME, [("NOT-THIS", 100, UMBRELLA),
                                        ("ONLY-THIS", 200, UMBRELLA)], when=MONDAY))
        self.advance(days=1)
        self.assertEqual(self.returns_in(self.mailbox()), [("ONLY-THIS", "AC04", 200)])
        self.assertEqual(self.get("/_mock/payments/NOT-THIS").json()["status"], "accepted")

    def test_one_pacs004_per_original_file_with_a_txinf_per_payment(self):
        self.return_later(days=1)
        self.send(pain001("RL-4", ACME, [("A1", 100, UMBRELLA), ("A2", 200, UMBRELLA)],
                          when=MONDAY))
        self.send(pain001("RL-5", ACME, [("B1", 300, UMBRELLA)], when=MONDAY))
        self.advance(days=1)
        collected = self.mailbox()
        m = ns(PACS004)
        reports = self.of_type(collected, PACS004)
        self.assertEqual(
            sorted((r.findtext("m:PmtRtr/m:OrgnlGrpInf/m:OrgnlMsgId", namespaces=m),
                    r.findtext("m:PmtRtr/m:GrpHdr/m:NbOfTxs", namespaces=m)) for r in reports),
            [("RL-4", "2"), ("RL-5", "1")])
        for report in reports:
            self.assertEqual(report.findtext("m:PmtRtr/m:OrgnlGrpInf/m:OrgnlMsgNmId",
                                             namespaces=m), "pain.001.001.09")
            self.assertEqual(report.findtext("m:PmtRtr/m:GrpHdr/m:SttlmInf/m:SttlmMtd",
                                             namespaces=m), "INDA")
        # and one camt.054 credit for the account and day, an entry per return
        credits = [n for n in self.of_type(collected, CAMT054)
                   if n.find(".//{%s}Ntry/{%s}CdtDbtInd" % ((CAMT054.namespace,) * 2)).text == "CRDT"]
        self.assertEqual(len(credits), 1)
        self.assertEqual(len(credits[0].findall(".//{%s}Ntry" % CAMT054.namespace)), 3)

    def test_the_credit_notification_names_the_reason_and_the_invoice(self):
        self.return_later(days=1)
        self.send(pain001("RL-6", ACME, [("INV-9", 900, UMBRELLA)], when=MONDAY))
        self.mailbox()
        self.advance(days=1)
        [notice] = self.of_type(self.mailbox(), CAMT054)
        m = ns(CAMT054)
        entry = notice.find("m:BkToCstmrDbtCdtNtfctn/m:Ntfctn/m:Ntry", m)
        self.assertEqual(entry.findtext("m:CdtDbtInd", namespaces=m), "CRDT")
        self.assertEqual(
            (entry.findtext("m:BkTxCd/m:Domn/m:Cd", namespaces=m),
             entry.findtext("m:BkTxCd/m:Domn/m:Fmly/m:Cd", namespaces=m),
             entry.findtext("m:BkTxCd/m:Domn/m:Fmly/m:SubFmlyCd", namespaces=m)),
            schema.RETURNED_CREDIT)
        details = entry.find("m:NtryDtls/m:TxDtls", m)
        self.assertEqual(details.findtext("m:Refs/m:EndToEndId", namespaces=m), "INV-9")
        self.assertEqual(details.findtext("m:RtrInf/m:Rsn/m:Cd", namespaces=m), "AC04")
        self.assertEqual(details.findtext("m:RtrInf/m:OrgnlBkTxCd/m:Domn/m:Fmly/m:SubFmlyCd",
                                          namespaces=m), schema.BOOKED_DEBIT[2])

    def test_other_behaviours_never_return(self):
        self.send(pain001("RL-7", ACME, [("N1", 100, UMBRELLA)], when=MONDAY))
        self.advance(days=10)
        self.assertEqual(self.returns_in(self.mailbox()), [])
        self.assertIsNone(self.get("/_mock/payments/N1").json()["return_due"])


class TheReadmeStory(ReturnCase):
    """The README's diagram, end to end: one payment comes back three business
    days later with AC04, and the balance is restored by exactly its amount."""

    def test_one_payment_comes_back_and_the_balance_is_restored(self):
        self.return_later(end_to_end_id="INV-2026-0104")
        before = self.balance("ACME")
        text = sample("pain001_four_payments.xml", when=MONDAY)
        self.send(text)
        paid = amounts(text)
        self.assertEqual(before - self.balance("ACME"), paid[0] + paid[3])
        self.mailbox()
        self.advance(days=3)
        self.assertEqual(self.returns_in(self.mailbox()), [("INV-2026-0104", "AC04", paid[3])])
        self.assertEqual(before - self.balance("ACME"), paid[0])


class InTheStatement(ReturnCase):

    def statement(self, collected, day):
        m = ns(CAMT053)
        for root in self.of_type(collected, CAMT053):
            stmt = root.find("m:BkToCstmrStmt/m:Stmt", m)
            if stmt.findtext("m:Acct/m:Id/m:IBAN", namespaces=m) != ACME:
                continue
            if stmt.findtext("m:FrToDt/m:FrDtTm", namespaces=m)[:10] != day.isoformat():
                continue
            balances = {}
            for bal in stmt.findall("m:Bal", m):
                sign = -1 if bal.findtext("m:CdtDbtInd", namespaces=m) == "DBIT" else 1
                balances[bal.findtext("m:Tp/m:CdOrPrtry/m:Cd", namespaces=m)] = \
                    sign * int(bal.findtext("m:Amt", namespaces=m).replace(".", ""))
            entries = []
            for e in stmt.findall("m:Ntry", m):
                sign = -1 if e.findtext("m:CdtDbtInd", namespaces=m) == "DBIT" else 1
                entries.append((e.findtext("m:NtryDtls/m:TxDtls/m:Refs/m:EndToEndId", namespaces=m),
                                sign * int(e.findtext("m:Amt", namespaces=m).replace(".", "")),
                                e.findtext("m:NtryDtls/m:TxDtls/m:RtrInf/m:Rsn/m:Cd", namespaces=m)))
            return balances["OPBD"], entries, balances["CLBD"]
        self.fail("no statement for ACME on %s" % day)

    def test_the_return_is_a_credit_entry_and_every_day_reconciles(self):
        self.return_later()
        before = self.balance("ACME")
        self.send(pain001("ST-1", ACME, [("S1", 5000, UMBRELLA)], when=MONDAY))
        self.advance(to=(THURSDAY + datetime.timedelta(days=1)).isoformat())  # Friday 00:00
        collected = self.mailbox()
        days = [MONDAY + datetime.timedelta(days=n) for n in range(4)]
        statements = [self.statement(collected, day) for day in days]
        # each closes at its opening plus its entries, and the next opens there
        for opening, entries, closing in statements:
            self.assertEqual(closing, opening + sum(amount for _, amount, _ in entries))
        for (_, _, closing), (opening, _, _) in zip(statements, statements[1:]):
            self.assertEqual(opening, closing)
        self.assertEqual(statements[0][1], [("S1", -5000, None)])
        self.assertEqual(statements[3][1], [("S1", 5000, "AC04")])
        self.assertEqual((statements[0][0], statements[3][2]), (before, before))


class Parameters(ReturnCase):

    def test_what_return_later_cannot_act_on_is_refused_by_name(self):
        for parameters, phrase in (({"reason": "XX99"}, "return reason code"),
                                   ({"days": -1}, "0 to 60"),
                                   ({"days": "three"}, "whole number"),
                                   ({"dyas": 3}, "dyas"),
                                   ({"end_to_end_id": ""}, "end_to_end_id")):
            with self.subTest(parameters):
                resp = self.request("PATCH", "/_mock/accounts/ACME",
                                    body={"behaviour": "return-later", "parameters": parameters})
                self.assertEqual(resp.status, 400)
                self.assertIn(phrase, resp.json()["error"])

    def test_parameters_set_before_the_behaviour_are_checked_when_it_arrives(self):
        self.request("PATCH", "/_mock/accounts/ACME", body={"parameters": {"reason": "XX99"}})
        resp = self.request("PATCH", "/_mock/accounts/ACME", body={"behaviour": "return-later"})
        self.assertEqual(resp.status, 400)
