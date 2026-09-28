#!/usr/bin/env python3
"""A payment run: pay what SAP says is due, and hear from the bank what it took.

The payables side of the chain `pay_invoices` walks from the EDI end. Here
the invoices are already posted in SAP, and the run does what `F110` does:
select the open supplier items that are due, pay each one to the account its
invoice names, and read the bank's `pain.002` to learn which payments it took.

    mock-sap  ──open items──▶  payment_run  ──pain.001──▶  mock-bank
                                            ◀──pain.002──  accepted, or why not
                                            ◀──camt.053──  what actually left
    mock-sap  ◀──FINSTA01─────  payment_run                the items it cleared

Then it reconciles: each `camt.053` for the paying account is converted into a
`FINSTA01` bank statement and posted to SAP, which clears every open item a
debit line pays by reference and amount.

What it gets right that is easy to get wrong:

- **SAP decides what is payable, not the run.** The selection asks for
  supplier lines (`K`) that are not cleared and not blocked, and pays only the
  ones due by the run date. A blocked invoice is blocked for a reason; a run
  that pays it anyway has overruled somebody.
- **One payment per open item, and its `EndToEndId` is the supplier's own
  invoice number** (`SupplierInvoiceIDByInvcgParty`), which is what the
  supplier's remittance and the bank statement will both carry back. The open
  item does not have it, so each item is joined to its supplier invoice
  through the accounting document.
- **The account is the one the invoice names** (`BPBankAccountInternalID`),
  not whichever of the supplier's accounts comes back first.
- **The run is its own identity.** `MsgId` is made of the run date and the
  run's identification, as `F110`'s are, so running the same run again sends
  a file the bank has already seen. The bank refuses it with `DUPL`, and that
  refusal must not undo what the first file did.
- **A statement is checked before it is believed.** Opening balance plus the
  entries must be the closing balance, to the cent. When it is not, the
  difference is a payment the statement left out; the accepted payment for
  exactly that amount is reported as unreconciled, and its item stays open.
- **Accepted is not paid.** An item is paid when a statement shows the debit
  and SAP clears it. A payment made after the cutoff is on the next business
  day's statement, not today's, and until then it is simply still accepted.
- **Paid is not final.** A payment the receiving bank sends back arrives days
  later as a credit on a statement, under the same `EndToEndId`. Posted to
  SAP, it reverses the clearing and the invoice is owed again: `returned`,
  with the bank's reason, and open to the next run. SAP keeps it
  distinguishable from an invoice that was never paid.

Standard library only, and it imports neither mock: it talks to both over
HTTP as it would to a real S/4HANA system and a real bank. What it shares
with `pay_invoices.py` is in `bank_messages.py`, which a copy takes with it.
"""
from __future__ import annotations

import datetime
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from bank_messages import ACCEPTED, PAIN001, bank_documents, call, child_text, entries, tag

ODATA = "/sap/opu/odata/sap"
ITEMS = ODATA + "/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube"
INVOICES = ODATA + "/API_SUPPLIERINVOICE_PROCESS_SRV/A_SupplierInvoice"
BANKS = ODATA + "/API_BUSINESS_PARTNER_SRV/A_BusinessPartnerBank"

# What SAP is asked for: supplier lines, not cleared, not blocked. Due is
# compared here, against the run date, rather than in the filter.
OPEN_SUPPLIER_ITEMS = ("AccountingDocumentItemType eq 'K' and "
                       "ClearingAccountingDocument eq '' and "
                       "PaymentBlockingReason eq ''")

# Payment methods this run pays by transfer. Blank is the supplier's default,
# which is what an inbound INVOIC posts with.
TRANSFER = {"", "T"}


def odata(base: str, path: str, **query) -> List[Dict]:
    """Every row of an entity set, following the server's paging."""
    query["$format"] = "json"
    url = base.rstrip("/") + path + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
    rows: List[Dict] = []
    while url:
        with urllib.request.urlopen(url) as response:
            payload = json.loads(response.read())["d"]
        rows.extend(payload["results"])
        url = payload.get("__next", "")
    return rows


