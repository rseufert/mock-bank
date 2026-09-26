"""What the bank sends back, and when.

Two kinds of message leave the bank in 0.1, on two schedules:

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

``release_due`` is the one function the clock calls as it moves: it books
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
        sequence = db.count(conn, "message", "type = '%s' AND account = '%s'"
                            % (messages.CAMT054.name, account_id.replace("'", "''"))) + 1
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


def collect(conn, now) -> List[Dict[str, Any]]:
    """Every released message not yet taken, oldest first, marked taken.

    The minimal mailbox: ``GET /_mock/mailbox``. Peeking, the raw XML
    sequence, filtering by type, one message by id and putting one back are
    #8's, on top of this.
    """
    rows = db.rows(conn, "SELECT id, type, account, released_at, body FROM message"
                         " WHERE released_at IS NOT NULL AND taken_at IS NULL ORDER BY id")
    if rows:
        conn.execute("UPDATE message SET taken_at = ? WHERE id IN (%s)"
                     % ",".join("?" * len(rows)), [db.stamp(now)] + [r["id"] for r in rows])
        conn.commit()
    return rows
