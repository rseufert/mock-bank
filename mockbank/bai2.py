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
==============  ==============================================================

``010`` and ``015`` are the two status codes this mock needs and are not in
doubt. **The two transaction codes are still placeholders.** They are listed in
``PLACEHOLDER_CODES`` so that nothing else in the package has to know which is
which.

#57 was meant to settle them against a file from outside the project, and the
file it found could not. ``bai2-sample1.txt`` carries ``100`` and ``400`` (a
credit and a debit summary total), ``108`` and ``409`` (a detail credit and
debit), and ``040``/``045`` (available balances). None of those is an outgoing
customer transfer or one coming back, which are the only two movements this mock
books, so the sample settles a great deal about the *file* and nothing about
these two codes. The one public repository holding the full BAI code list carries
**no licence at all**, so it is neither vendored here nor cited as authority.

Two unverified codes plainly marked is the honest state. They are not quietly
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
# are certain and these two are not, so they are named here and nowhere else. The
# outside sample #57 vendored settled the record layout, the counts, the control
# totals and the 02's parties, and could not settle these: it books nothing that
# is an outgoing customer transfer or a return of one.
DEBIT = "495"
RETURNED_CREDIT = "165"
PLACEHOLDER_CODES = (DEBIT, RETURNED_CREDIT)

# `Z` means the amount is immediately available; BAI2's other funds types say
# when it becomes so. Everything this mock books is already booked, so there is
# nothing to distribute over later dates.
AVAILABLE_NOW = "Z"

Field = namedtuple("Field", "name kind")

# An integer in minor units or a count; text; a date as yymmdd; a time as hhmm;
# SIGNED, an amount that carries an explicit `+` or `-`; and GROUP, which is the
# 03 record's repeating summary of (code, amount, item count, funds type).
#
# SIGNED is a kind rather than something the callers format, for the reason the
# comment in `_record` gives about `_safe`: the three control totals are written
# from three places, and doing it at the call sites is how the 01 and the 02 got
# missed last time.
N, A, DATE, TIME, SIGNED, GROUP = "N", "A", "D", "T", "S", "G"


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
        ("funds type", A), ("bank reference number", A),
        ("customer reference number", A), ("text", A))),
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


def _shape(code: str):
    """(fixed fields after the code, whether a repeating group follows).

    The 03's summary repeats in fours, so its record has a floor and a stride
    rather than an exact width; every other record has an exact one.
    """
    fields = RECORDS[code][1]
    group = any(f.kind is GROUP for f in fields)
    fixed = len([f for f in fields[1:] if f.kind is not GROUP])
    return fixed, group


