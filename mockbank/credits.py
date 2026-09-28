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
from typing import Any, Dict, List, Optional

from . import accounts, db, schema

# How wide a line of note to payee is shown. 140 is `Ustrd`'s own limit and
# keeps what the payer wrote; 35 and 70 are the widths banks re-cut to.
WRAPS = (35, 70, 140)
DEFAULT_WRAP = 140

FIELDS = ("account", "amount", "currency", "value_date", "debtor", "reference",
          "note", "end_to_end_id", "wrap")


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
    return [text[i:i + width] for i in range(0, len(text), width)]


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
    currency = body.get("currency", account["currency"])
    if currency != account["currency"]:
        raise Refused(400, "the credit is in %s but account %s is held in %s, and "
                           "the mock does no FX" % (currency, account["id"],
                                                     account["currency"]))

    today = clock.today()
    raw = body.get("value_date", today.isoformat())
    try:
        value_date = datetime.date.fromisoformat(raw) if isinstance(raw, str) else None
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

    width = body.get("wrap", DEFAULT_WRAP)
    if width not in WRAPS:
        raise Refused(400, "wrap is how wide the bank cuts the note: one of %s, "
                           "not %r" % (", ".join(map(str, WRAPS)), width))
    note = body.get("note", [])
    note = [note] if isinstance(note, str) else note
    if not isinstance(note, list) or not all(isinstance(n, str) for n in note):
        raise Refused(400, "note is the note to payee: a string, or a list of lines")
    reference = body.get("reference", "")
    end_to_end_id = body.get("end_to_end_id", "NOTPROVIDED")
    for name, value, limit in (("reference", reference, 35),
                               ("end_to_end_id", end_to_end_id, 35),
                               ("debtor name", debtor.get("name", ""), 140)):
        if not isinstance(value, str) or len(value) > limit:
            raise Refused(400, "%s is text of at most %d characters" % (name, limit))

    # The first day the bank can book it: the value date, or later if that is
    # a weekend or holiday, or today after the cutoff - the same rule a
    # payment's settlement follows.
    booking = clock.settlement_date(now, value_date)
    cursor = conn.execute(
        "INSERT INTO credit (account_id, amount, currency, value_date, booking_date,"
        " end_to_end_id, debtor_name, debtor_iban, debtor_bic, reference, note,"
        " received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (account["id"], amount, currency, value_date.isoformat(), booking.isoformat(),
         end_to_end_id or "NOTPROVIDED", debtor.get("name", ""), iban,
         str(debtor.get("bic", "")).upper(), reference,
         json.dumps(wrap(note, width)), db.stamp(now)))
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


def book_due(conn, today: datetime.date) -> List[Dict[str, Any]]:
    """Book every credit whose day has come: the balance goes up. Returns them."""
    due = db.rows(conn, "SELECT * FROM credit WHERE booked_at IS NULL"
                        " AND booking_date <= ? ORDER BY id", (today.isoformat(),))
    now = db.now()
    for row in due:
        conn.execute("UPDATE account SET balance = balance + ? WHERE id = ?",
                     (row["amount"], row["account_id"]))
        conn.execute("UPDATE credit SET booked_at = ? WHERE id = ?", (now, row["id"]))
        row["booked_at"] = now
    return [_row(r) for r in due]


def booked_on(conn, account_id: str, day: str) -> List[Dict[str, Any]]:
    return [_row(r) for r in db.rows(
        conn, "SELECT * FROM credit WHERE account_id = ? AND booked_at IS NOT NULL"
              " AND booking_date = ? ORDER BY id", (account_id, day))]


def booked_after(conn, account_id: str, day: str) -> int:
    """What arrived after `day`: undone to reach that day's closing balance."""
    return int(db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM credit"
                            " WHERE account_id = ? AND booked_at IS NOT NULL"
                            " AND booking_date > ?", (account_id, day))["total"])
