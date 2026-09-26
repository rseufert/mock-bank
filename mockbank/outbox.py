"""What the bank sends back, and when.

Three kinds of message leave the bank in 0.1, on three schedules:

* **``pain.002``** is written when a file is decided and queued in the
  ``message`` table, due ``--status-delay-ms`` after receipt (0 by default,
  so a test sees it at once; a tester can ask for the realistic few minutes).
  A ``silent`` debtor gets none, and neither does a body the bank could not
  read far enough to find a ``MsgId``: a status report has to name the
  message it reports on, and the mock will not invent one.
* **``camt.054``** is written when payments book: one per account for each
  booking, an entry per payment, so everything one move of the clock books
  on an account is reported together. It cannot be written earlier, because
  a later file can add payments to the same account and day, so it is
  released the moment it is written.

* **``camt.053``** is written when a business day ends: one per open account
  the bank holds, for every business day an advance of the clock passes the
  end of, in order, empty days included (``issue_statements``).

``release_due`` is the function the clock calls first as it moves: it books
every payment whose settlement date has come, writes their ``camt.054``, and
releases every queued message whose time has come. It is safe to call twice:
what it has done, it does not do again. ``collect`` is the minimal mailbox
over what it released; #8 builds the rest of the mailbox on it.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List

from . import accounts, db, messages


def queue_status(conn, decision, file_id, now, delay_ms=0) -> List[Dict[str, Any]]:
    """Queue the ``pain.002`` for a decided and booked file; what was queued."""
    if not decision.reported or decision.msg_id is None or file_id is None:
        return []
    msg_id = "MB-P002-%06d" % file_id
    body = messages.write_pain002(decision, msg_id, now)
    due = now + datetime.timedelta(milliseconds=delay_ms)
    # A file with batches from several debtor accounts gets one pain.002, and
    # it is attributed to the first debtor account the bank holds; the
    # report itself covers every batch.
    debtor = next((d.account["id"] for d in decision.payments if d.account), None)
    if debtor is None and decision.payment_file is not None:
        for batch in decision.payment_file.batches:
            held = accounts.by_iban(conn, batch.debtor_account or "")
            if held:
                debtor = held["id"]
                break
    cursor = conn.execute(
        "INSERT INTO message (type, account, file_id, due_at, body) VALUES (?,?,?,?,?)",
        (messages.PAIN002.name, debtor, file_id, db.stamp(due), body.decode("utf-8")))
    return [{"id": cursor.lastrowid, "type": messages.PAIN002.name, "account": debtor,
             "due_at": db.stamp(due)}]


def upcoming(conn, file_id) -> List[Dict[str, Any]]:
    """The ``camt.054`` s a file's accepted, unbooked payments will bring, and
    when: one per account per settlement date."""
    rows = db.rows(conn, "SELECT DISTINCT account_id, settlement_date FROM payment"
                         " WHERE file_id = ? AND status = ? AND booked_at IS NULL"
                         " AND account_id IS NOT NULL ORDER BY settlement_date, account_id",
                   (file_id, accounts.ACCEPTED))
    return [{"type": messages.CAMT054.name, "account": r["account_id"],
             "due_on": r["settlement_date"]} for r in rows]


def release_due(conn, now, today) -> List[Dict[str, Any]]:
    """Book what has come due, write its ``camt.054``, and release every
    queued message whose time has come. Returns what was released."""
    booked = accounts.book_due(conn, today, commit=False)
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in booked:
        groups.setdefault((row["account_id"], row["settlement_date"]), []).append(row)
    stamp = db.stamp(now)
    for (account_id, day), rows in sorted(groups.items()):
        account = accounts.require(conn, account_id)
        for row in rows:
            row["msg_id"] = db.one(conn, "SELECT msg_id FROM file WHERE id = ?",
                                   (row["file_id"],))["msg_id"]
        sequence = db.next_value(conn, "camt.054:" + account_id)
        msg_id = "MB-C054-%s-%d" % (account_id[:18], sequence)
        body = messages.write_camt054(account, rows, datetime.date.fromisoformat(day),
                                      msg_id, now)
        conn.execute(
            "INSERT INTO message (type, account, due_at, body) VALUES (?,?,?,?)",
            (messages.CAMT054.name, account_id, stamp, body.decode("utf-8")))
    released = db.rows(conn, "SELECT id, type, account, due_at FROM message"
                             " WHERE released_at IS NULL AND due_at <= ? ORDER BY id",
                       (stamp,))
    conn.execute("UPDATE message SET released_at = ? WHERE released_at IS NULL"
                 " AND due_at <= ?", (stamp, stamp))
    conn.commit()
    return released


def collect(conn, now, leave=False, kind="") -> List[Dict[str, Any]]:
    """Every released message not yet taken, oldest first.

    Taken as they are handed over, which is what makes a second call return
    what arrived since rather than everything again. ``leave`` peeks instead,
    for a test that wants to look without consuming - and peeking is the
    exception, because a mailbox that never empties cannot tell a client
    "nothing new has happened".

    ``kind`` filters on the message type by prefix, so ``pain.002`` finds
    ``pain.002.001.10``: a caller matching on the version would have to change
    when the mock starts writing a newer one, and the version is not what they
    mean.
    """
    sql = ("SELECT id, type, account, file_id, due_at, released_at, body"
           " FROM message WHERE released_at IS NOT NULL AND taken_at IS NULL")
    params: List[Any] = []
    if kind:
        sql += " AND type LIKE ?"
        params.append(kind + "%")
    rows = db.rows(conn, sql + " ORDER BY id", params)
    if rows and not leave:
        conn.execute("UPDATE message SET taken_at = ? WHERE id IN (%s)"
                     % ",".join("?" * len(rows)), [db.stamp(now)] + [r["id"] for r in rows])
        conn.commit()
    return rows


def message(conn, message_id) -> Dict[str, Any]:
    """One message by id, taken or not, or None.

    Not restricted to what is waiting: a test that has collected a message and
    wants to look at it again is the ordinary case, and a 404 for something the
    mock is still holding would be a lie.
    """
    return db.one(conn, "SELECT * FROM message WHERE id = ?", (message_id,))


def waiting_ids(conn) -> List[int]:
    """The ids a caller could ask for now, to name in a 404."""
    return [row["id"] for row in db.rows(
        conn, "SELECT id FROM message WHERE released_at IS NOT NULL"
              " AND taken_at IS NULL ORDER BY id")]


def unread(conn, message_id) -> Dict[str, Any]:
    """Put a taken message back in the mailbox, for a test that collects twice.

    A message that was never taken is left alone and said to be untaken rather
    than refused: the caller asked for it to be collectable and it is.
    Releasing it is a different question - an unreleased message is not in the
    mailbox to begin with, and this does not pretend otherwise.
    """
    row = message(conn, message_id)
    if row is None:
        return {}
    was_taken = row["taken_at"] is not None
    if was_taken:
        conn.execute("UPDATE message SET taken_at = NULL WHERE id = ?", (message_id,))
        conn.commit()
    return {
        "id": row["id"], "type": row["type"], "unread": True,
        "wasTaken": was_taken,
        "released": row["released_at"] is not None,
    }


# One `<?xml?>` declaration per message, concatenated - which is what a bank's
# drop directory looks like to a client that cats the files, and what `?raw`
# hands back. It is deliberately *not* a single document: wrapping several
# messages in an invented root element would put an element on the wire that no
# ISO 20022 schema has, and a client that learnt to expect it would be learning
# something no real bank sends. So the concatenation is documented instead of
# disguised, and a client that wants one message parses one message.
RAW_SEPARATOR = "\n"


def raw(rows) -> str:
    """The bodies of `rows` as an XML sequence, and nothing else."""
    return RAW_SEPARATOR.join(row["body"].strip() for row in rows) + "\n"


def summary(row) -> str:
    """One line a reader can scan: what it is and who it is about."""
    account = row.get("account") or "-"
    return "%s for %s" % (row["type"], account)


def as_json(rows) -> List[Dict[str, Any]]:
    """The mailbox listing: what each message is, and the message.

    The body is included, which was not my first instinct - a listing of four
    `camt.053` s is most of a screenful of XML. But mock-edi's mailbox returns
    the document with the row and #8 says to match it, and a tester who knows
    one mock is meant to know the other. It also means one request gets both
    what arrived and what it says, which is what a client written against this
    actually wants; `?raw` exists for the case where the XML is all you want,
    not to make the JSON form incomplete.
    """
    return [{"id": row["id"], "type": row["type"], "account": row["account"],
             "releasedAt": row["released_at"], "dueAt": row["due_at"],
             "fileId": row["file_id"], "summary": summary(row),
             "bytes": len(row["body"].encode("utf-8")),
             "body": row["body"]}
            for row in rows]


def ended_business_days(clock, before, after) -> List[datetime.date]:
    """The business days whose end an advance from ``before`` to ``after``
    passed: every business day from ``before`` 's date up to, and not
    including, ``after`` 's."""
    days, day = [], before.date()
    while day < after.date():
        if clock.is_business_day(day):
            days.append(day)
        day += datetime.timedelta(days=1)
    return days


