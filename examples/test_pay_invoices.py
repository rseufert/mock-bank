"""Integration tests for pay_invoices, against mock-edi and mock-bank.

    pip install mock-edi
    mock-edi --port 8080 &
    python3 -m mockbank --port 8090 &
    cd examples && python3 -m unittest -v test_pay_invoices

EDI_URL and BANK_URL point the tests elsewhere.
"""
import copy
import datetime
import json
import os
import unittest
import urllib.request

from .pay_invoices import PayInvoices, call

EDI = os.environ.get("EDI_URL", "http://127.0.0.1:8080")
BANK = os.environ.get("BANK_URL", "http://127.0.0.1:8090")

ACME = {"id": "ACME", "name": "ACME Corporation",
        "iban": "NL41MOCK0000000001", "bic": "MOCKNL2A"}
# The supplier is mock-edi itself. Its account is at a bank mock-bank does
# not hold, so paying it is an ordinary transfer that settles.
SUPPLIER = {"name": "Mock EDI Supply Co", "iban": "NL03MOCK0000000006",
            "bic": "MOCKNL2A"}
# INITECH's account, which mock-bank holds and has closed: the stale bank
# details a vendor master is never short of.
CLOSED = {"name": "Mock EDI Supply Co", "iban": "NL84MOCK0000000003",
          "bic": "MOCKNL2A"}


def control(base, method, path, body=None):
    """Talk to a mock's /_mock control plane."""
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read() or "null")


def orders(po_number, quantity=12):
    """An EDIFACT ORDERS from ACME for control panels at 89.00, in euros."""
    today = datetime.date.today().strftime("%Y%m%d")
    return "\n".join([
        "UNA:+.? '",
        "UNB+UNOC:3+ACME:14+MOCKEDI:ZZ+%s:0900+%s'" % (today[2:], po_number[-4:]),
        "UNH+1+ORDERS:D:96A:UN'",
        "BGM+220+%s+9'" % po_number,
        "DTM+137:%s:102'" % today,
        "NAD+BY+ACME::9++ACME Corporation'",
        "CUX+2:EUR:9'",
        "LIN+1++PANEL-A4:VP'",
        "QTY+21:%d:PCE'" % quantity,
        "PRI+AAA:89.00'",
        "UNS+S'",
        "CNT+2:1'",
        "UNT+11+1'",
        "UNZ+1+%s'" % po_number[-4:],
    ])


