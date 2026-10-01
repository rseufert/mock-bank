"""BAI2: the end-of-day statement in the format US treasury systems read.

A BAI2 file is comma-delimited text, one record to a line, terminated by ``/``:
a file header (01), then per group a group header (02), per account an account
identifier (03) with its transaction details (16) and an account trailer (49),
then a group trailer (98) and a file trailer (99). ``RECORDS`` declares every
record by field position, in the same "declare, do not hand-write" spirit as
``schema.py`` and ``nacha.py``, and both the writer and the reader are derived
from it: nothing here builds a line by joining strings of its own.

``write_statement`` takes the same arguments ``messages.write_camt053`` does,
because the two are two renderings of one statement rather than two statements.
``read`` parses a file back into records, and ``statements`` reduces those to
the three things a ``camt.053`` asserts - opening balance, entries, closing
balance - so a test can compare them without either side trusting the other's
arithmetic.

Amounts are integers in minor units throughout, as everywhere else in this
mock. BAI2 writes them that way too: the type code carries the sign sense, so a
debit is a positive amount under a debit code rather than a negative amount.
Balances are the exception and may be negative, which is how an overdrawn
account is reported.

Type codes
==========

==============  ==============================================================
``010``         opening ledger balance
``015``         closing ledger balance
``447``         the debit for a payment that left the account
``257``         the credit for a payment that came back
``142``         money arriving from somebody else, #91
``165``         a collection that settled, #131
==============  ==============================================================

Every code here is settled against a source outside the project (#127), and
each has a test in ``tests/test_bai2_type_codes.py`` that pastes the evidence
and would fail if the source changed. There are two sources, both from
moov-io/bai2 and vendored under ``tests/samples/external/``:

* ``bai2-type-codes.go``, moov's transcription of the specification's type
  code table ("Appendix A of Cash Management Balance Reporting Specifications
  Version 2"), which gives each code its direction and its description;
* ``bai2-sample3.txt`` and ``bai2-sample4.txt``, a bank's own exports, which
  show which code a bank writes for which movement.

``447``, *ACH Disbursement Funding Debit*: the code ``bai2-sample4.txt`` writes
for ``ACH Credit Payment``, an ACH credit the account holder sent, SEC ``CCD``
and ``CTX``, which is exactly what a NACHA account pays with.

``257``, *Individual ACH Return Item*, a credit. **The evidence for our case is
the table's description, not the sample.** The only ``257`` in the samples is
an ``ACH Debit Payment Return`` - a debit the account holder collected, coming
back - while ours is an ACH credit the account holder sent, coming back. The
description covers both, and it is the only individual-item return code on the
credit side (``168`` is a settlement total).

``142``, *ACH Credit Received*: the code ``bai2-sample3.txt`` writes for ACH
credits arriving (``PPD``). A NACHA account's rail is ACH, and this statement is
only written for NACHA accounts.

``165``, *Preauthorized ACH Credit*: the code ``bai2-sample4.txt`` writes for
``ACH Debit Collection``, the proceeds of a debit the account holder collected
(#131). The mock wrote it for a return until #127, which is the paragraph below;
it is back for the movement the sample shows it on.

**What they replaced, and why.** Until #127 these were ``495``, ``165`` and
``195``, marked as placeholders because no licensed code list existed when #57
looked. The same sources show each meant something else: ``bai2-sample4.txt``
writes ``495`` for an ``Outgoing Wire``, ``165`` for the proceeds of the
account holder's own ``ACH Debit Collection``, and ``195`` for an ``Incoming
Wire``. So ``165`` for a return was wrong, not merely unverified, and
``495``/``195`` described the wrong rail.

A reader that takes a movement's direction from the code's range - 100 to 399 a
credit, 400 to 699 a debit, as ``examples/payment_run.py`` does - reads the new
codes exactly as it read the old ones. A reader that matched on the old codes
does not.

``PLACEHOLDER_CODES`` stays, empty: a code that cannot be sourced goes back into
it, rather than being quietly left as if it were settled.

This module was written against the BAI2 record layout as it is commonly
reproduced, not against the published BAI specification, which the project does
not hold. Two readings were made explicitly and flagged as possibly wrong:

1. **Control totals sum signed amounts**, so a negative closing ledger
   subtracts from the account's total. ``CONTROL_TOTALS_ARE_SIGNED`` names it.
2. **Record counts include the trailer that carries them.** A ``49`` counts the
   ``03``, every ``16``, and itself. ``COUNTS_INCLUDE_THE_TRAILER`` names it.

**Both are settled, and both were right.** #57 vendored
``tests/samples/external/bai2-sample1.txt``, a file from moov-io/bai2, and
computed them from it: its first ``49`` states 14 and covers 14 records counting
itself, its ``98`` states 25 for the ``02`` through the ``98``, its ``99`` states
27 for a 27-line file, and every control total in it carries a sign. The flags
stay, because what they name is still a reading rather than something the project
can point at a specification for, and a test asserts each one so that changing a
reading breaks a test rather than a bank's parser.

The same file corrected three things this got wrong, all recorded where they
were wrong rather than only here: the ``02``'s two parties (see ``_parties``),
the notation for a positive amount (see ``_signed``), and the reason given for
keeping movement totals out of the ``03`` (see ``_account``).

What it could **not** settle is the transaction codes above - #127 did, from
two more of moov-io/bai2's files - and what it exposed is that ``read`` could
not parse a real BAI2 file at all - it did not declare the ``88`` continuation
record, and it assumed a funds type occupies one field where a value-dated one
occupies three. That is #114, and
``tests/test_bai2_external.py`` pins each way it fails so that none of them can
be fixed silently or quietly rot.
"""
from __future__ import annotations

import datetime
import re
from collections import namedtuple
from typing import Dict, List, Optional, Sequence, Tuple

NAME = "BAI2"
VERSION = 2

# The mailbox type and the pickup extension for a statement written this way.
# A NACHA-format account is sent this instead of a camt.053 (#57), numbered from
# the same counter, so a format change does not restart a statement's numbering.
STATEMENT = "bai2.statement"
TEXT_TYPES = {STATEMENT: "bai2"}
TERMINATOR = "/"
SEPARATOR = ","

# The two readings named in the docstring, as constants rather than as habits
# of the code, so that changing one is a change in one place.
CONTROL_TOTALS_ARE_SIGNED = True
COUNTS_INCLUDE_THE_TRAILER = True

