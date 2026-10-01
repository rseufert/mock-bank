"""Money arriving: a credit from somebody else to an account the bank holds (#91).

Everything else the mock books is money leaving - a payment sent - or coming
back - a payment returned. This is the third kind: a customer paying an
invoice, which is the most common thing that happens to a company's account
and the whole of the receivable side. The test describes what the payer sent
(`POST /_mock/credits`); the bank books it on the clock and reports it the way
it reports anything, in a `camt.054` and on the day's `camt.053`.

What makes it worth having is that **the payer controls the reference**, and
cash application is where that goes wrong: a short payment, a note that names
no invoice, one credit for several invoices, a reference split across two
lines. The mock states what arrived; judging it is the receiving system's job.
Three of those are simply what the request says. The fourth is the bank's
doing, not the payer's, so it is the one thing here that is not: `wrap` re-cuts
the note at 35 or 70 characters wherever the cut falls, as a bank reformatting
remittance information does.
"""
from __future__ import annotations

import datetime
import json
import re
from typing import Any, Dict, List, Optional

from . import accounts, db, messages, schema

# How wide a line of note to payee is shown. 140 is `Ustrd`'s own limit and
# keeps what the payer wrote; 35 and 70 are the widths banks re-cut to.
WRAPS = (35, 70, 140)
DEFAULT_WRAP = 140

FIELDS = ("account", "amount", "currency", "value_date", "debtor", "reference",
          "note", "end_to_end_id", "wrap")

DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# What XML 1.0 cannot carry, and line breaks, which no field here should hold.
UNWRITABLE = re.compile("[\x00-\x1f\x7f\ud800-\udfff\ufffe\uffff\u2028\u2029]")


