"""pain.008: a collection settles, and the creditor account is credited (#131, step b2).

Step b1 decided a collection and gave it a settlement date. Here the clock
reaches that date: the creditor account's balance goes up, a `camt.054` says so,
and the day's statement carries the entry in whichever format the account takes.
The balances are worked out by hand, never read back from the statement they
check. Only the account holder's side books: a debtor the bank holds is read at
acceptance and never debited.
"""
import datetime
from xml.etree import ElementTree as ET

from test_collections_read import pain008
from test_statements import (ACME, CAMT053, FRIDAY, MONDAY, TODAY, TUESDAY, StatementCase,
                             read_statement)

from mockbank import accounts, bai2, schema

CAMT054 = schema.MESSAGES["camt.054.001.08"]
CAMT052 = schema.MESSAGES["camt.052.001.08"]
UMBRELLA_IBAN, GLOBEX_IBAN = "NL30MOCK0000000005", "NL14MOCK0000000002"


def entries(root):
    """Every `Ntry` of a camt.05x, as the XML says it, whatever its namespace."""
    ns = {"m": schema.split_tag(root.tag)[0]}
    out = []
    for entry in root.iter("{%s}Ntry" % ns["m"]):
        tx = entry.find("m:NtryDtls/m:TxDtls", ns)
        code = entry.find("m:BkTxCd/m:Domn", ns)
        out.append({
            "side": entry.findtext("m:CdtDbtInd", namespaces=ns),
            "amount": entry.findtext("m:Amt", namespaces=ns),
            "booked": entry.findtext("m:BookgDt/m:Dt", namespaces=ns),
            "value": entry.findtext("m:ValDt/m:Dt", namespaces=ns),
            "servicer_ref": entry.findtext("m:AcctSvcrRef", namespaces=ns),
            "code": (code.findtext("m:Cd", namespaces=ns),
                     code.findtext("m:Fmly/m:Cd", namespaces=ns),
                     code.findtext("m:Fmly/m:SubFmlyCd", namespaces=ns)),
            "refs": {schema.split_tag(r.tag)[1]: r.text for r in tx.find("m:Refs", ns)},
            "debtor": tx.findtext("m:RltdPties/m:Dbtr/m:Pty/m:Nm", namespaces=ns),
            "debtor_iban": tx.findtext("m:RltdPties/m:DbtrAcct/m:Id/m:IBAN", namespaces=ns),
            "debtor_bic": tx.findtext("m:RltdAgts/m:DbtrAgt/m:FinInstnId/m:BICFI",
                                      namespaces=ns),
        })
    return out


class BookingCase(StatementCase):

    def collect(self, collections, when=FRIDAY, **kw):
        resp = self.post("/payments", body=pain008(collections, when=when.isoformat(), **kw))
        self.assertEqual(resp.status, 202, resp.body)
        return resp.json()

    def collection(self, end_to_end_id):
        return self.get("/_mock/collections/" + end_to_end_id).json()

    def acme_statements(self):
        return self.statements_for(ACME)


class SettlingOnTheClock(BookingCase):

    def test_it_books_on_its_settlement_date_and_the_statement_reconciles(self):
        before = self.balance("ACME")
        answer = self.collect([("C1", 1000), ("C2", 2500)])
        self.assertEqual([c["booked"] for c in answer["collections"]], [False, False])
        self.assertEqual(self.balance("ACME"), before, "not before its day")
        self.advance(MONDAY)                         # ends Thursday and Friday
        self.assertEqual(self.balance("ACME"), before + 3500)
        thursday, friday = self.acme_statements()
        self.assertEqual((thursday["opening"], thursday["closing"], thursday["entries"]),
                         (before, before, []))
        self.assertEqual((friday["opening"], friday["closing"]), (before, before + 3500))
        self.assertEqual(friday["entries"], [("C1", 1000), ("C2", 2500)])
        self.assertEqual(friday["payees"], ["Customer C1", "Customer C2"])

    def test_the_listing_says_when_it_was_received_and_when_it_booked(self):
        self.collect([("C1", 1000)], when=MONDAY)
        waiting = self.collection("C1")
        self.assertEqual((waiting["settlement_date"], waiting["booked_at"]),
                         (MONDAY.isoformat(), None))
        # Both on the bank clock (#147): received on the Thursday the file came
        # in, booked on the Tuesday the clock was moved to - not on the day it
        # was due, and not on the day this test ran.
        self.assertEqual(waiting["received_at"][:10], TODAY.isoformat())
        self.advance(TUESDAY)
        booked = self.collection("C1")
        self.assertEqual((booked["received_at"], booked["booked_at"][:10],
                          booked["settlement_date"]),
                         (waiting["received_at"], TUESDAY.isoformat(), MONDAY.isoformat()))
        self.assertEqual(self.get("/_mock/state").json()["collections"],
                         {"accepted": 1, "rejected": 0, "booked": 1})

    def test_advancing_twice_books_it_once(self):
        before = self.balance("ACME")
        self.collect([("C1", 1000)])
        self.advance(FRIDAY)
        self.advance(MONDAY)
        self.assertEqual(self.balance("ACME"), before + 1000)
        self.assertEqual(len(self.of_type(self.mailbox(), CAMT054)), 1)

    def test_a_rejected_collection_books_nothing(self):
        before = self.balance("ACME")
        answer = self.collect([("C1", 1000), ("C2", 2500)], mandate=False)
        self.assertEqual({c["reason"] for c in answer["collections"]}, {"MD02"})
        self.advance(MONDAY)
        self.assertEqual(self.balance("ACME"), before)
        self.assertEqual([s["entries"] for s in self.acme_statements()], [[], []])

    def test_a_debtor_the_bank_holds_is_not_debited(self):
        # Only the account holder's side books, as for a payment into an
        # account the bank holds.
        globex = self.balance("GLOBEX")
        answer = self.collect([("C1", 1000)], debtors={"C1": GLOBEX_IBAN})
        self.assertEqual(answer["accepted"], 1, answer)
        self.advance(MONDAY)
        self.assertEqual(self.balance("GLOBEX"), globex)
        self.assertEqual(self.statements_for(GLOBEX_IBAN)[-1]["entries"], [])


