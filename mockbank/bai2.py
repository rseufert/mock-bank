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
``495``         the debit for a payment that left the account (**placeholder**)
``165``         the credit for a payment that came back (**placeholder**)
``195``         money arriving from somebody else, #91 (**placeholder**)
==============  ==============================================================

``010`` and ``015`` are the two status codes this mock needs and are not in
doubt. **The transaction codes are still placeholders.** They are listed in
``PLACEHOLDER_CODES`` so that nothing else in the package has to know which is
which.

#57 was meant to settle them against a file from outside the project, and the
file it found could not. ``bai2-sample1.txt`` carries ``100`` and ``400`` (a
credit and a debit summary total), ``108`` and ``409`` (a detail credit and
debit), and ``040``/``045`` (available balances). None of those is an outgoing
customer transfer, one coming back, or a transfer received, which are the three
movements this mock books, so the sample settles a great deal about the *file*
and nothing about these codes. The one public repository holding the full BAI code list carries
**no licence at all**, so it is neither vendored here nor cited as authority.

Unverified codes plainly marked is the honest state. They are not quietly
promoted to settled because #57 closed.

The first draft used ``455`` for the debit and kept ``165`` for the return,
both reasoned from "these are ACH movements". That was shown to be the wrong
reasoning in review: in BAI2 **"preauthorized" describes a movement the *other*
party initiated** - a direct debit pulling from the account, or a credit pushed
into it. Neither is what this mock books. What leaves these accounts is a
payment the account holder sent, and what comes back is one of those returning.
So ``455`` was not merely unverified, it described the wrong originator.

``495`` replaces it on that reasoning, with ``466`` (an ACH settlement) named as
the other candidate. ``165`` stays for the moment because #56 names it
explicitly and an issue is the maintainer's to change, but the same objection
applies to it and is recorded on the pull request rather than acted on here.

Swapping one unverified code for another is not progress on its own, which is
why both are marked rather than quietly corrected: the honest state is that the
reasoning improved and the evidence did not.

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

What it could **not** settle is the two transaction codes above, and what it
exposed is that ``read`` cannot parse a real BAI2 file at all - it does not
declare the ``88`` continuation record, and it assumes a funds type occupies one
field where a value-dated one occupies three. That is #114, and
``tests/test_bai2_external.py`` pins each way it fails so that none of them can
be fixed silently or quietly rot.
"""
from __future__ import annotations

import datetime
import re
from collections import namedtuple
from typing import Dict, List, Optional, Sequence

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
# two movements it books. See the module docstring for why these two.
OPENING_LEDGER = "010"
CLOSING_LEDGER = "015"

# **Still placeholders after #57.** See the module docstring: the balance codes
# are certain and these are not, so they are named here and nowhere else. The
# outside sample #57 vendored settled the record layout, the counts, the control
# totals and the 02's parties, and could not settle these: it books nothing that
# is an outgoing customer transfer, a return of one, or a transfer received.
DEBIT = "495"
RETURNED_CREDIT = "165"
# Money arriving (#91): Incoming Money Transfer, by the reasoning that gives the
# debit 495 - a transfer, not a "preauthorized" movement the other party pulled.
RECEIVED_CREDIT = "195"
PLACEHOLDER_CODES = (DEBIT, RETURNED_CREDIT, RECEIVED_CREDIT)

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
    treasury system reconciles on, and the creditor's name in the text. A
    comma in either would end the field early, so it is replaced rather than
    escaped: BAI2 has no escape, and a name with a comma in it is common.
    """
    if payment.get("incoming"):
        # Money arriving from somebody else (#91): the payer's name is the
        # text, and their structured reference the customer reference number.
        return _record("16", RECEIVED_CREDIT, _movement(payment["amount"]),
                       AVAILABLE_NOW, _safe(payment["end_to_end_id"]),
                       _safe(payment.get("reference") or ""),
                       _safe(payment.get("debtor_name") or ""))
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
                   _safe(payment.get("creditor_name") or ""))


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
    """
    if text is None:
        return ""
    out = str(text)
    for bad in UNSAFE + CONTROLS:
        out = out.replace(bad, " ")
    return out.strip()


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
# What this cannot do: a text field containing `/16,` would still split wrongly,
# and a wrapped line that happens to begin `16,` would start a record. Both are
# inherent to a format with no escape character and a run-to-end last field - a
# human reading the line could not tell either - and they are written here rather
# than pretended away. Nothing in the samples comes within two characters.
_CODES = "|".join(sorted(set(RECORDS) | {CONTINUATION}))
NEXT_RECORD = re.compile(r"/[ \t]*(?=(?:%s)%s)" % (_CODES, re.escape(SEPARATOR)))
STARTS_RECORD = re.compile(r"^(?:%s)%s" % (_CODES, re.escape(SEPARATOR)))


def _records(text: str):
    """(line number, the record's text) for each record, in order.

    The line number is where the record *started*, because that is the line
    somebody looking for the problem should open.
    """
    out = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.rstrip()
        if not line.strip():
            continue
        if not STARTS_RECORD.match(line.lstrip()):
            if not out:
                raise Unreadable(
                    "line %d starts with no declared record code: %r"
                    % (number, line.strip()[:40]))
            # A wrapped record: the newline is inside its last field, so it is
            # kept rather than turned into a separator. `sample3` wraps a 16's
            # text at a column and puts the terminator on the second line.
            where, so_far = out[-1]
            out[-1] = (where, so_far + "\n" + _ended(line))
            continue
        for piece in NEXT_RECORD.split(line.lstrip()):
            if piece.strip():
                out.append((number, _ended(piece)))
    return out


def _ended(piece: str) -> str:
    """A record's text without its terminator, which is optional in practice.

    Only the terminator comes off. A field may be padded with spaces - sample1
    writes `RETURNED CHEQUE     ` in a fixed-width text field - and that padding
    is the field's content, not whitespace around a record. An earlier version
    here stripped it and turned a 20-character text into a 15-character one.
    """
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
