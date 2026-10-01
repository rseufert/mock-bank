"""Integration tests for procure_to_pay, against all three mocks.

    pip install "mock-sap>=0.13.2" mock-edi
    mock-sap --port 8000 &
    mock-edi --port 8080 &
    python3 -m mockbank --port 8090 --clock 2026-10-02T16:00 &
    cd examples && python3 -m unittest -v test_procure_to_pay

SAP_URL, EDI_URL and BANK_URL point the tests elsewhere.

**mock-sap 0.13.2 is a real floor.** Before it, the `INVOIC` this example sends
created no payable at all and the `850` declared no currency, so there was
nothing to pay and the payment run refused what there was.

**mock-edi has no floor**, which is worth stating rather than leaving to a pin
nobody checked: these tests were run against every published mock-edi back to
0.2.1, the oldest on PyPI, and all of them pass. The example uses the `850`'s
`CUR` segment, `/_mock/send` and the `short-ship` behaviour, and all three have
been there throughout.

Each test runs a whole purchase: a purchase order in SAP, an 850 to the supplier,
the supplier's answers, the three-way match, the posting, a payment run and a
statement. That is slower to arrange than a two-mock test and it is the only way
to see the failures here, all of which live between a pair of mocks that each
believe they are fine.

Two things about the arrangement are worth knowing before reading the tests.

**The due date is read, not assumed.** The supplier's `810` carries its own date
and its net payment days, so the payable falls due about thirty days out. The
bank's clock is then advanced to that date, because a payment run pays what is
due and the bank decides what day it is. Hard-coding a date here would be
asserting the arithmetic of whichever day the suite ran.

**The settlement date is read too.** That due date falls on whichever day of the
week is thirty days from the day the tests run, weekends included, and the bank
settles a weekend's run on Monday. So after paying, the bank's clock is advanced
to the day after the settlement date the bank itself gives for each payment,
which is the first day the statement carrying it exists (#151).

**Every order needs its own interchange control number.** mock-edi refuses a
replayed interchange with a `TA1` rather than fulfilling it twice, so a test
placing two orders passes two numbers. `self.control_number` counts them.
"""
from __future__ import annotations

import datetime
import json
import os
import unittest
import urllib.parse
import urllib.request
from decimal import Decimal

from invoice_check import PO_SERVICE, Sap
from payment_run import ITEMS, OPEN_SUPPLIER_ITEMS, odata
from procure_to_pay import DurableInvoiceCheck, ProcureToPay, odata_string

SAP = os.environ.get("SAP_URL", "http://127.0.0.1:8000")
EDI = os.environ.get("EDI_URL", "http://127.0.0.1:8080")
BANK = os.environ.get("BANK_URL", "http://127.0.0.1:8090")

ACME = {"name": "ACME Corporation", "iban": "NL41MOCK0000000001", "bic": "MOCKNL2A"}

# mock-sap's seeded suppliers that bank where mock-bank can act on them. GLOBEX's
# account is open; INITECH's is closed, and SAP still believes in it.
GLOBEX, INITECH = "1000013", "1000014"

CUBE = ("/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
        "/A_OperationalAcctgDocItemCube")


def control(base, method, path, body=None):
    """Talk to a mock's /_mock control plane."""
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read() or "null")


