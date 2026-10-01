"""pain.008: the debtor's bank says no (#131, step c).

`POST /_mock/collections/<EndToEndId>/refuse` is the debtor's bank answering,
for a debtor at another bank. Before the collection settles it is rejected, with
a further `pain.002`, and nothing ever books. After, the money goes back: a
`pacs.004`, a `camt.054` debit and an entry on the statement, and the balance
comes down by exactly what went up. A debtor this bank holds is not refused by
hand: `return-later` on that account sends the collection back by itself.
"""
import datetime
from xml.etree import ElementTree as ET

from test_collections_book import CAMT052, CAMT054, UMBRELLA_IBAN, BookingCase, entries
from test_statements import (ACME, CAMT053, FRIDAY, MONDAY, TODAY, TUESDAY,
                             read_statement)

from mockbank import accounts, bai2, schema

PAIN002 = schema.MESSAGES["pain.002.001.10"]
PACS004 = schema.MESSAGES["pacs.004.001.09"]
GLOBEX_IBAN = "NL14MOCK0000000002"
SATURDAY = TODAY + datetime.timedelta(days=2)


class RefusingCase(BookingCase):

    def refuse(self, end_to_end_id, reason="MD01", status=200):
        resp = self.post("/_mock/collections/%s/refuse" % end_to_end_id,
                         body={"reason": reason})
        self.assertEqual(resp.status, status, resp.body)
        return resp.json()

    def returns(self, collected):
        """Each pacs.004 as (original MsgId, [(EndToEndId, reason, amount, debtor,
        creditor IBAN)])."""
        out = []
        for root in self.of_type(collected, PACS004):
            m = {"m": PACS004.namespace}
            out.append((root.findtext("m:PmtRtr/m:OrgnlGrpInf/m:OrgnlMsgId", namespaces=m), [
                (tx.findtext("m:OrgnlEndToEndId", namespaces=m),
                 tx.findtext("m:RtrRsnInf/m:Rsn/m:Cd", namespaces=m),
                 tx.findtext("m:RtrdIntrBkSttlmAmt", namespaces=m),
                 tx.findtext("m:OrgnlTxRef/m:Dbtr/m:Pty/m:Nm", namespaces=m),
                 tx.findtext("m:OrgnlTxRef/m:CdtrAcct/m:Id/m:IBAN", namespaces=m))
                for tx in root.iter("{%s}TxInf" % PACS004.namespace)]))
        return out


class BeforeItSettles(RefusingCase):

    def test_it_is_rejected_with_a_further_pain002_and_never_books(self):
        before = self.balance("ACME")
        self.collect([("C1", 1000), ("C2", 2500)])
        self.mailbox()                               # the file's own pain.002
        refused = self.refuse("C1", "MD01")
        self.assertEqual((refused["status"], refused["reason"], refused["settlement_date"],
                          refused["booked_at"], refused["return_due"]),
                         ("rejected", "MD01", None, None, None))
        [report] = self.of_type(self.mailbox(), PAIN002)
        m = {"m": PAIN002.namespace}
        body = report.find("m:CstmrPmtStsRpt", m)
        self.assertEqual(
            (body.findtext("m:OrgnlGrpInfAndSts/m:OrgnlMsgId", namespaces=m),
             body.findtext("m:OrgnlGrpInfAndSts/m:OrgnlMsgNmId", namespaces=m),
             body.findtext("m:OrgnlGrpInfAndSts/m:GrpSts", namespaces=m),
             body.findtext("m:OrgnlPmtInfAndSts/m:OrgnlPmtInfId", namespaces=m),
             [(t.findtext("m:OrgnlEndToEndId", namespaces=m), t.findtext("m:TxSts", namespaces=m),
               t.findtext("m:StsRsnInf/m:Rsn/m:Cd", namespaces=m))
              for t in body.iter("{%s}TxInfAndSts" % PAIN002.namespace)]),
            ("DD-1", "pain.008.001.08", None, "DD-1-B1", [("C1", "RJCT", "MD01")]))
        self.advance(MONDAY)
        self.assertEqual(self.balance("ACME"), before + 2500, "only the other one booked")
        self.assertEqual(self.acme_statements()[-1]["entries"], [("C2", 2500)])
        self.assertEqual(self.get("/_mock/state").json()["collections"],
                         {"accepted": 1, "rejected": 1, "returned": 0, "booked": 1})

    def test_its_notification_is_no_longer_coming(self):
        self.collect([("C1", 1000)])
        self.assertEqual(len(self.get("/_mock/queue").json()), 1)
        self.refuse("C1")
        self.assertEqual(self.get("/_mock/queue").json(), [])