# Status codes for the balances this mock reports, and transaction codes for the
# three movements it books. See the module docstring for the evidence for each.
OPENING_LEDGER = "010"
CLOSING_LEDGER = "015"

# Settled against moov-io/bai2's type code table and a bank's own exports
# (#127); the module docstring gives the evidence for each, and
# `tests/test_bai2_type_codes.py` holds each to it.
DEBIT = "447"             # ACH Disbursement Funding Debit: "ACH Credit Payment"
RETURNED_CREDIT = "257"   # Individual ACH Return Item
RECEIVED_CREDIT = "142"   # ACH Credit Received (#91)
COLLECTED_CREDIT = "165"  # Preauthorized ACH Credit: sample4's "ACH Debit Collection" (#131)
# Codes still unverified. Empty since #127; one that loses its source goes back.
PLACEHOLDER_CODES: Tuple[str, ...] = ()

# `Z` means the amount is immediately available; BAI2's other funds types say
# when it becomes so. Everything this mock books is already booked, so there is
# nothing to distribute over later dates.
AVAILABLE_NOW = "Z"

Field = namedtuple("Field", "name kind")

# An integer in minor units or a count; text; a date as yymmdd; a time as hhmm;
# SIGNED, an amount that carries an explicit `+` or `-`; FUNDS, a funds type
# whose *width* depends on its value; OPT, a trailing field a producer may simply
# leave off; and GROUP, the 03's repeating summary of (code, amount, item count,
# funds type).
#
# SIGNED is a kind rather than something the callers format, for the reason the
# comment in `_record` gives about `_safe`: the three control totals are written
# from three places, and doing it at the call sites is how the 01 and the 02 got
# missed last time.
#
# FUNDS and OPT exist because #114 found that a record's width is not a constant.
# REST is #128's: the last field of a record is not delimited at all. BAI2 has no
# escape character, so a field that could contain the separator can only be the
# last one, and it runs to the terminator - commas included. `sample4` writes a
# 16 whose text is
#
#     ACH Credit Payment,Entry Description: EXP; -, SEC: CCD, Client Ref ID: 1111
#
# as one field. Counting commas made that record four fields too wide.
#
# All three are read-side facts - this mock writes `Z`, fills every field, and
# replaces a comma in its text - so they change what `read` accepts and nothing
# about what the writer produces. #129 is the other half, where the writer stops
# replacing a comma it never needed to.
N, A, DATE, TIME, SIGNED, FUNDS, OPT, REST, GROUP = ("N", "A", "D", "T", "S",
                                                     "F", "O", "R", "G")

# A continuation. It has no fields of its own: it continues the comma-separated
# field stream of the record before it, and `_fold` joins the two before anything
# is parsed. That is why it is not in `RECORDS`.
#
# Established from moov-io/bai2's samples rather than reasoned about (#114):
# `88` follows an `03`, a `16` and another `88`; it chains, 68 deep in `sample4`;
# and a field group may split straight across the boundary, as in
#
#     03,0975312468,,010,500000,,,190,70000000,4,0,110/
#     88,70000000,15,D,3,0,20000000,1,30000000,3,20000000/
#
# where the type code `110` ends one record and its amount begins the next. A
# continuation that carried fields of its own could not do that.
CONTINUATION = "88"


def _fields(*spec) -> tuple:
    return tuple(Field(name, kind) for name, kind in spec)


RECORDS = {
    "01": ("file header", _fields(
        ("record code", A), ("sender identification", A),
        ("receiver identification", A), ("file creation date", DATE),
        ("file creation time", TIME), ("file identification number", N),
        ("physical record length", N), ("block size", N),
        ("version number", N))),
    "02": ("group header", _fields(
        ("record code", A), ("ultimate receiver identification", A),
        ("originator identification", A), ("group status", N),
        ("as-of date", DATE), ("as-of time", TIME), ("currency code", A),
        ("as-of date modifier", N))),
    "03": ("account identifier", _fields(
        ("record code", A), ("customer account number", A),
        ("currency code", A), ("summary", GROUP))),
    "16": ("transaction detail", _fields(
        ("record code", A), ("type code", A), ("amount", N),
        ("funds type", FUNDS), ("bank reference number", OPT),
        ("customer reference number", OPT), ("text", REST))),
    "49": ("account trailer", _fields(
        ("record code", A), ("account control total", SIGNED),
        ("number of records", N))),
    "98": ("group trailer", _fields(
        ("record code", A), ("group control total", SIGNED),
        ("number of accounts", N), ("number of records", N))),
    "99": ("file trailer", _fields(
        ("record code", A), ("file control total", SIGNED),
        ("number of groups", N), ("number of records", N))),
}


# How many fields a funds type occupies, counting the funds type itself. A
# record's width is a function of its contents, which is the second half of what
# #114 found - the first being continuations.
#
# Measured over moov-io/bai2's `sample1.txt` and `sample2.txt`: with these widths
# every 16 in both files ends with nought to three trailing fields and every 03's
# summary groups consume *exactly* all of their fields, including the D group
# split across an 88 boundary (19 of 19). Get either a width or the fold wrong
# and one of those overruns or falls short, which is why they are checked
# together in `tests/test_bai2_external.py`.
FUNDS_WIDTHS = {
    "V": 3,        # V, availability date, availability time
    "S": 4,        # S, and three availability amounts: now, one day, two or more
}
DISTRIBUTED = "D"  # D, a count, then that many (number of days, amount) pairs


def _funds_width(value: str, rest: Sequence[str]) -> int:
    """Fields the funds type at the head of `value + rest` occupies.

    Blank, `Z` and a single digit take one field and nothing follows: `Z` is
    available now, a digit is that many days away. Raises `Unreadable` for a `D`
    whose count is not a number, because the alternative is reading the rest of
    the record at an offset that happens to parse.
    """
    if value in FUNDS_WIDTHS:
        return FUNDS_WIDTHS[value]
    if value == DISTRIBUTED:
        if not rest or not rest[0].strip().isdigit():
            raise Unreadable(
                "a distributed funds type (D) is followed by how many "
                "(days, amount) pairs it carries; this one has %r"
                % (rest[0] if rest else ""))
        return 2 + 2 * int(rest[0])
    return 1