class Refused(ValueError):
    """A credit the bank would not book, and the status that says why."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def wrap(lines: List[str], width: int) -> List[str]:
    """The note as a bank would show it: joined, then cut every `width`.

    Cut by count, not at spaces: a bank that reformats remittance does not
    care where an invoice number falls, and a reference split across two
    lines is exactly what a cash application has to survive. At 140 each of
    the payer's lines is only cut if it is longer than the field allows.
    """
    if width == DEFAULT_WRAP:
        return [piece for line in lines
                for piece in ([line[i:i + width] for i in range(0, len(line), width)]
                              or [""])]
    text = " ".join(lines)
    # A cut that leaves only a space is dropped: the bank's re-cut never
    # makes an empty line the payer did not write.
    return [piece for piece in (text[i:i + width] for i in range(0, len(text), width))
            if piece.strip()]


def create(conn, clock, now: datetime.datetime, body: Any) -> Dict[str, Any]:
    """Validate what the payer sent, and set it to book. Raises `Refused`."""
    if not isinstance(body, dict):
        raise Refused(400, "the body is a JSON object describing the credit, as in "
                           '{"account": "ACME", "amount": 125000, "note": "INV-1001"}')
    unknown = sorted(set(body) - set(FIELDS))
    if unknown:
        raise Refused(400, "unknown field%s %s; a credit has: %s" % (
            "s" if len(unknown) > 1 else "", ", ".join(map(repr, unknown)),
            ", ".join(FIELDS)))

    named = body.get("account")
    if not isinstance(named, str) or not named:
        raise Refused(400, "account is the id or IBAN of the account the money "
                           "arrives in")
    account = accounts.get(conn, named) or accounts.by_iban(conn, named)
    if account is None:
        raise Refused(409, "no account %r; the bank holds %s" % (named, ", ".join(
            a["id"] for a in accounts.listing(conn))))
    if account["closed"]:
        raise Refused(409, "account %s is closed, so nothing can arrive in it"
                           % account["id"])

    amount = body.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise Refused(400, "amount is a whole number of minor units above zero - "
                           "1250.00 is 125000 - not %r" % (amount,))
    # Returns due back count as well as credits waiting: either books on top
    # of this one, and a return after a maximum credit was the second route
    # to a balance no statement could write (#106).
    if (account["balance"] + accounts.still_to_arrive(conn, account["id"]) + amount
            > accounts.MAX_BALANCE):
        raise Refused(400, "amount %d would take account %s past the 18 digits a "
                           "statement balance has" % (amount, account["id"]))
    currency = body.get("currency", account["currency"])
    if currency != account["currency"]:
        raise Refused(400, "the credit is in %s but account %s is held in %s, and "
                           "the mock does no FX" % (currency, account["id"],
                                                     account["currency"]))

    today = clock.today()
    raw = body.get("value_date", today.isoformat())
    try:
        # The pattern first: `fromisoformat` takes 20261005 from Python 3.11
        # on and refuses it before, and the answer must not depend on that.
        value_date = (datetime.date.fromisoformat(raw)
                      if isinstance(raw, str) and DATE.fullmatch(raw) else None)
    except ValueError:
        value_date = None
    if value_date is None:
        raise Refused(400, "value_date is YYYY-MM-DD, not %r" % (raw,))
    if value_date < today:
        raise Refused(400, "value_date %s is before the bank's today, %s; that "
                           "day's statement may already be out"
                           % (value_date.isoformat(), today.isoformat()))

    debtor = body.get("debtor", {})
    if not isinstance(debtor, dict) or set(debtor) - {"name", "iban", "bic"}:
        raise Refused(400, "debtor is the payer, as in {\"name\": \"Customer Ltd\", "
                           "\"iban\": \"...\", \"bic\": \"...\"}")
    iban = str(debtor.get("iban", "")).replace(" ", "").upper()
    if iban and not schema.iban_is_valid(iban):
        raise Refused(400, "the debtor's iban %r fails its check digits" % iban)
    bic = str(debtor.get("bic", "")).upper()
    if bic and not re.fullmatch(schema.PATTERNS["BICFIDec2014Identifier"], bic):
        raise Refused(400, "the debtor's bic %r is not a BIC: 8 or 11 letters and "
                           "digits, the country at 5 and 6" % bic)

    width = body.get("wrap", DEFAULT_WRAP)
    if isinstance(width, bool) or not isinstance(width, int) or width not in WRAPS:
        raise Refused(400, "wrap is how wide the bank cuts the note: one of %s, "
                           "not %r" % (", ".join(map(str, WRAPS)), width))
    note = body.get("note", [])
    note = [note] if isinstance(note, str) else note
    if not isinstance(note, list) or not all(isinstance(n, str) for n in note):
        raise Refused(400, "note is the note to payee: a string, or a list of lines")
    reference = body.get("reference", "")
    end_to_end_id = body.get("end_to_end_id", "NOTPROVIDED")
    for name, value, limit in [("reference", reference, 35),
                               ("end_to_end_id", end_to_end_id, 35),
                               ("debtor name", debtor.get("name", ""), 140)] + [
                               ("line %d of the note" % n, line, None)
                               for n, line in enumerate(note, start=1)]:
        # A line of note is not limited: the bank cuts it to the wrap.
        if not isinstance(value, str) or (limit and len(value) > limit):
            raise Refused(400, "%s is text of at most %d characters" % (name, limit))
        if value and not value.strip():
            raise Refused(400, "%s is blank: leave it out, or give it text" % name)
        if name.startswith("line") and not value:
            raise Refused(400, "%s is empty: leave the line out" % name)
        if UNWRITABLE.search(value):
            raise Refused(400, "%s holds a control character or line break, which "
                               "the bank's XML cannot carry" % name)

    # The first day the bank can book it: the value date, or later if that is
    # a weekend or holiday, or today after the cutoff - the same rule a
    # payment's settlement follows.
    try:
        booking = clock.settlement_date(now, value_date)
    except OverflowError:
        # 9999-12-31 on a holiday rolls forward past the last date there is.
        raise Refused(400, "value_date %s has no business day on or after it"
                           % value_date.isoformat())
    record = {"id": 0, "amount": amount, "currency": currency,
              "value_date": value_date.isoformat(),
              "end_to_end_id": end_to_end_id or "NOTPROVIDED",
              "debtor_name": debtor.get("name", ""), "debtor_iban": iban,
              "debtor_bic": bic, "reference": reference, "note": wrap(note, width),
              "incoming": True}
    try:
        # Written once now, as the release will write it: a credit the bank
        # cannot report is refused here, not stored to fail every release after.
        messages.write_camt054(account, [record], booking, "MB-C054-CHECK", now)
    except ValueError as error:
        raise Refused(400, "the bank could not report this credit: %s" % error)
    cursor = conn.execute(
        "INSERT INTO credit (account_id, amount, currency, value_date, booking_date,"
        " end_to_end_id, debtor_name, debtor_iban, debtor_bic, reference, note,"
        " received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (account["id"], amount, currency, value_date.isoformat(), booking.isoformat(),
         record["end_to_end_id"], record["debtor_name"], iban, bic, reference,
         json.dumps(record["note"]), db.stamp(now)))
    conn.commit()
    return get(conn, cursor.lastrowid)


def _row(record: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(record)
    out["note"] = json.loads(out["note"] or "[]")
    return out


def get(conn, credit_id: int) -> Optional[Dict[str, Any]]:
    found = db.one(conn, "SELECT * FROM credit WHERE id = ?", (credit_id,))
    return _row(found) if found else None


def listing(conn) -> List[Dict[str, Any]]:
    """Every credit, newest first, booked or waiting."""
    return [_row(r) for r in db.rows(conn, "SELECT * FROM credit ORDER BY id DESC")]


def book_due(conn, today: datetime.date, clock, now) -> List[Dict[str, Any]]:
    """Book every credit whose day has come: the balance goes up. Returns them.

    Not into an account closed while the credit waited: a closed account gets
    no statement, so booked there it would be money that arrived and was never
    reported. Its booking date moves on to the next business day instead, each
    time it comes due, so that if the account is reopened it books on a day
    that account has a statement for, never under a day already passed.
    """
    due = db.rows(conn, "SELECT credit.*, account.closed AS closed FROM credit"
                        " JOIN account ON account.id = credit.account_id"
                        " WHERE credit.booked_at IS NULL"
                        " AND credit.booking_date <= ? ORDER BY credit.id",
                  (today.isoformat(),))
    later = clock.next_business_day(today).isoformat()
    # The bank clock's moment, as a payment's `booked_at` is (#147): a credit
    # books because the bank clock reached its `booking_date`. A credit's
    # `received_at` was already bank time - `create` is handed `state.now()` -
    # so the two stamps on one credit now agree about which clock they are on.
    stamped = db.stamp(now)
    booked = []
    for row in due:
        closed = row.pop("closed")
        if closed:
            conn.execute("UPDATE credit SET booking_date = ? WHERE id = ?", (later, row["id"]))
            continue
        conn.execute("UPDATE account SET balance = balance + ? WHERE id = ?",
                     (row["amount"], row["account_id"]))
        conn.execute("UPDATE credit SET booked_at = ? WHERE id = ?",
                     (stamped, row["id"]))
        row["booked_at"] = stamped
        booked.append(row)
    return [_row(r) for r in booked]


def booked_on(conn, account_id: str, day: str) -> List[Dict[str, Any]]:
    return [_row(r) for r in db.rows(
        conn, "SELECT * FROM credit WHERE account_id = ? AND booked_at IS NOT NULL"
              " AND booking_date = ? ORDER BY id", (account_id, day))]


def booked_after(conn, account_id: str, day: str) -> int:
    """What arrived after `day`: undone to reach that day's closing balance."""
    return int(db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM credit"
                            " WHERE account_id = ? AND booked_at IS NOT NULL"
                            " AND booking_date > ?", (account_id, day))["total"])