class AfterItSettled(RefusingCase):

    def test_the_money_goes_back_and_the_statements_reconcile(self):
        before = self.balance("ACME")
        self.collect([("C1", 1000), ("C2", 2500)])
        self.advance(MONDAY)                         # settled on Friday
        self.assertEqual(self.balance("ACME"), before + 3500)
        self.mailbox()
        returned = self.refuse("C1", "AM04")
        # Monday morning, a business day before the cutoff: back today, and the
        # stamp is the bank's Monday (#147).
        self.assertEqual((returned["status"], returned["return_due"], returned["return_reason"],
                          returned["returned_at"][:10], returned["settlement_date"]),
                         ("returned", MONDAY.isoformat(), "AM04", MONDAY.isoformat(),
                          FRIDAY.isoformat()))
        self.assertEqual(self.balance("ACME"), before + 2500)
        self.advance(TUESDAY)
        monday = self.acme_statements()[-1]
        self.assertEqual((monday["opening"], monday["closing"], monday["entries"]),
                         (before + 3500, before + 2500, [("C1", 1000)]))
        self.assertEqual(self.get("/_mock/state").json()["collections"],
                         {"accepted": 1, "rejected": 0, "returned": 1, "booked": 2})

    def test_a_pacs004_names_the_pain008_and_a_camt054_debits_it(self):
        self.collect([("C1", 1000)])
        self.advance(MONDAY)
        self.mailbox()
        self.refuse("C1", "MD06")
        collected = self.mailbox()
        self.assertEqual(self.returns(collected),
                         [("DD-1", [("C1", "MD06", "10.00", "Customer C1", ACME)])])
        [note] = self.of_type(collected, CAMT054)
        [entry] = entries(note)
        self.assertEqual((entry["side"], entry["amount"], entry["booked"], entry["code"],
                          entry["servicer_ref"], entry["refs"]["EndToEndId"],
                          entry["refs"]["MndtId"], entry["debtor"], entry["debtor_iban"]),
                         ("DBIT", "10.00", MONDAY.isoformat(), ("PMNT", "IDDT", "UPDD"),
                          "MB-CRT-%d" % self.collection("C1")["id"], "C1", "M-C1",
                          "Customer C1", UMBRELLA_IBAN))
        ns = {"m": CAMT054.namespace}
        info = note.find(".//m:RtrInf", ns)
        self.assertEqual((info.findtext("m:Rsn/m:Cd", namespaces=ns),
                          info.findtext("m:OrgnlBkTxCd/m:Domn/m:Fmly/m:SubFmlyCd", namespaces=ns)),
                         ("MD06", "ESDD"))
        self.assertEqual(entry["code"], schema.RETURNED_COLLECTION)
        self.assertIn(schema.RETURNED_COLLECTION, schema.BANK_TRANSACTION_CODES)

    def test_refused_on_a_weekend_it_goes_back_on_monday_and_is_queued_until_then(self):
        before = self.balance("ACME")
        self.collect([("C1", 1000)])
        self.advance(SATURDAY)      # Saturday
        self.mailbox()
        waiting = self.refuse("C1")
        self.assertEqual((waiting["status"], waiting["return_due"], waiting["returned_at"]),
                         ("accepted", MONDAY.isoformat(), None))
        self.assertEqual(self.balance("ACME"), before + 1000, "not before its day")
        file_id = waiting["file_id"]
        keys = ["camt.054.001.08/ACME/%s/collections-returned" % MONDAY.isoformat(),
                "pacs.004.001.09/ACME/%s/collections-returned/file-%d"
                % (MONDAY.isoformat(), file_id)]
        self.assertEqual([(e["key"], e["reports"]) for e in self.get("/_mock/queue").json()],
                         [(key, "collections returned") for key in keys])
        self.refuse("C1", status=409)                # already on its way back
        self.advance(MONDAY)
        self.assertEqual(self.balance("ACME"), before)
        self.assertEqual(self.get("/_mock/queue").json(), [])
        self.assertEqual(sorted(m["key"] for m in self.get("/_mock/mailbox?leave").json()
                                if not m["key"].startswith("m")), keys)

    def test_a_holiday_moves_a_return_still_to_come(self):
        self.collect([("C1", 1000)])
        self.advance(SATURDAY)
        self.refuse("C1")
        resp = self.request("PUT", "/_mock/holidays", body=[MONDAY.isoformat()])
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(self.collection("C1")["return_due"], TUESDAY.isoformat())

    def test_a_nacha_accounts_bai2_statement_says_557(self):
        self.patch("/_mock/accounts/ACME", body={"format": "nacha", "currency": "USD"})
        before = self.balance("ACME")
        self.collect([("C1", 1000)], ccy="USD", account_ccy="USD")
        self.advance(MONDAY)
        # The reason is said in the account's terms: an R code here (#176).
        self.assertIn("one of R01", self.refuse("C1", "AC04", status=400)["error"])
        self.refuse("C1", "R02")
        self.advance(TUESDAY)
        monday = [m["body"] for m in self.get("/_mock/mailbox?type=bai2").json()][-1]
        [statement] = bai2.statements(monday)
        self.assertEqual([tuple(e) for e in statement.entries],
                         [("557", 1000, "C1", "Customer C1")])
        self.assertEqual((statement.opening, statement.closing), (before + 1000, before))
        self.assertEqual(bai2.trailers_agree(monday), [])
        self.assertEqual(bai2.RETURNED_COLLECTION, "557")

    def test_every_message_a_return_brings_fits_the_dictionary(self):
        self.collect([("C1", 1000), ("C2", 2500)])
        self.refuse("C2", "MS02")                    # before: a pain.002
        self.advance(MONDAY)
        self.refuse("C1", "AM04")                    # after: a pacs.004 and a camt.054
        report = self.post("/_mock/accounts/ACME/report").json()
        self.assertEqual(report["entries"], 1)
        self.advance(TUESDAY)
        collected = self.get("/_mock/mailbox?leave").json()
        for item in collected:
            root = ET.fromstring(item["body"].encode("utf-8"))
            self.assertEqual(schema.check(schema.identify(root), root), [], item["type"])
        [intraday] = [ET.fromstring(m["body"].encode("utf-8")) for m in collected
                      if m["type"] == CAMT052.name]
        self.assertEqual([(e["side"], e["code"]) for e in entries(intraday)],
                         [("DBIT", ("PMNT", "IDDT", "UPDD"))])