def _walk(code: str, values: Sequence[str]):
    """Consume `values` against the declaration of `code`.

    Returns (fields consumed, problem) - and a problem is a sentence, not a
    boolean, because "this record is the wrong shape" is not an answer anybody
    can act on.

    A record is no longer a fixed count. It is a prefix of fixed fields, then
    possibly a funds type whose width depends on its value, then trailing fields
    a producer may leave off, or a group that repeats until the fields run out.
    """
    name, fields = RECORDS[code]
    spec = fields[1:]
    used = 0
    for index, field in enumerate(spec):
        if field.kind is GROUP:
            return _walk_groups(code, values, used)
        if field.kind is REST:
            # The last field runs to the terminator, commas and all, so whatever
            # is left is one field however many separators it holds. That is what
            # makes a record with a comma in its text readable, and it is also
            # why this can never be short: there is nothing after it to be short
            # of.
            return len(values), ""
        if field.kind is OPT:
            # Trailing fields may simply be absent - sample2's
            # `16,115,450000,S,100000,200000,150000,,,/` fills them and
            # `16,165,123000000000,S,100000000000,20000000000,3000000000/` stops
            # at the availability amounts. Once one is missing the rest are.
            if used >= len(values):
                return used, ""
            used += 1
            continue
        if used >= len(values):
            return used, ("the %s (%s) needs a %s; the record ends first"
                          % (name, code, field.name))
        if field.kind is FUNDS:
            used += _funds_width(values[used], values[used + 1:])
            if used > len(values):
                return used, ("the %s (%s) states a funds type that runs past "
                              "the end of the record" % (name, code))
            continue
        used += 1
    if used != len(values):
        return used, ("the %s (%s) carries %d field(s) after the code; this one "
                      "has %d" % (name, code, used, len(values)))
    return used, ""


def _walk_groups(code: str, values: Sequence[str], used: int):
    """The 03's summary, which repeats until the fields run out.

    A group is (type code, amount, item count, funds type), and the funds type
    carries its own width, so the stride is four *or more*. The old reader
    checked `(len(values) - fixed) % 4`, which passed whenever a six-field V
    group happened to appear in pairs and then read every amount out of the wrong
    slot - a wrong answer rather than a refusal, which is the worse kind (#114).
    """
    name = RECORDS[code][0]
    while used < len(values):
        if not values[used].strip():
            # A trailing empty field: the record ended on a separator.
            used += 1
            continue
        if used + 3 >= len(values):
            return used, ("the %s (%s) ends part-way through a summary group, "
                          "which is a type code, an amount, an item count and a "
                          "funds type" % (name, code))
        used += 3 + _funds_width(values[used + 3], values[used + 4:])
        if used > len(values):
            return used, ("the %s (%s) has a summary group that runs past the "
                          "end of the record" % (name, code))
    return used, ""


def _field_count_problem(code: str, values) -> str:
    """Why this record does not fit its declaration, or ""."""
    return _walk(code, values)[1]


class Wrong(ValueError):
    """Base of the two below, so a caller may catch either."""


class Unwritable(Wrong):
    """This statement cannot be written as BAI2, and saying so beats guessing.

    Raised by the writer: a record that does not fit its declaration, an amount
    whose sign contradicts its type code, a credit that is not a return. Each is
    a bug in the caller rather than a fact about somebody else's file, so it
    stops here instead of producing a file two readers would disagree about.

    Separate from `Unreadable` because an earlier version raised that from the
    writer, which reads as though something had been parsed.
    """


class Unreadable(Wrong):
    """A file that is not BAI2 at all, named rather than guessed at.

    A findings list is what `validate` produces for a payment file the bank was
    sent. This is the other direction - a file this mock wrote, read back - so a
    problem here is a bug in the writer or the reader, not a fact about somebody
    else's file, and it should stop rather than be collected.
    """


def _yymmdd(day: datetime.date) -> str:
    return day.strftime("%y%m%d")


def _hhmm(at: datetime.datetime) -> str:
    return at.strftime("%H%M")


def _signed(minor: int) -> str:
    """An amount that states its sign: a balance, or a control total.

    `moov-io/bai2`'s `sample1.txt` writes every one of these with an explicit
    sign - `49,+00000000000834000,14/`, and `+000000000000` for a zero balance in
    the 03 - and writes movement amounts on the 16 with no sign at all. That is
    the same line this module already draws between `_amount` and `_movement`:
    whatever states a position states its sign, and whatever states the size of a
    movement does not, because its direction is in its type code.

    Writing `-` for a negative and nothing for a positive, which this did
    before #57, was the asymmetry: a reader that requires the sign refuses the
    positive case, and the file is inconsistent with itself.

    **Correcting what #57 recorded here.** That said the sample "settles the
    notation". It settles *a* notation. #114 read six more of moov-io/bai2's
    samples: 45 control totals across them are written bare and 7 signed, and
    `spec-section3.txt` - the specification's own section 3 example - writes
    `49,72000000,3/`. `sample2.txt` writes `010,+4350000` and `040,2830000` in
    one record, so it is not even consistent within a file.

    So signing is a choice and not a requirement. It is kept because it is
    self-consistent and `-` was already being written, not because the format
    asks. What the format does require is that a *reader* take either, which is
    `_int`, and a test holds it.

    Not adopted: that sample zero-pads these to a fixed width - 17 digits for a
    control total, 12 for a summary amount, 15 for an unsigned 16 amount. BAI2 is
    comma-delimited, so a width carries no meaning a reader needs, and three
    different widths in one file read as that producer's habit rather than as the
    format asking. Unpadded is what this writes, and if a real reader ever
    refuses it, that is a fact and this comment is where to put it.
    """
    return "%+d" % minor


def _amount(minor: int) -> str:
    """A balance in minor units, signed. Negative is an overdraft."""
    return _signed(minor)


def _movement(minor: int) -> str:
    """The size of a movement, which is never negative.

    A transaction's direction lives in its type code, so a negative amount on a
    16 would say the opposite of what the code says *and* lower the control
    total. `_amount` allowed it because it was used for both; they are separate
    now, and this refuses rather than writing a record two readers would disagree
    about.
    """
    if minor < 0:
        raise Unwritable(
            "a movement of %d cannot be written: a 16 record carries the size of "
            "the movement and its type code carries the direction, so a negative "
            "amount would contradict the code and lower the control total" % minor)
    return str(minor)


