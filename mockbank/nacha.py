"""NACHA: the US ACH file, read into the same model as a ``pain.001``.

A NACHA file is fixed-width text, 94 characters a line: a file header (1),
then per batch a batch header (5), entry details (6) each with optional addenda
(7), and a batch control (8); then a file control (9), and lines of nines to
fill the last block of ten. ``RECORDS`` declares every record type by field
position and width, in the same "declare, do not hand-write" spirit as
``schema.py``, and the reader is derived from it: nothing here slices a line at
a number of its own.

``inspect`` returns the same ``PaymentFile`` a ``pain.001`` reads into, so the
decision engine, booking and the clock need no change to take one (#53). The
mapping, and the choices it makes where NACHA has no equivalent:

==========================  ==============================================
model                       NACHA
==========================  ==============================================
``PaymentFile.msg_id``      immediate origin, creation date and time, and
                            file ID modifier: what an ODFI tells two files
                            apart by, since NACHA has no message id
``Batch.pmt_inf_id``        company identification and batch number,
                            as ``1234567890-1``
``Batch.debtor_account``    **company identification.** NACHA carries no
                            originator account; the ODFI knows the
                            originator by this, so it stands in for one
``Batch.requested_...``     effective entry date
``Payment.end_to_end_id``   **individual identification number**, the
                            originator's own reference for the payment (an
                            invoice number, say), which the receiver sees.
                            ``NOTPROVIDED`` when blank, as ISO 20022 writes
                            a reference the sender did not give. The trace
                            number is the ODFI's, so it is not this
``Payment.instruction_id``  trace number
``Payment.creditor_...``    DFI account number and individual name, and
                            the receiving DFI's routing number as
                            ``creditor_clearing_id``
``Payment.remittance``      each addenda's payment related information
==========================  ==============================================

Amounts are cents, in USD. Only credit entries are read as payments; a debit,
a prenote or a return is a finding, because the mock is a bank that sends
money, not one that collects it.

Findings, not exceptions, as in ``validate``: a line that is not 94
characters, a routing number that fails its check digit, an entry hash, a
count or a total that disagrees with the entries, and a wrong block count.
Each names the line, the record and the field with its columns.
"""
from __future__ import annotations

import datetime
from collections import namedtuple
from decimal import Decimal
from typing import Dict, List, Optional

from . import messages, schema

Finding = schema.Finding

NAME = "NACHA"
LINE = 94
BLOCK = 10
NOT_PROVIDED = messages.NOT_PROVIDED

Field = namedtuple("Field", "name start width numeric optional")


def _fields(*spec):
    """Fields from (name, width, kind), laid end to end from column 1."""
    out, start = [], 1
    for name, width, kind in spec:
        out.append(Field(name, start, width, kind in (N, OPTIONAL_N), kind == OPTIONAL_N))
        start += width
    assert start == LINE + 1, "a record declares %d columns" % (start - 1)
    return tuple(out)


# Numeric, alphanumeric, and numeric but allowed to be blank - which NACHA
# says of a few fields, and a real file leaves them blank.
N, A, OPTIONAL_N = "N", "A", "N?"

# Every record type, by field position and width (NACHA Operating Rules,
# Appendix Three). Numeric fields are digits, right-justified and zero-filled;
# alphanumeric ones are left-justified and space-filled.
RECORDS = {
    "1": ("file header", _fields(
        ("record type code", 1, N), ("priority code", 2, N),
        ("immediate destination", 10, A), ("immediate origin", 10, A),
        ("file creation date", 6, N), ("file creation time", 4, OPTIONAL_N),
        ("file ID modifier", 1, A), ("record size", 3, N),
        ("blocking factor", 2, N), ("format code", 1, N),
        ("immediate destination name", 23, A), ("immediate origin name", 23, A),
        ("reference code", 8, A))),
    "5": ("batch header", _fields(
        ("record type code", 1, N), ("service class code", 3, N),
        ("company name", 16, A), ("company discretionary data", 20, A),
        ("company identification", 10, A), ("standard entry class code", 3, A),
        ("company entry description", 10, A), ("company descriptive date", 6, A),
        ("effective entry date", 6, N), ("settlement date", 3, A),
        ("originator status code", 1, A),
        ("originating DFI identification", 8, N), ("batch number", 7, N))),
    "6": ("entry detail", _fields(
        ("record type code", 1, N), ("transaction code", 2, N),
        ("receiving DFI identification", 8, N), ("check digit", 1, N),
        ("DFI account number", 17, A), ("amount", 10, N),
        ("individual identification number", 15, A), ("individual name", 22, A),
        ("discretionary data", 2, A), ("addenda record indicator", 1, N),
        ("trace number", 15, N))),
    "7": ("addenda", _fields(
        ("record type code", 1, N), ("addenda type code", 2, N),
        ("payment related information", 80, A),
        ("addenda sequence number", 4, N),
        ("entry detail sequence number", 7, N))),
    "8": ("batch control", _fields(
        ("record type code", 1, N), ("service class code", 3, N),
        ("entry/addenda count", 6, N), ("entry hash", 10, N),
        ("total debit entry dollar amount", 12, N),
        ("total credit entry dollar amount", 12, N),
        ("company identification", 10, A),
        ("message authentication code", 19, A), ("reserved", 6, A),
        ("originating DFI identification", 8, N), ("batch number", 7, N))),
    "9": ("file control", _fields(
        ("record type code", 1, N), ("batch count", 6, N),
        ("block count", 6, N), ("entry/addenda count", 8, N),
        ("entry hash", 10, N), ("total debit entry dollar amount", 12, N),
        ("total credit entry dollar amount", 12, N), ("reserved", 39, A))),
}