class PurchaseCase(unittest.TestCase):
    def setUp(self):
        for base in (SAP, EDI, BANK):
            control(base, "POST", "/_mock/reset")
        self.sap = Sap(SAP)
        self.control_number = 0
        self.p2p = self.middleware()

    def middleware(self, durable=True):
        return ProcureToPay(SAP, EDI, BANK, our_id="ACME", company=ACME,
                            durable=durable)

    # -- arranging -------------------------------------------------------------

    def purchase(self, supplier=GLOBEX, price="12.50", quantity="100",
                 currency="EUR", p2p=None):
        """A purchase order in SAP, sent to the supplier as an 850."""
        po = self.sap.request("POST", PO_SERVICE + "/A_PurchaseOrder", {
            "PurchaseOrderType": "NB", "CompanyCode": "1710",
            "PurchasingOrganization": "1710", "PurchasingGroup": "001",
            "Supplier": supplier, "DocumentCurrency": currency,
            "to_PurchaseOrderItem": [
                {"Material": "TG11", "OrderQuantity": quantity,
                 "NetPriceAmount": price, "PurchaseOrderQuantityUnit": "PC",
                 "Plant": "1010"}]})["d"]["PurchaseOrder"]
        self.control_number += 1
        summary = (p2p or self.p2p).order(po, sender="ACME",
                                         control=self.control_number)
        self.assertTrue(summary["accepted"], summary)
        return po

    def supplier_resends(self, po_number, *kinds):
        """The supplier sends documents again, after we have taken the first set.

        Not the `duplicate-invoice` behaviour, which sends both copies at once:
        those arrive in a single mailbox read and the in-memory check does catch
        them. This is the copy that arrives *after* the first was processed, with
        the same invoice number - the one that gets through.

        Which documents are resent turns out to matter, and the reason is worth
        knowing. A restarted middleware has forgotten its ship notices as well as
        what it posted, so an invoice arriving alone is blocked for billing more
        than was shipped - `item 00010 bills 100, shipped 0`. That is protection
        by accident, from a second thing being lost rather than from anything
        checking. Resend the despatch advice with it, as a partner replaying a
        batch does, and the invoice posts again.
        """
        for kind in kinds:
            control(EDI, "POST", "/_mock/send",
                    {"partner": "ACME", "kind": kind, "order": po_number})

    def post_invoice(self, supplier, reference, gross):
        """An INVOIC straight into SAP, for a reference mock-edi will not mint."""
        idoc = ("""<?xml version="1.0"?>
<INVOIC02><IDOC BEGIN="1">
<EDI_DC40 SEGMENT="1"><IDOCTYP>INVOIC02</IDOCTYP><MESTYP>INVOIC</MESTYP></EDI_DC40>
<E1EDK01 SEGMENT="1"><CURCY>EUR</CURCY><ZTERM>NT30</ZTERM></E1EDK01>
<E1EDKA1 SEGMENT="1"><PARVW>LF</PARVW><PARTN>%s</PARTN></E1EDKA1>
<E1EDK02 SEGMENT="1"><QUALF>009</QUALF><BELNR>%s</BELNR></E1EDK02>
<E1EDS01 SEGMENT="1"><SUMID>010</SUMID><SUMME>%s</SUMME></E1EDS01>
</IDOC></INVOIC02>""" % (supplier, reference, gross))
        receipt = self.sap.request("POST", "/sap/bc/idoc", idoc, "application/xml")
        self.assertEqual(receipt["STATUS"], "53", receipt)
        return receipt["APPLIED"][0]

    def supplier_behaves(self, behaviour):
        control(EDI, "PATCH", "/_mock/partners/ACME", {"behaviour": behaviour})

    def bank_today(self):
        return datetime.date.fromisoformat(
            control(BANK, "GET", "/_mock/state")["clock"]["date"])

    def advance_bank_to(self, day):
        """Move the bank's clock to `day`, never backwards.

        A clock that can be rewound is not a clock, and mock-bank refuses with
        400 - correctly. A second payment run in one test is therefore made on
        the bank's own today rather than on a date computed from the due date,
        which has already passed by then.
        """
        if day > self.bank_today():
            control(BANK, "POST", "/_mock/advance?to=%s" % day.isoformat())

    # -- looking ---------------------------------------------------------------

    def open_items(self):
        return odata(SAP, ITEMS, **{"$filter": OPEN_SUPPLIER_ITEMS})

    def cube_rows(self, reference=""):
        """Every accounting item, cleared or not, for reading clearing state."""
        query = {"$filter": "AccountingDocumentItemType eq 'K'", "$format": "json"}
        rows = self.sap.request("GET", "%s?%s" % (CUBE, urllib.parse.urlencode(query)))
        rows = rows["d"]["results"]
        if not reference:
            return rows
        numbers = self.invoice_numbers(reference)
        return [r for r in rows if r["AccountingDocument"] in numbers]

    def invoice_numbers(self, reference):
        """The accounting documents behind a supplier's own invoice number."""
        query = urllib.parse.urlencode({
            "$filter": "SupplierInvoiceIDByInvcgParty eq '%s'" % odata_string(reference),
            "$format": "json"})
        found = self.sap.request(
            "GET", "/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV"
                   "/A_SupplierInvoice?" + query)["d"]["results"]
        return {row["AccountingDocument"] for row in found}

    def pay_what_is_due(self, identification="RUN1", p2p=None):
        """Advance the bank to the due date, pay, settle, and reconcile."""
        runner = p2p or self.p2p
        due = runner.last_due_date()
        self.assertIsNotNone(due, "nothing is owed, so there is nothing to pay")
        self.advance_bank_to(due)
        run_on = max(due, self.bank_today())
        run = runner.pay(run_on, identification)
        self.advance_bank_to(self.day_after_settlement(run))
        runner.reconcile(run)
        return run

    def day_after_settlement(self, run):
        """The day after the bank settles this run: the first day its statement
        exists.

        Asked of the bank rather than taken as tomorrow. The due date comes from
        the supplier's invoice, dated on the real day the tests run, so it lands
        on every day of the week in turn; and a run made on a Saturday settles on
        Monday, whose statement is not out on Sunday. Advancing one day passed on
        every date that put the run on a weekday and failed on the rest (#151).
        """
        settled = [control(BANK, "GET", "/_mock/payments/%s"
                           % urllib.parse.quote(item.reference, safe=""))["settlement_date"]
                   for item in run.items if item.status == "accepted"]
        last = max([datetime.date.fromisoformat(day) for day in settled]
                   + [self.bank_today()])
        return last + datetime.timedelta(days=1)


