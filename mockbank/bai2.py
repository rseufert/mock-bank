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
doubt. **The transaction codes are placeholders, and are wrong until #57
settles them against a file from outside the project.** They are listed in
``PLACEHOLDER_CODES`` so that nothing else in the package has to know which is
which.

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

What is *not* certain, and is asked on #56 rather than assumed quietly: this
module has been written against the BAI2 record layout as it is commonly
reproduced, not against the published BAI specification, which the project does
not hold. Two readings are made explicitly and may be wrong:

1. **Control totals sum signed amounts**, so a negative closing ledger
   subtracts from the account's total. ``CONTROL_TOTALS_ARE_SIGNED`` names it.
2. **Record counts include the trailer that carries them.** A ``49`` counts the
   ``03``, every ``16``, and itself. ``COUNTS_INCLUDE_THE_TRAILER`` names it.

Both are computed from the records rather than tracked alongside them, and
``tests/test_bai2.py`` recomputes them from the parsed file, so a wrong reading
shows up as the writer and the reader agreeing on something the spec does not
say rather than as a number nobody checks. #57 holds the file to a sample from
outside the project, which is where either reading gets settled.
"""
from __future__ import annotations

import datetime
from collections import namedtuple
from typing import Dict, List, Optional, Sequence

NAME = "BAI2"
VERSION = 2
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

# **Placeholders pending #57.** See the module docstring: the balance codes are
# certain and these two are not, so they are named here and nowhere else, and
# #57 replaces them against a file from outside the project.
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
# and GROUP, which is the 03 record's repeating summary of (code, amount, item
# count, funds type).
N, A, DATE, TIME, GROUP = "N", "A", "D", "T", "G"


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
        ("record code", A), ("account control total", N),
        ("number of records", N))),
    "98": ("group trailer", _fields(
        ("record code", A), ("group control total", N),
        ("number of accounts", N), ("number of records", N))),
    "99": ("file trailer", _fields(
        ("record code", A), ("file control total", N),
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


class Unreadable(ValueError):
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


def _amount(minor: int) -> str:
    """A balance in minor units. Negative carries its minus, for an overdraft."""
    return str(minor)


def _movement(minor: int) -> str:
    """The size of a movement, which is never negative.

    A transaction's direction lives in its type code, so a negative amount on a
    16 would say the opposite of what the code says *and* lower the control
    total. `_amount` allowed it because it was used for both; they are separate
    now, and this refuses rather than writing a record two readers would disagree
    about.
    """
    if minor < 0:
        raise Unreadable(
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
        raise Unreadable("no record type %r is declared" % code)
    name, fields = RECORDS[code]
    expected = len(fields) - 1                      # the record code itself
    if len(values) != expected:
        raise Unreadable(
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
    # Physical record length and block size are left blank. BAI2 allows that for
    # a variable-length delimited file, and it is the honest answer here: an
    # earlier draft wrote 80, which the 03 record already exceeds, so the file
    # would have described itself wrongly in its first line.
    lines = [_record(
        "01", sender, receiver or account["id"], _yymmdd(created_at.date()),
        _hhmm(created_at), number, "", "", VERSION)]
    lines.extend(_group(account, day, number, opening, closing, payments,
                        created_at))
    lines.append(_record("99", _control_total(lines), 1,
                         _count(lines, trailers=1)))
    return "\n".join(lines) + "\n"


def _group(account, day, number, opening, closing, payments, created_at) -> List[str]:
    """A group header, one account, and the group trailer.

    One group and one account per file: a group is a set of accounts sharing an
    as-of date and currency, and this mock writes a statement for one account at
    a time, as it does a `camt.053`. Group status 1 says the data is complete
    as of the date given.
    """
    lines = [_record("02", receiver_or_blank(account), account["id"], 1,
                     _yymmdd(day), _hhmm(created_at), account["currency"], "")]
    lines.extend(_account(account, opening, closing, payments))
    lines.append(_record("98", _control_total(lines), 1,
                         _count(lines, trailers=1)))
    return lines


def receiver_or_blank(account: Dict) -> str:
    return account.get("name", "")[:35]


def _account(account: Dict, opening: int, closing: int,
             payments: Sequence[Dict]) -> List[str]:
    """The 03, its 16s, and the 49 that counts them."""
    # The two balances, and nothing else. BAI2 allows an account to summarise
    # its movements here as well, and an earlier draft did: it put a 455 total
    # and a 165 total beside the balances. That is legal and it is a bad idea in
    # this file, because the control total sums *every* amount field, so each
    # movement would be counted twice - once in its summary and once in its own
    # 16 - and a reader reconciling the total against the entries would have to
    # know to halve it. The issue reads the same way: 010 and 015 here, the
    # movement codes on the transactions.
    summary = [OPENING_LEDGER, _amount(opening), "", AVAILABLE_NOW,
               CLOSING_LEDGER, _amount(closing), "", AVAILABLE_NOW]
    lines = [_record("03", account["iban"], account["currency"], summary)]
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
            raise Unreadable(
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
UNSAFE = (SEPARATOR, TERMINATOR, "\r", "\n", "\u2028", "\u2029", "\x0b",
          "\x0c", "\x1c", "\x1d", "\x1e", "\u0085")


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
    for bad in UNSAFE:
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
