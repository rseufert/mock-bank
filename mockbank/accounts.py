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

from . import db, messages, nacha, schema

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


# The largest balance, either side of zero, that a statement can write: a camt
# amount has 18 digits, and in minor units that is every digit (#106). Past it
# every statement for the account would be refused and the clock would answer
# 500, so a balance is kept inside it where it is set and where a debit books.
MAX_BALANCE = 10 ** 18 - 1


def still_to_arrive(conn, account_id: str) -> int:
    """What will be credited to an account without anyone asking again, in minor
    units: payments due back under ``return-later``, money arriving (#91) that
    has not booked yet, and collections accepted and not yet settled (#131). A
    balance is only safe if it has room for these.

    A payment that was never debited will never be credited back, however its
    return is answered, so it reserves nothing. A NACHA account's rejections are
    given a return day too (#54, option (a)), and counting those reserved room
    for money that was not coming (#144).
    """
    returns = db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM payment"
                           " WHERE account_id = ? AND return_due IS NOT NULL"
                           " AND booked_at IS NOT NULL"
                           " AND returned_at IS NULL", (account_id,))["total"]
    credits = db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM credit"
                           " WHERE account_id = ? AND booked_at IS NULL",
                     (account_id,))["total"]
    collections = db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM collection"
                               " WHERE account_id = ? AND status = ? AND booked_at IS NULL",
                         (account_id, ACCEPTED))["total"]
    return int(returns) + int(credits) + int(collections)


def _minor_units(value: Any) -> int:
    """A balance, as the whole number of minor units it has to be.

    `12.50` is refused rather than rounded, and so is `"12.50"`. A mock that
    quietly took a float would be one cent out on some statement eventually,
    and the statement not reconciling is the failure this project exists to
    rehearse - it must not be able to come from here.
    """
    if isinstance(value, bool):
        raise Invalid("balance is a whole number of minor units, not %r" % value)
    number = None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.match(r"^-?\d+$", value.strip()):
        number = int(value.strip())
    if number is not None:
        if abs(number) > MAX_BALANCE:
            raise Invalid("balance %d is more than the 18 digits a statement can "
                          "write, either side of zero" % number)
        return number
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
    if isinstance(payment_file, messages.CollectionFile):
        # The mirror (#131, #176): the batch names the creditor account, and
        # each collection a debtor by routing and account number.
        for batch in payment_file.batches:
            if batch.creditor_account and by_iban(conn, batch.creditor_account) is None:
                held = by_account_number(conn, batch.creditor_account)
                if held is not None:
                    batch.creditor_account = held["iban"]
            for collection in batch.collections:
                if (getattr(collection, "debtor_clearing_id", None) == ROUTING
                        and collection.debtor_account
                        and by_iban(conn, collection.debtor_account) is None):
                    held = by_account_number(conn, collection.debtor_account)
                    if held is not None:
                        collection.debtor_account = held["iban"]
        return
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
                     json.loads(checked.get("parameters", "{}")),
                     checked.get("format", FIELDS["format"]))

    columns = dict(FIELDS)
    columns["parameters"] = json.dumps({})
    columns.update(checked)
    columns["name"] = columns["name"] or identifier
    # The same trial `update` does (#166 part 1), because `create` is the other
    # door onto the same rows and did not do it: a name of only spaces is truthy,
    # so it survived the fallback above and every message for the account was then
    # unwritable. Found while doing part 2 - `POST /_mock/accounts` with
    # `"name": "   "` made every later advance a 500, with no payment involved.
    try:
        messages.write_camt054(dict(columns, id=identifier), [], _PROBE_DAY,
                               "MB-C054-CHECK", _PROBE_AT)
    except ValueError as error:
        raise Invalid("the bank could not write its own messages for this "
                      "account: %s" % error) from None

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
    if "balance" in changes:
        # Returns and credits still to come are booked on top of whatever is
        # set now, so the balance they will make has to fit as well (#106).
        coming = still_to_arrive(conn, identifier)
        if changes["balance"] + coming > MAX_BALANCE:
            raise Invalid("balance %d and the %d still to arrive in the account - "
                          "returns due back and credits waiting to book - would be "
                          "more than the 18 digits a statement can write"
                          % (changes["balance"], coming))
    # Checked against the format it will have, so that switching an account to
    # NACHA with an ISO 20022 return reason already set is refused too (#54).
    check_parameters(changes.get("behaviour", existing["behaviour"]),
                     json.loads(changes["parameters"]) if "parameters" in changes
                     else existing["parameters"],
                     changes.get("format", existing["format"]))
    becoming = changes.get("format", existing["format"])
    if becoming != existing["format"]:
        # The reason a return comes back with is stored on the payment when it
        # books, so changing the format afterwards does not change it - and the
        # release cannot write a `pacs.004` carrying a NACHA code, or an ACH
        # return carrying an ISO one. Before #166 the switch was taken, and the
        # bank then answered 500 to every advance and every mailbox read from the
        # day the return fell due until it was reset.
        stuck = scheduled_returns(conn, identifier, becoming)
        if stuck:
            due = sorted(row["return_due"] for row in stuck)
            raise Invalid(
                "%d return%s already scheduled on this account carr%s a reason "
                "only %s can write (%s), the last due %s; the format cannot "
                "change under them - let them come back first, or reset the mock"
                % (len(stuck), "s" if len(stuck) > 1 else "",
                   "y" if len(stuck) > 1 else "ies", existing["format"],
                   ", ".join(sorted({row["return_reason"] for row in stuck})),
                   due[-1]))
    # Every message the bank writes for this account carries its name, so a name
    # the writers cannot carry is refused here rather than found on the release
    # path, where it makes every advance and every mailbox read a 500 until the
    # mock is reset (#166 case a). The same shape as `credits.create`: write it
    # once, now, the way the release will, and refuse what the writer refuses. A
    # `camt.054` with no entries is the cheapest message that carries the account
    # and nothing else, so this says nothing about any payment.
    candidate = dict(existing)
    candidate.update({key: value for key, value in changes.items()
                      if key != "parameters"})
    try:
        messages.write_camt054(candidate, [], _PROBE_DAY, "MB-C054-CHECK", _PROBE_AT)
    except ValueError as error:
        raise Invalid("the bank could not write its own messages for this "
                      "account: %s" % error) from None
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