class TestOnePurchase(PurchaseCase):
    def test_1_a_purchase_becomes_a_cleared_payment(self):
        """The whole loop, which is the thing no two mocks can show."""
        self.purchase()

        [approved] = self.p2p.approve()
        self.assertEqual((approved["status"], approved["problems"]), ("posted", []))
        self.assertTrue(approved["supplier_invoice"])

        [item] = self.open_items()
        self.assertEqual(item["Supplier"], GLOBEX)
        self.assertEqual(item["TransactionCurrency"], "EUR")
        self.assertEqual(abs(Decimal(item["AmountInTransactionCurrency"])),
                         Decimal("1250.00"))

        run = self.pay_what_is_due()

        self.assertEqual([i.status for i in run.items], ["cleared"])
        self.assertEqual(run.problems, [])
        self.assertEqual(self.open_items(), [], "paid and cleared is not open")

    def test_the_payment_carries_the_suppliers_own_invoice_number(self):
        """The reference is what makes the bank's answer findable in SAP.

        `EndToEndId` is the supplier's invoice number, which SAP stored as
        `SupplierInvoiceIDByInvcgParty` when the `INVOIC` posted. If those two
        ever stop being the same string the statement still balances and nothing
        clears, which is a bad afternoon.
        """
        self.purchase()
        [approved] = self.p2p.approve()

        run = self.pay_what_is_due()

        [item] = run.items
        self.assertEqual(item.reference, approved["invoice"])
        paid = control(BANK, "GET", "/_mock/payments/%s" % item.reference)
        self.assertEqual(paid["end_to_end_id"], approved["invoice"])