def _record(code: str, *values) -> str:
    """One record, from its declaration. Raises if the values do not fit it.

    The check is the point: a caller that forgets a field, or adds one, is a
    writer bug that would otherwise leave a short record for somebody else's
    parser to reject.
    """
    if code not in RECORDS:
        raise Unwritable("no record type %r is declared" % code)
    name, fields = RECORDS[code]
    expected = len(fields) - 1                      # the record code itself
    if len(values) != expected:
        raise Unwritable(
            "a %s (%s) takes %d field(s) after the code, not %d"
            % (name, code, expected, len(values)))
    # Every alphanumeric field is made safe here rather than at the call sites.
    # An earlier version did it at three places, all in the 16, and left the 01
    # and the 02 open: an account named `ACME, Inc.` wrote a nine-field 02 with
    # everything after the name shifted, and `A/S Nordisk` ended the record at
    # `02,A/`. The account's name is free text on the control plane, so this is
    # reachable from a PATCH, not only from a payment file.
    parts = [code]
    for field, value in zip(fields[1:], values):
        if field.kind is GROUP:
            parts.extend(_safe(item) for item in value)
        elif field.kind is A:
            parts.append(_safe(value))
        elif field.kind is REST:
            # By kind, not at the call site. `REST` fell through to `str(value)`
            # when #128 declared it, and the text was safe only because
            # `_transaction` happened to call `_safe` itself - which is the
            # arrangement #57 fixed, where three safe call sites left the 01 and
            # the 02 open.
            parts.append(_free_text(value))
        elif field.kind is SIGNED:
            parts.append(_signed(value))
        else:
            parts.append("" if value is None else str(value))
    return SEPARATOR.join(parts) + TERMINATOR


def write_statement(account: Dict, day: datetime.date, number: int,
                    opening: int, closing: int, payments: Sequence[Dict],
                    *, created_at: datetime.datetime,
                    sender: str = "MOCKBANK", receiver: str = "") -> str:
    """One account's statement for one business day, as a BAI2 file.

    The **statement data** is `messages.write_camt053`'s, in the same order and
    meaning: `opening` and `closing` are the booked balances in minor units,
    signed; `payments` are the rows whose entries the statement shows, debits and
    - with `credit` set - returns.

    The tails differ and are keyword-only here for that reason. `write_camt053`
    ends `msg_id, created_at, zone`; this ends `created_at, sender, receiver`,
    because BAI2 has no message id (the file identification number is `number`)
    and no per-message zone, and it does name a sender and a receiver. An earlier
    docstring claimed the whole signature matched, which would have let #57 pass
    the same positional arguments to both and silently bind `msg_id` to
    `created_at`. Keyword-only makes that a TypeError instead of a wrong file.

    Like the `camt.053` writer, this does not check that the balances reconcile.
    Under `statement-gap` they are meant not to, and a writer that refused would
    make that behaviour impossible to render.
    """
    # Physical record length and block size are left blank. An earlier draft
    # wrote 80, which the 03 record already exceeds, so the file would have
    # described itself wrongly in its first line.
    #
    # #95 gave the reason as "BAI2 allows blank for a variable-length delimited
    # file", which was a guess about the field rather than an observation. The
    # sample vendored in #57 *populates* it - `01,0004,12345,060321,0829,001,80,1,2/`
    # - and its longest record is 75 characters, so the field is a real bound that
    # a producer keeps to rather than an optional note. Blank is still right here,
    # for the other reason: this writer's records exceed 80 and it does not wrap
    # them, so any number it could state would be one it breaks. Stating nothing
    # is honest; stating 80 was not.
    bank, customer = _parties(account, sender, receiver)
    lines = [_record(
        "01", bank, customer, _yymmdd(created_at.date()),
        _hhmm(created_at), number, "", "", VERSION)]
    lines.extend(_group(account, day, number, opening, closing, payments,
                        created_at, bank, customer))
    lines.append(_record("99", _control_total(lines), 1,
                         _count(lines, trailers=1)))
    return "\n".join(lines) + "\n"


def _parties(account: Dict, sender: str, receiver: str):
    """(the bank, the customer) - the two names every header in the file uses.

    Derived once because the 01 and the 02 name the same two parties in opposite
    order, and #57 found this module had them fighting: the 01 said
    `sender=MOCKBANK, receiver=<account>` and the 02 said
    `ultimate receiver=<the account's name>, originator=<the account's id>`, so
    the bank appeared nowhere in the 02 and the customer originated its own
    statement.

    `moov-io/bai2`'s `sample1.txt` settles the direction. Its 01 is
    `sender=0004, receiver=12345` and its 02 is
    `ultimate receiver=12345, originator=0004`: `0004` is the bank in both
    records, `12345` the customer in both. **The originator of a group is the
    bank that produced the statement**, which reads oddly beside ISO 20022,
    where an originator is whoever started a payment - in BAI2 it is whoever
    originated the *file*.

    Returning a pair, rather than each record reaching for what it needs, is the
    fix for the class of bug and not only for this instance: there is now no way
    to change one record's idea of who the customer is without changing the
    other's.
    """
    return sender, (receiver or account["id"])


def _group(account, day, number, opening, closing, payments, created_at,
           bank, customer) -> List[str]:
    """A group header, one account, and the group trailer.

    One group and one account per file: a group is a set of accounts sharing an
    as-of date and currency, and this mock writes a statement for one account at
    a time, as it does a `camt.053`. Group status 1 says the data is complete
    as of the date given.
    """
    lines = [_record("02", customer, bank, 1,
                     _yymmdd(day), _hhmm(created_at), account["currency"], "")]
    lines.extend(_account(account, opening, closing, payments))
    lines.append(_record("98", _control_total(lines), 1,
                         _count(lines, trailers=1)))
    return lines


def _identifier(account: Dict) -> str:
    """What the 03's customer account number carries.

    The **account number** for an account that banks in NACHA, and the IBAN
    otherwise. The field's own name is the first argument, and the second is that
    a NACHA account is named by its routing number and account number everywhere
    else in this mock (#53) - its payment files carry that and no IBAN, and a US
    treasury system reading this statement has no use for one. The seed happens
    to give every account both, which is what made writing the IBAN unnoticeable
    rather than right.

    It is the same principle that made this statement BAI2 at all: an account is
    described the way its own format describes it.
    """
    if account.get("format") == "nacha" and account.get("account_number"):
        return account["account_number"]
    return account["iban"]


