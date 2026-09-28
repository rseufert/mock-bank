"""Accounts: what the bank holds, and what it does with a payment.

``BEHAVIOURS`` comes first because it is the single place the behaviour names
and their meaning are declared: the command line prints it, the README's
behaviour table is checked against it, ``check()`` refuses anything not in it,
and ``decide()`` dispatches on it.  The codes named are the
ISO 20022 external reason codes a real bank uses, not inventions of the mock.

Below it is the account itself - the row, what may be set on it, and what the
mock refuses to hold - because the two belong together: an account is an IBAN,
a balance and one of these behaviours.  The storage is ``db.py``; the rules
about what a valid account *is* are here.
"""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
from typing import Any, Dict, List, Optional

from . import db, schema

# name -> what the bank does. Kept in the order the README lists them.
BEHAVIOURS = {
    "accept": "accepts every payment and settles it on the requested date (ACCP)",
    "closed-account": "rejects payments to one creditor account in the pain.002 (AC04)",
    "insufficient-funds": "rejects payments once the debtor's balance would go negative (AM04)",
    "bad-bank-id": "rejects a payment whose creditor bank identifier does not resolve (RC01)",
    "return-later": "accepts and settles, then returns the payment N business days later in a pacs.004 (AC04 or MD07)",
    "reject-file": "rejects the whole file at group level (RJCT, FF01)",
    "duplicate-file": "rejects a file whose MsgId it has already seen (DUPL)",
    "silent": "sends no pain.002 at all",
    "statement-gap": "leaves one settled entry off the camt.053",
}

DEFAULT_BEHAVIOUR = "accept"

# ---------------------------------------------------------------------------
# The rows themselves
# ---------------------------------------------------------------------------
#
# An account is a row: an IBAN an arriving payment names, a balance in minor
# units, and a behaviour. The behaviour is the reason the project exists -
# anyone can stand up something that accepts a well-formed `pain.001`; what is
# hard to get hold of is a bank that rejects one payment out of four for AC04,
# or lets a file through and returns it on Thursday. Changed at runtime through
# `PATCH /_mock/accounts/<id>`, so a test reproduces a specific failure without
# restarting anything.

# Every field a caller may set, with the default a new account takes. Anything
# not named here is refused rather than dropped: a PATCH that silently ignores
# a misspelled field sends whoever wrote it looking for the bug somewhere else
# entirely.
FIELDS = {
    "name": "",
    "iban": "",
    "bic": "",
    "currency": "EUR",
    "balance": 0,
    "behaviour": DEFAULT_BEHAVIOUR,
    "parameters": {},
    "closed": 0,
    "format": "iso20022",
    "account_number": "",
}

# What the bank can write for an account (#53): ISO 20022 throughout, or a
# NACHA account, which is sent a plain acknowledgement where the other gets a
# pain.002. Statements stay camt.053 until BAI2.
FORMATS = ("iso20022", "nacha")

# The bank's ABA routing number, which a NACHA file names it by. Fictional on
# purpose: it passes the check digit, and no Federal Reserve district starts
# with 99, so it cannot be mistaken for a real bank's.
ROUTING = "999999992"

# A US domestic account number: digits, at most the 17 a NACHA entry holds.
ACCOUNT_NUMBER_PATTERN = re.compile(r"^\d{1,17}$")

# The fields that are text. A JSON client can plausibly send a number, an
# object or a list for any of them, and every one of those used to reach
# sqlite3 and come back as a 500 with a traceback - which tells the caller
# nothing about which field they got wrong.
TEXT_FIELDS = ("name", "iban", "bic", "currency", "behaviour", "format",
               "account_number")

# 4 letters of institution, 2 of country, 2 of location, and an optional
# 3-character branch: ISO 9362. The mock checks the shape, not whether the
# institution exists - it has no registry and will not pretend to one. That is
# the whole point of the `bad-bank-id` behaviour: the shape is right and the
# bank is not there, and only the account's behaviour says so.
BIC_PATTERN = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?$")

CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")

# An id goes into a URL (`/_mock/accounts/<id>`), so it may not carry a
# delimiter: letters, digits, and `.`, `-` or `_` between them.
ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


class UnknownAccount(KeyError):
    """An id that no account has. Answered 404 rather than created silently."""


class Invalid(ValueError):
    """An account field the mock would not be able to act on, and why."""