class WhatTheBankWillNotTake(RefusingCase):

    def test_each_refusal_says_why(self):
        self.collect([("C1", 1000), ("C2", 2500), ("C3", 300)], debtors={"C3": GLOBEX_IBAN})
        self.collect([("N1", 100)], msg_id="DD-2", mandate=False)   # MD02 at acceptance
        for path, body, status, says in (
                ("C9", {"reason": "MD01"}, 404, "no collection"),
                ("C1", {"reason": "NOPE"}, 400, "one of AC01"),
                ("C1", {}, 400, '{"reason": "MD01"}'),
                ("C1", {"reason": "MD01", "when": "now"}, 400, "the body names the reason"),
                ("N1", {"reason": "MD01"}, 409, "is rejected"),
                ("C3", {"reason": "MD01"}, 409, "one this bank holds")):
            with self.subTest(path=path, body=body):
                resp = self.post("/_mock/collections/%s/refuse" % path, body=body)
                self.assertEqual(resp.status, status, resp.body)
                self.assertIn(says, resp.json()["error"])
        self.refuse("C1")
        self.assertIn("is rejected", self.refuse("C1", status=409)["error"])
        self.advance(MONDAY)
        self.refuse("C2")
        self.assertIn("is returned", self.refuse("C2", status=409)["error"])
        # Nothing above moved anything it should not have.
        self.assertEqual([(c["end_to_end_id"], c["status"]) for c in
                          self.get("/_mock/collections").json()],
                         [("N1", "rejected"), ("C3", "accepted"), ("C2", "returned"),
                          ("C1", "rejected")])

    def test_the_newest_collection_with_that_id_is_the_one_refused(self):
        self.collect([("C1", 1000)], msg_id="DD-1")
        self.collect([("C1", 2000)], msg_id="DD-2")
        self.assertEqual(self.refuse("C1")["msg_id"], "DD-2")
        self.assertEqual([(c["msg_id"], c["status"]) for c in
                          self.get("/_mock/collections/C1?all").json()],
                         [("DD-2", "rejected"), ("DD-1", "accepted")])

    def test_a_return_that_would_overdraw_past_what_a_statement_can_write(self):
        self.collect([("C1", 1000)])
        self.advance(MONDAY)
        self.patch("/_mock/accounts/ACME", body={"balance": -accounts.MAX_BALANCE + 999})
        self.assertIn("18 digits", self.refuse("C1", status=409)["error"])
        self.patch("/_mock/accounts/ACME", body={"balance": -accounts.MAX_BALANCE + 1000})
        self.assertEqual(self.refuse("C1")["status"], "returned")
        self.assertEqual(self.balance("ACME"), -accounts.MAX_BALANCE)

    def test_an_account_closed_meanwhile_is_not_debited_until_it_reopens(self):
        self.collect([("C1", 1000)])
        self.advance(SATURDAY)
        self.refuse("C1")
        self.patch("/_mock/accounts/ACME", body={"closed": True})
        self.assertEqual(self.get("/_mock/queue").json(), [])
        self.advance(MONDAY)
        self.assertEqual((self.collection("C1")["status"], self.collection("C1")["return_due"]),
                         ("accepted", TUESDAY.isoformat()))
        before = self.balance("ACME")
        self.patch("/_mock/accounts/ACME", body={"closed": False})
        self.advance(TUESDAY)
        self.assertEqual(self.balance("ACME"), before - 1000)


