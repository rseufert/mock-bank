"""Integration tests for payment_run, against mock-sap and mock-bank.

    pip install mock-sap
    mock-sap --port 8000 &
    python3 -m mockbank --port 8090 --clock 2026-10-02T16:00 &
    cd examples && python3 -m unittest -v test_payment_run

SAP_URL and BANK_URL point the tests elsewhere.

mock-bank starts at 16:00 on a Friday, after its 15:00 cutoff, and a reset
returns it there. Test 6 needs exactly that moment, and the others advance to
Monday morning first, so each test knows which statement its payments are on.

mock-sap's seed has suppliers banking at the accounts mock-bank holds, but no
supplier invoices, so each test posts its own as inbound `INVOIC` IDocs, the
chain a real system runs. A block is set the way a real system sets one, on
the supplier invoice, and SAP carries it to the open item. Nothing here writes
to the open-item cube, which is read-only in SAP and in mock-sap.
"""
import datetime
import json
import os
import unittest
import urllib.parse
import urllib.request
from decimal import Decimal

from bank_messages import call
from payment_run import (ITEMS, ODATA, OPEN_SUPPLIER_ITEMS, Item, PaymentRun, Run,
                         SapSession, odata)

SAP = os.environ.get("SAP_URL", "http://127.0.0.1:8000")
BANK = os.environ.get("BANK_URL", "http://127.0.0.1:8090")

ACME = {"name": "ACME Corporation", "iban": "NL41MOCK0000000001", "bic": "MOCKNL2A"}

# mock-sap's seeded suppliers, which bank where mock-bank's seed says they do
# (mock-sap 0.13.1). INITECH's account is closed at the bank, and SAP still
# believes in it: stale bank details.
GLOBEX, INITECH, UMBRELLA = "1000013", "1000014", "1000016"
ACCOUNTS = {
    GLOBEX: "NL14MOCK0000000002",       # held by mock-bank, open
    UMBRELLA: "NL30MOCK0000000005",     # at another bank: settles
    INITECH: "NL84MOCK0000000003",      # held by mock-bank, closed: AC04
}


def control(base, method, path, body=None):
    """Talk to a mock's /_mock control plane."""
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read() or "null")


class Sap(SapSession):
    """The test's own writes to mock-sap: vendor master and inbound invoices."""

    def __init__(self, today):
        super().__init__(SAP)
        self.today = today

    def invoice(self, supplier, reference, gross, terms="0001", dated=None):
        """An inbound INVOIC: the supplier bills us, and SAP posts a payable.

        Returns what SAP posted: the supplier invoice, its fiscal year and the
        accounting document behind the open item.
        """
        # The bank's date, not the host's: the run compares the due date with
        # the bank's today, and the two differ whenever bank time is pinned.
        dated = (dated or self.today).strftime("%Y%m%d")
        receipt = self.write("POST", "/sap/bc/idoc/idoc_xml", """<?xml version="1.0"?>
<INVOIC02><IDOC BEGIN="1">
<EDI_DC40 SEGMENT="1"><IDOCTYP>INVOIC02</IDOCTYP><MESTYP>INVOIC</MESTYP></EDI_DC40>
<E1EDK01 SEGMENT="1"><CURCY>EUR</CURCY><ZTERM>%s</ZTERM></E1EDK01>
<E1EDKA1 SEGMENT="1"><PARVW>LF</PARVW><PARTN>%s</PARTN></E1EDKA1>
<E1EDK02 SEGMENT="1"><QUALF>009</QUALF><BELNR>%s</BELNR></E1EDK02>
<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>%s</DATUM></E1EDK03>
<E1EDS01 SEGMENT="1"><SUMID>010</SUMID><SUMME>%s</SUMME></E1EDS01>
</IDOC></INVOIC02>""" % (terms, supplier, reference, dated, gross), "application/xml")
        return json.loads(receipt)["APPLIED"][0]

    def block(self, posted, reason="A"):
        """Block a posted invoice for payment, on the invoice, as SAP users do."""
        self.write("PATCH", ODATA + "/API_SUPPLIERINVOICE_PROCESS_SRV/A_SupplierInvoice"
                   "(SupplierInvoice='%s',FiscalYear='%s')"
                   % (posted["SUPPLIERINVOICE"], posted["FISCALYEAR"]),
                   json.dumps({"PaymentBlockingReason": reason}), "application/json")


