"""Reading and writing the messages, on top of the dictionary in ``schema``.

This is the reader half: ``read_pain001`` turns a payment file into a
``PaymentFile`` of ``Batch`` es of ``Payment`` s. Both ``pain.001.001.09`` and
``.001.03`` read into the same model, because ``schema.read`` already keys
the two versions alike; the one shape they still differ in, the requested
execution date (a date in ``.03``, a date-or-datetime choice in ``.09``), is
evened out here.

The writer half builds what the bank sends back - ``write_pain002`` and
``write_camt054`` - as mappings handed to ``schema.serialize``, which puts
every element in the order the dictionary declares. No writer here names an
element order or concatenates XML; if one does, the declaration is missing.

The reader is forgiving on purpose: what does not fit the declaration is left
as None rather than raised, because ``validate`` is where a file is judged
and it has to be able to say what is wrong with any file. Amounts are
integers in the currency's minor units; the only decimal strings are on the
wire.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
from typing import List, Optional
from xml.etree import ElementTree as ET

from . import schema

READABLE = [name for name, m in schema.MESSAGES.items() if m.direction == "in"]


class Payment:
    """One credit transfer: ``CdtTrfTxInf``."""

    def __init__(self, node):
        self.node = node
        ids = node.get("PmtId", {})
        self.end_to_end_id = _text(ids.get("EndToEndId"))
        self.instruction_id = _text(ids.get("InstrId"))
        amount = node.get("Amt", {})
        instructed = amount.get("InstdAmt")
        equivalent = amount.get("EqvtAmt", {}).get("Amt")
        # EqvtAmt asks the bank to convert; the mock does no FX, and says so
        # in validate. It is still read, so the finding can name the amount.
        self.equivalent = equivalent is not None
        chosen = instructed if instructed is not None else equivalent
        self.amount = chosen.minor if isinstance(chosen, schema.Amount) else None
        self.currency = chosen.ccy if isinstance(chosen, schema.Amount) else None
        creditor = node.get("Cdtr", {})
        self.creditor_name = _text(creditor.get("Nm"))
        self.creditor_account = _account_id(node.get("CdtrAcct"))
        self.creditor_bic = _bic(node.get("CdtrAgt"))
        remittance = node.get("RmtInf", {})
        self.remittance = [_text(line) for line in remittance.get("Ustrd", [])]
        self.remittance_references = []
        for structured in remittance.get("Strd", []):
            for doc in structured.get("RfrdDocInf", []):
                if doc.get("Nb"):
                    self.remittance_references.append(_text(doc["Nb"]))
            reference = structured.get("CdtrRefInf", {}).get("Ref")
            if reference:
                self.remittance_references.append(_text(reference))

    @property
    def path(self):
        return self.node.path

    def to_json(self):
        return {"end_to_end_id": self.end_to_end_id, "instruction_id": self.instruction_id,
                "amount": self.amount, "currency": self.currency,
                "creditor_name": self.creditor_name, "creditor_account": self.creditor_account,
                "creditor_bic": self.creditor_bic, "remittance": self.remittance,
                "remittance_references": self.remittance_references}

    def __repr__(self):
        return "Payment(%s, %s %s)" % (self.end_to_end_id, self.amount, self.currency)


class Batch:
    """One payment information block: ``PmtInf``, one debtor account."""

    def __init__(self, node):
        self.node = node
        self.pmt_inf_id = _text(node.get("PmtInfId"))
        self.requested_execution_date = _requested_date(node.get("ReqdExctnDt"))
        self.debtor_name = _text(node.get("Dbtr", {}).get("Nm"))
        self.debtor_account = _account_id(node.get("DbtrAcct"))
        self.debtor_account_currency = _text(node.get("DbtrAcct", {}).get("Ccy"))
        self.debtor_bic = _bic(node.get("DbtrAgt"))
        self.nb_of_txs = _count(node.get("NbOfTxs"))
        self.ctrl_sum = _decimal(node.get("CtrlSum"))
        self.payments: List[Payment] = [Payment(tx) for tx in node.get("CdtTrfTxInf", [])]

    @property
    def path(self):
        return self.node.path

    def to_json(self):
        when = self.requested_execution_date
        return {"pmt_inf_id": self.pmt_inf_id,
                "requested_execution_date": when.isoformat() if when else None,
                "debtor_name": self.debtor_name, "debtor_account": self.debtor_account,
                "debtor_account_currency": self.debtor_account_currency,
                "debtor_bic": self.debtor_bic,
                "payments": [p.to_json() for p in self.payments]}

    def __repr__(self):
        return "Batch(%s, %d payments)" % (self.pmt_inf_id, len(self.payments))


class PaymentFile:
    """A ``pain.001``: its group header and its batches."""

    def __init__(self, message, document):
        self.message = message.name
        body = document.get("CstmrCdtTrfInitn", {})
        header = body.get("GrpHdr", {})
        self.node = body
        self.header = header
        self.msg_id = _text(header.get("MsgId"))
        self.creation_time = _text(header.get("CreDtTm"))
        self.initiating_party = _text(header.get("InitgPty", {}).get("Nm"))
        self.nb_of_txs = _count(header.get("NbOfTxs"))
        self.ctrl_sum = _decimal(header.get("CtrlSum"))
        self.batches: List[Batch] = [Batch(b) for b in body.get("PmtInf", [])]

    @property
    def payments(self) -> List[Payment]:
        return [p for batch in self.batches for p in batch.payments]

    def to_json(self):
        """The mock's reading of the file; amounts in minor units."""
        return {"message": self.message, "msg_id": self.msg_id,
                "creation_time": self.creation_time,
                "initiating_party": self.initiating_party,
                "batches": [b.to_json() for b in self.batches]}

    def __repr__(self):
        return "PaymentFile(%s, %s, %d batches)" % (self.message, self.msg_id, len(self.batches))


