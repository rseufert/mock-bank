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
  refusal must not undo what the first file did. A NACHA header has no
  `MsgId`, so there the identification goes into the file creation time and
  modifier, which hold one to three letters or digits exactly. Two runs on one
  day are then never one file, and a longer identification is refused.
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
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from bank_messages import (ACCEPTED, NO_ANSWER, PAIN001, bank_documents, call, child_text,
                           entries, tag)

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

# The mailbox types a statement arrives as, and the NACHA return file an ACH
# payment's reason comes back in; and what each is called in a problem.
STATEMENT_KINDS = ("camt.053", "bai2")
# What a BAI2 statement cannot carry back unchanged (#171, #164 item 2). BAI2 has
# no escape character, so `mockbank.bai2._safe` replaces anything that would end a
# field, a record or a line with a space - and SAP matches the structured
# reference exactly. A reference that comes back changed never clears its invoice,
# so every run selects it again and pays it again.
#
# This is `mockbank.bai2.UNSAFE + CONTROLS`, written out because this example
# imports no mock; `tests/test_payment_run_readers.py` holds the two equal, so the
# copy cannot drift from the writer it is about.
UNCARRIABLE = ((",", "/", "\u2028", "\u2029", "\u0085")
               + tuple(chr(code) for code in range(0x20)) + ("\x7f",))

NACHA_RETURN = "nacha.return"
WHAT = {"camt.053": "statement", "bai2": "BAI2 statement",
        NACHA_RETURN: "return file"}

# NACHA mode: the run identifications a file header can tell apart (see
# `nacha_time_and_modifier`), the 36 file ID modifiers, and the first amount an
# entry's ten digits of cents cannot hold.
IDENTIFICATION = re.compile(r"[A-Z0-9]{1,3}")
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
NACHA_AMOUNT_LIMIT = Decimal("100000000.00")


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
    routing: str = ""           # in NACHA mode: the receiving bank's ABA number
    account: str = ""           # in NACHA mode: the account number there
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
    nacha_origin: str = ""      # the company identification, in NACHA mode
    # NACHA mode: each returned entry's R code, by its identification number,
    # from the bank's return files. Kept on the run because the return file and
    # the statement that credits the money back need not arrive together.
    return_reasons: Dict[str, str] = field(default_factory=dict)

    @property
    def msg_id(self) -> str:
        """The file's identity: what the bank tells two files apart by.

        A `pain.001`'s is its `MsgId`. A NACHA file has none; the bank reads the
        immediate origin, creation date, time and file ID modifier instead, so
        in NACHA mode the header is built from the run and this says what the
        bank will read from it. Either way the same run is the same file.
        """
        if self.nacha_origin:
            return "%s-%s%s%s" % ((self.nacha_origin, self.run_on.strftime("%y%m%d"))
                                  + nacha_time_and_modifier(self.identification))
        return "F110-%s-%s" % (self.run_on.strftime("%Y%m%d"), self.identification)

    def paying(self) -> List[Item]:
        return [i for i in self.items if i.status not in ("selected", "skipped")]


