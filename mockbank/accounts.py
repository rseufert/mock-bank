"""Accounts: what the bank holds, and what it does with a payment.

``BEHAVIOURS`` comes first because it is the single place the behaviour names
and their meaning are declared: the command line prints it, the README's
behaviour table is checked against it, ``check()`` refuses anything not in it,
and ``decide()`` (once it exists) dispatches on it.  The codes named are the
ISO 20022 external reason codes a real bank uses, not inventions of the mock.

Below it is the account itself - the row, what may be set on it, and what the
mock refuses to hold - because the two belong together: an account is an IBAN,
a balance and one of these behaviours.  The storage is ``db.py``; the rules
about what a valid account *is* are here.
"""
from __future__ import annotations

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
}

# The fields that are text. A JSON client can plausibly send a number, an
# object or a list for any of them, and every one of those used to reach
# sqlite3 and come back as a 500 with a traceback - which tells the caller
# nothing about which field they got wrong.
TEXT_FIELDS = ("name", "iban", "bic", "currency", "behaviour")

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
    try:
        conn.execute("UPDATE account SET %s WHERE id = ?"
                     % ", ".join("%s = ?" % key for key in changes),
                     list(changes.values()) + [identifier])
    except sqlite3.IntegrityError:
        conn.rollback()
        raise Invalid("iban %s belongs to another account already"
                      % changes.get("iban")) from None
    conn.commit()
    return require(conn, identifier)


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