def _account(account: Dict, opening: int, closing: int,
             payments: Sequence[Dict]) -> List[str]:
    """The 03, its 16s, and the 49 that counts them."""
    # The two balances, and nothing else. BAI2 allows an account to summarise its
    # movements here as well, and an earlier draft did: it put a 455 total and a
    # 165 total beside the balances. That draft was dropped on the reasoning that
    # the control total sums every amount field, so each movement would be
    # counted twice and a reader reconciling the total against the entries would
    # have to know to halve it.
    #
    # #57's sample from outside the project shows the double-counting is real and
    # the objection to it was not. Each 49 in `bai2-sample1.txt` is exactly twice
    # the sum of its own 16s, because the 88's credit and debit totals are counted
    # as well: 417000 + 417000 = 834000. A BAI2 reader does know to expect that,
    # because it is what BAI2 does.
    #
    # Balances only is still what this writes, for the reason the issue gives -
    # the 03 states the account's position and the 16s state its movements - but
    # it is a choice now rather than an avoidance of a problem that was not
    # there.
    summary = [OPENING_LEDGER, _amount(opening), "", AVAILABLE_NOW,
               CLOSING_LEDGER, _amount(closing), "", AVAILABLE_NOW]
    lines = [_record("03", _identifier(account), account["currency"], summary)]
    for payment in payments:
        lines.append(_transaction(payment))
    lines.append(_record("49", _control_total(lines), _count(lines, trailers=1)))
    return lines


def _transaction(payment: Dict) -> str:
    """One 16 record.

    The `EndToEndId` goes in the bank reference number, which is the field a
    treasury system reconciles on, and the other party's name in the text.

    The references are delimited, so a comma in one would end the field early
    and `_safe` replaces it. The text is not: it runs to the terminator, so
    `_record` makes it safe by kind with `_free_text`, which keeps the comma.
    Both branches pass the name through unchanged for that reason - money
    arriving names the payer where a payment out names the payee, and neither
    should be misspelt on a statement (#129).
    """
    if payment.get("incoming"):
        # Money arriving from somebody else (#91): the payer's name is the
        # text, and their structured reference the customer reference number.
        return _record("16", RECEIVED_CREDIT, _movement(payment["amount"]),
                       AVAILABLE_NOW, _safe(payment["end_to_end_id"]),
                       _safe(payment.get("reference") or ""),
                       payment.get("debtor_name") or "")
    if payment.get("collected"):
        # A collection that settled (#131): the debtor's name is the text, and
        # the file it was asked for in the customer reference, as on a debit.
        return _record("16", COLLECTED_CREDIT, _movement(payment["amount"]),
                       AVAILABLE_NOW, _safe(payment["end_to_end_id"]),
                       _safe(payment.get("msg_id") or ""),
                       payment.get("debtor_name") or "")
    if payment.get("credit"):
        # Any other credit is a payment of this bank's own coming back, and
        # carries the reason it came back. Coding a customer's payment as a
        # return would be a wrong statement, not a cosmetic one, so a credit
        # that is neither money arriving nor explained stops here instead of
        # being mislabelled.
        if not payment.get("return_reason"):
            raise Unwritable(
                "a credit with no return reason is not a return, and it is not "
                "money arriving either: %r would be written as %s, which says "
                "the payment came back."
                % (payment.get("end_to_end_id"), RETURNED_CREDIT))
        code = RETURNED_CREDIT
    else:
        code = DEBIT
    return _record("16", code, _movement(payment["amount"]), AVAILABLE_NOW,
                   _safe(payment["end_to_end_id"]),
                   _safe(payment.get("msg_id") or ""),
                   payment.get("creditor_name") or "")


# Everything that would end a field, a record, or a line. The line breaks are
# here because `splitlines()` breaks on more than CR and LF: U+2028 and U+2029
# split too, so a creditor name carrying one wrote a record across two lines and
# `read` then raised on the half of it. The trailers counted it once either way,
# which is what made the file wrong rather than merely unreadable.
UNSAFE = (SEPARATOR, TERMINATOR, "\u2028", "\u2029", "\u0085")

# Every C0 control, rather than the line breaks among them. An earlier version
# listed CR, LF and the separators it could think of, which left tab and \x01 to
# pass through - harmless to the field boundaries and still junk in a file a bank
# parses. A list of the dangerous ones is a list somebody has to keep complete;
# "no control characters" is not.
CONTROLS = tuple(chr(code) for code in range(0x20)) + ("\x7f",)


def _safe(text) -> str:
    """A value with nothing in it that would end a field, a record or a line.

    BAI2 has no escape, so there is nothing to escape *to*: a comma in a name
    ends the field early and every field after it shifts, which is a wrong
    statement rather than an unreadable one. Replaced with a space, and `None`
    becomes empty rather than the text "None".

    For the *last* field of a record see `_free_text`, which keeps the comma
    because nothing follows it to shift.
    """
    if text is None:
        return ""
    out = str(text)
    for bad in UNSAFE + CONTROLS:
        out = out.replace(bad, " ")
    return out.strip()


def _free_text(text) -> str:
    """The last field of a record, which may contain the separator.

    A comma is safe here and nowhere else: this field runs to the terminator, so
    there is no field after it to shift. `sample4` carries `ACH Credit Payment,
    Entry Description: EXP; -, SEC: CCD, Client Ref ID: 1111` as one field, and
    replacing those commas is how a BAI2 statement came to name a creditor
    `Umbrella  Logistics  S.A.` where the `camt.053` of the same statement said
    `Umbrella, Logistics, S.A.` (#129).

    What is still replaced:

    - **line breaks and control characters.** Not cosmetic: since #128 a line
      that begins with a declared code and a separator starts a record, so a
      payee called `Foo\n49,+0,2` would put a **forged account trailer** into
      the file. `trailers_agree` would then report the file as inconsistent,
      which is a wrong file caught late rather than one never written;
    - **the separator that completes a `/<code>,`**, which `_records` would read
      as the end of the record. The slash is kept.

    A bare slash is kept too, which is why the sequence above needs handling at
    all. A first version of this excluded only the separator from the replaced
    set and left the terminator in it, so every slash still became a space -
    `A/S Nordisk` came out `A S Nordisk`, and `SPLITS_A_RECORD` was unreachable
    because no slash survived to form the sequence. A slash is safe on its own:
    the writer puts one record on a line and appends the terminator, so
    `_ended` takes that one off and leaves the field's own.

    That last choice is the interesting one, and the obvious version of it is
    wrong. Neutralising the *slash* looks right - it is the delimiter-ish
    character - but `NEXT_RECORD` allows whitespace between the slash and the
    code, so blanking or removing one slash can expose the one before it:
    `A//16,B` becomes `A/ 16,B` or `A/16,B`, both of which still split. Fixing
    that needs iterating to a fixed point.

    Replacing the **separator** needs one pass and provably so: a match requires
    a comma, so turning a comma into a space can only remove matches and never
    create one. It also keeps the slash, which real files carry freely - twenty-two
    of them in `sample5` - and sacrifices the one comma that would have split the
    record rather than every comma in the name.
    """
    if text is None:
        return ""
    out = str(text)
    for bad in CONTROLS + tuple(c for c in UNSAFE
                                if c not in (SEPARATOR, TERMINATOR)):
        out = out.replace(bad, " ")
    # `.strip()` to match `_safe`, so a name that differs only in the padding
    # around it is written the same way by either. No test holds it and none
    # should: it is a consistency between the two, not a property of the format.
    return SPLITS_A_RECORD.sub(r"\1 ", out).strip()