def check(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalise the fields of an account.

    Returns them as the database should hold them - `parameters` as JSON text,
    `balance` and `closed` as integers - or raises `Invalid` naming what is
    wrong and, where there is a fixed set, what would have been accepted.
    """
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise Invalid("unknown field%s %s; an account has: %s"
                      % ("s" if len(unknown) > 1 else "",
                         ", ".join(repr(u) for u in unknown),
                         ", ".join(sorted(FIELDS))))

    out = dict(fields)

    for field in TEXT_FIELDS:
        if field in out and not isinstance(out[field], str):
            raise Invalid("%s has to be text, not %s (%r)"
                          % (field, type(out[field]).__name__, out[field]))

    if "behaviour" in out and out["behaviour"] not in BEHAVIOURS:
        raise Invalid("unknown behaviour %r; the %d this mock has are: %s"
                      % (out["behaviour"], len(BEHAVIOURS),
                         ", ".join(sorted(BEHAVIOURS))))

    if "iban" in out:
        value = str(out["iban"]).replace(" ", "").upper()
        if not schema.iban_is_valid(value):
            raise Invalid(
                "iban %r is not one an arriving payment could be matched on: "
                "two letters of country, two check digits, then up to 30 "
                "letters and digits, and the check digits have to agree with "
                "the rest (ISO 13616)" % out["iban"])
        out["iban"] = value

    if "bic" in out and str(out["bic"]):
        value = str(out["bic"]).upper()
        if not BIC_PATTERN.match(value):
            raise Invalid(
                "bic %r is not the shape ISO 9362 gives: eight or eleven "
                "characters, as in MOCKDEFF or MOCKDEFFXXX" % out["bic"])
        out["bic"] = value

    if "currency" in out:
        value = str(out["currency"]).upper()
        if not CURRENCY_PATTERN.match(value):
            raise Invalid("currency %r is not a three-letter ISO 4217 code, "
                          "as in EUR or USD" % out["currency"])
        out["currency"] = value

    if "format" in out and out["format"] not in FORMATS:
        raise Invalid("format %r is not one the bank writes; it is one of: %s"
                      % (out["format"], ", ".join(FORMATS)))

    if out.get("account_number") and not ACCOUNT_NUMBER_PATTERN.match(out["account_number"]):
        raise Invalid("account_number %r is not a domestic account number: up to "
                      "17 digits, as a NACHA entry holds it, or \"\" for none"
                      % out["account_number"])

    if "balance" in out:
        out["balance"] = _minor_units(out["balance"])

    if "closed" in out:
        out["closed"] = _flag("closed", out["closed"])

    if "parameters" in out:
        value = out["parameters"]
        if not isinstance(value, dict):
            raise Invalid("parameters is a JSON object of what the behaviour "
                          "needs, as in {\"days\": 3}, not %r" % (value,))
        out["parameters"] = json.dumps(value, sort_keys=True)

    return out


def _minor_units(value: Any) -> int:
    """A balance, as the whole number of minor units it has to be.

    `12.50` is refused rather than rounded, and so is `"12.50"`. A mock that
    quietly took a float would be one cent out on some statement eventually,
    and the statement not reconciling is the failure this project exists to
    rehearse - it must not be able to come from here.
    """
    if isinstance(value, bool):
        raise Invalid("balance is a whole number of minor units, not %r" % value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if re.match(r"^-?\d+$", text):
            return int(text)
    raise Invalid(
        "balance is in minor units - a whole number of cents - so 12.50 EUR is "
        "1250, not %r" % (value,))


def _flag(name: str, value: Any) -> int:
    """`closed` is a flag: what a JSON client plausibly sends, and not prose."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    if isinstance(value, str) and value.strip() in ("0", "1"):
        return int(value.strip())
    raise Invalid("%s is a flag: true or false, 1 or 0, not %r" % (name, value))


def row(record: Dict[str, Any]) -> Dict[str, Any]:
    """One account as the control plane serves it.

    `parameters` comes back as the object it was sent as rather than the text it
    is stored as, and `closed` as a boolean, because a client that has to parse
    a nested JSON string out of a JSON field is being made to do the mock's job.
    """
    out = dict(record)
    out["parameters"] = json.loads(out.get("parameters") or "{}")
    out["closed"] = bool(out.get("closed"))
    return out


def get(conn, identifier: str) -> Optional[Dict[str, Any]]:
    found = db.one(conn, "SELECT * FROM account WHERE id = ?", (identifier,))
    return row(found) if found else None


def require(conn, identifier: str) -> Dict[str, Any]:
    found = get(conn, identifier)
    if found is None:
        raise UnknownAccount(identifier)
    return found


def listing(conn) -> List[Dict[str, Any]]:
    return [row(r) for r in db.rows(conn, "SELECT * FROM account ORDER BY id")]


def by_iban(conn, value: str) -> Optional[Dict[str, Any]]:
    """The account an arriving payment names, or None.

    The lookup the pipeline uses: a payment file carries IBANs, never the
    mock's own ids.
    """
    found = db.one(conn, "SELECT * FROM account WHERE iban = ?",
                   ((value or "").replace(" ", "").upper(),))
    return row(found) if found else None


def by_account_number(conn, value: str) -> Optional[Dict[str, Any]]:
    """The account with this domestic account number, or None; never for ""."""
    if not value:
        return None
    found = db.one(conn, "SELECT * FROM account WHERE account_number = ?", (value,))
    return row(found) if found else None


def _number_taken(number: str, other: Dict[str, Any]) -> str:
    return ("account_number %s is already account %r; two accounts with one "
            "number would leave a NACHA payment matching either" % (number, other["id"]))


def resolve(conn, payment_file) -> None:
    """Name the accounts the bank holds by IBAN, whatever the file named them by.

    The one step between reading a file and deciding it (#53). `decide` looks
    accounts up by IBAN, and a NACHA file has none: it names a creditor by
    routing and account number, and the account paying by its company
    identification. A `pain.001` can do the same, with `Othr/Id` and
    `ClrSysMmbId`. So:

    - a creditor whose bank is this one (`ROUTING`) and whose account number
      is a held account's is that account;
    - a debtor account whose identifier is a held account's number is that
      account.

    Anything else is left as it was, which already means what it should: a
    creditor at another bank, or a debtor the bank does not hold (`AC02`).
    Changes the model in place; nothing is stored here.
    """
    for batch in (payment_file.batches if payment_file else []):
        if batch.debtor_account and by_iban(conn, batch.debtor_account) is None:
            held = by_account_number(conn, batch.debtor_account)
            if held is not None:
                batch.debtor_account = held["iban"]
        for payment in batch.payments:
            if (payment.creditor_clearing_id == ROUTING and payment.creditor_account
                    and by_iban(conn, payment.creditor_account) is None):
                held = by_account_number(conn, payment.creditor_account)
                if held is not None:
                    payment.creditor_account = held["iban"]


def create(conn, identifier: str, **fields: Any) -> Dict[str, Any]:
    """Register an account, refusing anything the mock could not then act on."""
    _check_id(identifier)
    if get(conn, identifier) is not None:
        raise Invalid("there is already an account %r; PATCH it instead"
                      % identifier)
    if "iban" in fields and not isinstance(fields["iban"], str):
        raise Invalid("iban has to be text, not %s (%r)"
                      % (type(fields["iban"]).__name__, fields["iban"]))
    if not fields.get("iban", ""):
        raise Invalid("an account needs an iban: it is what an arriving "
                      "payment names, and one without it can never be matched")
    checked = check(fields)
    check_parameters(checked.get("behaviour", DEFAULT_BEHAVIOUR),
                     json.loads(checked.get("parameters", "{}")))

    columns = dict(FIELDS)
    columns["parameters"] = json.dumps({})
    columns.update(checked)
    columns["name"] = columns["name"] or identifier

    keys = ["id"] + sorted(columns)
    values = [identifier] + [columns[key] for key in sorted(columns)]
    try:
        conn.execute("INSERT INTO account (%s) VALUES (%s)"
                     % (", ".join(keys), ", ".join("?" * len(keys))), values)
    except sqlite3.IntegrityError:
        conn.rollback()
        clash = by_account_number(conn, columns["account_number"])
        if clash is not None:
            raise Invalid(_number_taken(columns["account_number"], clash)) from None
        raise Invalid("iban %s is already account %r; two accounts with one "
                      "IBAN would leave an arriving payment matching either"
                      % (columns["iban"],
                         (by_iban(conn, columns["iban"]) or {}).get("id"))) from None
    conn.commit()
    return require(conn, identifier)


def update(conn, identifier: str, **fields: Any) -> Dict[str, Any]:
    """Change an account, refusing anything the mock could not then act on.

    An id cannot be changed: it is what the control plane's URLs are built on.
    An IBAN can, because a tester moving an account onto the IBAN their own
    test data uses is the ordinary case.
    """
    existing = require(conn, identifier)
    if not fields:
        return existing
    changes = check(fields)
    check_parameters(changes.get("behaviour", existing["behaviour"]),
                     json.loads(changes["parameters"]) if "parameters" in changes
                     else existing["parameters"])
    try:
        conn.execute("UPDATE account SET %s WHERE id = ?"
                     % ", ".join("%s = ?" % key for key in changes),
                     list(changes.values()) + [identifier])
    except sqlite3.IntegrityError:
        conn.rollback()
        clash = by_account_number(conn, changes.get("account_number", ""))
        if clash is not None and clash["id"] != identifier:
            raise Invalid(_number_taken(changes["account_number"], clash)) from None
        raise Invalid("iban %s belongs to another account already"
                      % changes.get("iban")) from None
    conn.commit()
    return require(conn, identifier)


# What each behaviour reads from `parameters`, with its default. Only
# `return-later` takes any; a parameter it does not know is refused by name,
# so a misspelt `dyas` cannot silently mean three days.
RETURN_LATER = {"days": 3, "reason": "AC04", "end_to_end_id": None}


def check_parameters(behaviour: str, parameters: Dict[str, Any]) -> None:
    """Refuse parameters the behaviour could not act on, naming what it takes."""
    if behaviour != "return-later":
        return
    unknown = sorted(set(parameters) - set(RETURN_LATER))
    if unknown:
        raise Invalid("return-later takes %s in parameters, not %s"
                      % (", ".join(sorted(RETURN_LATER)), ", ".join(unknown)))
    days = parameters.get("days", RETURN_LATER["days"])
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 60:
        raise Invalid("return-later's days is a whole number of business days from "
                      "0 to 60, not %r" % (days,))
    reason = parameters.get("reason", RETURN_LATER["reason"])
    codes = schema.CODE_SETS["ExternalReturnReason1Code"]
    if reason not in codes:
        raise Invalid("return-later's reason is an ISO 20022 return reason code, one "
                      "of %s; %r is not" % (", ".join(sorted(codes)), reason))
    e2e = parameters.get("end_to_end_id")
    if e2e is not None and (not isinstance(e2e, str) or not e2e):
        raise Invalid("return-later's end_to_end_id is the EndToEndId of the one "
                      "payment to return, or left out to return every payment")


def _check_id(identifier: str) -> None:
    if not isinstance(identifier, str):
        raise Invalid("an account's id has to be text, not %s (%r): it becomes "
                      "a path in the control plane's URLs"
                      % (type(identifier).__name__, identifier))
    if not identifier.strip():
        raise Invalid("an account needs an id")
    if not ID_PATTERN.match(identifier):
        raise Invalid("id %r may use only letters, digits, and '.', '-' or '_' "
                      "between them: it becomes a path in the control plane's "
                      "URLs" % identifier)


def totals(conn) -> Dict[str, int]:
    """What the mock holds, per currency, in minor units.

    Per currency rather than one number, because a total across currencies is
    a number that means nothing the moment somebody creates a USD account.
    """
    return {r["currency"]: int(r["total"]) for r in db.rows(
        conn, "SELECT currency, SUM(balance) AS total FROM account"
              " GROUP BY currency ORDER BY currency")}


# ---------------------------------------------------------------------------
# Deciding and booking
# ---------------------------------------------------------------------------

# A behaviour describes the account it is set on. These describe an account
# as a debtor, and are read from the debtor account of the batch...
DEBTOR_SIDE = ("accept", "insufficient-funds", "reject-file", "duplicate-file",
               "silent", "statement-gap", "return-later")
# ...and these as a creditor, read from the creditor account when the bank
# holds it. A creditor it does not hold is another bank's customer, and
# nothing about it can be known at acceptance: its payment settles.
CREDITOR_SIDE = ("closed-account", "bad-bank-id")

ACCEPTED, REJECTED = "accepted", "rejected"


class PaymentDecision:
    """What the bank decided about one payment."""

    def __init__(self, payment, batch, account, outcome, reason=None, reason_text=None,
                 settlement_date=None):
        self.payment = payment
        self.batch = batch
        self.account = account          # the held debtor account, or None
        self.outcome = outcome
        self.reason = reason
        self.reason_text = reason_text
        self.settlement_date = settlement_date

    def to_json(self):
        return {"end_to_end_id": self.payment.end_to_end_id,
                "pmt_inf_id": self.batch.pmt_inf_id,
                "outcome": self.outcome, "reason": self.reason,
                "reason_text": self.reason_text,
                "amount": self.payment.amount, "currency": self.payment.currency,
                "settlement_date": (self.settlement_date.isoformat()
                                    if self.settlement_date else None)}


class Decision:
    """What the bank decided about a file: a group status and, unless the
    file was rejected outright, one PaymentDecision per payment."""

    def __init__(self, payment_file, status, reason=None, reason_text=None,
                 payments=(), reported=True):
        self.payment_file = payment_file
        self.msg_id = payment_file.msg_id if payment_file else None
        self.status = status            # ACCP, PART or RJCT
        self.reason = reason            # set only when rejected outright
        self.reason_text = reason_text
        self.payments = list(payments)
        self.reported = reported

    @property
    def rejected_outright(self):
        return self.status == "RJCT" and self.reason is not None

    @property
    def accepted(self):
        return [d for d in self.payments if d.outcome == ACCEPTED]

    @property
    def rejected(self):
        return [d for d in self.payments if d.outcome == REJECTED]


def decide(payment_file, findings, conn, clock, received_at, allow_duplicates=False):
    """Decide a payment file. Reads the accounts; changes nothing.

    Each accepted payment settles on ``clock.settlement_date(received_at,
    requested)``: the later of the requested execution date and the day the
    bank can start on (today before the cutoff, the next business day at or
    after it), rolled past weekends and holidays. A past requested date
    therefore settles as soon as the bank can, as the validator's DT01
    warning says.

    A behaviour describes the account it is set on: ``DEBTOR_SIDE`` ones are
    read from the batch's debtor account, ``CREDITOR_SIDE`` ones from the
    creditor account when the bank holds it. The first rule that applies
    wins, in this order:

    1. A file-level finding rejects the whole file, whatever the behaviour:
       a file the mock could not read, any structural (``FF01``) finding, and
       any error outside a payment (a header's ``AM10``/``AM18``, a debtor
       IBAN's ``AC01``), with that finding's code.
    2. A ``MsgId`` the bank has received before rejects the file with
       ``DUPL``, as real banks do whatever you ask; ``--allow-duplicates``
       turns this off. The ``duplicate-file`` behaviour is therefore the
       default already and does nothing more.
    3. A debtor account with ``reject-file`` rejects the file: ``RJCT``,
       ``FF01``.
    4. Per payment, in file order:

       a. a debtor account the bank does not hold: ``AC02``; one it holds
          that is closed: ``AC04``;
       b. a payment-level finding: its code (``AC01``, ``AM03``, ``AM05``);
       c. a payment in another currency than the held debtor account: ``AM03``;
       d. a held creditor account that is closed or ``closed-account``: ``AC04``;
       e. a held creditor account that is ``bad-bank-id``: ``RC01``;
       f. a debtor account with ``insufficient-funds``: ``AM04`` for each
          payment that would take the available balance (the balance less
          what is accepted and not yet booked) below zero, accepting later
          smaller ones that fit. It is the only behaviour that looks at the
          balance; under every other one a payment books even below zero, as
          on an account with an overdraft.

    5. A debtor account with ``silent``: decided and booked like any other,
       but marked unreported, so no ``pain.002`` is sent (#7).
    6. ``accept``, ``statement-gap`` and ``return-later`` accept here; their
       effect is on the statement (#9) and the returns (#14).

    Rules 3 and 5 are about the file, so they apply when any debtor account
    in the file carries the behaviour.
    """
    errors = [f for f in findings if f.level == "error"]
    if payment_file is None:
        first = errors[0] if errors else None
        return Decision(None, "RJCT", first.code if first else schema.STRUCTURAL,
                        first.text if first else "the file could not be read")

    payments = payment_file.payments
    outside = [f for f in errors
               if f.code == schema.STRUCTURAL or not any(_under(f.path, p.path) for p in payments)]
    if outside:
        return Decision(payment_file, "RJCT", outside[0].code,
                        "%s: %s" % (outside[0].path, outside[0].text))

    if not allow_duplicates and payment_file.msg_id and db.one(
            conn, "SELECT id FROM file WHERE msg_id = ?", (payment_file.msg_id,)):
        return Decision(payment_file, "RJCT", "DUPL",
                        "MsgId %s has been received before" % payment_file.msg_id)

    debtors = {id(batch): by_iban(conn, batch.debtor_account or "")
               for batch in payment_file.batches}
    behaviours = {a["behaviour"] for a in debtors.values() if a}
    if "reject-file" in behaviours:
        return Decision(payment_file, "RJCT", "FF01",
                        "the debtor account's behaviour is reject-file")

    available = {}
    decided = []
    for batch in payment_file.batches:
        debtor = debtors[id(batch)]
        when = clock.settlement_date(received_at, batch.requested_execution_date)
        for payment in batch.payments:
            reason, text = _payment_reason(conn, debtor, batch, payment, errors, available)
            if reason:
                decided.append(PaymentDecision(payment, batch, debtor, REJECTED, reason, text))
            else:
                decided.append(PaymentDecision(payment, batch, debtor, ACCEPTED,
                                               settlement_date=when))
    accepted = sum(1 for d in decided if d.outcome == ACCEPTED)
    status = "ACCP" if accepted == len(decided) else ("RJCT" if not accepted else "PART")
    return Decision(payment_file, status, payments=decided,
                    reported="silent" not in behaviours)


def _under(path, ancestor):
    return path == ancestor or path.startswith(ancestor + "/")


def _payment_reason(conn, debtor, batch, payment, errors, available):
    """The code and prose a payment is rejected with, or (None, None)."""
    if debtor is None:
        held = ", ".join(a["iban"] for a in listing(conn))
        return "AC02", ("the debtor account %s is not one this bank holds; it holds %s"
                        % (batch.debtor_account or "(none)", held))
    if debtor["closed"]:
        return "AC04", "the debtor account %s is closed" % debtor["iban"]
    for finding in errors:
        if _under(finding.path, payment.path):
            return finding.code, finding.text
    if payment.currency != debtor["currency"]:
        return "AM03", ("the amount is in %s but the debtor account %s is held in %s"
                        % (payment.currency, debtor["iban"], debtor["currency"]))
    creditor = by_iban(conn, payment.creditor_account or "")
    if creditor and (creditor["closed"] or creditor["behaviour"] == "closed-account"):
        return "AC04", "the creditor account %s is closed" % creditor["iban"]
    if creditor and creditor["behaviour"] == "bad-bank-id":
        return "RC01", ("the creditor account %s's bank identifier does not resolve"
                        % creditor["iban"])
    if debtor["behaviour"] == "insufficient-funds":
        if debtor["id"] not in available:
            available[debtor["id"]] = debtor["balance"] - _pending(conn, debtor["id"])
        if payment.amount > available[debtor["id"]]:
            return "AM04", ("%s would take the debtor account below zero; %s is available"
                            % (schema.format_amount(payment.amount, payment.currency),
                               schema.format_amount(max(available[debtor["id"]], 0),
                                                    debtor["currency"])))
        available[debtor["id"]] -= payment.amount
    return None, None


def _pending(conn, account_id):
    """What is accepted on an account and not yet booked, in minor units."""
    found = db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM payment"
                         " WHERE account_id = ? AND status = ? AND booked_at IS NULL",
                   (account_id, ACCEPTED))
    return int(found["total"])