class PaymentRun:
    """Select from SAP, pay through the bank, and read back the pain.002.

    `company` is the paying account: `{"name", "iban", "bic"}`. House-bank
    data is not read from SAP here; it is the run's own configuration.

    `file_format="nacha"` pays in dollars by ACH instead (#55): the file is a
    NACHA file, the answer the bank's acknowledgement, and the suppliers are
    paid to the routing and account numbers SAP holds for them (`BankNumber`,
    `BankAccount`). `company` then also carries `company_id`, the company
    identification the bank knows the account by, and `routing`, the bank's.
    A NACHA account's statement may be BAI2 rather than a `camt.053`, and the
    reason an ACH payment came back is in the bank's NACHA return file:
    `reconcile` reads all three into the same statement (#57).
    """

    def __init__(self, sap: str, bank: str, company: Dict[str, str],
                 file_format: str = "iso20022"):
        self.sap = sap
        self.bank = bank
        self.company = company
        if file_format not in ("iso20022", "nacha"):
            raise ValueError("file_format is 'iso20022' or 'nacha', not %r" % file_format)
        self.nacha = file_format == "nacha"
        self.currency = "USD" if self.nacha else "EUR"
        self.session = SapSession(sap)

    def run(self, run_on: datetime.date, identification: str) -> Run:
        run = Run(run_on, identification,
                  nacha_origin=self.company["company_id"] if self.nacha else "")
        if self.nacha:
            refusal = self.nacha_refusal(identification)
            if refusal:
                # Nothing selected is nothing paid; the file could not have
                # been told apart from another, or could not be written.
                run.problems.append(refusal + ", so no open item was selected")
                return run
        try:
            run.items = self.select(run_on)
        except urllib.error.HTTPError as error:
            run.problems.append("SAP answered %d to the selection, so no open item "
                                "was selected" % error.code)
            return run
        except (urllib.error.URLError, OSError) as error:
            # Nothing selected is nothing paid, which is the safe side; the
            # problem says why the list is empty rather than leaving it to look
            # like a day with nothing due.
            run.problems.append(unanswered("SAP", "no open item was selected", error))
            return run
        self.send(run)
        self.read_status(run)
        return run

    def nacha_refusal(self, identification: str) -> str:
        """Why this run cannot be a NACHA file from this company, or "".

        The bank tells files apart by origin, date, time and modifier, and
        the time and modifier are all a run's identification can go into: an
        identification of up to three letters and digits fits exactly, so two
        runs never share a file and the same run is always the same file (see
        `nacha_time_and_modifier`). A longer one would have to be hashed, and
        two runs that hashed alike would be one file to the bank: the second
        refused as `DUPL`, a repeat to all appearances, and never paid.
        """
        if not IDENTIFICATION.fullmatch(identification):
            return ("identification %r is not one to three capital letters or digits, "
                    "which is what a NACHA file header can tell apart" % identification)
        company_id, name = self.company["company_id"], self.company["name"]
        if len(company_id) > 10 or not company_id.isascii():
            return "company identification %r is not ten ASCII characters or fewer" % company_id
        if not name.isascii():
            return "company name %r is not ASCII, which a NACHA file is" % name
        return ""

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
        if item.currency != self.currency:
            item.status, item.reason = "skipped", "%s is in %s, not %s" % (
                "an ACH credit" if self.nacha else "a SEPA transfer", self.currency,
                item.currency)
            return
        if self.nacha and (len(item.reference) > 15 or not item.reference.isascii()
                           or " " in item.reference):
            # The individual identification number holds 15; one cut short
            # would be a reference the supplier cannot match, and one with a
            # space could not be read back from the acknowledgement's words.
            item.status, item.reason = "skipped", (
                "reference %r is not up to 15 ASCII characters without a space, "
                "as a NACHA entry's identification number is" % item.reference)
            return
        if self.nacha:
            cannot_carry = sorted({c for c in item.reference if c in UNCARRIABLE})
            if cannot_carry:
                # Not a NACHA limit: the entry would carry it. The statement that
                # has to bring it back is the one that cannot, so the payment
                # could never be matched to its invoice and the invoice would be
                # paid again on the next run.
                item.status, item.reason = "skipped", (
                    "reference %r holds %s, which a BAI2 statement cannot carry "
                    "back unchanged, so the payment could not be matched to the "
                    "invoice" % (item.reference,
                                 ", ".join(repr(c) for c in cannot_carry)))
                return
        if self.nacha and Decimal(item.amount) >= NACHA_AMOUNT_LIMIT:
            item.status, item.reason = "skipped", (
                "%s is more than a NACHA entry's ten digits hold" % item.amount)
            return
        banks = odata(self.sap, BANKS, **{"$filter": (
            "BusinessPartner eq '%s' and BankIdentification eq '%s'"
            % (item.supplier, invoice["BPBankAccountInternalID"]))})
        if not banks or (not self.nacha and not banks[0].get("IBAN")):
            item.status, item.reason = "skipped", "no IBAN for account %s of %s" % (
                invoice["BPBankAccountInternalID"], item.supplier)
            return
        bank = banks[0]
        item.iban, item.bic = bank["IBAN"], bank.get("SWIFTCode", "")
        item.name = bank.get("BankAccountHolderName") or item.supplier
        if self.nacha:
            item.routing, item.account = bank.get("BankNumber", ""), bank.get("BankAccount", "")
            if not (len(item.routing) == 9 and item.routing.isdigit() and item.account):
                item.status, item.reason = "skipped", (
                    "no ABA routing and account number for account %s of %s"
                    % (invoice["BPBankAccountInternalID"], item.supplier))
            elif len(item.account) > 17 or not item.account.isascii():
                # Cut short, an account number is somebody else's account.
                item.status, item.reason = "skipped", (
                    "account number %r is longer than a NACHA entry's 17 characters"
                    % item.account)
            elif not item.name.isascii():
                item.status, item.reason = "skipped", (
                    "name %r is not ASCII, which a NACHA file is" % item.name)

    # -- 2. send ---------------------------------------------------------------

    def send(self, run: Run) -> None:
        paying = [i for i in run.items if i.status == "selected"]
        if not paying:
            return
        run.http_status, body = call(
            self.bank, "POST", "/payments",
            self.nacha_file(run, paying) if self.nacha else self.payment_file(run, paying),
            "text/plain" if self.nacha else "application/xml")
        if run.http_status == NO_ANSWER:
            # The file did not reach the bank, so nothing was sent: every item
            # stays selected, for the same run to be sent again.
            run.problems.append("the bank did not answer, so the payment file was "
                                "not sent: %s" % body.decode("utf-8", "replace"))
            return
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

    def nacha_file(self, run: Run, items: List[Item]) -> str:
        """The same payments as a NACHA file: one CCD batch of ACH credits.

        Written by hand here, as the `pain.001` is, because the example imports
        neither mock. The header is built from the run, so the same run is the
        same file (see `Run.msg_id`); every control total is computed.
        """
        odfi = self.company["routing"][:8]
        cents = [int(Decimal(i.amount) * 100) for i in items]
        entry_hash = sum(int(i.routing[:8]) for i in items)
        time, modifier = nacha_time_and_modifier(run.identification)
        lines = [
            "101 %s%s%s%s%s094101%-23s%-23s%8s" % (
                self.company["routing"], self.company["company_id"].rjust(10),
                run.run_on.strftime("%y%m%d"), time, modifier,
                "MOCK BANK", self.company["name"][:23], ""),
            "5220%-16s%20s%-10sCCD%-10s%6s%s   1%s%07d" % (
                self.company["name"][:16], "", self.company["company_id"][:10],
                "SUPPLIERS", "", run.run_on.strftime("%y%m%d"), odfi, 1)]
        for number, (item, amount) in enumerate(zip(items, cents), start=1):
            lines.append("622%s%-17s%010d%-15s%-22s  0%s%07d" % (
                item.routing, item.account, amount, item.reference,
                item.name[:22], odfi, number))
        lines.append("8220%06d%010d%012d%012d%-10s%19s%6s%s%07d" % (
            len(items), entry_hash % 10 ** 10, 0, sum(cents),
            self.company["company_id"][:10], "", "", odfi, 1))
        blocks = -(-(len(lines) + 1) // 10)
        lines.append("9%06d%06d%08d%010d%012d%012d%39s" % (
            1, blocks, len(items), entry_hash % 10 ** 10, 0, sum(cents), ""))
        while len(lines) % 10:
            lines.append("9" * 94)
        return "\n".join(lines) + "\n"

    # -- 3. read the pain.002, or the acknowledgement ----------------------------

    def read_status(self, run: Run) -> None:
        """Match the bank's status reports to the run by MsgId, then EndToEndId.

        Only `pain.002`s are collected, so the `camt` messages stay in the
        mailbox for whatever reconciles the statement.
        """
        if self.nacha:
            return self.read_acknowledgement(run)
        status, raw = call(self.bank, "GET", "/_mock/mailbox?raw&type=pain.002")
        if status != 200:
            run.problems.append("the bank's mailbox %s, so no status report was read"
                                % said(status, raw))
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

    def read_acknowledgement(self, run: Run) -> None:
        """NACHA mode: the bank's acknowledgement, matched by FILE, then ENTRY.

        The acknowledgement is mock-bank's plain shape, one fact to a line
        starting with what it is, so it is read with `split()`: `STATUS RJCT
        DUPL` is the bank having this run already, and an `ENTRY` line names the
        entry's identification number - the supplier's invoice number - and
        whether it was accepted or rejected with a return code.
        """
        status, raw = call(self.bank, "GET", "/_mock/mailbox?raw&type=nacha.ack")
        if status != 200:
            run.problems.append("the bank's mailbox %s, so no acknowledgement was read"
                                % said(status, raw))
            return
        by_reference = {i.reference: i for i in run.paying()}
        for ack in raw.decode("utf-8").split("ACKNOWLEDGEMENT ")[1:]:
            lines = [line.split() for line in ack.splitlines()]
            fields = {words[0]: words[1:] for words in lines if words}
            if fields.get("FILE") != [run.msg_id]:
                continue
            # `or`, not a default: a bare STATUS line is there and empty.
            state = fields.get("STATUS") or [""]
            if state[:2] == ["RJCT", "DUPL"]:
                run.duplicate = True
                continue
            for words in (w for w in lines if w and w[0] == "ENTRY"):
                item = by_reference.get(words[2])
                if item is None:
                    continue
                if words[4] == "ACCEPTED":
                    item.status, item.reason = "accepted", ""
                else:
                    item.status, item.reason = "rejected", words[5]
            if state[0] == "RJCT" and len(state) > 1 and not any(
                    w[0] == "ENTRY" for w in lines if w):
                for item in by_reference.values():
                    item.status, item.reason = "rejected", state[1]

    # -- 4. reconcile: post the statements to SAP ------------------------------

    def reconcile(self, run: Run) -> None:
        """Post every statement the bank has sent for the paying account.

        Only statements are collected; a statement is the bank's record, a
        `camt.054` is news. Oldest first, because SAP chains each statement to
        the one before it.

        In NACHA mode the statement may be a `camt.053` or a BAI2 file, which
        is what a US bank sends an ACH account (#57), and both are read into
        the same statement. The reason an ACH payment came back is in the
        bank's NACHA return file rather than on either statement, so that is
        collected too, and a returned item is given its `R` code. The first
        mailbox that does not answer is the one problem recorded; what was
        already collected is still posted.
        """
        kinds = ((STATEMENT_KINDS + (NACHA_RETURN,)) if self.nacha
                 else STATEMENT_KINDS[:1])
        statements = []
        for kind in kinds:
            status, raw = call(self.bank, "GET", "/_mock/mailbox?raw&type=" + kind)
            if status != 200:
                run.problems.append("the bank's mailbox %s, so no %s was read"
                                    % (said(status, raw), WHAT[kind]))
                break
            text = raw.decode("utf-8")
            if kind == NACHA_RETURN:
                run.return_reasons.update(nacha_return_reasons(text))
                continue
            try:
                found = camt_statements(text) if kind == "camt.053" else bai2_statements(text)
            except ValueError as error:
                run.problems.append("a %s from the bank could not be read: %s"
                                    % (WHAT[kind], error))
                continue
            for s in found:
                if s["account"] not in self.own_account():
                    continue
                if s["opening"] is None or s["closing"] is None:
                    run.problems.append(
                        "the %s for %s gives no opening and closing ledger balance "
                        "(010 and 015), so it was not posted" % (WHAT[kind], s["day"]))
                    continue
                statements.append(s)
        statements.sort(key=lambda s: (s["day"], int(s["number"] or 0)))
        for statement in statements:
            self.post_statement(run, statement)

    def own_account(self) -> set:
        """What a statement may name the paying account by: its IBAN, or in a
        BAI2 file its domestic account number."""
        return {self.company["iban"], self.company.get("company_id", "")} - {""}

    def post_statement(self, run: Run, statement: Dict) -> Dict:
        number, day = statement["number"], statement["day"]
        opening, closing = statement["opening"], statement["closing"]
        lines = statement["lines"]
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
        except (urllib.error.URLError, OSError) as error:
            applied = {}
            record["error"] = unanswered("SAP", "statement %s was not posted" % number,
                                         error)
            run.problems.append(record["error"])
        record.update(cleared=applied.get("CLEARED", []),
                      reopened=applied.get("REOPENED", []),
                      unprocessed=applied.get("UNPROCESSED", []),
                      findings=applied.get("FINDINGS", []))
        run.statements.append(record)

        # Keyed on the accounting document, not the invoice number (#171, #164
        # item 5). An invoice number is a supplier's own sequence, so two
        # suppliers can both bill `INV-1`: keyed on the reference, SAP's row for
        # one supplier's document would be attributed to the other supplier's
        # item. The accounting document is SAP's own identifier and is unique, and
        # SAP returns it on every CLEARED and REOPENED row. mock-sap#87 is the
        # other half - it makes SAP pick the right item; this makes the run agree
        # with whichever item SAP picked.
        by_document = {i.document.rsplit("/", 1)[-1]: i for i in run.paying()}

        def attributed(line, what):
            """SAP's row matched to the item it is about, or a problem said out loud."""
            document = line.get("ACCOUNTINGDOCUMENT")
            if not document:
                run.problems.append(
                    "SAP's %s row for statement %s names no accounting document, "
                    "so it could not be matched to an item: %r" % (what, number, line))
                return None
            return by_document.get(document)

        for line in record["cleared"]:
            item = attributed(line, "cleared")
            if item is not None and item.status == "accepted":
                item.status, item.reason = "cleared", line["CLEARINGDOCUMENT"]
        # A return is a credit on a later statement under the original
        # EndToEndId; SAP has reversed the clearing and the invoice is owed
        # again. The reason code is the bank's, from the entry's RtrInf.
        why = {e["end_to_end_id"]: e["returned_for"] for e in lines if e["side"] == "CRDT"}
        why.update(run.return_reasons)
        for line in record["reopened"]:
            item = attributed(line, "reopened")
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


def camt_statements(text: str) -> List[Dict]:
    """Every `Stmt` in a run of `camt.053` documents, as a plain statement."""
    return [{"account": child_text(s, "Acct", "Id", "IBAN"),
             "number": child_text(s, "ElctrncSeqNb"),
             "day": child_text(s, "FrToDt", "FrDtTm")[:10],
             "opening": balance(s, "OPBD"), "closing": balance(s, "CLBD"),
             "lines": entries(s)}
            for d in bank_documents(text) if tag(d[0]) == "BkToCstmrStmt"
            for s in d[0] if tag(s) == "Stmt"]


# The records a BAI2 file is made of. A line starts one only when it begins
# with one of these and a comma, and a `/` inside a line ends one only when one
# of these and a comma follow it: a slash inside a reference (`AB/GS/RP0001`) or
# an address (`Unit 4/88 Harbour St`) is text, and a line that starts with
# neither is the record above wrapped onto a second line. The same rule as
# `mockbank.bai2` since #128, written again here because this example imports
# neither mock, and the rule moov-io/bai2's scanner states too.
BAI2_CODES = ("01", "02", "03", "16", "49", "88", "98", "99")
BAI2_STARTS = re.compile(r"^(?:%s)," % "|".join(BAI2_CODES))
BAI2_NEXT = re.compile(r"/[ \t]*(?=(?:%s),)" % "|".join(BAI2_CODES))


def _bai2_continues(lines: List[str], index: int) -> bool:
    """Whether the line after `lines[index]` continues its record: the next line
    that is not blank, and only if it does not start with a record code."""
    for later in lines[index + 1:]:
        if not later.rstrip():
            continue
        return not BAI2_STARTS.match(later.lstrip())
    return False


def _bai2_ended(piece: str) -> str:
    """A record's text with its terminator off, where one really ends (#150)."""
    return piece[:-1] if piece.endswith("/") else piece


def bai2_records(text: str) -> List[List[str]]:
    """The records of a BAI2 file as lists of fields, continuations folded in.

    A record may end at `/`, at the end of its line, or share a line with the
    next; an `88` continues the field stream of the record before it.
    """
    raw: List[str] = []
    lines = text.splitlines()
    for index, raw_line in enumerate(lines):
        trimmed = raw_line.rstrip()
        if not trimmed:
            continue
        # Trailing whitespace survives only when the next line continues this
        # record, because the join inserts nothing and a wrapped field's padding
        # is its content (#142). Everywhere else it is padding around a record
        # and comes off, as it did before. moov trims in exactly the same place.
        # A `/` at the end of such a line is content for the same reason (#150):
        # a `/` ends a record only when a record code follows it, on this line or
        # the next non-blank one, or when nothing follows. `AB/` + `GS/RP0001`
        # read as `ABGS/RP0001` before this, so a reference lost a character and
        # the invoice behind it stopped matching. A line the next one continues
        # therefore contributes its raw text, terminator and padding alike.
        wrapped = _bai2_continues(lines, index)
        line = raw_line if wrapped else trimmed
        if not BAI2_STARTS.match(line.lstrip()):
            if not raw:
                raise ValueError("a BAI2 file starts with a record code, not %r"
                                 % line.strip()[:40])
            # Joined with nothing (#142): a wrapped line continues the one
            # above and the break is not part of any field. moov-io/bai2's own
            # scanner does the same, and `mockbank.bai2._records` is held to this
            # by `tests/test_payment_run_readers.py` - change one and that test
            # fails, which is the point of it.
            raw[-1] += line if wrapped else _bai2_ended(line)
            continue
        # Only the line's last piece can be carried into the next line: anything
        # before it is followed by a record code here, so its `/` ends a record.
        pieces = [p for p in BAI2_NEXT.split(line.lstrip()) if p.strip()]
        raw += [p if (wrapped and position == len(pieces)) else _bai2_ended(p)
                for position, p in enumerate(pieces, start=1)]
    records: List[List[str]] = []
    for record in raw:
        fields = record.split(",")
        if fields[0] == "88" and records:
            records[-1] += fields[1:]
        else:
            records.append(fields)
    return records


def funds_width(fields: List[str]) -> int:
    """How many fields a funds type takes, starting with the type itself.

    Blank, `Z`, `0`, `1` and `2` stand alone; `V` adds a date and a time, `S`
    three amounts, and `D` a count and that many (days, amount) pairs.
    """
    kind = fields[0] if fields else ""
    if kind in ("", "Z", "0", "1", "2"):
        return 1
    if kind == "V":
        return 3
    if kind == "S":
        return 4
    if kind == "D":
        return 2 + 2 * int(fields[1] or "0")
    raise ValueError("funds type %r is not one BAI2 defines" % kind)


def bai2_statements(text: str) -> List[Dict]:
    """Every account in a run of BAI2 files, as the same plain statement.

    Read by hand, as the `camt.053` is, because the example imports neither
    mock. The `01` gives the file number, the `02` the as-of date, the `03` the
    account and its `010` opening and `015` closing ledger balances, and each
    `16` a movement: amounts are in cents, and the direction is the type code's
    range, 100 to 399 a credit and 400 to 699 a debit, so no particular code
    need be known. The `EndToEndId` is the bank reference number and the file's
    `MsgId` the customer reference, found after however many fields the funds
    type takes; the text after them runs to the end of the record, commas and
    all, and is not needed here. An account the bank reports
    without both ledger balances - an intraday position, say - comes back with
    `opening` and `closing` of None rather than stopping the file: it is not a
    statement, but the accounts beside it may be.
    """
    out: List[Dict] = []
    number = day = ""
    current: Optional[Dict] = None
    for fields in bai2_records(text):
        code = fields[0]
        if code == "01":
            number = fields[5]
        elif code == "02":
            day = "20%s-%s-%s" % (fields[4][:2], fields[4][2:4], fields[4][4:6])
        elif code == "03":
            balances, at = {}, 3
            while at + 2 < len(fields):
                if fields[at]:
                    balances[fields[at]] = cents(fields[at + 1])
                at += 3 + funds_width(fields[at + 3:])
            current = {"account": fields[1], "number": number, "day": day,
                       "opening": balances.get("010"), "closing": balances.get("015"),
                       "lines": []}
        elif code == "16" and current is not None:
            kind = int(fields[1])
            if not (100 <= kind <= 699):
                raise ValueError("type code %s is neither a credit nor a debit" % fields[1])
            after = 3 + funds_width(fields[3:])
            refs = fields[after:after + 2] + ["", ""]
            current["lines"].append({
                "amount": str(cents(fields[2])),
                "side": "CRDT" if kind < 400 else "DBIT",
                "booked_on": day, "end_to_end_id": refs[0], "msg_id": refs[1],
                "returned_for": ""})
        elif code == "49" and current is not None:
            out.append(current)
            current = None
    return out


def cents(text: str) -> Decimal:
    """A BAI2 amount, which is in cents with no point: `-1250` is -12.50."""
    return Decimal(int(text or "0")).scaleb(-2)


def nacha_return_reasons(text: str) -> Dict[str, str]:
    """Each returned entry's `R` code, by its identification number.

    Read by hand from the bank's NACHA return files: an entry detail (`6`)
    names the original entry by its individual identification number,
    columns 40 to 54, and the addenda after it (`7`, type `99`) gives the
    return reason code in columns 4 to 6.
    """
    reasons, last = {}, ""
    for line in text.splitlines():
        if line.startswith("6"):
            last = line[39:54].strip()
        elif line.startswith("799") and last:
            reasons[last] = line[3:6]
            last = ""
    return reasons


def nacha_time_and_modifier(identification: str) -> Tuple[str, str]:
    """The file creation time and file ID modifier for a run, from its identification.

    NACHA tells two files of one day from one origin apart by these two, so two
    runs need two pairs and the same run always the same pair: derived from the
    identification, not the clock, which a re-run would move on. Numbering
    every identification of one to three letters and digits in order gives
    47,988 of them, fewer than the 1,440 minutes of a day times the 36
    modifiers, so each has its own pair; `PaymentRun.nacha_refusal` refuses
    the rest.
    """
    number = (sum(36 ** length for length in range(1, len(identification)))
              + int(identification, 36))
    minutes, modifier = divmod(number, 36)
    return "%02d%02d" % divmod(minutes, 60), ALPHABET[modifier]


def said(status: int, body: bytes) -> str:
    """How the bank answered, in words: not at all, or with which status."""
    text = body.decode("utf-8", "replace")[:200]
    if status == NO_ANSWER:
        return "did not answer (%s)" % text
    return "answered %d: %s" % (status, text)


def unanswered(side: str, consequence: str, error) -> str:
    """A host that did not answer at all, named, and what that left undone."""
    return "%s did not answer, so %s: %s" % (side, consequence,
                                              getattr(error, "reason", error))


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