Summary = namedtuple("Summary", "type_code amount count funds")
Detail = namedtuple("Detail", "type_code amount funds availability reference "
                              "customer_reference text")


def summary_groups(values: Sequence[str]) -> List["Summary"]:
    """An 03's repeating summary, walked rather than counted in fours.

    `values` is the record's fields after the code, continuations already folded
    in. A group is (type code, amount, item count, funds type) and the funds type
    carries its own width, so the stride is four or more.

    The old version took every second field of four. Over a real file that reads
    each amount out of a later group's slot, and on `sample1` it raised a bare
    `ValueError` from `int("")` - not even a refusal (#114).
    """
    out, index, rest = [], 2, values
    while index < len(rest):
        if not rest[index].strip():
            index += 1                      # the record ended on a separator
            continue
        if index + 3 >= len(rest):
            break                           # `_walk_groups` already refused this
        funds = rest[index + 3]
        out.append(Summary(rest[index], _int(rest[index + 1]),
                           rest[index + 2], funds))
        index += 3 + _funds_width(funds, rest[index + 4:])
    return out


def detail(values: Sequence[str]) -> "Detail":
    """A 16's fields, with the variable-width funds type accounted for.

    Read by walking and not by position: with a value-dated funds type the bank
    reference is at index 5 rather than 3, which is how `statements` was reading
    a real file's availability date as its reference.
    """
    width = _funds_width(values[2], values[3:]) if len(values) > 2 else 1
    after = 2 + width
    trailing = list(values[after:])
    # The text is everything from the third trailing field on, rejoined: it is
    # one field that happens to contain separators, not several fields (#128).
    text = SEPARATOR.join(trailing[2:]) if len(trailing) > 2 else ""
    trailing += ["", ""]
    return Detail(values[0], _int(values[1]),
                  values[2] if len(values) > 2 else "",
                  tuple(values[3:after]),
                  trailing[0], trailing[1], text)


def _int(value: str) -> int:
    """An amount, signed or bare.

    Both occur, and bare is the majority: across moov-io/bai2's seven samples 45
    control totals are written bare and 7 signed, and `spec-section3.txt` - the
    specification's own section 3 example - writes `49,72000000,3/`. `sample2`
    writes `010,+4350000` and `040,2830000` in one record. `int` takes either, so
    this is a name for the fact rather than a conversion, and a test holds it.
    """
    return int(value.strip() or 0)


def _amounts_in(line: str) -> List[int]:
    """Every amount a record carries, for a control total.

    Read from the record's own declaration rather than by position, so a record
    that gains a field does not silently change what a trailer totals.
    """
    parts = line.rstrip(TERMINATOR).split(SEPARATOR)
    return _amounts_of(parts[0], parts[1:])


def _amounts_of(code: str, values: Sequence[str]) -> List[int]:
    """The same, for a record already folded and split."""
    if code not in RECORDS:
        return []
    if code == "03":
        return [group.amount for group in summary_groups(values)]
    if code == "16":
        return [_int(values[1])]
    return []


def _control_total(lines: Sequence[str]) -> int:
    """The sum of the amounts in these records.

    Signed, per `CONTROL_TOTALS_ARE_SIGNED`: an overdrawn closing ledger
    subtracts. Trailers already written into `lines` contribute nothing, because
    a control total is not an amount.
    """
    total = sum(sum(_amounts_in(line)) for line in lines)
    return total if CONTROL_TOTALS_ARE_SIGNED else abs(total)


def _count(lines: Sequence[str], trailers: int = 0) -> int:
    """How many records a trailer reports, per `COUNTS_INCLUDE_THE_TRAILER`."""
    return len(lines) + (trailers if COUNTS_INCLUDE_THE_TRAILER else 0)


Record = namedtuple("Record", "code name values")


# Where one record ends and the next begins, which is two questions in BAI2 and
# not one.
#
# **A `/` is not a delimiter to split on.** `sample5` writes customer references
# like `AB/GS/RPFILERP0001/RPBA0001` and remittance text like `08/18/23 Invoice`;
# splitting on `/` shatters twenty-two fields in that one file. A mid-record `/`
# ends a record only when a declared code and a separator follow it. Over all five
# of moov-io/bai2's samples that is exact both ways: all eleven of `sample3`'s
# packed records are found and none of `sample5`'s content slashes is mistaken
# for one. A declared code rather than any two digits, because four of those
# content slashes *are* followed by two digits - `08/18/23` gives `/18/23` and
# `/23 In` - and none by a code.
#
# **A newline is not a delimiter either.** `sample4` leaves 102 of its 116 lines
# unterminated, so a newline often does end a record - but `sample3` wraps one
# 16's text onto a second line that ends with the `/`, and `sample5` writes a
# continuation as `88:EREF: ...` with a colon where the separator should be.
# Treating every newline as a terminator makes the first a record whose code is
# `111111111111111` and the second one whose code is `88:EREF: 07370568132`.
#
# So the rule is the other way round: **a line starts a record when it begins
# with a declared code and a separator, and otherwise continues the record
# above.** Across the five samples exactly two lines continue one, and they are
# those two. Nothing needs a special case and nothing needs a per-file mode.
#
# moov-io/bai2's own scanner reaches the same rule from the other direction, and
# requires the separator too - `pkg/util/scanner.go` at the pinned commit:
#
#     // If the next three bytes are any of the defined BAI2 record codes
#     // (followed by a comma), we consider the next line as a new record
#
# and, where a line runs on without one:
#
#     // Here, the current line "continued" onto the next line without a
#     // delimiter and without a new record code on the subsequent line. Parse
#     // the next line as though it is a continuation of the current line.
#
# Two readers agreeing is not proof, but they were written from the same files
# and not from each other, and the separator is the part a guess would drop.
#
# What this cannot do: a text field containing `/16,` would still split wrongly,
# and a wrapped line that happens to begin `16,` would start a record. Both are
# inherent to a format with no escape character and a run-to-end last field - a
# human reading the line could not tell either - and they are written here rather
# than pretended away. Nothing in the samples comes within two characters.
_CODES = "|".join(sorted(set(RECORDS) | {CONTINUATION}))
NEXT_RECORD = re.compile(r"/[ \t]*(?=(?:%s)%s)" % (_CODES, re.escape(SEPARATOR)))
STARTS_RECORD = re.compile(r"^(?:%s)%s" % (_CODES, re.escape(SEPARATOR)))