def book(conn, decision):
    """Record a decided file and its payments.

    Booking the debits is ``book_due``'s, which ``outbox.release_due`` calls
    straight after, so the ``camt.054`` for what books is written in the same
    step. A file the mock could not read far enough to have a MsgId is not
    recorded: there is nothing a duplicate check could match it on. Returns
    the file's row id, or None.
    """
    if decision.msg_id is None:
        return None
    received = db.now()
    cursor = conn.execute(
        "INSERT INTO file (msg_id, message, received_at, status, reason, reported)"
        " VALUES (?,?,?,?,?,?)",
        (decision.msg_id, decision.payment_file.message, received, decision.status,
         decision.reason, int(decision.reported)))
    file_id = cursor.lastrowid
    for d in decision.payments:
        p = d.payment
        conn.execute(
            "INSERT INTO payment (file_id, pmt_inf_id, end_to_end_id, instruction_id,"
            " account_id, debtor_iban, amount, currency, creditor_name, creditor_iban,"
            " creditor_bic, creditor_clearing_id, status, reason, reason_text,"
            " settlement_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (file_id, d.batch.pmt_inf_id, p.end_to_end_id, p.instruction_id,
             d.account["id"] if d.account else None, d.batch.debtor_account,
             p.amount, p.currency, p.creditor_name, p.creditor_account, p.creditor_bic,
             getattr(p, "creditor_clearing_id", None),
             d.outcome, d.reason, d.reason_text,
             d.settlement_date.isoformat() if d.settlement_date else None))
    conn.commit()
    return file_id