# Transaction codes that move money to the receiver: checking, savings,
# general ledger and loan credits. Everything else is a debit, a prenote or a
# return, which this bank does not originate.
CREDITS = {"22", "32", "42", "52"}

PADDING = "9" * LINE


def recognise(data: bytes) -> bool:
    """Whether a body is a NACHA file: its first line is a file header."""
    return data[:3] == b"101"


def check_digit(routing8: str) -> int:
    """The ABA check digit for the first eight digits of a routing number."""
    weights = (3, 7, 1) * 3
    return (10 - sum(int(d) * w for d, w in zip(routing8, weights)) % 10) % 10


class Record:
    """One line, read by its declaration: its fields as text, and where it is."""

    def __init__(self, number: int, line: str):
        self.number = number
        self.type = line[:1]
        self.label, fields = RECORDS[self.type]
        self.fields = fields
        self.values = {f.name: line[f.start - 1:f.start - 1 + f.width] for f in fields}
        self.path = "/line %d (%s)" % (number, self.label)

    def text(self, name: str) -> str:
        return self.values[name].strip()

    def number_of(self, name: str) -> Optional[int]:
        value = self.values[name].strip()
        return int(value) if value.isdigit() else None

    def field_path(self, name: str) -> str:
        field = next(f for f in self.fields if f.name == name)
        return "%s/%s (columns %d-%d)" % (self.path, name, field.start,
                                          field.start + field.width - 1)


# -- the model ---------------------------------------------------------------
# Subclasses of the pain.001 model, filled from records instead of a tree, so
# that everything downstream of the reader takes either without knowing.

class Payment(messages.Payment):
    def __init__(self, entry: Record, addenda: List[Record]):
        self.node = None
        self._path = entry.path
        self.end_to_end_id = entry.text("individual identification number") or NOT_PROVIDED
        self.instruction_id = entry.text("trace number")
        self.equivalent = False
        self.amount = entry.number_of("amount")
        self.currency = "USD"
        self.creditor_name = entry.text("individual name")
        self.creditor_account = entry.text("DFI account number")
        self.creditor_bic = None
        self.creditor_clearing_id = (entry.text("receiving DFI identification")
                                     + entry.text("check digit"))
        self.remittance = [a.text("payment related information") for a in addenda]
        self.remittance_references = []

    @property
    def path(self):
        return self._path


class Batch(messages.Batch):
    def __init__(self, header: Record, payments: List[Payment]):
        self.node = None
        self._path = header.path
        company = header.text("company identification")
        number = header.number_of("batch number")
        self.pmt_inf_id = "%s-%s" % (company, number if number is not None
                                     else header.text("batch number"))
        self.requested_execution_date = _date(header.text("effective entry date"))
        self.debtor_name = header.text("company name")
        self.debtor_account = company
        self.debtor_account_currency = "USD"
        self.debtor_bic = None
        self.payments = payments
        self.nb_of_txs = len(payments)
        self.ctrl_sum = _dollars(p.amount for p in payments)

    @property
    def path(self):
        return self._path


class PaymentFile(messages.PaymentFile):
    def __init__(self, header: Record, batches: List[Batch]):
        self.message = NAME
        self.node = self.header = None
        self.msg_id = "%s-%s%s%s" % (
            header.text("immediate origin"), header.text("file creation date"),
            header.text("file creation time"), header.text("file ID modifier"))
        created = _date(header.text("file creation date"))
        time = header.text("file creation time")
        self.creation_time = ("%sT%s:%s:00" % (created.isoformat(), time[:2], time[2:])
                              if created and len(time) == 4 and time.isdigit() else None)
        self.initiating_party = header.text("immediate origin name")
        self.batches = batches
        self.nb_of_txs = sum(len(b.payments) for b in batches)
        self.ctrl_sum = _dollars(p.amount for p in self.payments)