# The mirror of `NEXT_RECORD`, for the writer: the one sequence a run-to-end field
# cannot carry, because `_records` would read it as the end of the record. Written
# beside the reader's rule so the two cannot drift - if a code is ever added,
# both change together.
SPLITS_A_RECORD = re.compile(r"(/[ \t]*(?:%s))%s"
                             % (_CODES, re.escape(SEPARATOR)))


def _continues(lines: List[str], number: int) -> bool:
    """Whether the line after `lines[number - 1]` continues its record.

    The next line that is not blank, since a blank line is skipped rather than
    treated as a break. A line continues the one above when it does not begin
    with a declared code and a separator - the same test `_records` applies, so
    the two cannot disagree about where a record ends. The end of the file
    continues nothing.
    """
    for later in lines[number:]:
        if not later.rstrip():
            continue
        return not STARTS_RECORD.match(later.lstrip())
    return False


def _records(text: str):
    """(line number, the record's text) for each record, in order.

    The line number is where the record *started*, because that is the line
    somebody looking for the problem should open.
    """
    out = []
    lines = text.splitlines()
    for number, raw_line in enumerate(lines, start=1):
        trimmed = raw_line.rstrip()
        if not trimmed:
            continue
        # Trailing whitespace is the last field's own padding **only** when the
        # next line continues this record. Then it has to survive, because the
        # join inserts nothing: stripping it ran `PAYMENT FOR ` straight into
        # `INVOICE 12`, and a fixed-width `ACME      ` into the text below, where
        # before #142 the line break held them apart. Anywhere else it is padding
        # around a record and comes off, as it always has - a `49,+125000,2   `
        # that ends its record states 2, not `2   `.
        #
        # moov draws the line in the same place: at its `fullLine` label, where
        # the next line starts a record, it does
        # `bytes.TrimRight(b.currentLine.Bytes(), " \t")`, and on the
        # continuation path it leaves the buffer alone.
        #
        # A `/` at the end of such a line is its field's content for the same
        # reason (#150). A `/` ends a record only when a record code follows it -
        # on this line, or on the next non-blank one - or when nothing follows at
        # all. That is #128's rule for a `/` inside a line, carried across a line
        # break, and it leaves one rule where there were two. Before this, a
        # reference written `AB/` + `GS/RP0001` read as `ABGS/RP0001`: the file
        # meant `AB/GS/RP0001` and a character went missing, which for
        # `payment_run` is an invoice that cannot be matched.
        #
        # So a line the next one continues contributes its raw text, terminator
        # and padding alike; any other line contributes its trimmed text with a
        # terminator taken off.
        wrapped = _continues(lines, number)
        line = raw_line if wrapped else trimmed
        if not STARTS_RECORD.match(line.lstrip()):
            if not out:
                raise Unreadable(
                    "line %d starts with no declared record code: %r"
                    % (number, line.strip()[:40]))
            # A wrapped record: the line continues the one above, joined with
            # **nothing** (#142). `sample3` wraps a 16's text at a column and
            # puts the terminator on the second line.
            #
            # #128 kept the line break here, and the argument for that was
            # confused: it said a space is a character the field could have
            # contained and a newline is not, so joining with a space would be
            # indistinguishable from the producer having written one. True, and
            # an argument against joining with a *space*. It says nothing against
            # joining with nothing, which never got considered - and a newline
            # left inside a value is no better, because it is a character no
            # producer meant either. A wrap in a reference read back as
            # `INV-2026-\n0101`, and `payment_run` reconciles on that field.
            #
            # moov-io/bai2's scanner joins with nothing, in `pkg/util/scanner.go`:
            # a newline goes to its `fullLine` label without reaching the buffer
            # (only the `default` branch writes), and the continuation path
            # re-enters the loop with that same buffer, so the next line's
            # characters are appended directly.
            #
            # One implementation and no file: **no BAI2 file anywhere in
            # moov-io/bai2 wraps outside a 16's text**, so nothing attests the
            # case this rule is for. The two wraps that exist both continue a
            # 16's text, but only `sample3`'s is filler that reads as
            # meaninglessly one way as the other. `sample5` line 62 ends
            # `GS ID: SC213480000120999` and line 63 is `88:EREF: 07370568132` -
            # a continuation typed with a colon, so a wrap and not an 88 - and
            # joining with nothing runs the two references together. moov's
            # scanner reads it the same way, so the file does not contradict the
            # rule's source; it does bear on a decision taken on "no file
            # adjudicates", and both readings are posted on #142. That is
            # recorded there rather than presented as settled, and the rule is
            # the PM's decision on that basis.
            where, so_far = out[-1]
            out[-1] = (where, so_far + _ended(line, wrapped))
            continue
        # Only the last piece of the line can be the one a wrap carries into the
        # next: every piece before it is followed by a record code on this line,
        # so its `/` is a terminator by the same rule.
        pieces = [piece for piece in NEXT_RECORD.split(line.lstrip())
                  if piece.strip()]
        for position, piece in enumerate(pieces, start=1):
            out.append((number, _ended(piece, wrapped and position == len(pieces))))
    return out


def _ended(piece: str, carried: bool = False) -> str:
    """A record's text without its terminator, which is optional in practice.

    Only the terminator comes off. A field may be padded with spaces - sample1
    writes `RETURNED CHEQUE     ` in a fixed-width text field - and that padding
    is the field's content, not whitespace around a record. An earlier version
    here stripped it and turned a 20-character text into a 15-character one.

    `carried` says the next non-blank line continues this record, which makes a
    trailing `/` content rather than a terminator (#150): nothing ends here, so
    there is no terminator to remove.
    """
    if carried:
        return piece
    return piece[:-1] if piece.endswith(TERMINATOR) else piece