# How much of an `InstrId` a NACHA return can carry back: the original entry
# trace number's own width, read off the declaration rather than written out, so
# the two cannot disagree (#166 case c).
NACHA_TRACE_WIDTH = next(field.width for field in nacha.RETURN_ADDENDA[1]
                         if field.name == "original entry trace number")

# A NACHA account's return reason is an R code, and closed is the default, as
# AC04 is for an ISO 20022 account (#54).
NACHA_RETURN_REASON = "R02"


def return_reason(parameters: Dict[str, Any], account_format: str) -> str:
    """The reason a return-later payment comes back with, for this format."""
    default = NACHA_RETURN_REASON if account_format == "nacha" else RETURN_LATER["reason"]
    return parameters.get("reason", default)


# A fixed moment for the trial write in `update`. The probe asks whether this
# account's own fields can be written into a message, and nothing about that
# depends on when it is asked.
_PROBE_DAY = datetime.date(2000, 1, 1)
_PROBE_AT = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)


def reason_fits(reason: str, account_format: str) -> bool:
    """Whether a return reason is one this format's return message can carry.

    One test for two callers: `check_parameters`, which refuses a reason the
    account is given, and `update`, which refuses a format change that would
    leave an already-scheduled return unwritable (#166 case f).
    """
    if account_format == "nacha":
        return reason in nacha.RETURN_REASONS
    return reason in schema.CODE_SETS["ExternalReturnReason1Code"]