def book_due(conn, today, commit=True):
    """Debit every accepted payment whose settlement date has come.

    ``outbox.release_due`` calls it when a file arrives and as the clock
    moves. Returns the payments booked, oldest first.
    """
    due = db.rows(conn, "SELECT * FROM payment WHERE status = ? AND booked_at IS NULL"
                        " AND settlement_date <= ? AND account_id IS NOT NULL ORDER BY id",
                  (ACCEPTED, today.isoformat()))
    now = db.now()
    for row_ in due:
        conn.execute("UPDATE account SET balance = balance - ? WHERE id = ?",
                     (row_["amount"], row_["account_id"]))
        conn.execute("UPDATE payment SET booked_at = ? WHERE id = ?", (now, row_["id"]))
        row_["booked_at"] = now
    if commit:
        conn.commit()
    return due


RETURNED = "returned"


def schedule_returns(conn, booked, clock):
    """Set the day each just-booked payment from a `return-later` account comes
    back: `days` business days after it settled, with `reason`. Only the payment
    named by `end_to_end_id`, if the account names one; otherwise every one."""
    scheduled = []
    for row_ in booked:
        account = get(conn, row_["account_id"])
        if account is None or account["behaviour"] != "return-later":
            continue
        wanted = dict(RETURN_LATER, **account["parameters"])
        if wanted["end_to_end_id"] and wanted["end_to_end_id"] != row_["end_to_end_id"]:
            continue
        due = clock.business_days_after(
            datetime.date.fromisoformat(row_["settlement_date"]), wanted["days"])
        conn.execute("UPDATE payment SET return_due = ?, return_reason = ? WHERE id = ?",
                     (due.isoformat(), wanted["reason"], row_["id"]))
        scheduled.append(row_["id"])
    return scheduled