class ADebtorThisBankHolds(RefusingCase):
    """`return-later` on the account collected from: the collection settles and
    then goes back by itself, `days` business days on."""

    def test_return_later_sends_a_settled_collection_back(self):
        self.patch("/_mock/accounts/GLOBEX", body={
            "behaviour": "return-later", "parameters": {"days": 1, "reason": "MD06"}})
        before, globex = self.balance("ACME"), self.balance("GLOBEX")
        self.collect([("C1", 1000), ("C2", 2500)], debtors={"C1": GLOBEX_IBAN})
        self.advance(FRIDAY)
        self.assertEqual(self.balance("ACME"), before + 3500)
        settled = self.collection("C1")
        self.assertEqual((settled["status"], settled["return_due"], settled["return_reason"]),
                         ("accepted", MONDAY.isoformat(), "MD06"))
        self.assertIsNone(self.collection("C2")["return_due"], "another bank's debtor")
        self.mailbox()
        self.advance(TUESDAY)
        self.assertEqual(self.balance("ACME"), before + 2500)
        self.assertEqual(self.balance("GLOBEX"), globex, "the debtor is never booked")
        collected = self.mailbox()
        self.assertEqual(self.returns(collected),
                         [("DD-1", [("C1", "MD06", "10.00", "Customer C1", ACME)])])
        monday = [s for s in (read_statement(r) for r in self.of_type(collected, CAMT053))
                  if s["iban"] == ACME][-1]
        self.assertEqual((monday["opening"], monday["closing"], monday["entries"]),
                         (before + 3500, before + 2500, [("C1", 1000)]))

    def test_one_move_of_the_clock_over_three_days_states_each_day_its_own(self):
        # C1 settles on Friday and goes back on Monday; C2 settles on Monday and
        # goes back on Tuesday. The clock is moved once, so every booking is
        # done before the first statement is written, and each day's position
        # has to undo what came after it.
        self.patch("/_mock/accounts/GLOBEX", body={
            "behaviour": "return-later", "parameters": {"days": 1}})
        before = self.balance("ACME")
        self.collect([("C1", 100)], msg_id="DD-1", debtors={"C1": GLOBEX_IBAN})
        self.collect([("C2", 200)], msg_id="DD-2", when=MONDAY, debtors={"C2": GLOBEX_IBAN})
        self.advance(TUESDAY + datetime.timedelta(days=1))
        self.assertEqual(self.balance("ACME"), before)
        _thursday, friday, monday, tuesday = self.acme_statements()
        self.assertEqual((friday["opening"], friday["closing"], friday["entries"]),
                         (before, before + 100, [("C1", 100)]))
        self.assertEqual((monday["opening"], monday["closing"], monday["entries"]),
                         (before + 100, before + 200, [("C2", 200), ("C1", 100)]))
        self.assertEqual((tuesday["opening"], tuesday["closing"], tuesday["entries"]),
                         (before + 200, before, [("C2", 200)]))

    def test_it_names_one_collection_when_the_account_does(self):
        self.patch("/_mock/accounts/GLOBEX", body={
            "behaviour": "return-later", "parameters": {"days": 1, "end_to_end_id": "C2"}})
        self.collect([("C1", 100), ("C2", 200)], debtors={"C1": GLOBEX_IBAN, "C2": GLOBEX_IBAN})
        self.advance(FRIDAY)
        self.assertEqual([(c["end_to_end_id"], c["return_due"], c["return_reason"])
                          for c in self.get("/_mock/collections").json()],
                         [("C2", MONDAY.isoformat(), "AC04"), ("C1", None, None)])

    def test_a_nacha_debtors_reason_is_said_in_iso(self):
        # The debtor account's reason is an R code; the answer is a pacs.004.
        self.patch("/_mock/accounts/GLOBEX", body={"format": "nacha"})
        self.patch("/_mock/accounts/GLOBEX", body={
            "behaviour": "return-later", "parameters": {"days": 1, "reason": "R01"}})
        self.collect([("C1", 100)], debtors={"C1": GLOBEX_IBAN})
        self.advance(FRIDAY)
        self.assertEqual(self.collection("C1")["return_reason"], "AM04")