class TestTheSameInvoiceTwice(PurchaseCase):
    """#93's headline: the failure only the third mock makes visible."""

    def test_2_without_asking_sap_the_duplicate_is_paid_too(self):
        """Two payables for one invoice, and the second is paid in the next run.

        The near miss is the interesting part. In *one* run `select` skips the
        second item, because two payments in one file must not share an
        `EndToEndId` - so it looks as though something caught it. It did not: the
        next run pays it, because by then the first has cleared and the second is
        alone in the selection.

        There is a second near miss upstream, in `supplier_resends`: a restarted
        middleware has also forgotten its ship notices, so an invoice arriving on
        its own is blocked for billing more than was shipped. Both near misses
        are accidents of what else was lost, and neither is a check.
        """
        plain = self.middleware(durable=False)
        po = self.purchase(p2p=plain)

        plain.approve()                       # posts the invoice
        self.supplier_resends(po, "despatch", "invoice")
        restarted = self.middleware(durable=False)
        posted_again = restarted.approve()     # the copy looks new to a fresh process
        self.assertTrue(all(r["status"] == "posted" for r in posted_again), posted_again)

        payables = self.open_items()
        self.assertEqual(len(payables), 2, "one invoice, two things owed")
        self.assertEqual({p["Supplier"] for p in payables}, {GLOBEX})

        first = self.pay_what_is_due("RUN1", p2p=restarted)
        statuses = sorted(i.status for i in first.items)
        self.assertEqual(statuses, ["cleared", "skipped"],
                         "one paid; the other skipped for sharing its reference")

        second = self.pay_what_is_due("RUN2", p2p=restarted)
        self.assertEqual([i.status for i in second.items], ["cleared"],
                         "nothing refused the duplicate: the supplier is paid twice")
        self.assertEqual(self.open_items(), [])

    def test_the_duplicate_question_is_asked_per_supplier(self):
        """Two suppliers may both number an invoice `INV-1`, and one is not the other.

        `SupplierInvoiceIDByInvcgParty` is only unique within an invoicing party,
        so a check that matched on the reference alone would refuse a second
        supplier's unrelated invoice as a duplicate - and the money would simply
        never be paid, which is the quietest failure of the lot. This drives the
        check directly, because mock-edi numbers its own invoices and will not
        issue the same number as two different partners.
        """
        self.purchase()
        [approved] = self.p2p.approve()
        reference = approved["invoice"]

        self.assertTrue(self.p2p.check.already_posted(reference, GLOBEX))
        self.assertFalse(self.p2p.check.already_posted(reference, INITECH),
                         "another supplier's invoice of the same number is not ours")

    def test_a_resent_invoice_alone_is_blocked_for_the_wrong_reason(self):
        """The upstream near miss, held by a test rather than only described.

        Resend only the invoice and a restarted middleware does block it - but
        for billing more than was shipped, because the restart forgot the ship
        notice too. That is a second thing being missing, not the duplicate being
        caught, and it is why `test_2` resends the despatch advice as well. If
        this ever starts failing with a different reason, the story the example
        tells about accidental protection has changed.
        """
        po = self.purchase()
        self.p2p.approve()
        self.supplier_resends(po, "invoice")           # the invoice, and nothing else

        restarted = self.middleware(durable=False)     # no SAP check, so the
        [result] = restarted.approve()                 # only objection is the match

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("bills 100, shipped 0" in p for p in result["problems"]),
                        result["problems"])
        self.assertEqual(len(self.open_items()), 1, "still one thing owed, not two")

    def test_an_invoice_number_holding_a_quote_is_asked_about_correctly(self):
        """`O'BRIEN-014` is a supplier's invoice number, not OData syntax.

        The number is the supplier's to choose, and an apostrophe closes a
        `$filter` literal early: mock-sap answers `400`, so without doubling the
        quote this check raises instead of answering and every invoice from that
        supplier stops being paid. Doubling is OData's own escape.

        mock-edi numbers its invoices itself and will not produce one, so the
        invoice is posted straight into SAP and the question asked of it.
        """
        reference = "O'BRIEN-014"
        self.post_invoice(GLOBEX, reference, "500.00")

        self.assertTrue(self.p2p.check.already_posted(reference, GLOBEX))
        self.assertFalse(self.p2p.check.already_posted(reference, INITECH),
                         "another supplier's number of the same name is not ours")
        self.assertFalse(self.p2p.check.already_posted("O'NEILL-1", GLOBEX),
                         "a quoted number that is not there answers no, not 400")

    def test_2b_asking_sap_refuses_the_duplicate_across_a_restart(self):
        """The same arrangement, with the question asked where the answer lives."""
        po = self.purchase()

        self.p2p.approve()
        self.supplier_resends(po, "despatch", "invoice")
        restarted = self.middleware(durable=True)
        again = restarted.approve()

        self.assertTrue(again, "the copy was read")
        self.assertTrue(all(r["status"] == "blocked" for r in again), again)
        self.assertTrue(any("already in SAP" in p for r in again
                            for p in r["problems"]), again)
        self.assertEqual(len(self.open_items()), 1, "one invoice, one thing owed")

        run = self.pay_what_is_due(p2p=restarted)
        self.assertEqual([i.status for i in run.items], ["cleared"])