Folded = namedtuple("Folded", "line code values lines")


def _fold(text: str) -> List["Folded"]:
    """The file as logical records, each remembering how many lines it occupied.

    A `88` carries no fields of its own. It continues the comma-separated field
    stream of the record before it, so it is joined on here and nothing further
    down has to know continuations exist. The line number kept is the *first*
    line of the logical record, because that is the one a reader looking for the
    problem should open.

    `lines` is kept because the two things a trailer states are counted over
    different units: a **record count** counts physical records, and sample1's
    `49` states 14 for a span that holds 13 lines plus itself *including* the
    continuation - so a continuation counts as a record. A **control total** sums
    the amounts of the logical record, where the continuation's fields are part
    of the record above. Counting either one over the other's unit gives a number
    that is wrong in a file with an `88` in it and right in every file this mock
    writes.

    This is what `examples/payment_run.py` had been doing since #113 while this
    module refused any file containing an `88` - the module and the example
    disagreeing about the format, with only the example held to a real file.
    """
    out = []
    for number, raw in _records(text):
        values = raw.split(SEPARATOR)
        code, rest = values[0], values[1:]
        if code == CONTINUATION:
            if not out:
                raise Unreadable(
                    "line %d is a continuation (%s) with no record before it to "
                    "continue" % (number, CONTINUATION))
            out[-1].values.extend(rest)
            out[-1] = out[-1]._replace(lines=out[-1].lines + 1)
            continue
        if code not in RECORDS:
            raise Unreadable("line %d has record code %r, which is not declared"
                             % (number, code))
        out.append(Folded(number, code, rest, 1))
    return out


def read(text: str) -> List[Record]:
    """A BAI2 file as records, by the same declarations that wrote it.

    Continuations are folded into the record they continue, so a file this mock
    wrote and a file a bank wrote read the same way. Still strict: a record that
    does not fit its declaration raises rather than being collected as a finding,
    because this reads statements rather than validating payment files somebody
    sent.
    """
    out = []
    for number, code, values, _ in _fold(text):
        # Counted against the declaration. Without this the reader accepted a
        # nine-field 02 written by an account name with a comma in it, so a file
        # whose fields had all shifted read back without complaint - which is why
        # neither that nor the line-break case showed up as a failing test.
        problem = _field_count_problem(code, values)
        if problem:
            raise Unreadable("line %d: %s" % (number, problem))
        out.append(Record(code, RECORDS[code][0], values))
    return out


Parsed = namedtuple("Parsed", "account currency opening closing entries")
Entry = namedtuple("Entry", "type_code amount reference text")


def statements(text: str) -> List[Parsed]:
    """Each account in the file reduced to what a `camt.053` asserts.

    Opening balance, closing balance and the entries, so a test can compare the
    two renderings without either trusting the other's arithmetic.

    This needs the two ledger balances, so it is narrower than `read`, which since
    #114 takes any BAI2 file. `sample1.txt` reads and cannot be reduced: its 03
    reports available balances (`040`, `045`) and movement totals (`100`, `400`)
    and never states a ledger balance. That is a legal statement and not something
    this can answer about, so it says which code is missing rather than raising a
    `KeyError` from a dictionary two frames down.
    """
    out, current, entries = [], None, []
    for record in read(text):
        if record.code == "03":
            current, entries = record, []
        elif record.code == "16" and current is not None:
            one = detail(record.values)
            entries.append(Entry(one.type_code, one.amount,
                                 one.reference, one.text))
        elif record.code == "49" and current is not None:
            summary = _summary_of(current)
            for code, what in ((OPENING_LEDGER, "opening"),
                               (CLOSING_LEDGER, "closing")):
                if code not in summary:
                    raise Unreadable(
                        "account %s states no %s ledger balance (%s); it reports "
                        "%s, so it cannot be reduced to the three things a "
                        "camt.053 asserts"
                        % (current.values[0], what, code,
                           ", ".join(sorted(summary)) or "nothing"))
            out.append(Parsed(current.values[0], current.values[1],
                              summary[OPENING_LEDGER], summary[CLOSING_LEDGER],
                              entries))
            current, entries = None, []
    return out


def _summary_of(record: Record) -> Dict[str, int]:
    """The 03 record's summary as {type code: amount}."""
    return {group.type_code: group.amount
            for group in summary_groups(record.values)}


def trailers_agree(text: str) -> List[str]:
    """Recompute every trailer from the records it covers; report disagreements.

    The writer computes these going forwards and this checks them going back,
    which is the only way a control total is worth anything. It is used by the
    tests and by `#57`'s comparison against an outside file.
    """
    problems, lines = [], _fold(text)
    account_start = group_start = None
    for index, record in enumerate(lines):
        code = record.code
        if code == "03":
            account_start = index
        elif code == "02":
            group_start = index
        elif code == "49" and account_start is not None:
            problems += _check(lines, account_start, index, "49",
                               ("account control total", "number of records"))
            account_start = None
        elif code == "98" and group_start is not None:
            problems += _check(lines, group_start, index, "98",
                               ("group control total", None, "number of records"))
            group_start = None
        elif code == "99":
            problems += _check(lines, 0, index, "99",
                               ("file control total", None, "number of records"))
    return problems


def _check(folded, start, trailer, code, names) -> List[str]:
    """One trailer against what it covers.

    The two numbers are counted over different units, which a file with a
    continuation in it can tell apart and a file this mock writes cannot: the
    **total** sums the amounts of each logical record, and the **count** counts
    physical records, continuations included. `sample1`'s 49 states 14 for
    thirteen lines and itself.
    """
    covered = folded[start:trailer]
    stated = folded[trailer].values
    total = sum(sum(_amounts_of(r.code, r.values)) for r in covered)
    if not CONTROL_TOTALS_ARE_SIGNED:
        total = abs(total)
    physical = sum(r.lines for r in covered)
    count = physical + (1 if COUNTS_INCLUDE_THE_TRAILER else 0)
    wanted = [total, None, count] if len(names) == 3 else [total, count]
    problems = []
    for name, want, got in zip(names, wanted, stated):
        if name is None or want is None:
            continue
        if _int(got) != want:
            problems.append("the %s record's %s says %s; the records it covers "
                            "give %d" % (code, name, got, want))
    return problems
