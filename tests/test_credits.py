"""Money arriving: a credit from somebody else, booked and stated (#91).

Everything the mock booked before this was money leaving or coming back. Here
a payer the test describes pays in, and the credit has to reach the account on
the right day and show on the statement in a shape a cash application can be
tested against: a received transfer, the payer named, the note to payee as
the bank shows it and a structured reference apart from it. The balances are
worked out by hand, never read back from the statement they check.
"""
import datetime
from xml.etree import ElementTree as ET

from test_statements import (ACME, CAMT053, FRIDAY, M, MONDAY, TODAY, StatementCase,
                             minor, read_statement)

from mockbank import schema

CAMT054 = schema.MESSAGES["camt.054.001.08"]
N = {"m": CAMT054.namespace}
SATURDAY = TODAY + datetime.timedelta(days=2)
CUSTOMER = {"name": "Customer Ltd", "iban": "NL14MOCK0000000002", "bic": "MOCKNL2A"}


class CreditCase(StatementCase):

    def credit(self, status=201, **fields):
        body = dict({"account": "ACME", "amount": 125000, "debtor": CUSTOMER}, **fields)
        resp = self.post("/_mock/credits", body=body)
        self.assertEqual(resp.status, status, resp.body)
        return resp.json()

    def credit_entries(self, statement_root):
        """Each CRDT entry that is money received, as the XML says it."""
        out = []
        for entry in statement_root.iter("{%s}Ntry" % CAMT053.namespace):
            code = entry.find("m:BkTxCd/m:Domn", M)
            family = (code.findtext("m:Cd", namespaces=M),
                      code.findtext("m:Fmly/m:Cd", namespaces=M),
                      code.findtext("m:Fmly/m:SubFmlyCd", namespaces=M))
            if family != ("PMNT", "RCDT", "ESCT"):
                continue
            tx = entry.find("m:NtryDtls/m:TxDtls", M)
            out.append({
                "side": entry.findtext("m:CdtDbtInd", namespaces=M),
                "amount": minor(entry.findtext("m:Amt", namespaces=M)),
                "booked": entry.findtext("m:BookgDt/m:Dt", namespaces=M),
                "value": entry.findtext("m:ValDt/m:Dt", namespaces=M),
                "debtor": tx.findtext("m:RltdPties/m:Dbtr/m:Pty/m:Nm", namespaces=M),
                "debtor_iban": tx.findtext("m:RltdPties/m:DbtrAcct/m:Id/m:IBAN", namespaces=M),
                "note": [u.text for u in tx.findall("m:RmtInf/m:Ustrd", M)],
                "reference": tx.findtext("m:RmtInf/m:Strd/m:CdtrRefInf/m:Ref", namespaces=M),
            })
        return out

    def statement_roots(self):
        return [r for r in self.of_type(self.mailbox(), CAMT053)
                if read_statement(r)["iban"] == ACME]


class ArrivingOnTheClock(CreditCase):

    def test_it_books_on_its_value_date_and_the_statement_reconciles(self):
        before = self.balance("ACME")
        waiting = self.credit(value_date=FRIDAY.isoformat(), note="INV-1001")
        self.assertEqual((waiting["booking_date"], waiting["booked_at"]),
                         (FRIDAY.isoformat(), None))
        self.assertEqual(self.balance("ACME"), before, "not before its day")
        self.advance(MONDAY)                         # ends Thursday and Friday
        self.assertEqual(self.balance("ACME"), before + 125000)
        thursday, friday = [read_statement(r) for r in self.statement_roots()]
        self.assertEqual((thursday["opening"], thursday["closing"]), (before, before))
        self.assertEqual((friday["opening"], friday["closing"]), (before, before + 125000))
        self.assertEqual(friday["closing"], self.balance("ACME"))

    def test_a_weekend_credit_books_on_monday_with_its_own_value_date(self):
        self.credit(value_date=SATURDAY.isoformat())
        self.advance(MONDAY + datetime.timedelta(days=1))
        entries = [e for r in self.statement_roots() for e in self.credit_entries(r)]
        self.assertEqual([(e["booked"], e["value"]) for e in entries],
                         [(MONDAY.isoformat(), SATURDAY.isoformat())])

    def test_it_is_notified_as_it_books(self):
        self.credit()                                 # today, before the cutoff
        notices = self.of_type(self.mailbox(), CAMT054)
        received = [n for n in notices
                    if n.find(".//m:BkTxCd/m:Domn/m:Fmly/m:Cd", N).text == "RCDT"]
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].findtext(".//m:Ntry/m:CdtDbtInd", namespaces=N), "CRDT")


