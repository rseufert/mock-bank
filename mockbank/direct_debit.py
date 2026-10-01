"""Direct debits: what the bank decides about a file of collections (#131).

The other choreography. In a ``pain.001`` the account holder is the debtor and
pushes money out; in a ``pain.008`` the account holder is the **creditor** and
asks the bank to pull money from debtors under their mandates. The mock is the
creditor's bank. The debtors are at other banks, or are accounts this bank
holds, and either way **only the account holder's side is booked**: a payment
into an account the bank holds does not credit that account, and a collection
from one does not debit it.

This module decides and records. Booking the credit on the settlement date is
the next step.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from . import accounts, db, schema
from .accounts import ACCEPTED, REJECTED

# A behaviour describes the account it is set on. A collection reads the
# account it collects *from*, when the bank holds it, for these three - the
# mirror of `accounts.CREDITOR_SIDE`, which a payment reads from the account it
# pays into. Its own list, because the two are not the same set.
DEBTOR_SIDE_OF_A_COLLECTION = ("closed-account", "insufficient-funds", "return-later")

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
       e. a held debtor account with ``insufficient-funds``: ``AM04`` for each
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
    decided = []
    for batch in collection_file.batches:
        creditor = creditors[id(batch)]
        wanted = batch.requested_collection_date
        when = clock.settlement_date(received_at, max(wanted or earliest, earliest))
        for collection in batch.collections:
            reason, text = _reason(conn, creditor, batch, collection, errors, available)
            decided.append(accounts.PaymentDecision(
                collection, batch, creditor, REJECTED if reason else ACCEPTED, reason,
                text, None if reason else when))
    accepted = sum(1 for d in decided if d.outcome == ACCEPTED)
    status = "ACCP" if accepted == len(decided) else ("RJCT" if not accepted else "PART")
    return accounts.Decision(collection_file, status, payments=decided,
                             reported="silent" not in behaviours)


def _reason(conn, creditor, batch, collection, errors, available):
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
    if debtor is None:
        # Another bank's customer: nothing about it can be known at acceptance.
        return None, None
    if debtor["closed"] or debtor["behaviour"] == "closed-account":
        return "AC04", "the debtor account %s is closed" % debtor["iban"]
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
    return None, None


def record(conn, decision):
    """Record a decided file and its collections; the file's row id, or None
    for a file with no ``MsgId``. Nothing is booked here."""
    file_id = accounts.record_file(conn, decision)
    if file_id is None:
        return None
    for d in decision.payments:
        c, when = d.payment, d.batch.requested_collection_date
        conn.execute(
            "INSERT INTO collection (file_id, pmt_inf_id, end_to_end_id, instruction_id,"
            " account_id, creditor_iban, amount, currency, debtor_name, debtor_iban,"
            " debtor_bic, mandate_id, mandate_signed, sequence_type, creditor_scheme_id,"
            " remittance, status, reason, reason_text, collection_date, settlement_date)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (file_id, d.batch.pmt_inf_id, c.end_to_end_id, c.instruction_id,
             d.account["id"] if d.account else None, d.batch.creditor_account,
             c.amount, c.currency, c.debtor_name, c.debtor_account, c.debtor_bic,
             c.mandate_id, c.mandate_signed, c.sequence_type, c.creditor_scheme_id,
             json.dumps(c.remittance), d.outcome, d.reason, d.reason_text,
             when.isoformat() if when else None,
             d.settlement_date.isoformat() if d.settlement_date else None))
    conn.commit()
    return file_id


def listing(conn, end_to_end_id=None) -> List[Dict[str, Any]]:
    """Every collection the bank decided on, newest first, or those with one
    ``EndToEndId`` (unique within a file, not across files)."""
    sql = ("SELECT collection.*, file.msg_id FROM collection"
           " JOIN file ON file.id = collection.file_id")
    where, params = ("", ()) if end_to_end_id is None else (
        " WHERE end_to_end_id = ?", (end_to_end_id,))
    rows = db.rows(conn, sql + where + " ORDER BY collection.id DESC", params)
    return [dict(row, remittance=json.loads(row["remittance"] or "[]")) for row in rows]


def counts(conn) -> Dict[str, int]:
    """What ``/_mock/state`` reports about collections."""
    out = {status: db.count(conn, "collection", "status = '%s'" % status)
           for status in (ACCEPTED, REJECTED)}
    out["booked"] = db.count(conn, "collection", "booked_at IS NOT NULL")
    return out