def sap_date(raw: str) -> Optional[datetime.date]:
    """OData V2's `/Date(1788220800000)/`, as a date."""
    found = re.search(r"-?\d+", raw or "")
    if not found:
        return None
    return (datetime.datetime(1970, 1, 1)
            + datetime.timedelta(milliseconds=int(found.group()))).date()


@dataclass
class Item:
    """One open item, and what became of it."""
    document: str               # company code/fiscal year/accounting document
    supplier: str
    reference: str = ""         # the supplier's invoice number: the EndToEndId
    amount: str = "0.00"
    currency: str = "EUR"
    due_on: str = ""
    name: str = ""
    iban: str = ""
    bic: str = ""
    status: str = "selected"    # selected, skipped, sent, accepted, rejected,
                                # cleared, unreconciled, returned
    reason: str = ""            # a reason code from the bank, or why it was skipped


@dataclass
class Run:
    run_on: datetime.date
    identification: str
    items: List[Item] = field(default_factory=list)
    http_status: Optional[int] = None
    duplicate: bool = False     # the bank already had this run's file
    # One per camt.053 posted to SAP: its number, day, whether it adds up,
    # what SAP cleared and could not place, and the FINSTA01 that was sent.
    statements: List[Dict] = field(default_factory=list)
    # What went wrong talking to either side, in words, rather than an
    # exception half way through a reconciliation.
    problems: List[str] = field(default_factory=list)

    @property
    def msg_id(self) -> str:
        return "F110-%s-%s" % (self.run_on.strftime("%Y%m%d"), self.identification)

    def paying(self) -> List[Item]:
        return [i for i in self.items if i.status not in ("selected", "skipped")]


