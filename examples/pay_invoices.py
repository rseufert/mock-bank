#!/usr/bin/env python3
"""Pay the supplier's invoices, and believe only the statement.

The step after `invoice_check`. A supplier sends an EDIFACT `INVOIC`; this pays
it on its due date with an ISO 20022 `pain.001` and then works out, from what
the bank sends back, which invoices are actually paid.

    mock-edi  ──INVOIC──▶  pay_invoices  ──pain.001──▶  mock-bank
                                         ◀──pain.002──  accepted, or why not
                                         ◀──camt.054──  debited on the settlement date
                                         ◀──camt.053──  the statement: this is "paid"

Four things it gets right that are easy to get wrong:

- **Accepted is not paid.** A `pain.002` that accepts a payment says the bank
  will try. The invoice is *scheduled* until a `camt.053` shows the debit, and
  a `camt.054` saying the money left is still not the statement.
- **A retried run sends the same file.** The `MsgId` is derived from the
  payments in it, so a run that crashed after sending and is run again sends a
  file the bank has already seen. The bank refuses it with `DUPL`, and the
  refusal must not undo the original's acceptance.
- **A return reopens what the statement paid.** A payment the supplier's bank
  sends back shows up days later as a credit on the statement, under the same
  `EndToEndId`. The invoice is owed again, and nothing else will say so.
- **Everything is matched by `EndToEndId`**, which is the invoice number, and
  by the `MsgId` it was sent in - never by position or by amount.

Standard library only, and it imports neither mock: it talks to them over
HTTP the way it would talk to a real supplier's VAN and a real bank. The
payables ledger is a plain dict so a test can snapshot it; a real one is a
table.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

from bank_messages import (  # noqa: F401 - call is used by the tests
    ACCEPTED, PAIN001, bank_documents, call, child_text, entries, tag)


# ---------------------------------------------------------------------------
# Reading the supplier's INVOIC
# ---------------------------------------------------------------------------

def edifact_segments(payload: str) -> List[List[List[str]]]:
    """An interchange as segments of elements of components.

    The `UNA` header, when present, names the separators; `?` releases the
    next character, so `Mueller ?+ Soehne` is one element.
    """
    component, element, release, terminator = ":", "+", "?", "'"
    if payload.startswith("UNA"):
        component, element, release, terminator = (payload[3], payload[4],
                                                   payload[6], payload[8])
        payload = payload[9:]
    segments, elements, parts, text = [], [], [], ""
    escaped = False
    for char in payload:
        if escaped:
            text += char
            escaped = False
        elif char == release:
            escaped = True
        elif char == component:
            parts.append(text)
            text = ""
        elif char == element:
            parts.append(text)
            elements.append(parts)
            parts, text = [], ""
        elif char == terminator:
            parts.append(text)
            elements.append(parts)
            if elements[0][0].strip():
                elements[0][0] = elements[0][0].strip()
                segments.append(elements)
            elements, parts, text = [], [], ""
        else:
            text += char
    return segments


def value(elements: List[List[str]], position: int, component: int = 0) -> str:
    """Element `position` (the tag is 0), component `component`, or ""."""
    try:
        return elements[position][component]
    except IndexError:
        return ""


@dataclass
class Invoice:
    supplier: str
    supplier_name: str
    number: str
    po_number: str
    invoiced_on: str        # ISO date
    terms_days: int
    currency: str
    total: str              # a decimal as text, so the ledger stays JSON

    @property
    def due_on(self) -> datetime.date:
        return (datetime.date.fromisoformat(self.invoiced_on)
                + datetime.timedelta(days=self.terms_days))


def read_invoices(payload: str) -> List[Invoice]:
    """Every INVOIC in an interchange: who billed what, and when it is due."""
    invoices: List[Invoice] = []
    current: Optional[Dict[str, str]] = None
    for seg in edifact_segments(payload):
        tag = seg[0][0]
        if tag == "UNH":
            current = {} if value(seg, 2) == "INVOIC" else None
        elif current is None:
            continue
        elif tag == "BGM":
            current["number"] = value(seg, 2)
        elif tag == "DTM" and value(seg, 1) == "137":
            raw = value(seg, 1, 1)
            current["invoiced_on"] = "%s-%s-%s" % (raw[:4], raw[4:6], raw[6:8])
        elif tag == "RFF" and value(seg, 1) == "ON":
            current["po_number"] = value(seg, 1, 1)
        elif tag == "NAD" and value(seg, 1) == "SU":
            current["supplier"] = value(seg, 2)
            current["supplier_name"] = value(seg, 4)
        elif tag == "CUX":
            current["currency"] = value(seg, 1, 1)
        elif tag == "PAT":
            current["terms_days"] = value(seg, 3, 3) or "30"
        elif tag == "MOA" and value(seg, 1) == "139":
            current["total"] = value(seg, 1, 1)
        elif tag == "UNT":
            invoices.append(Invoice(
                supplier=current.get("supplier", ""),
                supplier_name=current.get("supplier_name", ""),
                number=current.get("number", ""),
                po_number=current.get("po_number", ""),
                invoiced_on=current.get("invoiced_on", ""),
                terms_days=int(current.get("terms_days", "30")),
                currency=current.get("currency", "EUR"),
                total=current.get("total", "0.00")))
            current = None
    return invoices


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

class PayInvoices:
    """Collect invoices, pay them, and reconcile what the bank says.

    `company` is the paying account: `{"id", "name", "iban", "bic"}`, where
    `id` is our identity on the EDI side. `vendors` maps a supplier's EDI id
    to the bank details on file for it: `{"name", "iban", "bic"}`.
    """

    def __init__(self, edi: str, bank: str, company: Dict[str, str],
                 vendors: Dict[str, Dict[str, str]]):
        self.edi = edi
        self.bank = bank
        self.company = company
        self.vendors = vendors
        # supplier/invoice number -> record. Plain data, so it can be saved,
        # and so a test can take a copy of it before a "crash".
        self.ledger: Dict[str, Dict] = {}

    # -- 1. collect ---------------------------------------------------------

    def collect(self) -> Dict[str, List[str]]:
        """Take the supplier's invoices out of the EDI mailbox.

        A supplier with a retry bug sends the same invoice twice; it is keyed
        on supplier and number, so the second copy is noted and ignored.
        """
        status, raw = call(self.edi, "GET", "/_mock/mailbox?partner=%s&kind=invoice"
                           % self.company["id"])
        new, duplicates = [], []
        for message in json.loads(raw or b"[]") if status == 200 else []:
            for invoice in read_invoices(message["payload"]):
                key = "%s/%s" % (invoice.supplier, invoice.number)
                if key in self.ledger:
                    duplicates.append(key)
                    continue
                self.ledger[key] = {"invoice": asdict(invoice), "status": "open",
                                    "problem": "", "msg_id": "", "settles_on": "",
                                    "booked_on": ""}
                new.append(key)
        return {"new": new, "duplicates": duplicates}

    # -- 2. pay --------------------------------------------------------------

    def payable(self) -> List[str]:
        """Open invoices with nothing wrong with them, in a stable order."""
        return sorted(key for key, record in self.ledger.items()
                      if record["status"] == "open" and not record["problem"])

    def pay(self, today: Optional[datetime.date] = None) -> Dict:
        """Send one pain.001 for every payable invoice, each on its due date."""
        today = today or datetime.datetime.now(datetime.timezone.utc).date()
        payments = []
        for key in self.payable():
            record = self.ledger[key]
            invoice = Invoice(**record["invoice"])
            vendor = self.vendors.get(invoice.supplier)
            if vendor is None:
                record["problem"] = "no bank details on file for %s" % invoice.supplier
                continue
            payments.append((key, invoice, vendor, max(invoice.due_on, today)))
        if not payments:
            return {"msg_id": "", "payments": [], "http_status": None}

        msg_id = self.message_id(payments)
        status, _body = call(self.bank, "POST", "/payments",
                             self.payment_file(msg_id, payments), "application/xml")
        for key, _invoice, _vendor, when in payments:
            self.ledger[key].update(status="submitted", msg_id=msg_id,
                                    settles_on=when.isoformat())
        return {"msg_id": msg_id, "payments": [p[0] for p in payments],
                "http_status": status}

    def message_id(self, payments) -> str:
        """The same payments always make the same MsgId.

        That is what makes a retry safe: a run that crashed after sending
        sends a file the bank has already seen, and the bank refuses it.
        """
        digest = hashlib.sha1("\n".join(
            "%s|%s|%s" % (key, invoice.total, when.isoformat())
            for key, invoice, _vendor, when in payments).encode("utf-8"))
        return "%s-%s" % (self.company["id"], digest.hexdigest()[:12].upper())

    def payment_file(self, msg_id: str, payments) -> str:
        ET.register_namespace("", PAIN001)

        def add(parent, name, text=None, **attributes):
            element = ET.SubElement(parent, "{%s}%s" % (PAIN001, name), attributes)
            if text is not None:
                element.text = text
            return element

        def total(items) -> str:
            return str(sum((Decimal(p[1].total) for p in items), Decimal("0.00")))

        document = ET.Element("{%s}Document" % PAIN001)
        initiation = add(document, "CstmrCdtTrfInitn")
        header = add(initiation, "GrpHdr")
        add(header, "MsgId", msg_id)
        add(header, "CreDtTm", datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0).isoformat())
        add(header, "NbOfTxs", str(len(payments)))
        add(header, "CtrlSum", total(payments))
        add(add(header, "InitgPty"), "Nm", self.company["name"])

        # One batch per execution date: a batch has one ReqdExctnDt.
        for when in sorted({p[3] for p in payments}):
            batch_payments = [p for p in payments if p[3] == when]
            batch = add(initiation, "PmtInf")
            add(batch, "PmtInfId", "%s-%s" % (msg_id, when.strftime("%Y%m%d")))
            add(batch, "PmtMtd", "TRF")
            add(batch, "NbOfTxs", str(len(batch_payments)))
            add(batch, "CtrlSum", total(batch_payments))
            add(add(add(batch, "PmtTpInf"), "SvcLvl"), "Cd", "SEPA")
            add(add(batch, "ReqdExctnDt"), "Dt", when.isoformat())
            add(add(batch, "Dbtr"), "Nm", self.company["name"])
            account = add(batch, "DbtrAcct")
            add(add(account, "Id"), "IBAN", self.company["iban"])
            add(account, "Ccy", "EUR")
            add(add(add(batch, "DbtrAgt"), "FinInstnId"), "BICFI", self.company["bic"])
            add(batch, "ChrgBr", "SLEV")
            for _key, invoice, vendor, _when in batch_payments:
                transfer = add(batch, "CdtTrfTxInf")
                add(add(transfer, "PmtId"), "EndToEndId", invoice.number)
                add(add(transfer, "Amt"), "InstdAmt", invoice.total, Ccy=invoice.currency)
                add(add(add(transfer, "CdtrAgt"), "FinInstnId"), "BICFI", vendor["bic"])
                add(add(transfer, "Cdtr"), "Nm", vendor["name"])
                add(add(add(transfer, "CdtrAcct"), "Id"), "IBAN", vendor["iban"])
                add(add(transfer, "RmtInf"), "Ustrd",
                    "Invoice %s, order %s" % (invoice.number, invoice.po_number))
        return ET.tostring(document, encoding="unicode", xml_declaration=True)

    # -- 3. reconcile --------------------------------------------------------

    def record_for(self, msg_id: str, end_to_end_id: str) -> Optional[Dict]:
        """The ledger record a bank message is about: the invoice number, in
        the file it was sent in. Two suppliers can use the same number; two
        payments in one of our files cannot."""
        for record in self.ledger.values():
            if record["msg_id"] == msg_id and record["invoice"]["number"] == end_to_end_id:
                return record
        return None

    def reconcile(self) -> None:
        """Read everything the bank has sent, status reports before statements."""
        status, raw = call(self.bank, "GET", "/_mock/mailbox?raw")
        documents = list(bank_documents(raw.decode("utf-8"))) if status == 200 else []
        for root in (d for d in documents if tag(d[0]) == "CstmrPmtStsRpt"):
            self.read_status_report(root[0])
        for root in (d for d in documents if tag(d[0]) == "BkToCstmrDbtCdtNtfctn"):
            for notification in (c for c in root[0] if tag(c) == "Ntfctn"):
                if child_text(notification, "Acct", "Id", "IBAN") == self.company["iban"]:
                    self.read_notification(notification)
        statements = [s for d in documents if tag(d[0]) == "BkToCstmrStmt"
                      for s in d[0] if tag(s) == "Stmt"
                      and child_text(s, "Acct", "Id", "IBAN") == self.company["iban"]]
        for statement in sorted(statements, key=lambda s: child_text(s, "FrToDt", "FrDtTm")):
            self.read_statement(statement)

    def read_status_report(self, report) -> None:
        group = next(c for c in report if tag(c) == "OrgnlGrpInfAndSts")
        msg_id = child_text(group, "OrgnlMsgId")
        group_status = child_text(group, "GrpSts")
        group_reason = next((e.text for e in group.iter() if tag(e) == "Cd"), "") or ""
        if group_status == "RJCT" and group_reason == "DUPL":
            # This copy of the file was refused because the bank already has
            # it. The original's own status report is what counts; applying
            # this one would reopen invoices the bank is about to pay.
            return
        transactions = [e for e in report.iter() if tag(e) == "TxInfAndSts"]
        if group_status == "RJCT" and not transactions:
            # Rejected at file level (FF01, say): no payment was looked at.
            # A group RJCT *with* transaction statuses is different - every
            # payment was rejected, each for its own reason - and is read
            # below, payment by payment.
            for record in self.ledger.values():
                if record["msg_id"] == msg_id and record["status"] == "submitted":
                    record.update(status="open", problem="the bank rejected the "
                                  "whole file: %s" % (group_reason or "no reason given"))
            return
        for info in transactions:
            record = self.record_for(msg_id, child_text(info, "OrgnlEndToEndId"))
            if record is None or record["status"] != "submitted":
                continue
            tx_status = child_text(info, "TxSts")
            if tx_status in ACCEPTED:
                record["status"] = "scheduled"
            elif tx_status == "RJCT":
                reason = child_text(info, "StsRsnInf", "Rsn", "Cd")
                detail = child_text(info, "StsRsnInf", "AddtlInf")
                # Back to open, and held: paying it again would be rejected
                # again. Someone has to fix the vendor's bank details first.
                record.update(status="open", problem="%s: %s" % (reason, detail))

    def read_notification(self, notification) -> None:
        """camt.054: the money left. Worth knowing; not yet reconciled."""
        for entry in entries(notification):
            record = self.record_for(entry["msg_id"], entry["end_to_end_id"])
            if record is not None and record["status"] in ("submitted", "scheduled"):
                record.update(status="notified", booked_on=entry["booked_on"])

    def read_statement(self, statement) -> None:
        """camt.053: the only thing that makes an invoice paid."""
        day = child_text(statement, "FrToDt", "FrDtTm")[:10]
        for entry in entries(statement):
            record = self.record_for(entry["msg_id"], entry["end_to_end_id"])
            if record is None:
                continue
            if entry["side"] == "CRDT" and entry["returned_for"]:
                # The payment came back (a pacs.004): the invoice is owed
                # again. Held, not re-paid, because whatever made the
                # supplier's bank return it will make it return it again.
                record.update(status="open", problem="returned on %s: %s"
                              % (entry["booked_on"], entry["returned_for"]))
                continue
            if record["status"] == "paid":
                continue
            if entry["side"] != "DBIT" or Decimal(entry["amount"]) != Decimal(record["invoice"]["total"]):
                record["problem"] = ("the statement for %s shows %s %s, not the %s billed"
                                     % (day, entry["side"], entry["amount"],
                                        record["invoice"]["total"]))
                continue
            record.update(status="paid", booked_on=entry["booked_on"], problem="")
        # Anything the notification said was booked on or before this day and
        # that the statement does not show is not paid, whatever the camt.054
        # said: the statement is the bank's record, the notification is news.
        for record in self.ledger.values():
            if record["status"] == "notified" and record["booked_on"] <= day:
                record["problem"] = ("notified as booked on %s but not on the "
                                     "statement for %s" % (record["booked_on"], day))

    # -- the answer -----------------------------------------------------------

    def report(self) -> List[Dict[str, str]]:
        return [{"invoice": key, "total": record["invoice"]["total"],
                 "status": record["status"], "problem": record["problem"]}
                for key, record in sorted(self.ledger.items())]
