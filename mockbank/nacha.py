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

Amounts are cents, in USD. A credit entry is a payment. A **debit entry is a
collection** (#176): the company is the creditor and the receiver the debtor,
read into the model a ``pain.008`` reads into (``Collection``,
``CollectionBatch``, ``CollectionFile`` below). A file is one or the other,
never both, because the pipeline takes a file of payments or a file of
collections. A prenote, a zero-dollar entry or a loan debit is a finding.

A NACHA file carries no mandate: the receiver's authorization is held by the
originator, outside the file. So a collection read from one has no mandate id
and no date of signature, and is not held to ``MD02``. The one thing the file
does say is whether a ``WEB`` or ``TEL`` debit is recurring or single (the
payment type code), which is its sequence type.

Findings, not exceptions, as in ``validate``: a line that is not 94
characters, a byte that is not one NACHA allows, a routing number that fails
its check digit, an entry hash, a count or a total that disagrees with the
entries, a wrong block count, and lines of nines that do not fill the last
block. Each names the line, the record and the field with its columns.

A record is 94 bytes as well as 94 characters (#162): the characters NACHA
allows are ASCII's printable ones, space to tilde, one byte each. The reader
says where a byte is anything else, and the writer never writes one (``line``).
"""
from __future__ import annotations

import datetime
import unicodedata
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

# An addenda record is laid out by its type: 05 carries payment related
# information (declared above), 99 is a return's (#54).
RETURN_ADDENDA = ("return addenda", _fields(
    ("record type code", 1, N), ("addenda type code", 2, N),
    ("return reason code", 3, A), ("original entry trace number", 15, N),
    ("date of death", 6, A), ("original receiving DFI identification", 8, N),
    ("addenda information", 44, A), ("trace number", 15, N)))

# Transaction codes that move money to the receiver: checking, savings,
# general ledger and loan credits. Everything else is a debit, a prenote or a
# return, which this bank does not originate.
CREDITS = {"22", "32", "42", "52"}

# ...and those that take it from the receiver: a collection (#176). Checking,
# savings and general ledger. Not 55, the loan debit: moov-io/ach marks it for
# reversals only, and Nicolet National Bank's specification names 27 and 37.
DEBITS = {"27", "37", "47"}

# What the mock does not read, by the code's second digit, and why - so that a
# prenote does not read as a malformed debit.
NOT_READ = {
    "3": "a prenote of a credit: a zero-amount test entry, and no money would move",
    "8": "a prenote of a debit: a zero-amount test entry, and no money would move",
    "4": "a zero-dollar entry carrying remittance data, and no money would move",
    "9": "a zero-dollar entry carrying remittance data, and no money would move",
    "5": "a loan debit, which NACHA allows for reversals only",
}

# WEB and TEL entries carry a payment type code where the others carry
# discretionary data: R is recurring, anything else single (moov-io/ach,
# `SetPaymentType`). The nearest thing in the file to a mandate's sequence type.
PAYMENT_TYPE_CLASSES = ("WEB", "TEL")

# Each credit's automated return, which is what a return file carries (#54):
# a returned checking credit is 21, a savings one 31, and so on.
RETURN_OF = {"22": "21", "32": "31", "42": "41", "52": "51",
             # ...and each debit's (#176): a returned checking debit is 26.
             "27": "26", "37": "36", "47": "46"}
# Returns the reader takes: of credits, which this bank writes, and of debits
# (26, 36, 46), which it never sends but which a return file from anywhere
# else may carry - moov-io/ach's `return-WEB.ach` has one of each (#55).
# ... and 56, a returned loan debit: 26, 36, 46 and 56 are the automated returns
# of debits to checking, savings, the general ledger and a loan, exactly as 21,
# 31, 41 and 51 are of the credits. Three of the four were here, which was an
# asymmetry rather than a decision (#102).
RETURNS = set(RETURN_OF.values()) | {"26", "36", "46", "56"}


# The second digits that mean a credit. A tuple, not the string "1234": `in` on
# a string is a substring test, so `"" in "1234"` is True and a blank or
# one-character code counted as a credit - silently, and on the side the mock's
# own writer agreed with, which is the side where an error hides (#102).
CREDIT_DIGITS = ("1", "2", "3", "4")


def side(code: str) -> str:
    """Which control total an entry counts in: by its code's second digit,
    1 to 4 a credit and 6 to 9 a debit - returns included. A return of a debit
    (26) is a debit, however the money moves; moov-io/ach's file, which the
    reader is held to, counts it that way (#55).

    A code with no second digit is a debit here, which is not because a blank is
    a debit - it is neither - but because the credit total is the one the bank's
    own writer computes the same way, so anything unclassifiable landing there
    is the case that cannot be caught. Such a code only reaches this from a
    malformed line, which is already a finding of its own."""
    return "credit" if code[1:2] in CREDIT_DIGITS else "debit"

# The return codes the bank answers with, from what it decided (#54): the
# same behaviours a pain.002 answers with ISO 20022 reasons.
RETURN_FOR = {"AM04": "R01",    # insufficient funds
              "AC04": "R02",    # account closed
              "RC01": "R03"}    # no account, unable to locate
RETURN_REASONS = {"R01": "insufficient funds", "R02": "account closed",
                  "R03": "no account, or unable to locate the account"}
# What a debit comes back with (#176): those three, and the ones that are about
# the authorization a collection is made under. From moov-io/ach's table
# (`addenda99.go` @ 7ee7ad0); a credit is not returned for any of these five.
DEBIT_RETURN_REASONS = dict(RETURN_REASONS, **{
    "R05": "improper debit to a consumer account: a corporate debit the receiver "
           "did not authorize",
    "R07": "authorization revoked by the customer",
    "R08": "payment stopped",
    "R10": "the customer advises the originator is not known or not authorized",
    "R29": "the corporate customer advises the debit is not authorized"})

PADDING = "9" * LINE

# The characters a record is made of: ASCII's printable ones, space to tilde.
# Nacha's table of valid characters gives "ASCII values greater than
# hexadecimal 1F", and ASCII ends at 7F, which is a control character too.
FIRST, LAST = 0x20, 0x7E
# What stands in for a character the bank cannot write, and for a byte the
# reader could not read: one character for one, so that no column moves.
UNWRITABLE = "?"


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
        self.label, fields = (RETURN_ADDENDA if line[:3] == "799" else RECORDS[self.type])
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
    def __init__(self, entry: Record, addenda: List[Record], entry_class: str = ""):
        self.node = None
        # What a return of this payment has to echo (#54).
        self.transaction_code = entry.text("transaction code")
        self.entry_class = entry_class
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


class Return:
    """One return entry and its addenda 99: a payment coming back, and why."""

    def __init__(self, entry: Record, addenda: Record):
        self.path = entry.path
        self.transaction_code = entry.text("transaction code")
        self.amount = entry.number_of("amount")
        self.account = entry.text("DFI account number")
        self.name = entry.text("individual name")
        self.end_to_end_id = entry.text("individual identification number") or NOT_PROVIDED
        self.trace = entry.text("trace number")
        self.reason = addenda.text("return reason code")
        self.original_trace = addenda.text("original entry trace number")
        self.original_receiving_dfi = addenda.text("original receiving DFI identification")

    def to_json(self):
        return {"end_to_end_id": self.end_to_end_id, "amount": self.amount,
                "account": self.account, "reason": self.reason,
                "original_trace": self.original_trace,
                "original_receiving_dfi": self.original_receiving_dfi}


class Collection(messages.Collection):
    """A debit entry: money the company collects from the receiver (#176)."""

    def __init__(self, entry: Record, addenda: List[Record], entry_class: str = ""):
        self.node = None
        # What a return of this collection has to echo.
        self.transaction_code = entry.text("transaction code")
        self.entry_class = entry_class
        self._path = entry.path
        self.end_to_end_id = entry.text("individual identification number") or NOT_PROVIDED
        self.instruction_id = entry.text("trace number")
        self.amount = entry.number_of("amount")
        self.currency = "USD"
        self.debtor_name = entry.text("individual name")
        self.debtor_account = entry.text("DFI account number")
        self.debtor_bic = None
        self.debtor_clearing_id = (entry.text("receiving DFI identification")
                                   + entry.text("check digit"))
        # No mandate in the file: see the module.
        self.mandate_id = self.mandate_signed = self.creditor_scheme_id = None
        self.sequence_type = None
        if entry_class in PAYMENT_TYPE_CLASSES:
            recurring = entry.text("discretionary data").strip().upper() == "R"
            self.sequence_type = "RCUR" if recurring else "OOFF"
        self.remittance = [a.text("payment related information") for a in addenda]

    @property
    def path(self):
        return self._path


class CollectionBatch(messages.CollectionBatch):
    def __init__(self, header: Record, collections: List[Collection]):
        self.node = None
        self.returns: List[Return] = []
        self._path = header.path
        company = header.text("company identification")
        number = header.number_of("batch number")
        self.pmt_inf_id = "%s-%s" % (company, number if number is not None
                                     else header.text("batch number"))
        self.requested_collection_date = _date(header.text("effective entry date"))
        self.creditor_name = header.text("company name")
        self.creditor_account = company
        self.creditor_account_currency = "USD"
        self.creditor_bic = None
        self.collections = collections
        self.nb_of_txs = len(collections)
        self.ctrl_sum = _dollars(c.amount for c in collections)

    # What `_as_a_pain001_would` reads a batch by, whichever way the money moves.
    payments = property(lambda self: self.collections)
    requested_execution_date = property(lambda self: self.requested_collection_date)

    @property
    def path(self):
        return self._path


class CollectionFile(messages.CollectionFile):
    def __init__(self, header: Record, batches: List[CollectionBatch]):
        PaymentFile.__init__(self, header, batches)


class Batch(messages.Batch):
    def __init__(self, header: Record, payments: List[Payment],
                 returns: Optional[List[Return]] = None):
        self.node = None
        self.returns = returns or []
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
        # A return file carries returns, not payments (#54).
        self.returns = [r for b in batches for r in b.returns]
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
    """``(PaymentFile, CollectionFile or None, [Finding])`` for a NACHA file;
    never raises. A file whose entries are debits is a ``CollectionFile``
    (#176), whatever its service class says: the entry's own code decides.

    ``today`` is the bank's, for the one warning that needs it: an effective
    entry date in the past (``DT01``), executed on the next business day as a
    ``pain.001``'s past requested date is.
    """
    findings: List[Finding] = []
    records: List[Record] = []
    padding = lines = 0
    for number, raw in enumerate(data.split(b"\n"), start=1):
        raw = raw.rstrip(b"\r")
        if not raw:
            continue
        lines += 1
        line = "".join(chr(byte) if FIRST <= byte <= LAST else UNWRITABLE for byte in raw)
        findings += _characters(number, raw)
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
    entry_hash, count = 0, 0
    totals = {"credit": 0, "debit": 0}
    while index < len(records):
        record = records[index]
        if record.type == "5":
            batch, index, sums = _batch(records, index, findings)
            batches.append(batch)
            entry_hash += sums[0]
            for key in totals:
                totals[key] += sums[1][key]
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
        findings += _blocking(lines, lines - padding, padding)
        findings += _compare(control, "entry/addenda count", count, "AM18",
                             "the file holds %d entries and addenda")
        findings += _compare(control, "entry hash", entry_hash % 10 ** 10, "FF01",
                             "the receiving DFI identifications of its entries sum to "
                             "%d, to ten digits")
        findings += _compare(control, "total credit entry dollar amount",
                             totals["credit"], "AM10", "its credit entries sum to %d cents")
        findings += _compare(control, "total debit entry dollar amount",
                             totals["debit"], "AM10", "its debit entries sum to %d cents")
    collections = [c for b in batches for c in b.collecting.collections]
    payments = [p for b in batches for p in b.payments]
    if collections and payments:
        # A real ODFI takes a batch of both (service class 200). The pipeline
        # takes a file of payments or a file of collections, as a pain.001 and
        # a pain.008 are two files, so this one is refused and says why.
        findings.append(_finding(
            collections[0].path + "/transaction code (columns 2-3)", "FF01",
            "the file holds %d credit entr%s and %d debit entr%s; the mock takes a "
            "file of payments or a file of collections, not both. Send the debits "
            "in a file of their own"
            % (len(payments), "y" if len(payments) == 1 else "ies",
               len(collections), "y" if len(collections) == 1 else "ies")))
    if collections and not payments:
        payment_file = CollectionFile(header, [b.collecting for b in batches])
    else:
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
    is, and (entry hash, {"credit", "debit"} totals, entries and addenda) as
    computed."""
    header = records[index]
    index += 1
    entry_class = header.text("standard entry class code")
    payments, collections, returns, entry_hash, count = [], [], [], 0, 0
    totals = {"credit": 0, "debit": 0}
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
            # Every entry counts in its control total by its code, whatever the
            # mock makes of it: a debit it refuses to read is still a debit.
            totals[side(code)] += record.number_of("amount") or 0
            if code in CREDITS:
                payments.append(Payment(record, addenda, entry_class))
            elif code in DEBITS:
                collections.append(Collection(record, addenda, entry_class))
            elif code in RETURNS:
                why = [a for a in addenda if a.label == RETURN_ADDENDA[0]]
                if why:
                    returns.append(Return(record, why[0]))
                else:
                    findings.append(_finding(
                        record.field_path("transaction code"), "FF01",
                        "transaction code %s is a return, and a return carries an "
                        "addenda 99 saying why; this one has none" % code))
            else:
                findings.append(_finding(
                    record.field_path("transaction code"), "FF01",
                    "transaction code %s is %s; the mock reads credit entries (%s) "
                    "as payments and debit entries (%s) as collections"
                    % (code, NOT_READ.get(code[1:2], "not one NACHA defines for an entry"),
                       ", ".join(sorted(CREDITS)), ", ".join(sorted(DEBITS)))))
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
        findings += _compare(control, "total credit entry dollar amount",
                             totals["credit"], "AM10",
                             "the batch's credit entries sum to %d cents")
        findings += _compare(control, "total debit entry dollar amount",
                             totals["debit"], "AM10",
                             "the batch's debit entries sum to %d cents")
    batch = Batch(header, payments, returns)
    # Kept beside the payments until `inspect` knows which the file is of.
    batch.collecting = CollectionBatch(header, collections)
    return batch, index, (entry_hash, totals, count)


def _compare(record: Record, name: str, actual: int, code: str, why: str) -> List[Finding]:
    """A control field against what the records add up to; ``why`` has a %d."""
    declared = record.number_of(name)
    if declared is None or declared == actual:
        return []        # not a number at all is already a finding
    return [_finding(record.field_path(name), code,
                     "the %s says %d, but %s" % (name, declared, why % actual))]


def _characters(number: int, raw: bytes) -> List[Finding]:
    """The bytes of a line that are not characters NACHA allows, each by its
    column and the field it falls in: one finding for the line (#162)."""
    bad = [(column, byte) for column, byte in enumerate(raw, start=1)
           if not FIRST <= byte <= LAST]
    if not bad:
        return []
    kind = raw[:1].decode("ascii", "replace")
    label, fields = RECORDS.get(kind, ("record", ()))

    def where(column):
        name = next((f.name for f in fields if f.start <= column < f.start + f.width), None)
        return "column %d%s" % (column, " (%s)" % name if name else "")

    shown = ["byte 0x%02X at %s" % (byte, where(column)) for column, byte in bad[:5]]
    if len(bad) > len(shown):
        shown.append("%d more" % (len(bad) - len(shown)))
    return [_finding(
        "/line %d (%s)" % (number, label), "FF01",
        "%s: a NACHA record is ASCII, the characters from space (0x20) to tilde "
        "(0x7E) and one byte each. A letter with an accent is not one of them, "
        "in any encoding" % ", ".join(shown))]


def _blocking(lines: int, records: int, padding: int) -> List[Finding]:
    """The lines of nines after the file control against what fills the last
    block of ten (#162). An error, as a wrong block count is: it is the same
    rule, and a bank that enforces one enforces the other."""
    wanted = -records % BLOCK
    if padding == wanted:
        return []
    return [_finding(
        "/", "FF01",
        "the file is %d lines, %d record(s) and %d line(s) of nines; NACHA blocks "
        "records in tens, so %d record(s) are followed by %d line(s) of nines to "
        "make %d lines, and this file has %d"
        % (lines, records, padding, records, wanted, records + wanted, padding))]


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
    rejection carries the NACHA return code where the bank answers with one -
    ``R01``, ``R02``, ``R03``, and the entry comes back in a return file the
    next business day too (#54) - and otherwise the reason code a ``pain.002``
    would carry.
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
            # The return code where the bank answers with one (#54): the
            # entry comes back in a return file the next business day as well.
            lines.append("%s REJECTED %s %s" % (head, RETURN_FOR.get(d.reason, d.reason),
                                                " ".join((d.reason_text or "").split())))
    return "\n".join(lines) + "\n"


# -- writing: the return file -----------------------------------------------------

RETURN = "nacha.return"

# What the bank writes as text rather than XML, and the extension each gets in
# the pickup directory.
TEXT_TYPES = {ACK: "txt", RETURN: "ach"}


def writable(text: str) -> str:
    """``text`` in the characters a record is made of, one for one (#162).

    A name reaches the writer from an account or from a ``pain.001``, where
    ``Müller`` is a name like any other. A letter with an accent loses the
    accent, as a bank that keeps its ACH names in ASCII writes it; anything
    else that is not printable ASCII becomes ``?``. Never two characters for
    one (``ß`` is ``?``, not ``ss``), so a value keeps its width.
    """
    out = []
    for char in text:
        if not FIRST <= ord(char) <= LAST:
            plain = "".join(c for c in unicodedata.normalize("NFKD", char)
                            if not unicodedata.combining(c))
            char = plain if len(plain) == 1 and FIRST <= ord(plain) <= LAST else UNWRITABLE
        out.append(char)
    return "".join(out)


def line(layout, **values) -> str:
    """One record from its declaration: numbers zero-filled, text space-filled,
    and 94 bytes, because every character is one ``writable`` returns.

    ``layout`` is a ``RECORDS`` entry or ``RETURN_ADDENDA``; a value that does
    not fit its field is an error in the caller, not something to truncate.
    """
    out = []
    for field in layout[1]:
        value = "" if values.get(field.name) is None else writable(str(values[field.name]))
        value = value.rjust(field.width, "0") if field.numeric else value.ljust(field.width)
        if len(value) != field.width:
            raise ValueError("%s is %d characters, not %d: %r"
                             % (field.name, len(value), field.width, value))
        out.append(value)
    return "".join(out)


def return_file(routing: str, originator: dict, returns: List[dict], day, created_at,
                modifier: str = "A") -> str:
    """The return file a NACHA account is sent for payments that came back (#54).

    Two kinds of payment come back in it, not one: a payment that settled and
    then returned under `return-later`, and - the ordinary case - a payment the
    bank rejected for one of the three behaviours, which comes back the next
    business day as a return entry as well as being marked rejected in the
    acknowledgement. Both are written into one file per account per day.

    From the bank - ``routing``, its own - to ``originator``, the account that
    sent them (``account_number``, ``name``). Each of ``returns`` is a payment
    row with what the return has to echo - ``transaction_code``,
    ``entry_class``, ``instruction_id`` (the original trace), ``amount``,
    ``end_to_end_id``, ``creditor_name``, ``creditor_number`` and
    ``creditor_clearing_id`` - and its ``return_reason``, an ``R`` code. A
    row with ``collected`` set is a collection coming back (#176): the other
    party is its debtor (``debtor_name``, ``debtor_number``,
    ``debtor_clearing_id``), the entry is a returned debit (``26``, ``36``,
    ``46``), and it counts in the debit totals, as moov-io/ach's own return
    file counts one (#55).

    Everything a reader checks is computed here, never copied: the entry and
    addenda counts, the entry hash, the credit totals, the block count and the
    nines that pad the last block. The file reads back through ``inspect``
    with no finding, which is the test that holds it to that.
    """
    odfi = routing[:8]
    company = originator["account_number"][:10]
    records = [line(RECORDS["1"], **{
        "record type code": 1, "priority code": 1,
        "immediate destination": company.rjust(10), "immediate origin": " " + routing,
        "file creation date": created_at.strftime("%y%m%d"),
        "file creation time": created_at.strftime("%H%M"),
        "file ID modifier": modifier, "record size": LINE, "blocking factor": BLOCK,
        "format code": 1, "immediate destination name": originator["name"][:23],
        "immediate origin name": "MOCK BANK"})]
    by_class: Dict[str, List[dict]] = {}
    for row in returns:
        by_class.setdefault(row.get("entry_class") or "CCD", []).append(row)
    file_hash = file_count = sequence = 0
    file_totals = {"credit": 0, "debit": 0}
    for number, (entry_class, rows) in enumerate(sorted(by_class.items()), start=1):
        # Credits only, or debits only: one original file is of one kind.
        service = 225 if all(row.get("collected") for row in rows) else 220
        records.append(line(RECORDS["5"], **{
            "record type code": 5, "service class code": service,
            "company name": originator["name"][:16], "company identification": company,
            "standard entry class code": entry_class,
            "company entry description": "RETURN",
            "effective entry date": day.strftime("%y%m%d"),
            "originator status code": "1", "originating DFI identification": odfi,
            "batch number": number}))
        batch_hash = batch_count = 0
        batch_totals = {"credit": 0, "debit": 0}
        for row in rows:
            sequence += 1
            trace = "%s%07d" % (odfi, sequence)
            party = "debtor" if row.get("collected") else "creditor"
            code = RETURN_OF.get(row.get("transaction_code")
                                 or ("27" if row.get("collected") else "22"),
                                 "26" if row.get("collected") else "21")
            # A return goes back to the bank that sent the payment, so its
            # receiving DFI is this bank; the original receiving bank is named
            # in the addenda.
            records.append(line(RECORDS["6"], **{
                "record type code": 6,
                "transaction code": code,
                "receiving DFI identification": odfi, "check digit": routing[8],
                "DFI account number": (row.get(party + "_number") or "")[:17],
                "amount": row["amount"],
                "individual identification number": (row.get("end_to_end_id") or "")[:15],
                "individual name": (row.get(party + "_name") or "")[:22],
                "addenda record indicator": 1, "trace number": trace}))
            records.append(line(RETURN_ADDENDA, **{
                "record type code": 7, "addenda type code": 99,
                "return reason code": row["return_reason"],
                "original entry trace number": row.get("instruction_id") or "0",
                "original receiving DFI identification":
                    (row.get(party + "_clearing_id") or "0")[:8],
                "trace number": trace}))
            batch_hash += int(odfi)
            batch_totals[side(code)] += row["amount"]
            batch_count += 2
        records.append(line(RECORDS["8"], **{
            "record type code": 8, "service class code": service,
            "entry/addenda count": batch_count, "entry hash": batch_hash % 10 ** 10,
            "total debit entry dollar amount": batch_totals["debit"],
            "total credit entry dollar amount": batch_totals["credit"],
            "company identification": company,
            "originating DFI identification": odfi, "batch number": number}))
        file_hash += batch_hash
        for key in file_totals:
            file_totals[key] += batch_totals[key]
        file_count += batch_count
    records.append(line(RECORDS["9"], **{
        "record type code": 9, "batch count": len(by_class),
        "block count": -(-(len(records) + 1) // BLOCK),
        "entry/addenda count": file_count, "entry hash": file_hash % 10 ** 10,
        "total debit entry dollar amount": file_totals["debit"],
        "total credit entry dollar amount": file_totals["credit"]}))
    while len(records) % BLOCK:
        records.append(PADDING)
    return "\n".join(records) + "\n"