class PaymentRun:
    """Select from SAP, pay through the bank, and read back the pain.002.

    `company` is the paying account: `{"name", "iban", "bic"}`. House-bank
    data is not read from SAP here; it is the run's own configuration.
    """

    def __init__(self, sap: str, bank: str, company: Dict[str, str]):
        self.sap = sap
        self.bank = bank
        self.company = company
        self.session = SapSession(sap)

    def run(self, run_on: datetime.date, identification: str) -> Run:
        run = Run(run_on, identification, self.select(run_on))
        self.send(run)
        self.read_status(run)
        return run

    # -- 1. select -------------------------------------------------------------

    def select(self, run_on: datetime.date) -> List[Item]:
        items = []
        for row in odata(self.sap, ITEMS, **{"$filter": OPEN_SUPPLIER_ITEMS}):
            due = sap_date(row.get("NetDueDate"))
            if due is None or due > run_on:
                continue
            item = Item(document="%s/%s/%s" % (row["CompanyCode"], row["FiscalYear"],
                                               row["AccountingDocument"]),
                        supplier=row["Supplier"], due_on=due.isoformat(),
                        currency=row["TransactionCurrency"],
                        amount=str(abs(Decimal(row["AmountInTransactionCurrency"]))
                                   .quantize(Decimal("0.01"))))
            self.complete(item, row)
            items.append(item)
        items.sort(key=lambda i: (i.supplier, i.reference, i.document))
        # Two suppliers may use the same invoice number; two payments in one
        # file must not share an EndToEndId, or the pain.002 cannot say which.
        seen = set()
        for item in items:
            if item.status == "selected" and item.reference in seen:
                item.status, item.reason = "skipped", "another item in this run has the same reference"
            seen.add(item.reference)
        return items

    def complete(self, item: Item, row: Dict) -> None:
        """The invoice behind an open item, and the account it is to be paid into."""
        invoices = odata(self.sap, INVOICES, **{"$filter": (
            "AccountingDocument eq '%s' and FiscalYear eq '%s' and CompanyCode eq '%s'"
            % (row["AccountingDocument"], row["FiscalYear"], row["CompanyCode"]))})
        if not invoices:
            item.status, item.reason = "skipped", "no supplier invoice for this item"
            return
        invoice = invoices[0]
        item.reference = invoice["SupplierInvoiceIDByInvcgParty"]
        if not item.reference:
            item.status, item.reason = "skipped", "the invoice has no supplier reference"
            return
        if invoice.get("PaymentMethod", "") not in TRANSFER:
            item.status, item.reason = "skipped", "payment method %r is not a transfer" % invoice["PaymentMethod"]
            return
        if item.currency != "EUR":
            item.status, item.reason = "skipped", "a SEPA transfer is in euros, not %s" % item.currency
            return
        banks = odata(self.sap, BANKS, **{"$filter": (
            "BusinessPartner eq '%s' and BankIdentification eq '%s'"
            % (item.supplier, invoice["BPBankAccountInternalID"]))})
        if not banks or not banks[0].get("IBAN"):
            item.status, item.reason = "skipped", "no IBAN for account %s of %s" % (
                invoice["BPBankAccountInternalID"], item.supplier)
            return
        bank = banks[0]
        item.iban, item.bic = bank["IBAN"], bank.get("SWIFTCode", "")
        item.name = bank.get("BankAccountHolderName") or item.supplier

    # -- 2. send ---------------------------------------------------------------

    def send(self, run: Run) -> None:
        paying = [i for i in run.items if i.status == "selected"]
        if not paying:
            return
        run.http_status, body = call(self.bank, "POST", "/payments",
                                     self.payment_file(run, paying), "application/xml")
        if run.http_status not in (202, 422):
            # 422 is a file the bank rejected, and its pain.002 says why; any
            # other answer means the file may not have arrived at all.
            run.problems.append("the bank answered %d to the payment file: %s"
                                % (run.http_status, body.decode("utf-8", "replace")[:200]))
        for item in paying:
            item.status = "sent"

    def payment_file(self, run: Run, items: List[Item]) -> str:
        ET.register_namespace("", PAIN001)

        def add(parent, name, text=None, **attributes):
            element = ET.SubElement(parent, "{%s}%s" % (PAIN001, name), attributes)
            if text is not None:
                element.text = text
            return element

        total = str(sum((Decimal(i.amount) for i in items), Decimal("0.00")))
        document = ET.Element("{%s}Document" % PAIN001)
        initiation = add(document, "CstmrCdtTrfInitn")
        header = add(initiation, "GrpHdr")
        add(header, "MsgId", run.msg_id)
        add(header, "CreDtTm", datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0).isoformat())
        add(header, "NbOfTxs", str(len(items)))
        add(header, "CtrlSum", total)
        add(add(header, "InitgPty"), "Nm", self.company["name"])
        # Everything selected is due by the run date, so one batch, executed then.
        batch = add(initiation, "PmtInf")
        add(batch, "PmtInfId", run.msg_id)
        add(batch, "PmtMtd", "TRF")
        add(batch, "NbOfTxs", str(len(items)))
        add(batch, "CtrlSum", total)
        add(add(add(batch, "PmtTpInf"), "SvcLvl"), "Cd", "SEPA")
        add(add(batch, "ReqdExctnDt"), "Dt", run.run_on.isoformat())
        add(add(batch, "Dbtr"), "Nm", self.company["name"])
        account = add(batch, "DbtrAcct")
        add(add(account, "Id"), "IBAN", self.company["iban"])
        add(account, "Ccy", "EUR")
        add(add(add(batch, "DbtrAgt"), "FinInstnId"), "BICFI", self.company["bic"])
        add(batch, "ChrgBr", "SLEV")
        for item in items:
            transfer = add(batch, "CdtTrfTxInf")
            add(add(transfer, "PmtId"), "EndToEndId", item.reference)
            add(add(transfer, "Amt"), "InstdAmt", item.amount, Ccy=item.currency)
            if item.bic:
                add(add(add(transfer, "CdtrAgt"), "FinInstnId"), "BICFI", item.bic)
            add(add(transfer, "Cdtr"), "Nm", item.name[:140])
            add(add(add(transfer, "CdtrAcct"), "Id"), "IBAN", item.iban)
            add(add(transfer, "RmtInf"), "Ustrd", "Invoice %s" % item.reference)
        return ET.tostring(document, encoding="unicode", xml_declaration=True)

    # -- 3. read the pain.002 ----------------------------------------------------

    def read_status(self, run: Run) -> None:
        """Match the bank's status reports to the run by MsgId, then EndToEndId.

        Only `pain.002`s are collected, so the `camt` messages stay in the
        mailbox for whatever reconciles the statement.
        """
        status, raw = call(self.bank, "GET", "/_mock/mailbox?raw&type=pain.002")
        if status != 200:
            run.problems.append("the bank's mailbox answered %d, so no status report "
                                "was read" % status)
            return
        by_reference = {i.reference: i for i in run.paying()}
        for root in bank_documents(raw.decode("utf-8")):
            report = root[0]
            group = next(c for c in report if tag(c) == "OrgnlGrpInfAndSts")
            if child_text(group, "OrgnlMsgId") != run.msg_id:
                continue
            group_status = child_text(group, "GrpSts")
            group_reason = next((e.text for e in group.iter() if tag(e) == "Cd"), "") or ""
            if group_status == "RJCT" and group_reason == "DUPL":
                # The bank has this run already. What it said about the first
                # file stands; this refusal is about the copy, not the payments.
                run.duplicate = True
                continue
            transactions = [e for e in report.iter() if tag(e) == "TxInfAndSts"]
            if not transactions:
                # A status for the whole file: FF01, say, or every payment taken.
                for item in by_reference.values():
                    if group_status == "RJCT":
                        item.status, item.reason = "rejected", group_reason
                    elif group_status in ACCEPTED:
                        item.status, item.reason = "accepted", ""
            for transaction in transactions:
                item = by_reference.get(child_text(transaction, "OrgnlEndToEndId"))
                if item is None:
                    continue
                code = child_text(transaction, "TxSts")
                if code == "RJCT":
                    item.status = "rejected"
                    item.reason = next((e.text for e in transaction.iter()
                                        if tag(e) == "Cd"), "") or ""
                elif code in ACCEPTED:
                    item.status, item.reason = "accepted", ""

    # -- 4. reconcile: post the statements to SAP ------------------------------

    def reconcile(self, run: Run) -> None:
        """Post every `camt.053` the bank has sent for the paying account.

        Only `camt.053` is collected; a statement is the bank's record, a
        `camt.054` is news. Oldest first, because SAP chains each statement to
        the one before it.
        """
        status, raw = call(self.bank, "GET", "/_mock/mailbox?raw&type=camt.053")
        if status != 200:
            run.problems.append("the bank's mailbox answered %d, so no statement was "
                                "read: %s" % (status, raw.decode("utf-8", "replace")[:200]))
            return
        statements = [s for d in bank_documents(raw.decode("utf-8"))
                      if tag(d[0]) == "BkToCstmrStmt"
                      for s in d[0] if tag(s) == "Stmt"
                      and child_text(s, "Acct", "Id", "IBAN") == self.company["iban"]]
        statements.sort(key=lambda s: (child_text(s, "FrToDt", "FrDtTm"),
                                       int(child_text(s, "ElctrncSeqNb") or 0)))
        for statement in statements:
            self.post_statement(run, statement)

    def post_statement(self, run: Run, statement) -> Dict:
        number = child_text(statement, "ElctrncSeqNb")
        day = child_text(statement, "FrToDt", "FrDtTm")[:10]
        opening, closing = balance(statement, "OPBD"), balance(statement, "CLBD")
        lines = entries(statement)
        moved = sum((signed(e["amount"], e["side"]) for e in lines), Decimal("0"))
        record = {"number": number, "date": day,
                  "adds_up": opening + moved == closing,
                  "finsta": self.finsta(number, day, opening, closing, lines)}
        try:
            applied = self.session.post_idoc(record["finsta"])
        except urllib.error.HTTPError as error:
            # Recorded against the statement rather than raised: the ones
            # already posted stay posted, and this one can be posted again.
            applied = {}
            record["error"] = "SAP refused statement %s: %d %s" % (
                number, error.code, error.read().decode("utf-8", "replace")[:200])
            run.problems.append(record["error"])
        record.update(cleared=applied.get("CLEARED", []),
                      reopened=applied.get("REOPENED", []),
                      unprocessed=applied.get("UNPROCESSED", []),
                      findings=applied.get("FINDINGS", []))
        run.statements.append(record)

        by_reference = {i.reference: i for i in run.paying()}
        for line in record["cleared"]:
            item = by_reference.get(line["REFERENCE"])
            if item is not None and item.status == "accepted":
                item.status, item.reason = "cleared", line["CLEARINGDOCUMENT"]
        # A return is a credit on a later statement under the original
        # EndToEndId; SAP has reversed the clearing and the invoice is owed
        # again. The reason code is the bank's, from the entry's RtrInf.
        why = {e["end_to_end_id"]: e["returned_for"] for e in lines if e["side"] == "CRDT"}
        for line in record["reopened"]:
            item = by_reference.get(line["REFERENCE"])
            if item is not None and item.status == "cleared":
                item.status = "returned"
                item.reason = "%s, returned on %s; SAP reversed the clearing in %s" % (
                    why.get(item.reference) or "no reason given", day,
                    line["REVERSALDOCUMENT"])
        if not record["adds_up"]:
            self.name_the_shortfall(run, number, day, opening + moved - closing)
        return record

    def name_the_shortfall(self, run: Run, number: str, day: str, short: Decimal):
        """The accepted payment for exactly the amount a statement is short.

        A missing debit makes the closing balance lower than the entries say,
        by that debit. With one accepted payment of that amount, it is the one
        left out. With several, the statement cannot say which, and each is
        named as a candidate rather than one picked.
        """
        short = short.quantize(Decimal("0.01"))
        candidates = [i for i in run.paying()
                      if i.status == "accepted" and Decimal(i.amount) == short]
        for item in candidates:
            item.status = "unreconciled"
            if len(candidates) == 1:
                item.reason = ("statement %s for %s is %s short, which is this "
                               "payment; the item stays open" % (number, day, short))
            else:
                item.reason = ("statement %s for %s is %s short, which is the amount "
                               "of %s; one of them is missing and the statement "
                               "cannot say which" % (number, day, short, ", ".join(
                                   c.reference for c in candidates)))

    def finsta(self, number: str, day: str, opening: Decimal, closing: Decimal,
               lines: List[Dict[str, str]]) -> str:
        """A camt.053 as a FINSTA01, the way mock-sap reads one.

        A line's direction is its amount's sign, written SAP's way with the
        minus after the number: SAP declares no field for it, so this is the
        convention agreed with mock-sap on mock-bank#16. The reference is in
        the structured `E1EDP02`, which is matched exactly, rather than only in
        the note to payee.
        """
        def sap(amount: Decimal) -> str:
            return "%.2f-" % -amount if amount < 0 else "%.2f" % amount

        def amounts(*pairs) -> str:
            return "".join(
                "<E1IDPU5 SEGMENT=\"1\"><MOAQUAL>%s</MOAQUAL><MOABETR>%s</MOABETR>"
                "<CUXWAERZ>EUR</CUXWAERZ></E1IDPU5>" % (q, sap(a)) for q, a in pairs)

        body = []
        for position, line in enumerate(lines, 1):
            body.append(
                "<E1IDPF1 SEGMENT=\"1\"><LINLINEIT>%06d</LINLINEIT>"
                "<E1EDP02 SEGMENT=\"1\"><QUALF>009</QUALF><BELNR>%s</BELNR></E1EDP02>"
                "%s</E1IDPF1>" % (position, escape(line["end_to_end_id"]),
                                  amounts(("001", signed(line["amount"], line["side"])))))
        debits = sum((Decimal(e["amount"]) for e in lines if e["side"] == "DBIT"), Decimal(0))
        credits = sum((Decimal(e["amount"]) for e in lines if e["side"] == "CRDT"), Decimal(0))
        body.append("<E1IDPF1 SEGMENT=\"1\"><LINLINEIT>%06d</LINLINEIT>%s</E1IDPF1>" % (
            len(lines) + 1, amounts(("019", opening), ("021", closing),
                                    ("023", debits), ("024", credits))))
        iban = self.company["iban"]
        return ("<?xml version=\"1.0\" encoding=\"utf-8\"?><FINSTA01><IDOC BEGIN=\"1\">"
                "<EDI_DC40 SEGMENT=\"1\"><IDOCTYP>FINSTA01</IDOCTYP><MESTYP>FINSTA</MESTYP>"
                "</EDI_DC40><E1IDKU1 SEGMENT=\"1\"><BGMREF>%s</BGMREF>"
                "<E1EDK03 SEGMENT=\"1\"><IDDAT>026</IDDAT><DATUM>%s</DATUM></E1EDK03>"
                "<E1IDB02 SEGMENT=\"1\"><FIIBKENN>%s</FIIBKENN><FIIKONTO>%s</FIIKONTO>"
                "<FIIBLAND>%s</FIIBLAND><FIIKWAER>EUR</FIIKWAER></E1IDB02>%s"
                "</E1IDKU1></IDOC></FINSTA01>"
                % (escape(number), day.replace("-", ""), escape(self.company["bic"]),
                   escape(iban), iban[:2], "".join(body)))


