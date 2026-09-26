"""Reading and writing the messages, on top of the dictionary in ``schema``.

This is the reader half: ``read_pain001`` turns a payment file into a
``PaymentFile`` of ``Batch`` es of ``Payment`` s. Both ``pain.001.001.09`` and
``.001.03`` read into the same model, because ``schema.read`` already keys
the two versions alike; the one shape they still differ in, the requested
execution date (a date in ``.03``, a date-or-datetime choice in ``.09``), is
evened out here.

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