class MocksCase(unittest.TestCase):
    """Both mocks reset, and bank time read from the bank."""

    def setUp(self):
        control(SAP, "POST", "/_mock/reset")
        control(BANK, "POST", "/_mock/reset")
        self.today = self.bank_today()
        self.sap = Sap(self.today)
        self.payments = PaymentRun(SAP, BANK, ACME)

    def bank_today(self):
        return datetime.date.fromisoformat(
            control(BANK, "GET", "/_mock/state")["clock"]["date"])

    def post_three(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.sap.invoice(UMBRELLA, "UMB-0815", "238.00")
        self.sap.invoice(INITECH, "INI-2026-17", "595.00")

    def by_reference(self, run):
        return {item.reference: item for item in run.items}


class PayingOpenItems(MocksCase):

    def test_a_closed_account_is_rejected_ac04_and_the_rest_accepted(self):
        self.post_three()
        run = self.payments.run(self.today, "RUN1")
        items = self.by_reference(run)
        self.assertEqual(set(items), {"GLX-4711", "UMB-0815", "INI-2026-17"})
        self.assertEqual((items["INI-2026-17"].status, items["INI-2026-17"].reason),
                         ("rejected", "AC04"))
        self.assertEqual(items["GLX-4711"].status, "accepted")
        self.assertEqual(items["UMB-0815"].status, "accepted")
        # The EndToEndId the bank holds is the supplier's invoice number, and
        # each payment went to the account its invoice names.
        paid = control(BANK, "GET", "/_mock/payments/GLX-4711")
        self.assertEqual(paid["creditor_iban"], ACCOUNTS[GLOBEX])
        self.assertEqual(paid["amount"], 119000)

    def test_the_same_run_twice_is_dupl_and_pays_nothing_twice(self):
        self.post_three()
        first = self.payments.run(self.today, "RUN1")
        paid = control(BANK, "GET", "/_mock/payments")
        balance = control(BANK, "GET", "/_mock/accounts/ACME")["balance"]
        # Nothing clears an open item until the statement is posted back to
        # SAP (#47), so the same run selects the same items again.
        again = self.payments.run(self.today, "RUN1")
        self.assertEqual(again.msg_id, first.msg_id)
        self.assertTrue(again.duplicate)
        self.assertEqual(control(BANK, "GET", "/_mock/payments"), paid)
        self.assertEqual(control(BANK, "GET", "/_mock/accounts/ACME")["balance"], balance)
        # The refusal of the copy does not undo what the first file did.
        self.assertEqual(self.by_reference(first)["GLX-4711"].status, "accepted")
        self.assertNotIn("rejected", {i.status for i in again.items
                                      if i.reference != "INI-2026-17"})

    def test_an_item_not_yet_due_is_not_selected(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.sap.invoice(GLOBEX, "GLX-4712", "50.00", terms="NT30")
        run = self.payments.run(self.today, "RUN1")
        self.assertEqual([i.reference for i in run.items], ["GLX-4711"])
        status, _body = call(BANK, "GET", "/_mock/payments/GLX-4712")
        self.assertEqual(status, 404)

    def test_the_selection_asks_sap_to_leave_blocked_and_cleared_items_out(self):
        self.payments.select(self.today)
        asked = [urllib.parse.unquote(row["query"]) for row in
                 control(SAP, "GET", "/_mock/requests")["results"]
                 if row["path"].endswith("A_OperationalAcctgDocItemCube")]
        self.assertTrue(asked and OPEN_SUPPLIER_ITEMS in asked[0], asked)

    def test_a_blocked_invoice_is_never_selected(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.sap.block(self.sap.invoice(UMBRELLA, "UMB-0815", "238.00"))
        run = self.payments.run(self.today, "RUN1")
        self.assertEqual([i.reference for i in run.items], ["GLX-4711"])
        status, _body = call(BANK, "GET", "/_mock/payments/UMB-0815")
        self.assertEqual(status, 404)


class StatementCase(MocksCase):
    """Bank time pinned to a Friday after the cutoff, and SAP's items to look at."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.today.weekday(), 4, "start mock-bank with "
                         "--clock 2026-10-02T16:00, a Friday after the cutoff")
        self.monday = self.today + datetime.timedelta(days=3)

    def advance(self, to):
        control(BANK, "POST", "/_mock/advance?to=%s" % to.isoformat())

    def post_two(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.sap.invoice(UMBRELLA, "UMB-0815", "238.00")

    def cube_item(self, reference):
        """SAP's open-item line for a supplier invoice number."""
        invoice = odata(SAP, ODATA + "/API_SUPPLIERINVOICE_PROCESS_SRV/A_SupplierInvoice",
                        **{"$filter": "SupplierInvoiceIDByInvcgParty eq '%s'" % reference})[0]
        return odata(SAP, ITEMS, **{"$filter": (
            "AccountingDocument eq '%s' and AccountingDocumentItemType eq 'K'"
            % invoice["AccountingDocument"])})[0]

    def clearing(self, reference):
        """The clearing document on that line; "" while it is open."""
        return self.cube_item(reference)["ClearingAccountingDocument"]

    def pay_on_monday(self):
        """A run on Monday morning, before the cutoff: it settles that day."""
        self.advance(self.monday)
        run = self.payments.run(self.monday, "RUN1")
        self.advance(self.monday + datetime.timedelta(days=1))
        return run


class ReconcilingTheStatement(StatementCase):
    """#47: each camt.053 posted to SAP as a FINSTA01, clearing what it paid."""

    def test_1_a_clean_run_is_paid_matched_and_cleared(self):
        self.post_two()
        run = self.pay_on_monday()
        self.payments.reconcile(run)
        monday = [s for s in run.statements if s["date"] == self.monday.isoformat()]
        self.assertEqual(len(monday), 1, run.statements)
        self.assertTrue(monday[0]["adds_up"])
        self.assertEqual(monday[0]["findings"], [])
        self.assertEqual({i.reference: i.status for i in run.items},
                         {"GLX-4711": "cleared", "UMB-0815": "cleared"})
        for reference in ("GLX-4711", "UMB-0815"):
            self.assertNotEqual(self.clearing(reference), "", reference)
        # Cleared in SAP, so the next run has nothing left to pay.
        self.assertEqual(self.payments.select(self.monday), [])

    def test_5_a_statement_gap_leaves_the_missing_payment_unreconciled_and_open(self):
        control(BANK, "PATCH", "/_mock/accounts/ACME", {"behaviour": "statement-gap"})
        self.post_two()
        run = self.pay_on_monday()
        self.payments.reconcile(run)
        monday = [s for s in run.statements if s["date"] == self.monday.isoformat()][0]
        self.assertFalse(monday["adds_up"])
        # SAP's own arithmetic check says so too, in its own words.
        self.assertTrue(any("does not add up" in f for f in monday["findings"]),
                        monday["findings"])
        statuses = sorted(i.status for i in run.items)
        self.assertEqual(statuses, ["cleared", "unreconciled"])
        missing = [i for i in run.items if i.status == "unreconciled"][0]
        self.assertIn("short", missing.reason)
        self.assertEqual(self.clearing(missing.reference), "")

    def test_6_after_the_cutoff_it_waits_for_mondays_statement(self):
        self.post_two()
        run = self.payments.run(self.today, "RUN1")        # Friday, 16:00
        self.advance(self.today + datetime.timedelta(days=1))
        self.payments.reconcile(run)
        friday = [s for s in run.statements if s["date"] == self.today.isoformat()]
        self.assertEqual(len(friday), 1, run.statements)
        # An empty day's statement posts harmlessly: nothing cleared, nothing
        # it could not place, nothing wrong with it.
        self.assertEqual((friday[0]["cleared"], friday[0]["unprocessed"],
                          friday[0]["findings"]), ([], [], []))
        self.assertEqual({i.status for i in run.items}, {"accepted"})
        self.advance(self.monday + datetime.timedelta(days=1))
        self.payments.reconcile(run)
        self.assertEqual({i.status for i in run.items}, {"cleared"})

    def test_posting_the_same_statement_twice_clears_nothing_twice(self):
        self.post_two()
        run = self.pay_on_monday()
        self.payments.reconcile(run)
        monday = [s for s in run.statements if s["date"] == self.monday.isoformat()][0]
        before = {r: self.clearing(r) for r in ("GLX-4711", "UMB-0815")}
        again = self.payments.session.post_idoc(monday["finsta"])
        self.assertEqual(again.get("CLEARED"), [])
        self.assertEqual(again.get("REOPENED"), [])
        self.assertEqual({r: self.clearing(r) for r in before}, before)


class AReturnedPayment(StatementCase):
    """#48: a return on a later statement reopens the invoice it paid."""

    def test_3_a_return_reopens_the_invoice_distinguishable_from_one_never_paid(self):
        control(BANK, "PATCH", "/_mock/accounts/ACME", {
            "behaviour": "return-later",
            "parameters": {"end_to_end_id": "GLX-4711", "days": 3, "reason": "AC04"}})
        self.post_three()
        run = self.pay_on_monday()               # INITECH is rejected AC04: never paid
        self.payments.reconcile(run)
        self.assertEqual(self.by_reference(run)["GLX-4711"].status, "cleared")
        # Three business days after Monday's settlement is Thursday; Friday
        # morning, Thursday's statement is in.
        self.advance(self.monday + datetime.timedelta(days=4))
        self.payments.reconcile(run)
        items = self.by_reference(run)
        self.assertEqual(items["GLX-4711"].status, "returned")
        self.assertTrue(items["GLX-4711"].reason.startswith("AC04"), items["GLX-4711"].reason)
        self.assertEqual(items["UMB-0815"].status, "cleared")
        self.assertEqual(items["INI-2026-17"].status, "rejected")
        # In SAP: returned and never paid are both open, and only one was paid.
        returned, never = self.cube_item("GLX-4711"), self.cube_item("INI-2026-17")
        self.assertEqual((returned["ClearingAccountingDocument"], never["ClearingAccountingDocument"]),
                         ("", ""))
        self.assertEqual((returned["ClearingIsReversed"], never["ClearingIsReversed"]),
                         (True, False))
        self.assertNotEqual(self.cube_item("UMB-0815")["ClearingAccountingDocument"], "")
        # Owed again, so the next run selects it with the one never paid.
        self.assertEqual(sorted(i.reference for i in self.payments.select(self.bank_today())),
                         ["GLX-4711", "INI-2026-17"])


class WhenSomethingAnswersBadly(MocksCase):
    """#73's notes: say what went wrong rather than stop or stay silent."""

    def test_sap_refusing_a_statement_is_recorded_against_it(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        run = self.payments.run(self.today, "RUN1")
        control(BANK, "POST", "/_mock/advance?to=%s"
                % (self.today + datetime.timedelta(days=1)).isoformat())
        control(SAP, "POST", "/_mock/faults", {"method": "POST", "match": "/sap/bc/idoc",
                                               "status": 503, "count": 1})
        self.payments.reconcile(run)
        self.assertIn("503", run.statements[0]["error"])
        self.assertEqual(run.problems, [run.statements[0]["error"]])

    def test_a_bank_that_does_not_answer_is_a_problem_not_silence(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        nowhere = PaymentRun(SAP, SAP, ACME)           # SAP is no bank: 404 throughout
        run = nowhere.run(self.today, "RUN1")
        nowhere.reconcile(run)
        self.assertEqual(len(run.problems), 3, run.problems)
        self.assertIn("answered 404 to the payment file", run.problems[0])
        self.assertIn("no status report was read", run.problems[1])
        self.assertIn("no statement was read", run.problems[2])
        self.assertEqual(run.items[0].status, "sent")

    def test_two_payments_of_the_missing_amount_are_both_named(self):
        run = Run(self.today, "RUN1", [
            Item("1/2026/1", GLOBEX, "GLX-1", "238.00", status="accepted"),
            Item("1/2026/2", UMBRELLA, "UMB-1", "238.00", status="accepted"),
            Item("1/2026/3", UMBRELLA, "UMB-2", "50.00", status="accepted")])
        self.payments.name_the_shortfall(run, "7", "2026-10-05", Decimal("238"))
        self.assertEqual([i.status for i in run.items],
                         ["unreconciled", "unreconciled", "accepted"])
        self.assertIn("GLX-1, UMB-1", run.items[0].reason)
        self.assertIn("cannot say which", run.items[0].reason)


if __name__ == "__main__":
    unittest.main()