class TestWhatNeverReachesTheBank(PurchaseCase):
    def test_3_a_price_disagreement_is_blocked_before_any_money_moves(self):
        """A block with a real cause, rather than a flag a test set.

        `payment_run` already tests that a blocked item is left out of the
        selection, but it blocks the invoice itself to arrange it. Here the block
        is a disagreement between an actual `810` and the purchase order it bills
        against, which is the path a real one takes.
        """
        self.purchase(price="9.99")            # mock-edi bills its own catalogue price

        [result] = self.p2p.approve()

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("billed at" in p for p in result["problems"]),
                        result["problems"])
        self.assertEqual(self.open_items(), [], "a blocked invoice owes nothing")
        self.assertEqual(control(BANK, "GET", "/_mock/payments"), [],
                         "the bank was never asked")

    def test_a_short_shipment_is_paid_for_what_shipped(self):
        """The supplier ships and bills less than was ordered, and is paid that.

        Matching against the ordered quantity would block this, and paying the
        ordered amount would overpay. What the bank moves has to be what the
        supplier billed.
        """
        self.supplier_behaves("short-ship")
        self.purchase()

        [result] = self.p2p.approve()
        self.assertEqual((result["status"], result["problems"]), ("posted", []))

        [item] = self.open_items()
        billed = abs(Decimal(item["AmountInTransactionCurrency"]))
        self.assertLess(billed, Decimal("1250.00"), "it shipped short")

        run = self.pay_what_is_due()

        self.assertEqual([i.status for i in run.items], ["cleared"])
        [paid] = run.items
        self.assertEqual(Decimal(paid.amount), billed,
                         "the bank moved what the supplier billed")


class TestWhenTheBankSaysNo(PurchaseCase):
    def test_4_a_rejected_payment_leaves_the_invoice_owed(self):
        """SAP approved it, the bank refused it, and it is still owed.

        INITECH's account is closed at the bank and SAP still holds its details -
        stale bank master data, which is ordinary. The invoice must not clear on
        an acknowledgement the bank never gave, and the item must stay open so the
        next run selects it again.
        """
        self.purchase(supplier=INITECH)

        [approved] = self.p2p.approve()
        self.assertEqual(approved["status"], "posted")

        run = self.pay_what_is_due()

        [item] = run.items
        self.assertEqual(item.status, "rejected")
        self.assertTrue(item.reason.startswith("AC04"), item.reason)
        self.assertEqual(len(self.open_items()), 1, "still owed")
        [row] = self.cube_rows(approved["invoice"])
        self.assertEqual(row["ClearingAccountingDocument"], "",
                         "nothing cleared it")
        self.assertEqual(row["ClearingIsReversed"], False,
                         "never paid is not the same as paid and returned")


if __name__ == "__main__":
    unittest.main()