def scheduled_returns(conn, identifier: str, account_format: str) -> List[Dict[str, Any]]:
    """Returns and collection returns waiting that `account_format` cannot write.

    A return's reason is stored on the row when it books, not read from the
    account when it goes out, so changing the format afterwards leaves the stored
    reason behind - and a `pacs.004` carrying a NACHA `R02`, or an ACH return
    carrying `AC04`, cannot be written at all (#166 case f). Only returns that
    have not gone back yet matter: one already returned has been written.
    """
    waiting = db.rows(conn, "SELECT return_due, return_reason FROM payment"
                            " WHERE account_id = ? AND return_due IS NOT NULL"
                            " AND returned_at IS NULL", (identifier,))
    stuck = [row for row in waiting if row["return_reason"]
             and not reason_fits(row["return_reason"], account_format)]
    # A collection on its way back is in the same position (#176): its reason
    # is an R code or an ISO 20022 one by the format the account had when it
    # was set. A debit comes back with more R codes than a credit does.
    fits = (nacha.DEBIT_RETURN_REASONS if account_format == "nacha"
            else schema.CODE_SETS["ExternalReturnReason1Code"])
    collected = db.rows(conn, "SELECT return_due, return_reason FROM collection"
                              " WHERE account_id = ? AND return_due IS NOT NULL"
                              " AND returned_at IS NULL", (identifier,))
    return stuck + [row for row in collected if row["return_reason"]
                    and row["return_reason"] not in fits]