def book_returns(conn, today):
    """Credit back every payment whose return day has come, and mark it
    returned. Returns those payments, oldest first, with their file's MsgId."""
    due = db.rows(conn, "SELECT payment.*, file.msg_id, file.message FROM payment"
                        " JOIN file ON file.id = payment.file_id"
                        " WHERE payment.status = ? AND booked_at IS NOT NULL"
                        " AND return_due IS NOT NULL AND return_due <= ?"
                        " AND returned_at IS NULL ORDER BY payment.id",
                  (ACCEPTED, today.isoformat()))
    now = db.now()
    for row_ in due:
        conn.execute("UPDATE account SET balance = balance + ? WHERE id = ?",
                     (row_["amount"], row_["account_id"]))
        conn.execute("UPDATE payment SET status = ?, returned_at = ? WHERE id = ?",
                     (RETURNED, now, row_["id"]))
        row_["status"], row_["returned_at"] = RETURNED, now
    return due


def payments(conn, end_to_end_id=None):
    """Every payment the bank decided on, newest first, or those with one
    EndToEndId (which is unique within a file, not across files)."""
    sql = ("SELECT payment.*, file.msg_id FROM payment JOIN file ON file.id = payment.file_id")
    if end_to_end_id is None:
        return db.rows(conn, sql + " ORDER BY payment.id DESC")
    return db.rows(conn, sql + " WHERE end_to_end_id = ? ORDER BY payment.id DESC",
                   (end_to_end_id,))


def payment_counts(conn):
    """What /_mock/state reports about payments."""
    out = {"files": db.count(conn, "file")}
    for status in (ACCEPTED, REJECTED, RETURNED):
        out[status] = db.count(conn, "payment", "status = '%s'" % status)
    out["booked"] = db.count(conn, "payment", "booked_at IS NOT NULL")
    return out