def signed(amount: str, side: str) -> Decimal:
    """An ISO 20022 amount with its `CdtDbtInd`: money out is negative."""
    value = Decimal(amount or "0")
    return -value if side == "DBIT" else value


def balance(statement, code: str) -> Decimal:
    """`OPBD` or `CLBD`, signed: an overdrawn balance is DBIT, not a credit."""
    for element in statement:
        if tag(element) == "Bal" and child_text(element, "Tp", "CdOrPrtry", "Cd") == code:
            return signed(child_text(element, "Amt"), child_text(element, "CdtDbtInd"))
    return Decimal("0")


class SapSession:
    """Writes to SAP: an X-CSRF-Token fetched once, and the session it belongs to."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.token = ""

    def write(self, method: str, path: str, body: str, content_type: str,
              accept: str = "application/json") -> bytes:
        if not self.token:
            fetch = urllib.request.Request(
                self.base + ODATA + "/API_BUSINESS_PARTNER_SRV/",
                headers={"X-CSRF-Token": "Fetch"})
            with self.opener.open(fetch) as response:
                self.token = response.headers.get("X-CSRF-Token", "")
        request = urllib.request.Request(
            self.base + path, method=method, data=body.encode("utf-8"),
            headers={"Content-Type": content_type, "Accept": accept,
                     "X-CSRF-Token": self.token})
        with self.opener.open(request) as response:
            return response.read()

    def post_idoc(self, xml: str) -> Dict:
        """Post an inbound IDoc; what posting it applied, as SAP says."""
        receipt = json.loads(self.write("POST", "/sap/bc/idoc/idoc_xml", xml,
                                        "application/xml"))
        applied = receipt.get("APPLIED") or [{}]
        return applied[0]