class WhatTheBankSaysAboutIt(BookingCase):

    def test_the_notification_is_a_credit_under_an_issued_direct_debit(self):
        self.collect([("C1", 1000)])
        self.mailbox()                               # the pain.002, collected
        self.advance(FRIDAY)
        [note] = self.of_type(self.mailbox(), CAMT054)
        [entry] = entries(note)
        self.assertEqual(entry, {
            "side": "CRDT", "amount": "10.00", "booked": FRIDAY.isoformat(),
            "value": FRIDAY.isoformat(),
            "servicer_ref": "MB-COL-%d" % self.collection("C1")["id"],
            "code": ("PMNT", "IDDT", "ESDD"),
            "refs": {"MsgId": "DD-1", "PmtInfId": "DD-1-B1", "EndToEndId": "C1",
                     "MndtId": "M-C1"},
            "debtor": "Customer C1", "debtor_iban": UMBRELLA_IBAN, "debtor_bic": "MOCKNL2A"})
        self.assertEqual(entry["code"], schema.COLLECTED_CREDIT)
        self.assertIn(schema.COLLECTED_CREDIT, schema.BANK_TRANSACTION_CODES)

    def test_the_statement_and_the_intraday_report_carry_the_same_entry(self):
        before = self.balance("ACME")
        self.collect([("C1", 1000)])
        self.advance(FRIDAY)
        report = self.post("/_mock/accounts/ACME/report").json()
        self.assertEqual((report["opening"], report["interim"], report["entries"]),
                         (before, before + 1000, 1))
        self.advance(MONDAY)
        messages = self.mailbox()
        [intraday] = self.of_type(messages, CAMT052)
        friday = [r for r in self.of_type(messages, CAMT053)
                  if read_statement(r)["iban"] == ACME][-1]
        for root in (intraday, friday):
            [entry] = entries(root)
            self.assertEqual((entry["side"], entry["code"], entry["refs"]["EndToEndId"],
                              entry["refs"]["MndtId"], entry["debtor"]),
                             ("CRDT", ("PMNT", "IDDT", "ESDD"), "C1", "M-C1", "Customer C1"))

    def test_a_nacha_accounts_bai2_statement_says_165(self):
        # sample4 writes 165 for the proceeds of an "ACH Debit Collection".
        self.patch("/_mock/accounts/ACME", body={"format": "nacha", "currency": "USD"})
        before = self.balance("ACME")
        self.collect([("C1", 1000)], ccy="USD", account_ccy="USD")
        self.advance(MONDAY)
        self.assertEqual(self.balance("ACME"), before + 1000)
        friday = [m["body"] for m in self.get("/_mock/mailbox?type=bai2").json()][-1]
        [statement] = bai2.statements(friday)
        self.assertEqual([tuple(e) for e in statement.entries],
                         [("165", 1000, "C1", "Customer C1")])
        self.assertIn("16,165,1000,Z,C1,DD-1,Customer C1/", friday)
        self.assertEqual((statement.opening, statement.closing), (before, before + 1000))
        self.assertEqual(bai2.trailers_agree(friday), [])

    def test_it_is_in_the_queue_until_it_settles_and_arrives_under_that_key(self):
        answer = self.collect([("C1", 1000)], when=MONDAY)
        key = "camt.054.001.08/ACME/%s/collections-settling" % MONDAY.isoformat()
        self.assertEqual([(q["type"], q.get("due_on"), q["key"]) for q in answer["queued"][1:]],
                         [("camt.054.001.08", MONDAY.isoformat(), key)])
        [waiting] = self.get("/_mock/queue").json()
        self.assertEqual((waiting["key"], waiting["reports"], waiting["written"]),
                         (key, "collections settling", False))
        self.advance(MONDAY)
        self.assertEqual(self.get("/_mock/queue").json(), [])
        self.assertEqual([m["key"] for m in self.get("/_mock/mailbox?leave&type=camt.054").json()],
                         [key])