class WhatTheStatementShows(CreditCase):

    def test_a_received_transfer_with_the_payer_the_note_and_the_reference(self):
        self.credit(note=["Payment for INV-1001", "thank you"],
                    reference="RF18539007547034", end_to_end_id="CUST-77")
        self.advance(FRIDAY)
        [entry] = self.credit_entries(self.statement_roots()[0])
        self.assertEqual(entry["side"], "CRDT")
        self.assertEqual((entry["debtor"], entry["debtor_iban"]),
                         ("Customer Ltd", "NL14MOCK0000000002"))
        self.assertEqual(entry["note"], ["Payment for INV-1001", "thank you"])
        # The structured reference apart from the prose, not folded into it.
        self.assertEqual(entry["reference"], "RF18539007547034")

    def test_a_bank_that_rewraps_splits_an_invoice_number(self):
        # 35 characters, cut wherever they fall: here inside INV-2026-0041.
        self.credit(note="Paying invoice numbers INV-2026-0041 and INV-2026-0042",
                    wrap=35)
        self.advance(FRIDAY)
        [entry] = self.credit_entries(self.statement_roots()[0])
        self.assertEqual(entry["note"], ["Paying invoice numbers INV-2026-004",
                                         "1 and INV-2026-0042"])
        # Found only by joining the lines, which is what a reader has to do.
        self.assertNotIn("INV-2026-0041", entry["note"][0])
        self.assertIn("INV-2026-0041", "".join(entry["note"]))

    def test_a_credit_with_no_reference_at_all(self):
        self.credit()
        self.advance(FRIDAY)
        [entry] = self.credit_entries(self.statement_roots()[0])
        self.assertEqual((entry["note"], entry["reference"]), ([], None))

    def test_every_message_still_walks_clean(self):
        self.credit(note="x" * 200, reference="REF-1")
        self.advance(FRIDAY)
        for item in self.get("/_mock/mailbox?leave").json():
            root = ET.fromstring(item["body"].encode("utf-8"))
            self.assertEqual(schema.check(schema.identify(root), root), [], item["type"])


class Refused(CreditCase):

    def test_what_the_bank_would_not_book_is_refused_with_why(self):
        cases = [
            ({"account": "NOPE"}, 409, "no account"),
            ({"account": "INITECH"}, 409, "closed"),
            ({"amount": 12.5}, 400, "minor units"),
            ({"currency": "USD"}, 400, "no FX"),
            ({"value_date": "2026-09-01"}, 400, "before the bank's today"),
            ({"wrap": 50}, 400, "wrap"),
            ({"debtor": {"name": "X", "iban": "NL00MOCK0000000001"}}, 400, "check digits"),
            ({"colour": "blue"}, 400, "unknown field"),
        ]
        for fields, status, words in cases:
            with self.subTest(fields):
                answer = self.credit(status=status, **fields)
                self.assertIn(words, answer["error"])
        self.assertEqual(self.get("/_mock/credits").json(), [])

    def test_the_listing_shows_what_arrived_newest_first(self):
        self.credit(end_to_end_id="A")
        self.credit(end_to_end_id="B", value_date=FRIDAY.isoformat())
        self.assertEqual([c["end_to_end_id"] for c in self.get("/_mock/credits").json()],
                         ["B", "A"])