def read_pain001(data: bytes) -> PaymentFile:
    """The file as a PaymentFile. Raises ValueError for something that is
    not a pain.001 the mock reads; ``validate`` says why in a finding."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("not well-formed XML: %s" % exc)
    return from_tree(root)


def from_tree(root) -> PaymentFile:
    message = schema.identify(root)
    if message is None or message.name not in READABLE:
        raise ValueError("not a message the mock reads (%s)" % ", ".join(READABLE))
    return PaymentFile(message, schema.read(message, root))


# -- evening out -------------------------------------------------------------

def _text(value) -> Optional[str]:
    return value if isinstance(value, str) else None


def _account_id(account) -> Optional[str]:
    ident = (account or {}).get("Id", {})
    if isinstance(ident.get("IBAN"), str):
        return ident["IBAN"]
    return _text(ident.get("Othr", {}).get("Id"))


def _bic(agent) -> Optional[str]:
    return _text((agent or {}).get("FinInstnId", {}).get("BICFI"))


def _count(value) -> Optional[int]:
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _decimal(value) -> Optional[Decimal]:
    try:
        return Decimal(value) if isinstance(value, str) else None
    except InvalidOperation:
        return None


def _requested_date(value) -> Optional[datetime.date]:
    """``.03`` carries a date; ``.09`` a choice of date or datetime."""
    if isinstance(value, dict):
        value = value.get("Dt") or (value.get("DtTm") or "")[:10]
    try:
        return datetime.date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


# -- writing -----------------------------------------------------------------

PAIN002 = schema.MESSAGES["pain.002.001.10"]
CAMT054 = schema.MESSAGES["camt.054.001.08"]

# The bank's own BIC, as the sample files name it and the seed holds it.
BANK_BIC = "MOCKNL2A"


def _decimal_sum(amounts) -> str:
    """Amounts [(minor, ccy)] as a decimal string: a control sum."""
    total = sum((Decimal(minor).scaleb(-schema.exponent(ccy)) for minor, ccy in amounts),
                Decimal(0))
    return str(total)


def _reason(code, text):
    """A StsRsnInf: the code, and the prose cut to the 105 characters allowed."""
    out = {"Rsn": {"Cd": code}}
    if text:
        out["AddtlInf"] = [text[:105]]
    return out


def write_pain002(decision, msg_id, created_at) -> bytes:
    """The status report for a decided file: ``pain.002.001.10``.

    ``OrgnlGrpInfAndSts`` carries the original ``MsgId`` and the group status
    (``ACCP``, ``PART`` or ``RJCT``). A file rejected outright has that and
    its reason only. Otherwise there is one ``OrgnlPmtInfAndSts`` per batch,
    with the batch's own status, and a ``TxInfAndSts`` per payment carrying
    ``OrgnlEndToEndId``, ``TxSts`` and, for a rejection, ``StsRsnInf/Rsn/Cd``.
    """
    payment_file = decision.payment_file
    payments = payment_file.payments
    group = {"OrgnlMsgId": payment_file.msg_id,
             "OrgnlMsgNmId": payment_file.message,
             "OrgnlNbOfTxs": len(payments),
             "OrgnlCtrlSum": _decimal_sum((p.amount, p.currency) for p in payments
                                          if p.amount is not None),
             "GrpSts": decision.status}
    if decision.rejected_outright:
        group["StsRsnInf"] = [_reason(decision.reason, decision.reason_text)]
    batches = []
    if not decision.rejected_outright:
        for batch in payment_file.batches:
            mine = [d for d in decision.payments if d.batch is batch]
            accepted = sum(1 for d in mine if d.outcome == "accepted")
            transactions = []
            for d in mine:
                tx = {"OrgnlEndToEndId": d.payment.end_to_end_id,
                      "TxSts": "ACCP" if d.outcome == "accepted" else "RJCT"}
                if d.payment.instruction_id:
                    tx["OrgnlInstrId"] = d.payment.instruction_id
                if d.outcome != "accepted":
                    tx["StsRsnInf"] = [_reason(d.reason, d.reason_text)]
                transactions.append(tx)
            batches.append({
                "OrgnlPmtInfId": batch.pmt_inf_id,
                "OrgnlNbOfTxs": len(mine),
                "OrgnlCtrlSum": _decimal_sum((d.payment.amount, d.payment.currency)
                                             for d in mine),
                "PmtInfSts": ("ACCP" if accepted == len(mine)
                              else "RJCT" if not accepted else "PART"),
                "TxInfAndSts": transactions})
    return schema.serialize(PAIN002, {"CstmrPmtStsRpt": {
        "GrpHdr": {"MsgId": msg_id, "CreDtTm": created_at,
                   "DbtrAgt": {"FinInstnId": {"BICFI": BANK_BIC}}},
        "OrgnlGrpInfAndSts": group,
        "OrgnlPmtInfAndSts": batches}})


def _entry(p, day):
    """One booked movement as an ``Ntry``: what a ``camt.054`` and a
    ``camt.053`` both carry for a payment row (with its original ``msg_id``
    joined in).

    A row with ``credit`` set is the payment coming back: a ``CRDT`` under
    ``schema.RETURNED_CREDIT``, with ``RtrInf`` giving the reason and the code
    the original debit was booked under. Everything else about it - the
    ``EndToEndId`` above all - is the original payment's, because that is how a
    client finds the invoice a return reopens.
    """
    credit = bool(p.get("credit"))
    domain, family, sub = schema.RETURNED_CREDIT if credit else schema.BOOKED_DEBIT
    side = "CRDT" if credit else "DBIT"
    amount = schema.Amount(p["amount"], p["currency"])
    refs = {"MsgId": p["msg_id"], "EndToEndId": p["end_to_end_id"]}
    if p["pmt_inf_id"]:
        refs["PmtInfId"] = p["pmt_inf_id"]
    if p["instruction_id"]:
        refs["InstrId"] = p["instruction_id"]
    tx = {"Refs": refs, "Amt": amount, "CdtDbtInd": side}
    parties = {}
    if p["creditor_name"]:
        parties["Cdtr"] = {"Pty": {"Nm": p["creditor_name"][:140]}}
    if p["creditor_iban"]:
        parties["CdtrAcct"] = {"Id": (
            {"IBAN": p["creditor_iban"]} if schema.iban_is_valid(p["creditor_iban"])
            else {"Othr": {"Id": p["creditor_iban"]}})}
    if parties:
        tx["RltdPties"] = parties
    if p["creditor_bic"]:
        tx["RltdAgts"] = {"CdtrAgt": {"FinInstnId": {"BICFI": p["creditor_bic"]}}}
    if credit:
        original = schema.BOOKED_DEBIT
        tx["RtrInf"] = {
            "OrgnlBkTxCd": {"Domn": {"Cd": original[0], "Fmly": {
                "Cd": original[1], "SubFmlyCd": original[2]}}},
            "Rsn": {"Cd": p["return_reason"]}}
    return {
        "Amt": amount, "CdtDbtInd": side, "Sts": {"Cd": "BOOK"},
        "BookgDt": {"Dt": day}, "ValDt": {"Dt": day},
        "AcctSvcrRef": ("MB-RTR-%d" if credit else "MB-PMT-%d") % p["id"],
        "BkTxCd": {"Domn": {"Cd": domain, "Fmly": {"Cd": family, "SubFmlyCd": sub}}},
        "NtryDtls": [{"TxDtls": [tx]}]}


def write_camt054(account, payments, day, msg_id, created_at) -> bytes:
    """The debit notification for one account and one booking:
    ``camt.054.001.08``.

    One notification per account each time payments book, with an ``Ntry``
    per payment: everything that books together - on receipt, or as one move
    of the clock crosses its settlement date - is reported together. That is
    a choice: banks also send one per payment, and the README says which
    profile the mock follows. Each entry
    keeps its ``EndToEndId`` in ``NtryDtls/TxDtls/Refs``, carries the bank
    transaction code ``schema.BOOKED_DEBIT``, and is booked and valued on
    ``day``.

    ``account`` is the account row; ``payments`` are its booked payment rows
    for ``day``, with the original ``msg_id`` joined in.
    """
    entries = [_entry(p, day) for p in payments]
    return schema.serialize(CAMT054, {"BkToCstmrDbtCdtNtfctn": {
        "GrpHdr": {"MsgId": msg_id, "CreDtTm": created_at},
        "Ntfctn": [{"Id": msg_id, "CreDtTm": created_at,
                    "Acct": {"Id": {"IBAN": account["iban"]}, "Ccy": account["currency"],
                             "Ownr": {"Nm": account["name"][:140]},
                             "Svcr": {"FinInstnId": {"BICFI": BANK_BIC}}},
                    "Ntry": entries}]}})


CAMT053 = schema.MESSAGES["camt.053.001.08"]


def _balance(code, minor, ccy, day):
    """A ``Bal``: the amount is unsigned on the wire, the sign is CdtDbtInd."""
    return {"Tp": {"CdOrPrtry": {"Cd": code}},
            "Amt": schema.Amount(abs(minor), ccy),
            "CdtDbtInd": "CRDT" if minor >= 0 else "DBIT",
            "Dt": {"Dt": day}}


def write_camt053(account, day, number, opening, closing, payments, msg_id,
                  created_at, zone) -> bytes:
    """The end-of-day statement for one account and one business day:
    ``camt.053.001.08``.

    ``opening`` and ``closing`` are the booked balances (``OPBD``, ``CLBD``)
    in minor units, signed; ``payments`` are the payment rows whose entries
    the statement shows, debits and - with ``credit`` set - returns; ``number`` is the account's statement number, used
    for both ``ElctrncSeqNb`` and ``LglSeqNb``. ``TxsSummry`` totals the
    entries shown. The writer does not check that the balances reconcile:
    under ``statement-gap`` they are meant not to.
    """
    ccy = account["currency"]
    entries = [_entry(p, day) for p in payments]
    credits = [p["amount"] for p in payments if p.get("credit")]
    debits = [p["amount"] for p in payments if not p.get("credit")]
    summary = {"TtlNtries": {"NbOfNtries": len(entries)}}
    if entries:
        net = sum(credits) - sum(debits)
        summary["TtlNtries"].update({
            "Sum": format_decimal(sum(credits) + sum(debits), ccy),
            "TtlNetNtry": {"Amt": format_decimal(abs(net), ccy),
                           "CdtDbtInd": "CRDT" if net >= 0 else "DBIT"}})
        if credits:
            summary["TtlCdtNtries"] = {"NbOfNtries": len(credits),
                                       "Sum": format_decimal(sum(credits), ccy)}
        if debits:
            summary["TtlDbtNtries"] = {"NbOfNtries": len(debits),
                                       "Sum": format_decimal(sum(debits), ccy)}
    start = datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=zone)
    end = datetime.datetime.combine(day, datetime.time(23, 59, 59), tzinfo=zone)
    statement = {
        "Id": msg_id, "ElctrncSeqNb": number, "LglSeqNb": number,
        "CreDtTm": created_at, "FrToDt": {"FrDtTm": start, "ToDtTm": end},
        "Acct": {"Id": {"IBAN": account["iban"]}, "Ccy": ccy,
                 "Ownr": {"Nm": account["name"][:140]},
                 "Svcr": {"FinInstnId": {"BICFI": BANK_BIC}}},
        "Bal": [_balance("OPBD", opening, ccy, day), _balance("CLBD", closing, ccy, day)],
        "TxsSummry": summary,
        "Ntry": entries}
    return schema.serialize(CAMT053, {"BkToCstmrStmt": {
        "GrpHdr": {"MsgId": msg_id, "CreDtTm": created_at},
        "Stmt": [statement]}})


def format_decimal(minor, ccy) -> str:
    """Minor units as a DecimalNumber string, for sums that carry no Ccy."""
    return schema.format_amount(minor, ccy)


PACS004 = schema.MESSAGES["pacs.004.001.09"]


def write_pacs004(account, payments, day, msg_id, created_at) -> bytes:
    """The return of one or more payments from one original file:
    ``pacs.004.001.09``.

    ``payments`` are the returned rows, with the original file's ``msg_id``
    and ``message`` joined in; they all came from that one file, so
    ``OrgnlGrpInf`` can name it. Each ``TxInf`` carries the payment's own
    ``OrgnlEndToEndId``, what settled and when, what comes back and when, and
    ``RtrRsnInf`` with the reason. ``SttlmMtd`` is ``INDA``: the bank settles
    the return on its own books (see ``schema.CHOICES``).
    """
    ccy = account["currency"]
    first = payments[0]
    total = sum(p["amount"] for p in payments)
    party = {"Pty": {"Nm": account["name"][:140]}}
    transactions = []
    for p in payments:
        amount = schema.Amount(p["amount"], p["currency"])
        tx = {"RtrId": "MB-RTR-%d" % p["id"],
              "OrgnlEndToEndId": p["end_to_end_id"],
              "OrgnlIntrBkSttlmAmt": amount,
              "OrgnlIntrBkSttlmDt": datetime.date.fromisoformat(p["settlement_date"]),
              "RtrdIntrBkSttlmAmt": amount, "IntrBkSttlmDt": day,
              "RtrRsnInf": [{"Rsn": {"Cd": p["return_reason"]}}]}
        if p["instruction_id"]:
            tx["OrgnlInstrId"] = p["instruction_id"]
        original = {"Amt": {"InstdAmt": amount}, "Dbtr": party,
                    "DbtrAcct": {"Id": {"IBAN": account["iban"]}}}
        if p["creditor_name"]:
            original["Cdtr"] = {"Pty": {"Nm": p["creditor_name"][:140]}}
        if p["creditor_iban"]:
            original["CdtrAcct"] = {"Id": (
                {"IBAN": p["creditor_iban"]} if schema.iban_is_valid(p["creditor_iban"])
                else {"Othr": {"Id": p["creditor_iban"]}})}
        tx["OrgnlTxRef"] = original
        transactions.append(tx)
    return schema.serialize(PACS004, {"PmtRtr": {
        "GrpHdr": {"MsgId": msg_id, "CreDtTm": created_at, "NbOfTxs": len(payments),
                   "CtrlSum": format_decimal(total, ccy),
                   "TtlRtrdIntrBkSttlmAmt": schema.Amount(total, ccy),
                   "IntrBkSttlmDt": day, "SttlmInf": {"SttlmMtd": "INDA"}},
        "OrgnlGrpInf": {"OrgnlMsgId": first["msg_id"], "OrgnlMsgNmId": first["message"]},
        "TxInf": transactions}})
