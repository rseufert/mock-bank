"""Direct debits: what the bank decides about a file of collections (#131).

The other choreography. In a ``pain.001`` the account holder is the debtor and
pushes money out; in a ``pain.008`` the account holder is the **creditor** and
asks the bank to pull money from debtors under their mandates. The mock is the
creditor's bank. The debtors are at other banks, or are accounts this bank
holds, and either way **only the account holder's side is booked**: a payment
into an account the bank holds does not credit that account, and a collection
from one does not debit it.

This module decides, records and books: on its settlement date an accepted
collection credits the creditor account, and ``outbox`` reports it. The
debtor's bank can then say no (``refuse``): before settlement that rejects the
collection, and after it the money goes back.
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List

from . import accounts, db, messages, nacha, schema
from .accounts import ACCEPTED, REJECTED, RETURNED

# A behaviour describes the account it is set on. A collection reads the
# account it collects *from*, when the bank holds it, for these three - the
# mirror of `accounts.CREDITOR_SIDE`, which a payment reads from the account it
# pays into. Its own list, because the two are not the same set.
DEBTOR_SIDE_OF_A_COLLECTION = ("closed-account", "insufficient-funds", "return-later")
# The first two are read when the file is decided (`_reason`); `return-later`
# when the collection settles (`_schedule_return`).

# ...and every other behaviour says nothing about an account as the debtor of
# a collection. Named with the reason, so that none is ignored by omission and
# a behaviour added later has to be put on one list or the other
# (`tests/test_collections_decide.py` holds the two to `accounts.BEHAVIOURS`).
NOT_ABOUT_A_DEBTOR = {
    "accept": "the default: the collection settles",
    "reject-file": "about a file the account sends, and a debtor sends none",
    "duplicate-file": "about a file the account sends, and a debtor sends none",
    "silent": "about the status report for a file the account sends",
    "statement-gap": "about the account's own statement",
    "bad-bank-id": "about paying into the account: its bank identifier, as a "
                   "payment's creditor agent. Not one of the three Rick chose (#131)",
}

# What the account the file collects *for* is read for, as the sender of the
# file: the same file-level behaviours a pain.001's debtor account has.
SENDER_SIDE = ("reject-file", "silent")


def decide(collection_file, findings, conn, clock, received_at, allow_duplicates=False):
    """Decide a file of collections. Reads the accounts; changes nothing.

    Returns the same ``accounts.Decision`` a payment file gets, so the
    ``pain.002`` and the answer are written by the same code. The first rule
    that applies wins:

    1. and 2. are a payment file's: a file-level finding rejects the file with
       its code, and a ``MsgId`` received before is ``DUPL``
       (``accounts.rejected_outright``).
    3. A creditor account with ``reject-file`` rejects the file: ``FF01``.
    4. Per collection, in file order:

       a. a creditor account the bank does not hold: ``AC03``; one it holds
          that is closed, or ``closed-account``: ``AC04``;
       b. a collection-level finding: its code (``AM03``, ``AM05``, ``MD02``);
       c. a collection in another currency than the creditor account: ``AM03``;
       d. a held debtor account that is closed or ``closed-account``: ``AC04``;
       e. a collection that would take the creditor account past the 18
          digits a statement can write, with what is already on its way to it:
          ``AM02`` (#106);
       f. a held debtor account with ``insufficient-funds``: ``AM04`` for each
          collection larger than what that account has available - its
          balance less its own payments accepted and not yet booked, less the
          collections this file has already taken from it. **Read, never
          changed**: nothing is booked on the debtor, so the next file is held
          to the same balance.

    5. A creditor account with ``silent``: decided like any other, and marked
       unreported, so no ``pain.002`` is sent.

    An accepted collection settles on the requested collection date, rolled to
    a business day, and never before the business day after the bank can start
    on the file: a collection is presented to the debtor's bank a day ahead.
    """
    errors = [f for f in findings if f.level == "error"]
    outright = accounts.rejected_outright(collection_file, errors, conn, allow_duplicates)
    if outright:
        return outright
    creditors = {id(batch): accounts.by_iban(conn, batch.creditor_account or "")
                 for batch in collection_file.batches}
    behaviours = {a["behaviour"] for a in creditors.values() if a}
    if "reject-file" in behaviours:
        return accounts.Decision(collection_file, "RJCT", "FF01",
                                 "the creditor account's behaviour is reject-file")
    earliest = clock.next_business_day(clock.settlement_date(received_at))
    available: Dict[str, int] = {}
    room: Dict[str, int] = {}
    decided = []
    for batch in collection_file.batches:
        creditor = creditors[id(batch)]
        wanted = batch.requested_collection_date
        when = clock.settlement_date(received_at, max(wanted or earliest, earliest))
        for collection in batch.collections:
            reason, text = _reason(conn, creditor, batch, collection, errors, available,
                                   room)
            decided.append(accounts.PaymentDecision(
                collection, batch, creditor, REJECTED if reason else ACCEPTED, reason,
                text, None if reason else when))
    accepted = sum(1 for d in decided if d.outcome == ACCEPTED)
    status = "ACCP" if accepted == len(decided) else ("RJCT" if not accepted else "PART")
    return accounts.Decision(collection_file, status, payments=decided,
                             reported="silent" not in behaviours)


def _reason(conn, creditor, batch, collection, errors, available, room):
    """The code and prose a collection is rejected with, or (None, None)."""
    if creditor is None:
        held = ", ".join(a["iban"] for a in accounts.listing(conn))
        return "AC03", ("the creditor account %s is not one this bank holds; it holds %s"
                        % (batch.creditor_account or "(none)", held))
    if creditor["closed"] or creditor["behaviour"] == "closed-account":
        return "AC04", "the creditor account %s is closed" % creditor["iban"]
    for finding in errors:
        if finding.path == collection.path or finding.path.startswith(collection.path + "/"):
            return finding.code, finding.text
    if collection.currency != creditor["currency"]:
        return "AM03", ("the amount is in %s but the creditor account %s is held in %s"
                        % (collection.currency, creditor["iban"], creditor["currency"]))
    debtor = accounts.by_iban(conn, collection.debtor_account or "")
    if debtor and (debtor["closed"] or debtor["behaviour"] == "closed-account"):
        return "AC04", "the debtor account %s is closed" % debtor["iban"]
    if creditor["id"] not in room:
        # What the creditor account can still take: the ceiling, less its
        # balance and everything already due to be credited to it (#106).
        room[creditor["id"]] = (accounts.MAX_BALANCE - creditor["balance"]
                                - accounts.still_to_arrive(conn, creditor["id"]))
    if collection.amount > room[creditor["id"]]:
        return "AM02", ("%s would take the creditor account %s past the 18 digits a "
                        "statement can write" % (schema.format_amount(
                            collection.amount, collection.currency), creditor["iban"]))
    if debtor is None:
        # Another bank's customer: nothing more can be known at acceptance.
        room[creditor["id"]] -= collection.amount
        return None, None
    if debtor["behaviour"] == "insufficient-funds":
        if debtor["id"] not in available:
            available[debtor["id"]] = (debtor["balance"]
                                       - accounts.pending_debits(conn, debtor["id"]))
        if collection.amount > available[debtor["id"]]:
            return "AM04", ("%s is more than the debtor account %s has available, %s"
                            % (schema.format_amount(collection.amount, collection.currency),
                               debtor["iban"],
                               schema.format_amount(max(available[debtor["id"]], 0),
                                                    debtor["currency"])))
        available[debtor["id"]] -= collection.amount
    room[creditor["id"]] -= collection.amount
    return None, None


def record(conn, decision, received_at):
    """Record a decided file and its collections; the file's row id, or None
    for a file with no ``MsgId``. Nothing is booked here.

    ``received_at`` is the bank clock's moment of receipt, as it is for a file
    of payments: one stamp, one rule, from the shared ``accounts.record_file``
    (#147). ``GET /_mock/collections`` does not report it yet - #131's own step
    serves it there, the way a payment reports it beside its ``msg_id``.
    """
    file_id = accounts.record_file(conn, decision, received_at)
    if file_id is None:
        return None
    for d in decision.payments:
        c, when = d.payment, d.batch.requested_collection_date
        conn.execute(
            "INSERT INTO collection (file_id, pmt_inf_id, end_to_end_id, instruction_id,"
            " account_id, creditor_iban, amount, currency, debtor_name, debtor_iban,"
            " debtor_bic, mandate_id, mandate_signed, sequence_type, creditor_scheme_id,"
            " remittance, status, reason, reason_text, collection_date, settlement_date,"
            " entry_class, transaction_code, debtor_clearing_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (file_id, d.batch.pmt_inf_id, c.end_to_end_id, c.instruction_id,
             d.account["id"] if d.account else None, d.batch.creditor_account,
             c.amount, c.currency, c.debtor_name, c.debtor_account, c.debtor_bic,
             c.mandate_id, c.mandate_signed, c.sequence_type, c.creditor_scheme_id,
             json.dumps(c.remittance), d.outcome, d.reason, d.reason_text,
             when.isoformat() if when else None,
             d.settlement_date.isoformat() if d.settlement_date else None,
             # What a NACHA debit entry carries and a pain.008 does not (#176).
             getattr(c, "entry_class", None), getattr(c, "transaction_code", None),
             getattr(c, "debtor_clearing_id", None)))
    conn.commit()
    return file_id


def book_due(conn, today: datetime.date, clock, now) -> List[Dict[str, Any]]:
    """Credit every accepted collection whose settlement date has come. Returns
    them, oldest first, with their file's ``MsgId``.

    The creditor account is credited and nothing else: the debtor is another
    bank's customer, or an account this bank holds and does not book (see the
    module). Stamped with the bank clock's ``now``, as every booking is (#147).

    Not into an account closed while the collection waited, for a credit's
    reason (``credits.book_due``): a closed account gets no statement, so the
    settlement date moves on a business day each time it comes due.
    """
    due = db.rows(conn, "SELECT collection.*, file.msg_id, account.closed AS closed"
                        " FROM collection JOIN file ON file.id = collection.file_id"
                        " JOIN account ON account.id = collection.account_id"
                        " WHERE collection.status = ? AND booked_at IS NULL"
                        " AND settlement_date <= ? ORDER BY collection.id",
                  (ACCEPTED, today.isoformat()))
    later, stamped, booked = clock.next_business_day(today).isoformat(), db.stamp(now), []
    for row in due:
        if row.pop("closed"):
            conn.execute("UPDATE collection SET settlement_date = ? WHERE id = ?",
                         (later, row["id"]))
            continue
        conn.execute("UPDATE account SET balance = balance + ? WHERE id = ?",
                     (row["amount"], row["account_id"]))
        conn.execute("UPDATE collection SET booked_at = ? WHERE id = ?",
                     (stamped, row["id"]))
        booked.append(_entry(dict(row, booked_at=stamped)))
        _schedule_return(conn, clock, row)
    return booked


def _schedule_return(conn, clock, row) -> None:
    """A just-settled collection from a debtor account this bank holds with
    ``return-later`` goes back ``days`` business days on, with its reason: the
    mirror of ``accounts.schedule_returns``, read from the other party. A NACHA
    account's ``R`` code is said in ISO 20022, since the answer is a pacs.004."""
    debtor = accounts.by_iban(conn, row["debtor_iban"] or "")
    if debtor is None or debtor["behaviour"] != "return-later":
        return
    wanted = dict(accounts.RETURN_LATER, **debtor["parameters"])
    if wanted["end_to_end_id"] and wanted["end_to_end_id"] != row["end_to_end_id"]:
        return
    reason = accounts.return_reason(debtor["parameters"], debtor["format"])
    creditor = accounts.get(conn, row["account_id"])
    if creditor["format"] == "nacha":
        # The answer is a return file: an R code, the debtor's own or the one
        # for its ISO 20022 reason, and "account closed" where there is none.
        if reason not in nacha.DEBIT_RETURN_REASONS:
            reason = nacha.RETURN_FOR.get(reason, accounts.NACHA_RETURN_REASON)
    else:
        reason = messages.iso_return_reason(reason)
        if reason not in schema.CODE_SETS["ExternalReturnReason1Code"]:
            reason = "MS03"
    due = clock.business_days_after(
        datetime.date.fromisoformat(row["settlement_date"]), wanted["days"])
    conn.execute("UPDATE collection SET return_due = ?, return_reason = ? WHERE id = ?",
                 (due.isoformat(), reason, row["id"]))


class Refused(ValueError):
    """A refusal the bank would not take, and the status that says why."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def refuse(conn, clock, now: datetime.datetime, end_to_end_id: str, body: Any):
    """The debtor's bank says no to a collection, with ``reason``. Raises
    ``Refused``; returns the collection and, before settlement, True.

    **Before settlement** the collection is rejected with the reason and never
    books; the caller queues a further ``pain.002`` for it. **After**, it is a
    return: the money goes back on the first day the bank can book it - today
    before the cutoff on a business day - and ``book_returns`` debits the
    creditor account then.

    Only for a debtor at another bank. One this bank holds decides by its own
    state and behaviour (Rick, on #131), so there is an account to set that on.
    """
    if not isinstance(body, dict) or set(body) - {"reason"}:
        raise Refused(400, 'the body names the reason and nothing else, as in '
                           '{"reason": "MD01"}')
    found = db.rows(conn, "SELECT collection.*, file.msg_id, file.message FROM collection"
                          " JOIN file ON file.id = collection.file_id"
                          " WHERE end_to_end_id = ? ORDER BY collection.id DESC",
                    (end_to_end_id,))
    if not found:
        raise Refused(404, "no collection with EndToEndId %r" % end_to_end_id)
    row = found[0]
    # The reason is said in the terms the creditor account is answered in: an
    # R code where its answers are NACHA's (#176), an ISO 20022 reason that the
    # status report can also carry everywhere else.
    account = accounts.get(conn, row["account_id"] or "")
    in_nacha = bool(account) and account["format"] == "nacha"
    reasons = sorted(nacha.DEBIT_RETURN_REASONS if in_nacha else
                     set(schema.CODE_SETS["ExternalReturnReason1Code"])
                     & set(schema.CODE_SETS["ExternalStatusReason1Code"]))
    if body.get("reason") not in reasons:
        raise Refused(400, 'the body names the reason, as in {"reason": "%s"}: one of %s'
                           % ("R10" if in_nacha else "MD01", ", ".join(reasons)))
    if row["status"] != ACCEPTED or row["return_due"]:
        raise Refused(409, "collection %s is %s, so there is nothing left to refuse"
                           % (end_to_end_id, "already on its way back" if row["return_due"]
                              and row["status"] == ACCEPTED else row["status"]))
    if accounts.by_iban(conn, row["debtor_iban"] or ""):
        raise Refused(409, "the debtor account %s is one this bank holds, so its own "
                           "state and behaviour decide the collection; this is for a "
                           "debtor at another bank" % row["debtor_iban"])
    if row["booked_at"] is None:
        text = "refused by the debtor's bank before settlement"
        # NACHA has no message that rejects one entry of a file it accepted, so
        # a NACHA account is told as it is told anything comes back: a return
        # entry, on the day the collection would have settled. Nothing booked,
        # so nothing is debited (#54's option (a), for a collection).
        due = row["settlement_date"] if in_nacha else None
        conn.execute("UPDATE collection SET status = ?, reason = ?, reason_text = ?,"
                     " settlement_date = NULL, return_due = ?, return_reason = ?,"
                     " refused_at = ? WHERE id = ?",
                     (REJECTED, body["reason"], text, due, body["reason"] if due else None,
                      db.stamp(now), row["id"]))
        conn.commit()
        return (dict(row, status=REJECTED, reason=body["reason"], reason_text=text),
                not in_nacha)
    if (account["balance"] - accounts.pending_debits(conn, account["id"])
            - returns_due(conn, account["id"]) - row["amount"] < -accounts.MAX_BALANCE):
        raise Refused(409, "returning %s would overdraw account %s past the 18 digits "
                           "a statement can write" % (end_to_end_id, account["id"]))
    due = clock.settlement_date(now).isoformat()
    conn.execute("UPDATE collection SET return_due = ?, return_reason = ?, refused_at = ?"
                 " WHERE id = ?", (due, body["reason"], db.stamp(now), row["id"]))
    conn.commit()
    return dict(row, return_due=due, return_reason=body["reason"],
                refused_at=db.stamp(now)), False


def returns_due(conn, account_id: str) -> int:
    """What is on its way back out of an account: settled collections to be
    returned. One refused before it settled comes back too, on a NACHA account,
    and takes nothing with it: ``booked_at`` is what separates them (#144)."""
    return int(db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM collection"
                            " WHERE account_id = ? AND return_due IS NOT NULL"
                            " AND booked_at IS NOT NULL"
                            " AND returned_at IS NULL", (account_id,))["total"])


def schedule_rejected_returns(conn, file_id, clock, received_on) -> List[int]:
    """Return, the next business day, what a NACHA account's file of collections
    had rejected: the mirror of ``accounts.schedule_rejected_returns`` (#54,
    option (a)). The acknowledgement already says rejected; ACH also answers
    with a return entry, where the reason has an ``R`` code."""
    if file_id is None:
        return []
    due = clock.business_days_after(received_on, 1).isoformat()
    scheduled = []
    for row in db.rows(conn, "SELECT collection.id, collection.reason, account.format"
                             " FROM collection JOIN account ON account.id = collection.account_id"
                             " WHERE collection.file_id = ? AND collection.status = ?",
                       (file_id, REJECTED)):
        code = nacha.RETURN_FOR.get(row["reason"])
        if row["format"] == "nacha" and code:
            conn.execute("UPDATE collection SET return_due = ?, return_reason = ?"
                         " WHERE id = ?", (due, code, row["id"]))
            scheduled.append(row["id"])
    return scheduled


def rejected_returns_due(conn, today: datetime.date, now) -> List[Dict[str, Any]]:
    """The rejected collections whose return day has come, marked returned.
    Nothing is debited: they never settled."""
    due = db.rows(conn, "SELECT collection.*, file.msg_id, file.message FROM collection"
                        " JOIN file ON file.id = collection.file_id"
                        " WHERE collection.status = ? AND return_due IS NOT NULL"
                        " AND return_due <= ? AND returned_at IS NULL ORDER BY collection.id",
                  (REJECTED, today.isoformat()))
    stamped = db.stamp(now)
    for row in due:
        conn.execute("UPDATE collection SET returned_at = ? WHERE id = ?",
                     (stamped, row["id"]))
    return [_return(dict(row, returned_at=stamped)) for row in due]


def book_returns(conn, today: datetime.date, clock, now) -> List[Dict[str, Any]]:
    """Debit every collection whose return day has come, and mark it returned.
    Returns them, oldest first, as the writers take a returned collection.

    Not out of an account closed meanwhile, for ``book_due`` 's reason: the
    return day moves on a business day each time it comes due.
    """
    due = db.rows(conn, "SELECT collection.*, file.msg_id, file.message,"
                        " account.closed AS closed FROM collection"
                        " JOIN file ON file.id = collection.file_id"
                        " JOIN account ON account.id = collection.account_id"
                        " WHERE collection.status = ? AND booked_at IS NOT NULL"
                        " AND return_due IS NOT NULL AND return_due <= ?"
                        " AND returned_at IS NULL ORDER BY collection.id",
                  (ACCEPTED, today.isoformat()))
    later, stamped, returned = clock.next_business_day(today).isoformat(), db.stamp(now), []
    for row in due:
        if row.pop("closed"):
            conn.execute("UPDATE collection SET return_due = ? WHERE id = ?",
                         (later, row["id"]))
            continue
        conn.execute("UPDATE account SET balance = balance - ? WHERE id = ?",
                     (row["amount"], row["account_id"]))
        conn.execute("UPDATE collection SET status = ?, returned_at = ? WHERE id = ?",
                     (RETURNED, stamped, row["id"]))
        returned.append(_return(dict(row, status=RETURNED, returned_at=stamped)))
    return returned


def _return(row) -> Dict[str, Any]:
    """A returned collection as the writers take it: a debit, collected, returned."""
    return dict(row, remittance=json.loads(row["remittance"] or "[]"),
                collected=True, returned=True)


def returned_on(conn, account_id: str, day: str) -> List[Dict[str, Any]]:
    return [_return(r) for r in db.rows(
        conn, "SELECT collection.*, file.msg_id FROM collection"
              " JOIN file ON file.id = collection.file_id"
              " WHERE account_id = ? AND returned_at IS NOT NULL"
              " AND booked_at IS NOT NULL"
              " AND return_due = ? ORDER BY collection.id", (account_id, day))]


def returned_after(conn, account_id: str, day: str) -> int:
    """What went back after ``day``: undone to reach that day's closing balance."""
    return int(db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM collection"
                            " WHERE account_id = ? AND returned_at IS NOT NULL"
                            " AND booked_at IS NOT NULL"
                            " AND return_due > ?", (account_id, day))["total"])


def _entry(row) -> Dict[str, Any]:
    """A booked collection as the writers take it: a credit, and collected."""
    return dict(row, remittance=json.loads(row["remittance"] or "[]"),
                credit=True, collected=True)


def booked_on(conn, account_id: str, day: str) -> List[Dict[str, Any]]:
    return [_entry(r) for r in db.rows(
        conn, "SELECT collection.*, file.msg_id FROM collection"
              " JOIN file ON file.id = collection.file_id"
              " WHERE account_id = ? AND booked_at IS NOT NULL"
              " AND settlement_date = ? ORDER BY collection.id", (account_id, day))]


def booked_after(conn, account_id: str, day: str) -> int:
    """What was collected after ``day``: undone to reach that day's closing balance."""
    return int(db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM collection"
                            " WHERE account_id = ? AND booked_at IS NOT NULL"
                            " AND settlement_date > ?", (account_id, day))["total"])


def listing(conn, end_to_end_id=None) -> List[Dict[str, Any]]:
    """Every collection the bank decided on, newest first, or those with one
    ``EndToEndId`` (unique within a file, not across files). The file's
    ``received_at`` comes with its ``msg_id``, as it does on a payment (#147)."""
    sql = ("SELECT collection.*, file.msg_id, file.received_at FROM collection"
           " JOIN file ON file.id = collection.file_id")
    where, params = ("", ()) if end_to_end_id is None else (
        " WHERE end_to_end_id = ?", (end_to_end_id,))
    rows = db.rows(conn, sql + where + " ORDER BY collection.id DESC", params)
    return [dict(row, remittance=json.loads(row["remittance"] or "[]")) for row in rows]


def counts(conn) -> Dict[str, int]:
    """What ``/_mock/state`` reports about collections."""
    out = {status: db.count(conn, "collection", "status = '%s'" % status)
           for status in (ACCEPTED, REJECTED, RETURNED)}
    out["booked"] = db.count(conn, "collection", "booked_at IS NOT NULL")
    return out