def _date(yymmdd: str) -> Optional[datetime.date]:
    try:
        return datetime.datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        return None


def _dollars(amounts) -> Decimal:
    return Decimal(sum(a or 0 for a in amounts)).scaleb(-2)


# -- reading -----------------------------------------------------------------

def inspect(data: bytes, today: Optional[datetime.date] = None):
    """``(PaymentFile or None, [Finding])`` for a NACHA file; never raises.

    ``today`` is the bank's, for the one warning that needs it: an effective
    entry date in the past (``DT01``), executed on the next business day as a
    ``pain.001``'s past requested date is.
    """
    findings: List[Finding] = []
    records: List[Record] = []
    padding = 0
    text = data.decode("ascii", "replace")
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.rstrip("\r")
        if not line:
            continue
        if len(line) != LINE:
            label = RECORDS[line[:1]][0] if line[:1] in RECORDS else "record"
            findings.append(_finding("/line %d (%s)" % (number, label), "FF01",
                                     "the %s is %d characters; every NACHA record is %d"
                                     % (label, len(line), LINE)))
            line = line[:LINE].ljust(LINE)
        if line == PADDING and records and records[-1].type == "9":
            padding += 1
            continue
        if line[:1] not in RECORDS:
            findings.append(_finding("/line %d" % number, "FF01",
                                     "record type code %r is not one NACHA defines (%s)"
                                     % (line[:1], ", ".join(sorted(RECORDS)))))
            continue
        records.append(Record(number, line))

    for record in records:
        findings += _numeric(record)

    if not records or records[0].type != "1":
        return None, findings + [_finding("/line 1", "FF01",
                                          "a NACHA file starts with a file header (1)")]
    header, batches, control = records[0], [], None
    index = 1
    entry_hash, credits, count = 0, 0, 0
    while index < len(records):
        record = records[index]
        if record.type == "5":
            batch, index, sums = _batch(records, index, findings)
            batches.append(batch)
            entry_hash += sums[0]
            credits += sums[1]
            count += sums[2]
            continue
        if record.type == "9":
            control = record
            index += 1
            break
        findings.append(_finding(record.path, "FF01",
                                 "a %s outside a batch" % record.label))
        index += 1
    for record in records[index:]:
        findings.append(_finding(record.path, "FF01",
                                 "a %s after the file control" % record.label))

    if control is None:
        findings.append(_finding("/", "FF01", "the file has no file control (9)"))
    else:
        blocks = -(-len(records) // BLOCK)
        findings += _compare(control, "batch count", len(batches), "AM18",
                             "the file holds %d")
        findings += _compare(control, "block count", blocks, "FF01",
                             "its %d records fill %%d block(s) of %d" % (len(records), BLOCK))
        findings += _compare(control, "entry/addenda count", count, "AM18",
                             "the file holds %d entries and addenda")
        findings += _compare(control, "entry hash", entry_hash % 10 ** 10, "FF01",
                             "the receiving DFI identifications of its entries sum to "
                             "%d, to ten digits")
        findings += _compare(control, "total credit entry dollar amount", credits, "AM10",
                             "its credit entries sum to %d cents")
    payment_file = PaymentFile(header, batches)
    findings += _as_a_pain001_would(payment_file, today)
    return payment_file, findings


def _as_a_pain001_would(payment_file, today) -> List[Finding]:
    """The two checks a ``pain.001`` gets that NACHA has no rule of its own for.

    A past effective date is a ``DT01`` warning, not a rejection. A repeated
    individual identification number is ``AM05``, because it is the
    ``EndToEndId`` and a status for one would be a status for both. A blank one
    is ``NOTPROVIDED`` and is not compared: many entries may leave it blank.
    """
    out, seen = [], {}
    for batch in payment_file.batches:
        when = batch.requested_execution_date
        if today is not None and when is not None and when < today:
            out.append(Finding(
                "warning", batch.path + "/effective entry date (columns 70-75)", "DT01",
                "the effective entry date %s is in the past; the bank executes on "
                "the next business day instead" % when.isoformat()))
        for payment in batch.payments:
            reference = payment.end_to_end_id
            if reference == NOT_PROVIDED:
                continue
            if reference in seen:
                out.append(_finding(
                    payment.path + "/individual identification number (columns 40-54)",
                    "AM05", "%s already appears at %s" % (reference, seen[reference])))
            else:
                seen[reference] = payment.path
    return out


def _batch(records, index, findings):
    """One batch from its header at ``index``: the Batch, where the next record
    is, and (entry hash, credit total, entries and addenda) as computed."""
    header = records[index]
    index += 1
    payments, entry_hash, credits, count = [], 0, 0, 0
    control = None
    while index < len(records):
        record = records[index]
        if record.type == "6":
            addenda = []
            index += 1
            while index < len(records) and records[index].type == "7":
                addenda.append(records[index])
                index += 1
            count += 1 + len(addenda)
            routing = record.values["receiving DFI identification"]
            if routing.isdigit():
                entry_hash += int(routing)
                digit = record.number_of("check digit")
                if digit is not None and digit != check_digit(routing):
                    findings.append(_finding(
                        record.field_path("check digit"), "RC01",
                        "routing number %s%d fails its check digit; it would be %d"
                        % (routing, digit, check_digit(routing))))
            code = record.text("transaction code")
            if code in CREDITS:
                payment = Payment(record, addenda)
                payments.append(payment)
                credits += payment.amount or 0
            else:
                findings.append(_finding(
                    record.field_path("transaction code"), "FF01",
                    "transaction code %s is not a credit; the mock reads credit "
                    "entries (%s)" % (code, ", ".join(sorted(CREDITS)))))
            continue
        if record.type == "8":
            control = record
            index += 1
            break
        if record.type in ("5", "9"):
            break
        findings.append(_finding(record.path, "FF01",
                                 "a %s where an entry detail belongs" % record.label))
        index += 1
    if control is None:
        findings.append(_finding(header.path, "FF01", "the batch has no batch control (8)"))
    else:
        findings += _compare(control, "entry/addenda count", count, "AM18",
                             "the batch holds %d entries and addenda")
        findings += _compare(control, "entry hash", entry_hash % 10 ** 10, "FF01",
                             "the batch's receiving DFI identifications sum to %d, "
                             "to ten digits")
        findings += _compare(control, "total credit entry dollar amount", credits, "AM10",
                             "the batch's credit entries sum to %d cents")
    return Batch(header, payments), index, (entry_hash, credits, count)


def _compare(record: Record, name: str, actual: int, code: str, why: str) -> List[Finding]:
    """A control field against what the records add up to; ``why`` has a %d."""
    declared = record.number_of(name)
    if declared is None or declared == actual:
        return []        # not a number at all is already a finding
    return [_finding(record.field_path(name), code,
                     "the %s says %d, but %s" % (name, declared, why % actual))]


def _numeric(record: Record) -> List[Finding]:
    """A numeric field holding anything but digits, named."""
    out = []
    for field in record.fields:
        value = record.values[field.name]
        if field.optional and not value.strip():
            continue
        if field.numeric and not value.isdigit():
            out.append(_finding(record.field_path(field.name), "FF01",
                                "%r is not a number; the field is %d digits, zero-filled"
                                % (value, field.width)))
    return out


def _finding(path: str, code: str, text: str) -> Finding:
    return Finding("error", path, code, text)


# -- writing: the acknowledgement ------------------------------------------------

ACK = "nacha.ack"


def acknowledgement(decision, ack_id: str, created_at, source: str = "") -> str:
    """What a NACHA account is sent where another gets a ``pain.002`` (#53).

    Real ODFIs acknowledge a file in shapes that differ bank to bank - a
    report, a return file, a line in an online banking screen - and NACHA
    specifies none. This is the mock's own, and plain on purpose: one fact to
    a line, each line starting with what it is, so a client reads it with
    ``split()`` and nothing else. An entry line carries the trace number, the
    identification number, the amount in dollars and what became of it. A
    rejection carries the reason code the bank decided, the same code a
    ``pain.002`` would, until #54 answers those as NACHA returns.
    """
    lines = ["ACKNOWLEDGEMENT %s" % ack_id,
             "CREATED %s" % created_at.isoformat(),
             "FILE %s" % (decision.msg_id or NOT_PROVIDED)]
    if source:
        lines.append("NAME %s" % source)
    status = "STATUS %s" % decision.status
    if decision.rejected_outright:
        status += " %s %s" % (decision.reason, " ".join((decision.reason_text or "").split()))
    lines.append(status.rstrip())
    for d in decision.payments:
        amount = d.payment.amount or 0
        head = "ENTRY %s %s %d.%02d" % (d.payment.instruction_id or NOT_PROVIDED,
                                        d.payment.end_to_end_id or NOT_PROVIDED,
                                        amount // 100, amount % 100)
        if d.outcome == "accepted":
            lines.append("%s ACCEPTED %s" % (head, d.settlement_date.isoformat()))
        else:
            lines.append("%s REJECTED %s %s" % (head, d.reason,
                                                " ".join((d.reason_text or "").split())))
    return "\n".join(lines) + "\n"