def issue_statements(conn, clock, days, now) -> List[Dict[str, Any]]:
    """A ``camt.053`` for every open account for each of ``days``, in order,
    released at once. A day already issued for an account is skipped, so
    issuing is safe to repeat.

    The balances are computed, not carried: the closing balance of a day is
    the account's balance now less every debit booked after that day, and the
    opening is the closing plus that day's debits. So closing is opening
    less the entries to the cent, and the next opening is this closing, as
    long as the balance only moved by booking. A balance changed by ``PATCH``
    is a change no entry explains, and the next opening shows the jump.

    ``statement-gap`` leaves the last entry of a statement that has any off
    it, and nothing else: the balances stay right, so they no longer
    reconcile with the entries shown, by exactly the missing amount.
    """
    stamp = db.stamp(now)
    issued = []
    for day in sorted(days):
        for account in accounts.listing(conn):
            if account["closed"] or db.one(
                    conn, "SELECT id FROM statement WHERE account = ? AND day = ?",
                    (account["id"], day.isoformat())):
                continue
            later = db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM payment"
                                 " WHERE account_id = ? AND booked_at IS NOT NULL"
                                 " AND settlement_date > ?",
                           (account["id"], day.isoformat()))["total"]
            booked = db.rows(conn, "SELECT payment.*, file.msg_id FROM payment"
                                   " JOIN file ON file.id = payment.file_id"
                                   " WHERE account_id = ? AND booked_at IS NOT NULL"
                                   " AND settlement_date = ? ORDER BY payment.id",
                             (account["id"], day.isoformat()))
            closing = account["balance"] + int(later)
            opening = closing + sum(p["amount"] for p in booked)
            shown = booked[:-1] if account["behaviour"] == "statement-gap" else booked
            number = db.next_value(conn, "camt.053:" + account["id"])
            msg_id = "MB-C053-%s-%d" % (account["id"][:18], number)
            body = messages.write_camt053(account, day, number, opening, closing, shown,
                                          msg_id, now, clock.zone)
            cursor = conn.execute(
                "INSERT INTO message (type, account, due_at, released_at, body)"
                " VALUES (?,?,?,?,?)",
                (messages.CAMT053.name, account["id"], stamp, stamp, body.decode("utf-8")))
            conn.execute(
                "INSERT INTO statement (account, day, number, opening, closing, entries,"
                " message_id) VALUES (?,?,?,?,?,?,?)",
                (account["id"], day.isoformat(), number, opening, closing, len(shown),
                 cursor.lastrowid))
            issued.append({"account": account["id"], "day": day.isoformat(),
                           "number": number, "message_id": cursor.lastrowid})
    conn.commit()
    return issued


def statements(conn, account_id) -> List[Dict[str, Any]]:
    """What ``GET /_mock/accounts/<id>/statements`` answers, oldest first."""
    return db.rows(conn, "SELECT number, day, opening, closing, entries, message_id"
                         " FROM statement WHERE account = ? ORDER BY number", (account_id,))
