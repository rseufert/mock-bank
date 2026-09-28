"""procure_to_pay: one purchase, three systems, from order to cleared payment.

The other worked examples each use two mocks. This one uses all three, and it
exists for what only appears between them:

    SAP ──850──▶ supplier          (a purchase order becomes an EDI order)
        ◀──855/856/810── supplier  (confirmed, shipped, invoiced)
    SAP ◀──INVOIC──                (matched, posted, and now owed)
        ──pain.001──▶ bank         (a payment run selects what is due)
    SAP ◀──FINSTA01◀──camt.053──   (the statement clears what was paid)

Each pair of mocks can be correct on its own while the chain is broken, because
each pair's tests assert what the *next* system received rather than what it
could do with what it received. Three bugs in `invoice_check` were found this
way and none of them was visible to its own five green tests: an `INVOIC` that
named no supplier and so created no payable (mock-sap#68), a mock that reported
status 53 for having posted nothing (mock-sap#67), and an order placed in one
currency that came back invoiced in another (mock-sap#74).

The scenario worth reading first is the same invoice arriving twice. Two things
appear to catch it and neither does - see `DurableInvoiceCheck` below, and
`test_2_without_asking_sap_the_duplicate_is_paid_too`, which resends the despatch
advice alongside the invoice precisely because resending the invoice alone is
blocked for the wrong reason.

This module composes rather than reimplements. The three-way match is
`invoice_check.InvoiceCheck`, a checked copy of mock-sap's example; selection,
payment and reconciliation are `payment_run.PaymentRun`, which lives here. The
only logic of its own is `DurableInvoiceCheck`, and that is the point of the
duplicate scenario below.

**What it does not do.** It does not tell the supplier what was paid: that needs
a remittance advice (X12 820 or EDIFACT `REMADV`), which mock-edi does not speak
yet (mock-edi#149). So the loop ends with SAP and the bank agreeing and the
supplier none the wiser, which is exactly why a supplier keeps dunning you for an
invoice you paid. Saying so is better than implying the circle closes.

Stdlib only. The tests are in test_procure_to_pay.py.
"""
from __future__ import annotations

import datetime
import urllib.parse
from typing import Dict, List, Optional

import invoice_check
import payment_run

SUPPLIER_INVOICES = ("/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV"
                     "/A_SupplierInvoice")


def odata_string(value: str) -> str:
    """A string for an OData `$filter` literal, with its quotes doubled.

    A supplier's invoice number is the supplier's, not ours. `O'BRIEN-014` would
    close the literal early and the filter becomes a different question - or a
    syntax error, if you are lucky enough to notice. OData escapes a quote by
    doubling it, and `urlencode` then percent-encodes the result as the value it
    is rather than as syntax.
    """
    return str(value).replace("'", "''")


class DurableInvoiceCheck(invoice_check.InvoiceCheck):
    """The same match, but the duplicate question is asked of SAP.

    `InvoiceCheck` remembers what it has posted in a `set`, which is one
    process's memory. Restart the middleware, or run a second worker beside it,
    and the supplier's retried `810` looks new: SAP takes a second supplier
    invoice, a second open payable appears, and a payment run pays it.

    Two things will look like they caught it, and neither did. Both are accidents
    of what *else* the restart lost, which is why they are worth spelling out:

    * **Upstream**, a fresh `InvoiceCheck` has forgotten its ship notices as well
      as what it posted, so a resent invoice arriving on its own is blocked for
      billing more than was shipped - `item 00010 bills 100, shipped 0`. That is
      not the duplicate being caught; it is a second thing being missing. Resend
      the despatch advice with the invoice, which is what a partner replaying a
      batch does, and the invoice posts again.
    * **Downstream**, in *one* run, `PaymentRun.select` sorts by reference and
      marks the second item with a reference it has already seen as `skipped` -
      deliberately, so two payments in one file cannot share an `EndToEndId`. It
      looks caught. In **the next run** the first payment has cleared, the second
      item is alone in the selection, and it is paid. Nothing refused it.

    So neither end of the chain is checking. Each is forgetting or deduplicating
    for its own reasons, and the gap between them is where the second payment
    goes out.

    So the only place the question can be answered is upstream, against the
    system of record: does SAP already hold a supplier invoice with this
    invoicing party's reference? That is one `$filter`, and it survives a restart
    because SAP is where the answer lives.

    SAP itself will not answer it unasked. mock-sap files both and says so, and
    that is faithful: a duplicate check in SAP is configuration, not arithmetic,
    so a mock that invented one would hide exactly this bug.
    """

    def already_posted(self, reference: str, supplier: str) -> bool:
        query = urllib.parse.urlencode({"$filter": (
            "SupplierInvoiceIDByInvcgParty eq '%s' and InvoicingParty eq '%s'"
            % (odata_string(reference), odata_string(supplier))),
            "$format": "json"})
        found = self.sap.request("GET", "%s?%s" % (SUPPLIER_INVOICES, query))
        return bool(found["d"]["results"])

    def problems(self, invoice, po):
        if self.already_posted(invoice["number"], po["Supplier"]):
            return ["supplier invoice %s from %s is already in SAP"
                    % (invoice["number"], po["Supplier"])]
        return super().problems(invoice, po)


class ProcureToPay:
    """One purchase, carried from a purchase order to a cleared payment.

    `durable` chooses which duplicate check the middleware has, because the
    difference between them is a scenario rather than a setting somebody should
    pick by taste. `False` is `invoice_check` as it ships.
    """

    def __init__(self, sap: str, edi: str, bank: str, our_id: str,
                 company: Dict[str, str], durable: bool = True):
        self.sap_url, self.edi, self.bank = sap, edi, bank
        self.sap = invoice_check.Sap(sap)
        check = DurableInvoiceCheck if durable else invoice_check.InvoiceCheck
        self.check = check(self.sap, edi, our_id)
        self.payments = payment_run.PaymentRun(sap, bank, company)

    # -- 1. order --------------------------------------------------------------

    def order(self, po_number: str, sender: str, control: int) -> dict:
        """Send a purchase order to the supplier as an 850.

        `control` is the interchange control number and has to differ per order:
        mock-edi refuses a replayed interchange with a `TA1` rather than
        fulfilling it twice, which is correct of it and easy to trip over.
        """
        return invoice_check.send_order(self.sap, self.edi, po_number,
                                        sender=sender, control=control)

    # -- 2. match and post ----------------------------------------------------

    def approve(self) -> List[dict]:
        """Read what the supplier sent, and post the invoices that match."""
        return self.check.run()

    # -- 3. pay and reconcile -------------------------------------------------

    def last_due_date(self) -> Optional[datetime.date]:
        """The latest due date among the open payables; None if nothing is owed.

        Read rather than assumed, because the due date comes from the supplier's
        own invoice - its date and its terms - and hard-coding one would assert
        the arithmetic of whichever day the tests ran.

        The *latest*, so that a run made on it pays everything outstanding. No
        test distinguishes that from the earliest: every invoice in these tests
        is dated the day it was raised, so one due date covers them all. A run
        against invoices of different ages would tell them apart, and this mock's
        supplier does not produce them.
        """
        rows = payment_run.odata(self.sap_url, payment_run.ITEMS,
                                 **{"$filter": payment_run.OPEN_SUPPLIER_ITEMS})
        dates = [payment_run.sap_date(r.get("NetDueDate")) for r in rows]
        dates = [d for d in dates if d is not None]
        return max(dates) if dates else None

    def pay(self, run_on: datetime.date, identification: str):
        return self.payments.run(run_on, identification)

    def reconcile(self, run) -> None:
        self.payments.reconcile(run)