def check_parameters(behaviour: str, parameters: Dict[str, Any],
                     account_format: str = "iso20022") -> None:
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
    reason = return_reason(parameters, account_format)
    if not reason_fits(reason, account_format):
        if account_format == "nacha":
            raise Invalid("return-later's reason on a NACHA account is a NACHA return "
                          "code, one of %s; %r is not" % (", ".join(sorted(
                              nacha.RETURN_REASONS)), reason))
        codes = schema.CODE_SETS["ExternalReturnReason1Code"]
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
       c. on a NACHA account, an ``InstrId`` longer than the return addenda's
          original entry trace number: ``FF01``, because a return could not then
          be written for it (#166);
       d. a payment in another currency than the held debtor account: ``AM03``;
       e. a held creditor account that is closed or ``closed-account``: ``AC04``;
       f. a held creditor account that is ``bad-bank-id``: ``RC01``;
       g. a debtor account with ``insufficient-funds``: ``AM04`` for each
          payment that would take the available balance (the balance less
          what is accepted and not yet booked) below zero, accepting later
          smaller ones that fit. It is the only behaviour that looks at the
          balance; under every other one a payment books even below zero, as
          on an account with an overdraft;
       h. under any behaviour, a payment that would overdraw the account past
          the 18 digits a statement can write (``MAX_BALANCE``): ``AM02`` (#106).

    5. A debtor account with ``silent``: decided and booked like any other,
       but marked unreported, so no ``pain.002`` is sent (#7).
    6. ``accept``, ``statement-gap`` and ``return-later`` accept here; their
       effect is on the statement (#9) and the returns (#14).

    Rules 3 and 5 are about the file, so they apply when any debtor account
    in the file carries the behaviour.
    """
    errors = [f for f in findings if f.level == "error"]
    outright = rejected_outright(payment_file, errors, conn, allow_duplicates)
    if outright:
        return outright

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


def rejected_outright(payment_file, errors, conn, allow_duplicates=False):
    """Rules 1 and 2 of ``decide``, which no account is asked about: the
    ``Decision`` that rejects the whole file, or None. A file of collections
    (#131) is held to the same two, through the same ``file`` table, so a
    ``MsgId`` is a duplicate whichever kind of file used it first."""
    if payment_file is None:
        first = errors[0] if errors else None
        return Decision(None, "RJCT", first.code if first else schema.STRUCTURAL,
                        first.text if first else "the file could not be read")
    outside = [f for f in errors if f.code == schema.STRUCTURAL
               or not any(_under(f.path, p.path) for p in payment_file.payments)]
    if outside:
        return Decision(payment_file, "RJCT", outside[0].code,
                        "%s: %s" % (outside[0].path, outside[0].text))
    if not allow_duplicates and payment_file.msg_id and db.one(
            conn, "SELECT id FROM file WHERE msg_id = ?", (payment_file.msg_id,)):
        return Decision(payment_file, "RJCT", "DUPL",
                        "MsgId %s has been received before" % payment_file.msg_id)
    return None


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
    if (debtor["format"] == "nacha" and payment.instruction_id
            and len(payment.instruction_id) > NACHA_TRACE_WIDTH):
        # Refused here because the bank cannot answer for it later (#166 case c).
        # A NACHA account's rejections come back as returns (#54), and so does a
        # `return-later` payment, and the return addenda carries the original
        # entry's trace number in a fixed width. At receipt the bank does not know
        # whether this payment will come back, so it refuses the one value that
        # would make the answer unwritable if it did - rather than accepting it and
        # answering 500 to every advance and mailbox read from the day it is due.
        return schema.STRUCTURAL, (
            "InstrId %r is %d characters; a NACHA return carries the original "
            "entry trace number in %d, so if this payment came back the bank "
            "could not write the return"
            % (payment.instruction_id, len(payment.instruction_id),
               NACHA_TRACE_WIDTH))
    if payment.currency != debtor["currency"]:
        return "AM03", ("the amount is in %s but the debtor account %s is held in %s"
                        % (payment.currency, debtor["iban"], debtor["currency"]))
    creditor = by_iban(conn, payment.creditor_account or "")
    if creditor and (creditor["closed"] or creditor["behaviour"] == "closed-account"):
        return "AC04", "the creditor account %s is closed" % creditor["iban"]
    if creditor and creditor["behaviour"] == "bad-bank-id":
        return "RC01", ("the creditor account %s's bank identifier does not resolve"
                        % creditor["iban"])
    if debtor["id"] not in available:
        available[debtor["id"]] = debtor["balance"] - pending_debits(conn, debtor["id"])
    if available[debtor["id"]] - payment.amount < -MAX_BALANCE:
        # Any behaviour: an overdraft this deep has a balance no statement can
        # write, so the bank will not book it (#106).
        return "AM02", ("%s would overdraw the debtor account past the 18 digits a "
                        "statement can write" % schema.format_amount(
                            payment.amount, payment.currency))
    if debtor["behaviour"] == "insufficient-funds":
        if payment.amount > available[debtor["id"]]:
            return "AM04", ("%s would take the debtor account below zero; %s is available"
                            % (schema.format_amount(payment.amount, payment.currency),
                               schema.format_amount(max(available[debtor["id"]], 0),
                                                    debtor["currency"])))
    # For every account: what later payments in the same file are held to.
    available[debtor["id"]] -= payment.amount
    return None, None


def pending_debits(conn, account_id):
    """What is accepted on an account and not yet booked, in minor units."""
    found = db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM payment"
                         " WHERE account_id = ? AND status = ? AND booked_at IS NULL",
                   (account_id, ACCEPTED))
    return int(found["total"])


def record_file(conn, decision, received_at):
    """The ``file`` row for a decided file, of payments or of collections: its
    id, or None for a file with no ``MsgId`` to record it under.

    ``received_at`` is the bank clock's moment of receipt - the same one
    ``decide`` judged the cutoff on (#147). It used to be stored as real time,
    so the decision was taken on one clock and the record kept the other, and a
    file taken in on the bank's Thursday carried the real Monday. It is reported
    beside the file's ``msg_id`` on each of its payments.
    """
    if decision.msg_id is None:
        return None
    return conn.execute(
        "INSERT INTO file (msg_id, message, received_at, status, reason, reported)"
        " VALUES (?,?,?,?,?,?)",
        (decision.msg_id, decision.payment_file.message, db.stamp(received_at),
         decision.status, decision.reason, int(decision.reported))).lastrowid


def book(conn, decision, received_at):
    """Record a decided file and its payments.

    Booking the debits is ``book_due``'s, which ``outbox.release_due`` calls
    straight after, so the ``camt.054`` for what books is written in the same
    step. A file the mock could not read far enough to have a MsgId is not
    recorded: there is nothing a duplicate check could match it on. Returns
    the file's row id, or None.

    ``received_at`` is passed through to ``record_file``, which stamps the file
    with it (#147).
    """
    file_id = record_file(conn, decision, received_at)
    if file_id is None:
        return None
    for d in decision.payments:
        p = d.payment
        conn.execute(
            "INSERT INTO payment (file_id, pmt_inf_id, end_to_end_id, instruction_id,"
            " account_id, debtor_iban, amount, currency, creditor_name, creditor_iban,"
            " creditor_bic, creditor_clearing_id, transaction_code, entry_class,"
            " status, reason, reason_text, settlement_date)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (file_id, d.batch.pmt_inf_id, p.end_to_end_id, p.instruction_id,
             d.account["id"] if d.account else None, d.batch.debtor_account,
             p.amount, p.currency, p.creditor_name, p.creditor_account, p.creditor_bic,
             getattr(p, "creditor_clearing_id", None),
             getattr(p, "transaction_code", None), getattr(p, "entry_class", None),
             d.outcome, d.reason, d.reason_text,
             d.settlement_date.isoformat() if d.settlement_date else None))
    conn.commit()
    return file_id


def book_due(conn, today, now, commit=True):
    """Debit every accepted payment whose settlement date has come.

    ``outbox.release_due`` calls it when a file arrives and as the clock
    moves. Returns the payments booked, oldest first.

    ``now`` is the bank clock's moment, because the bank clock is what decided
    this: the payments booked are those due on the bank's ``today``. Stamping
    real time here recorded a different clock from the one that chose the
    row, and a payment booked on the bank's Thursday carried the real Monday
    (#147).
    """
    due = db.rows(conn, "SELECT * FROM payment WHERE status = ? AND booked_at IS NULL"
                        " AND settlement_date <= ? AND account_id IS NOT NULL ORDER BY id",
                  (ACCEPTED, today.isoformat()))
    stamped = db.stamp(now)
    for row_ in due:
        conn.execute("UPDATE account SET balance = balance - ? WHERE id = ?",
                     (row_["amount"], row_["account_id"]))
        conn.execute("UPDATE payment SET booked_at = ? WHERE id = ?",
                     (stamped, row_["id"]))
        row_["booked_at"] = stamped
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
                     (due.isoformat(), return_reason(account["parameters"],
                                                     account["format"]), row_["id"]))
        scheduled.append(row_["id"])
    return scheduled


def schedule_rejected_returns(conn, file_id, clock, received_on):
    """Return, the next business day, what a NACHA account's file had rejected.

    Option (a) on #54: the decision stands - the acknowledgement already says
    rejected - and the payments the three behaviours reject are also answered
    the way ACH answers them, as return entries: `R01`, `R02` or `R03`. Nothing
    was debited, so nothing is credited back; the return file is the whole of
    it. An ISO 20022 account's rejections are unchanged: the pain.002 is their
    answer.
    """
    if file_id is None:
        return []
    rows = db.rows(conn, "SELECT payment.id, payment.reason, account.format"
                         " FROM payment JOIN account ON account.id = payment.account_id"
                         " WHERE payment.file_id = ? AND payment.status = ?",
                   (file_id, REJECTED))
    due = clock.business_days_after(received_on, 1).isoformat()
    scheduled = []
    for row_ in rows:
        code = nacha.RETURN_FOR.get(row_["reason"])
        if row_["format"] == "nacha" and code:
            conn.execute("UPDATE payment SET return_due = ?, return_reason = ? WHERE id = ?",
                         (due, code, row_["id"]))
            scheduled.append(row_["id"])
    return scheduled


def rejected_returns_due(conn, today, now):
    """The rejected payments whose return day has come, marked returned. Nothing
    is credited: they were never debited.

    ``now`` is the bank clock's, like every stamp whose row the bank clock
    chose: ``return_due <= today`` is a bank-clock comparison (#147).
    """
    due = db.rows(conn, "SELECT payment.*, file.msg_id, file.message FROM payment"
                        " JOIN file ON file.id = payment.file_id"
                        " WHERE payment.status = ? AND return_due IS NOT NULL"
                        " AND return_due <= ? AND returned_at IS NULL ORDER BY payment.id",
                  (REJECTED, today.isoformat()))
    stamped = db.stamp(now)
    for row_ in due:
        conn.execute("UPDATE payment SET returned_at = ? WHERE id = ?",
                     (stamped, row_["id"]))
        row_["returned_at"] = stamped
    return due


def book_returns(conn, today, now):
    """Credit back every payment whose return day has come, and mark it
    returned. Returns those payments, oldest first, with their file's MsgId.

    ``now`` is the bank clock's, for the same reason as ``book_due``'s: the
    balance moves because the bank clock reached ``return_due`` (#147).
    """
    due = db.rows(conn, "SELECT payment.*, file.msg_id, file.message FROM payment"
                        " JOIN file ON file.id = payment.file_id"
                        " WHERE payment.status = ? AND booked_at IS NOT NULL"
                        " AND return_due IS NOT NULL AND return_due <= ?"
                        " AND returned_at IS NULL ORDER BY payment.id",
                  (ACCEPTED, today.isoformat()))
    stamped = db.stamp(now)
    for row_ in due:
        conn.execute("UPDATE account SET balance = balance + ? WHERE id = ?",
                     (row_["amount"], row_["account_id"]))
        conn.execute("UPDATE payment SET status = ?, returned_at = ? WHERE id = ?",
                     (RETURNED, stamped, row_["id"]))
        row_["status"], row_["returned_at"] = RETURNED, stamped
    return due


def payments(conn, end_to_end_id=None):
    """Every payment the bank decided on, newest first, or those with one
    EndToEndId (which is unique within a file, not across files).

    The file's ``received_at`` comes with its ``msg_id``: the bank-clock moment
    the file was taken in, which is the moment the cutoff was judged on and so
    the one that explains ``settlement_date``. It was written to the row and
    served nowhere before #147, leaving a client no bank-clock receipt time at
    all - the nearest was the ``pain.002``'s ``releasedAt``, which is the same
    moment only when ``--status-delay-ms`` is zero.
    """
    sql = ("SELECT payment.*, file.msg_id, file.received_at FROM payment"
           " JOIN file ON file.id = payment.file_id")
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


# -- a holiday declared after something was due on it (#107) --------------------

# Every date the bank has fixed for something still to happen, and the row
# that says it has happened: a payment's settlement, a return's (both kinds,
# #14 and #54), money arriving (#91) and a collection's settlement (#131). All
# are moved by the same rule, or they would disagree about the same day.
DUE_DATES = (("payment", "settlement_date", "booked_at", "settled"),
             ("payment", "return_due", "returned_at", "came back"),
             ("credit", "booking_date", "booked_at", "arrived"),
             ("collection", "settlement_date", "booked_at", "was collected"),
             ("collection", "return_due", "returned_at", "went back"))


def booked_on(conn, day: str) -> List[str]:
    """What has already happened on `day`, in words: nothing a holiday could
    move, because it is booked, and a holiday has no statement to show it on."""
    done = []
    for table, column, happened, verb in DUE_DATES:
        for row in db.rows(conn, "SELECT end_to_end_id, account_id FROM %s WHERE %s = ?"
                                 " AND %s IS NOT NULL" % (table, column, happened),
                           (day,)):
            done.append("%s %s on %s" % (row["end_to_end_id"], verb, row["account_id"]))
    return done


def move_off(conn, clock, day: str) -> List[Dict[str, Any]]:
    """Move everything still due on `day` to the first business day after it.

    Called when `day` has just become a holiday. Everything the bank had fixed
    for that day and not yet done moves together, so no statement is missing
    an entry and the next one still opens where its predecessor closed. Returns
    what moved: the column, the `EndToEndId`, and the dates from and to.
    """
    due = [(table, column, row) for table, column, happened, _verb in DUE_DATES
           for row in db.rows(conn, "SELECT id, end_to_end_id FROM %s WHERE %s = ?"
                                    " AND %s IS NULL" % (table, column, happened),
                              (day,))]
    if not due:
        return []
    try:
        target = clock.roll_forward(datetime.date.fromisoformat(day)).isoformat()
    except OverflowError:
        raise Invalid("%s has no business day after it to move what is due on it "
                      "to" % day) from None
    moved = []
    for table, column, row in due:
        conn.execute("UPDATE %s SET %s = ? WHERE id = ?" % (table, column),
                     (target, row["id"]))
        moved.append({"what": "%s.%s" % (table, column),
                      "end_to_end_id": row["end_to_end_id"],
                      "from": day, "to": target})
    return moved