class PayingSupplierInvoices(unittest.TestCase):

    def setUp(self):
        control(EDI, "POST", "/_mock/reset")
        control(BANK, "POST", "/_mock/reset")
        # ACME trades EDIFACT in euros here, so its invoices can be paid
        # by SEPA transfer from its account at mock-bank.
        control(EDI, "PATCH", "/_mock/partners/ACME",
                {"dialect": "EDIFACT", "version": "D:96A:UN", "qualifier": "14"})
        self.run_ = PayInvoices(EDI, BANK, ACME, {"MOCKEDI": dict(SUPPLIER)})

    # -- helpers ------------------------------------------------------------

    def supplier_behaves(self, behaviour):
        control(EDI, "PATCH", "/_mock/partners/ACME", {"behaviour": behaviour})

    def bank_behaves(self, **changes):
        control(BANK, "PATCH", "/_mock/accounts/ACME", changes)

    def invoiced(self, *po_numbers):
        """Order from the supplier and let it answer, up to the INVOIC."""
        for po_number in po_numbers:
            status, raw = call(EDI, "POST", "/edi", orders(po_number),
                               "application/edifact")
            self.assertEqual(status, 200, raw)
            self.assertTrue(json.loads(raw)["accepted"], raw)
        control(EDI, "POST", "/_mock/advance?all")
        return self.run_.collect()

    def balance(self):
        return control(BANK, "GET", "/_mock/accounts/ACME")["balance"]

    def settle(self):
        """Move bank time past every scheduled payment and its statement."""
        latest = max(datetime.date.fromisoformat(r["settles_on"])
                     for r in self.run_.ledger.values() if r["settles_on"])
        # Four days past: the settlement day's statement is issued when that
        # business day ends, and a Friday's is not issued until Monday.
        control(BANK, "POST", "/_mock/advance?to=%s"
                % (latest + datetime.timedelta(days=4)).isoformat())

    def statuses(self):
        return {key: (r["status"], r["problem"]) for key, r in self.run_.ledger.items()}

    # -- the tests ----------------------------------------------------------

    def test_accepted_is_scheduled_and_the_statement_makes_it_paid(self):
        collected = self.invoiced("PO-7001")
        [key] = collected["new"]
        record = self.run_.ledger[key]
        due = (datetime.date.fromisoformat(record["invoice"]["invoiced_on"])
               + datetime.timedelta(days=record["invoice"]["terms_days"]))
        before = self.balance()

        sent = self.run_.pay()
        self.assertEqual((sent["http_status"], sent["payments"]), (202, [key]))
        self.run_.reconcile()
        # The bank accepted it. It has not paid it: the money leaves on the
        # due date, and until a statement shows it the invoice is open to
        # anyone who asks whether it was paid.
        self.assertEqual(self.statuses()[key], ("scheduled", ""))
        self.assertEqual(record["settles_on"], due.isoformat())
        self.assertEqual(self.balance(), before)

        self.settle()
        self.run_.reconcile()
        self.assertEqual(self.statuses()[key], ("paid", ""))
        self.assertEqual(before - self.balance(), 106800)   # 12 x 89.00, in cents

    def test_closed_supplier_account_leaves_the_invoice_open(self):
        self.run_.vendors["MOCKEDI"] = dict(CLOSED)
        [key] = self.invoiced("PO-7002")["new"]
        before = self.balance()

        self.run_.pay()
        self.run_.reconcile()
        status, problem = self.statuses()[key]
        self.assertEqual(status, "open")
        self.assertTrue(problem.startswith("AC04"), problem)

        # Held, not retried: paying it again would bounce again.
        self.assertEqual(self.run_.pay()["payments"], [])
        self.settle_nothing_and_check(before)

    def settle_nothing_and_check(self, before):
        control(BANK, "POST", "/_mock/advance?days=45")
        self.run_.reconcile()
        self.assertEqual(self.balance(), before)

    def test_an_invoice_sent_twice_is_paid_once(self):
        self.supplier_behaves("duplicate-invoice")
        collected = self.invoiced("PO-7003")
        self.assertEqual((len(collected["new"]), collected["duplicates"]),
                         (1, collected["new"]))
        before = self.balance()

        self.assertEqual(len(self.run_.pay()["payments"]), 1)
        self.run_.reconcile()
        self.settle()
        self.run_.reconcile()
        self.assertEqual(before - self.balance(), 106800)

    def test_a_run_retried_after_a_crash_does_not_pay_twice(self):
        [key] = self.invoiced("PO-7004")["new"]
        before = self.balance()
        # The ledger as it was before the run. The run sends the file and
        # even reads the bank's acceptance, then dies before any of it is
        # saved - so the acceptance has been collected, and is gone.
        saved = copy.deepcopy(self.run_.ledger)
        first = self.run_.pay()
        self.run_.reconcile()

        self.run_.ledger = saved
        second = self.run_.pay()
        # The same payments make the same MsgId, so the bank knows it.
        self.assertEqual(second["msg_id"], first["msg_id"])
        self.assertEqual((first["http_status"], second["http_status"]), (202, 422))

        # All that is left in the mailbox is the copy's group-level DUPL.
        # It is about the copy, not the payments: they stay submitted, and
        # are not reopened for someone to pay again by hand.
        self.run_.reconcile()
        self.assertEqual(self.statuses()[key], ("submitted", ""))
        self.settle()
        self.run_.reconcile()
        self.assertEqual(self.statuses()[key], ("paid", ""))
        self.assertEqual(before - self.balance(), 106800)

    def test_insufficient_funds_leaves_invoices_open(self):
        self.bank_behaves(behaviour="insufficient-funds", balance=50000)
        keys = self.invoiced("PO-7005", "PO-7006")["new"]

        self.run_.pay()
        self.run_.reconcile()
        for key in keys:
            status, problem = self.statuses()[key]
            self.assertEqual(status, "open")
            self.assertTrue(problem.startswith("AM04"), problem)
        self.settle_nothing_and_check(50000)

    def test_a_returned_payment_reopens_the_invoice(self):
        # Paid, on the statement, and then sent back by the supplier's bank
        # three business days later: account closed, from their side.
        self.bank_behaves(behaviour="return-later",
                          parameters={"days": 3, "reason": "AC04"})
        [key] = self.invoiced("PO-7008")["new"]
        before = self.balance()

        self.run_.pay()
        self.run_.reconcile()
        # To the day after it settled, and no further: the return is three
        # business days behind it and must not have happened yet.
        number = self.run_.ledger[key]["invoice"]["number"]
        settled = control(BANK, "GET", "/_mock/payments/%s" % number)
        day = datetime.date.fromisoformat(settled["settlement_date"])
        control(BANK, "POST", "/_mock/advance?to=%s"
                % (day + datetime.timedelta(days=1)).isoformat())
        self.run_.reconcile()
        self.assertEqual(self.statuses()[key][0], "paid")

        control(BANK, "POST", "/_mock/advance?days=7")
        self.run_.reconcile()
        status, problem = self.statuses()[key]
        self.assertEqual(status, "open")
        self.assertTrue(problem.startswith("returned on ") and problem.endswith("AC04"),
                        problem)
        self.assertEqual(self.balance(), before)
        # Held, not paid again into the account that just sent it back.
        self.assertEqual(self.run_.pay()["payments"], [])

    def test_a_payment_missing_from_the_statement_is_not_paid(self):
        self.bank_behaves(behaviour="statement-gap")
        [key] = self.invoiced("PO-7007")["new"]

        self.run_.pay()
        self.run_.reconcile()
        self.settle()
        self.run_.reconcile()
        # The camt.054 said the money left; the camt.053 does not show it.
        # The statement is the bank's record, so this is not paid, and it
        # says why rather than sitting quietly in "notified".
        status, problem = self.statuses()[key]
        self.assertEqual(status, "notified")
        self.assertIn("not on the statement", problem)


if __name__ == "__main__":
    unittest.main()
