"""Integration tests for payment_run, against mock-sap and mock-bank.

    pip install mock-sap
    mock-sap --port 8000 &
    python3 -m mockbank --port 8090 &
    cd examples && python3 -m unittest -v test_payment_run

SAP_URL and BANK_URL point the tests elsewhere.

mock-sap's seed has no supplier invoices, and no supplier banks at the IBANs
mock-bank holds (rseufert/mock-sap#62). So each test posts its invoices as
inbound `INVOIC` IDocs, the chain a real system runs, and gives three seeded
suppliers mock-bank's accounts through `A_BusinessPartnerBank`, the API a real
vendor master is maintained with.
"""
import datetime
import http.cookiejar
import json
import os
import unittest
import urllib.parse
import urllib.request

from pay_invoices import call
from payment_run import ODATA, OPEN_SUPPLIER_ITEMS, PaymentRun

SAP = os.environ.get("SAP_URL", "http://127.0.0.1:8000")
BANK = os.environ.get("BANK_URL", "http://127.0.0.1:8090")

ACME = {"name": "ACME Corporation", "iban": "NL41MOCK0000000001", "bic": "MOCKNL2A"}

# Seeded suppliers, pointed at what mock-bank's seed holds. INITECH's account
# is closed at the bank, and SAP still believes in it: stale bank details.
GLOBEX, ELSEWHERE, INITECH = "1000009", "1000010", "1000011"
ACCOUNTS = {
    GLOBEX: "NL14MOCK0000000002",       # held by mock-bank, open
    ELSEWHERE: "NL30MOCK0000000005",    # at another bank: settles
    INITECH: "NL84MOCK0000000003",      # held by mock-bank, closed: AC04
}


def control(base, method, path, body=None):
    """Talk to a mock's /_mock control plane."""
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read() or "null")


class Sap:
    """Writes to mock-sap, with the CSRF token and session a real client keeps."""

    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        fetch = urllib.request.Request(SAP + ODATA + "/API_BUSINESS_PARTNER_SRV/",
                                       headers={"X-CSRF-Token": "Fetch"})
        with self.opener.open(fetch) as response:
            self.token = response.headers["X-CSRF-Token"]

    def write(self, method, path, body, content_type):
        req = urllib.request.Request(
            SAP + path, method=method, data=body.encode("utf-8"),
            headers={"Content-Type": content_type, "X-CSRF-Token": self.token})
        with self.opener.open(req) as response:
            return response.read()

    def bank_account(self, supplier, iban):
        self.write("PATCH", ODATA + "/API_BUSINESS_PARTNER_SRV/A_BusinessPartnerBank"
                   "(BusinessPartner='%s',BankIdentification='0001')" % supplier,
                   json.dumps({"BankCountryKey": "NL", "IBAN": iban,
                               "SWIFTCode": "MOCKNL2A"}), "application/json")

    def invoice(self, supplier, reference, gross, terms="0001", dated=None):
        """An inbound INVOIC: the supplier bills us, and SAP posts a payable."""
        dated = (dated or datetime.date.today()).strftime("%Y%m%d")
        self.write("POST", "/sap/bc/idoc/idoc_xml", """<?xml version="1.0"?>
<INVOIC02><IDOC BEGIN="1">
<EDI_DC40 SEGMENT="1"><IDOCTYP>INVOIC02</IDOCTYP><MESTYP>INVOIC</MESTYP></EDI_DC40>
<E1EDK01 SEGMENT="1"><CURCY>EUR</CURCY><ZTERM>%s</ZTERM></E1EDK01>
<E1EDKA1 SEGMENT="1"><PARVW>LF</PARVW><PARTN>%s</PARTN></E1EDKA1>
<E1EDK02 SEGMENT="1"><QUALF>009</QUALF><BELNR>%s</BELNR></E1EDK02>
<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>%s</DATUM></E1EDK03>
<E1EDS01 SEGMENT="1"><SUMID>010</SUMID><SUMME>%s</SUMME></E1EDS01>
</IDOC></INVOIC02>""" % (terms, supplier, reference, dated, gross), "application/xml")


class PayingOpenItems(unittest.TestCase):

    def setUp(self):
        control(SAP, "POST", "/_mock/reset")
        control(BANK, "POST", "/_mock/reset")
        self.sap = Sap()
        for supplier, iban in ACCOUNTS.items():
            self.sap.bank_account(supplier, iban)
        self.today = datetime.date.fromisoformat(
            control(BANK, "GET", "/_mock/state")["clock"]["date"])
        self.payments = PaymentRun(SAP, BANK, ACME)

    def post_three(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.sap.invoice(ELSEWHERE, "ELS-0815", "238.00")
        self.sap.invoice(INITECH, "INI-2026-17", "595.00")

    def by_reference(self, run):
        return {item.reference: item for item in run.items}

    def test_a_closed_account_is_rejected_ac04_and_the_rest_accepted(self):
        self.post_three()
        run = self.payments.run(self.today, "RUN1")
        items = self.by_reference(run)
        self.assertEqual(set(items), {"GLX-4711", "ELS-0815", "INI-2026-17"})
        self.assertEqual((items["INI-2026-17"].status, items["INI-2026-17"].reason),
                         ("rejected", "AC04"))
        self.assertEqual(items["GLX-4711"].status, "accepted")
        self.assertEqual(items["ELS-0815"].status, "accepted")
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
        # An INVOIC cannot arrive blocked, and blocking the supplier invoice
        # afterwards does not reach its open item; see rseufert/mock-sap#62.
        self.skipTest("mock-sap cannot post a blocked supplier invoice yet "
                      "(rseufert/mock-sap#62)")


if __name__ == "__main__":
    unittest.main()