class TheCeilingAndTheCalendar(BookingCase):

    def test_a_collection_the_balance_has_no_room_for_is_am02(self):
        # 1000 of room; 500 already on its way as money arriving; so 400 fits
        # and the 200 after it does not (#106).
        self.patch("/_mock/accounts/ACME", body={"balance": accounts.MAX_BALANCE - 1000})
        self.post("/_mock/credits", body={"account": "ACME", "amount": 500,
                                          "value_date": MONDAY.isoformat()})
        answer = self.collect([("C1", 400), ("C2", 200), ("C3", 100)])
        self.assertEqual([(c["end_to_end_id"], c["reason"]) for c in answer["collections"]],
                         [("C1", None), ("C2", "AM02"), ("C3", None)])
        self.assertIn("18 digits", answer["collections"][1]["reason_text"])
        # ...and what was accepted is itself on its way: no room is left to set.
        resp = self.patch("/_mock/accounts/ACME", body={"balance": accounts.MAX_BALANCE - 999})
        self.assertEqual(resp.status, 400, resp.body)
        self.advance(TUESDAY)
        self.assertEqual(self.balance("ACME"), accounts.MAX_BALANCE)
        self.assertEqual(self.acme_statements()[-1]["closing"], accounts.MAX_BALANCE)

    def test_a_holiday_moves_a_collection_still_to_settle(self):
        before = self.balance("ACME")
        self.collect([("C1", 1000)], when=MONDAY)
        resp = self.request("PUT", "/_mock/holidays", body=[MONDAY.isoformat()])
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(self.collection("C1")["settlement_date"], TUESDAY.isoformat())
        self.advance(TUESDAY + datetime.timedelta(days=1))
        self.assertEqual(self.balance("ACME"), before + 1000)
        by_day = {s["day"]: s["entries"] for s in self.acme_statements()}
        self.assertNotIn(MONDAY.isoformat(), by_day, "a holiday has no statement")
        self.assertEqual(by_day[TUESDAY.isoformat()], [("C1", 1000)])

    def test_a_day_a_collection_settled_on_cannot_become_a_holiday(self):
        self.collect([("C1", 1000)])
        self.advance(FRIDAY)
        resp = self.request("PUT", "/_mock/holidays", body=[FRIDAY.isoformat()])
        self.assertEqual(resp.status, 409, resp.body)
        self.assertIn("C1 was collected on ACME", resp.body.decode("utf-8"))

    def test_an_account_closed_while_it_waited_does_not_book_it(self):
        self.collect([("C1", 1000)])
        self.patch("/_mock/accounts/ACME", body={"closed": True})
        self.assertEqual(self.get("/_mock/queue").json(), [], "nothing is coming")
        self.advance(FRIDAY)
        waiting = self.collection("C1")
        self.assertEqual((waiting["booked_at"], waiting["settlement_date"]),
                         (None, MONDAY.isoformat()))
        # Reopened, it books on a day that has a statement, not its old one.
        before = self.balance("ACME")
        self.patch("/_mock/accounts/ACME", body={"closed": False})
        self.advance(TUESDAY)
        self.assertEqual(self.balance("ACME"), before + 1000)
        self.assertEqual(self.acme_statements()[-1]["entries"], [("C1", 1000)])


class TheWholeDayWalksClean(BookingCase):

    def test_every_message_a_collection_brings_fits_the_dictionary(self):
        self.collect([("C1", 1000)], debtors={"C1": "DE91100000000123456789"})
        self.advance(FRIDAY)
        self.post("/_mock/accounts/ACME/report")
        self.advance(MONDAY)
        for item in self.get("/_mock/mailbox?leave").json():
            root = ET.fromstring(item["body"].encode("utf-8"))
            self.assertEqual(schema.check(schema.identify(root), root), [], item["type"])