def _field_count_problem(code: str, values) -> str:
    """Why this record's field count does not fit its declaration, or ""."""
    fixed, group = _shape(code)
    name = RECORDS[code][0]
    if not group:
        if len(values) != fixed:
            return ("the %s (%s) carries %d field(s) after the code; this one "
                    "has %d" % (name, code, fixed, len(values)))
        return ""
    if len(values) < fixed:
        return ("the %s (%s) carries at least %d field(s) after the code; this "
                "one has %d" % (name, code, fixed, len(values)))
    # The group is one slot in the declaration and any multiple of four fields on
    # the line, so what is written is the fixed fields plus 4n - not 4n + 1.
    extra = len(values) - fixed
    if extra % 4:
        return ("the %s (%s) repeats its summary in fours; this one has %d "
                "field(s) left over" % (name, code, extra % 4))
    return ""


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
    the same line this module already draws between `_amount` and `_movement`, so
    the sample settles the notation rather than the design: whatever states a
    position states its sign, and whatever states the size of a movement does
    not, because its direction is in its type code.

    Writing `-` for a negative and nothing for a positive, which this did
    before #57, was the asymmetry: a reader that requires the sign refuses the
    positive case, and the file is inconsistent with itself.

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
    if payment.get("credit"):
        # Every credit this mock books today is a payment of its own coming
        # back, and carries the reason it came back. #96 adds money *arriving*,
        # whose rows are credits too - and coding a customer's payment as a
        # return would be a wrong statement, not a cosmetic one. So the
        # distinction is the return reason rather than the credit flag, and an
        # unexplained credit stops here instead of being mislabelled. Whichever
        # of #56 and #96 lands second adds the received-credit code.
        if not payment.get("return_reason"):
            raise Unwritable(
                "a credit with no return reason is not a return, and BAI2 has "
                "no code here for one yet: %r would be written as %s, which "
                "says the payment came back. A received credit needs its own "
                "type code (see #57)."
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


def _amounts_in(line: str) -> List[int]:
    """Every amount a record carries, for a control total.

    Read from the record's own declaration rather than by position, so a record
    that gains a field does not silently change what a trailer totals.
    """
    parts = line.rstrip(TERMINATOR).split(SEPARATOR)
    code, values = parts[0], parts[1:]
    if code not in RECORDS:
        return []
    if code == "03":
        # The repeating summary, as (type code, amount, item count, funds type)
        # after the account number and currency: every second field of four.
        summary = values[2:]
        return [int(summary[index + 1])
                for index in range(0, len(summary), 4)
                if len(summary) > index + 1 and summary[index]]
    if code == "16":
        return [int(values[1])]
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


def read(text: str) -> List[Record]:
    """A BAI2 file as records, by the same declarations that wrote it.

    For the tests, and deliberately strict: this reads files this mock wrote, so
    anything unexpected is a bug here rather than a fact about a file somebody
    sent, and it raises instead of collecting findings. #57 is where a file from
    outside the project is read.
    """
    out = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if not line.endswith(TERMINATOR):
            raise Unreadable("line %d does not end with %r: %r"
                             % (number, TERMINATOR, line[:40]))
        values = line[:-1].split(SEPARATOR)
        code = values[0]
        if code not in RECORDS:
            raise Unreadable("line %d has record code %r, which is not declared"
                             % (number, code))
        # Counted against the declaration. Without this the reader accepted a
        # nine-field 02 written by an account name with a comma in it, so a file
        # whose fields had all shifted read back without complaint - which is why
        # neither that nor the line-break case showed up as a failing test.
        problem = _field_count_problem(code, values[1:])
        if problem:
            raise Unreadable("line %d: %s" % (number, problem))
        out.append(Record(code, RECORDS[code][0], values[1:]))
    return out


Parsed = namedtuple("Parsed", "account currency opening closing entries")
Entry = namedtuple("Entry", "type_code amount reference text")


def statements(text: str) -> List[Parsed]:
    """Each account in the file reduced to what a `camt.053` asserts.

    Opening balance, closing balance and the entries, so a test can compare the
    two renderings without either trusting the other's arithmetic.
    """
    out, current, entries = [], None, []
    for record in read(text):
        if record.code == "03":
            current, entries = record, []
        elif record.code == "16" and current is not None:
            entries.append(Entry(record.values[0], int(record.values[1]),
                                 record.values[3], record.values[5]))
        elif record.code == "49" and current is not None:
            summary = _summary_of(current)
            out.append(Parsed(current.values[0], current.values[1],
                              summary[OPENING_LEDGER], summary[CLOSING_LEDGER],
                              entries))
            current, entries = None, []
    return out


def _summary_of(record: Record) -> Dict[str, int]:
    """The 03 record's repeating (code, amount, count, funds type) as a mapping."""
    values = record.values[2:]
    found = {}
    for index in range(0, len(values), 4):
        group = values[index:index + 4]
        if len(group) < 2 or not group[0]:
            continue
        found[group[0]] = int(group[1])
    return found


def trailers_agree(text: str) -> List[str]:
    """Recompute every trailer from the records it covers; report disagreements.

    The writer computes these going forwards and this checks them going back,
    which is the only way a control total is worth anything. It is used by the
    tests and by `#57`'s comparison against an outside file.
    """
    problems, lines = [], [l.strip() for l in text.splitlines() if l.strip()]
    account_start = group_start = None
    for index, line in enumerate(lines):
        code = line.split(SEPARATOR)[0]
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


def _check(lines, start, trailer, code, names) -> List[str]:
    covered = lines[start:trailer]
    stated = lines[trailer].rstrip(TERMINATOR).split(SEPARATOR)[1:]
    wanted = [_control_total(covered), None, _count(covered, trailers=1)] \
        if len(names) == 3 else [_control_total(covered), _count(covered, trailers=1)]
    problems = []
    for name, want, got in zip(names, wanted, stated):
        if name is None or want is None:
            continue
        if int(got) != want:
            problems.append("the %s record's %s says %s; the records it covers "
                            "give %d" % (code, name, got, want))
    return problems
