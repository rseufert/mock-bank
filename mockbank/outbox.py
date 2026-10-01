"""What the bank sends back, and when.

Four kinds of message leave the bank, each on its own schedule:

* **``pain.002``** is written when a file is decided and queued in the
  ``message`` table, due ``--status-delay-ms`` after receipt (0 by default,
  so a test sees it at once; a tester can ask for the realistic few minutes).
  A ``silent`` debtor gets none, and neither does a body the bank could not
  read far enough to find a ``MsgId``: a status report has to name the
  message it reports on, and the mock will not invent one.
* **``pacs.004``** is written when a returned payment comes back, on the day
  ``return-later`` set for it, with a ``camt.054`` credit beside it.
* **``camt.054``** is written when payments book: one per account for each
  booking, an entry per payment, so everything one move of the clock books
  on an account is reported together. It cannot be written earlier, because
  a later file can add payments to the same account and day, so it is
  released the moment it is written.

* **``camt.053``** is written when a business day ends: one per open account
  the bank holds, for every business day an advance of the clock passes the
  end of, in order, empty days included (``issue_statements``).

* **``camt.052``** is written when a test asks for one: the day so far for one
  account, released at once (``issue_report``, #132).

``release_due`` is the function the clock calls first as it moves: it books
every payment whose settlement date has come, writes their ``camt.054``, and
releases every queued message whose time has come. It is safe to call twice:
what it has done, it does not do again. ``collect`` is the minimal mailbox
over what it released; #8 builds the rest of the mailbox on it.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List

from . import accounts, bai2, credits, db, messages, nacha


# Every message type that is not XML, and the extension it is written under.
# The four callers of this ask "is this XML, and if not what does it end in" -
# not "is this NACHA" - so the union lives here rather than in either format's
# module, and neither has to know about the other.
TEXT_TYPES = dict(nacha.TEXT_TYPES, **bai2.TEXT_TYPES)

# What a message the bank writes on booking reports: `reports` in
# `GET /_mock/queue`, and part of the message's key (#155).
SETTLING, ARRIVING, RETURNED = "payments settling", "money arriving", "payments returned"


def queue_status(conn, decision, file_id, now, delay_ms=0,
                 source="") -> List[Dict[str, Any]]:
    """Queue the ``pain.002`` for a decided file; what was queued.

    A file refused before it had a ``MsgId`` is not booked, so it has no
    ``file_id``, and is still answered: one pipeline, two doors, one answer
    (#64). Over HTTP the refusal is in the response too, but a folder client
    has only the pickup directory. Its ``pain.002`` is numbered from its own
    sequence, and names ``source``, the dropped file, if there is one.
    """
    if not decision.reported:
        return []
    if file_id is None and not (decision.msg_id is None and decision.rejected_outright):
        return []
    if file_id is None and _silent(conn, decision):
        return []
    # A file with batches from several debtor accounts gets one status, and
    # it is attributed to the first debtor account the bank holds; the
    # report itself covers every batch.
    debtor = next((d.account for d in decision.payments if d.account), None)
    if debtor is None and decision.payment_file is not None:
        debtor = next((held for held in (accounts.by_iban(conn, _sender(b))
                                         for b in decision.payment_file.batches) if held),
                      None)
    # That account's format decides what the status is: a pain.002, or for a
    # NACHA account the plain acknowledgement (#53).
    if debtor is not None and debtor.get("format") == "nacha":
        kind, prefix = nacha.ACK, "MB-ACK-"
    else:
        kind, prefix = messages.PAIN002.name, "MB-P002-"
    if file_id is not None:
        msg_id = "%s%06d" % (prefix, file_id)
    else:
        msg_id = "%sN%06d" % (prefix, db.next_value(conn, "pain002-unread"))
    if kind == nacha.ACK:
        body = nacha.acknowledgement(decision, msg_id, now, source)
    else:
        body = messages.write_pain002(decision, msg_id, now, source).decode("utf-8")
    due = now + datetime.timedelta(milliseconds=delay_ms)
    account = debtor["id"] if debtor else None
    cursor = conn.execute(
        "INSERT INTO message (type, account, file_id, due_at, body) VALUES (?,?,?,?,?)",
        (kind, account, file_id, db.stamp(due), body))
    return [{"id": cursor.lastrowid, "key": "m%d" % cursor.lastrowid, "type": kind,
             "account": account, "due_at": db.stamp(due)}]


def _silent(conn, decision) -> bool:
    """Whether a debtor the bank could read is ``silent``, which sends nothing."""
    batches = decision.payment_file.batches if decision.payment_file else []
    for batch in batches:
        held = accounts.by_iban(conn, _sender(batch))
        if held and held["behaviour"] == "silent":
            return True
    return False


def _sender(batch) -> str:
    """The account a batch was sent for: a payment batch's debtor account, a
    collection batch's creditor account (#131)."""
    if isinstance(batch, messages.CollectionBatch):
        return batch.creditor_account or ""
    return batch.debtor_account or ""


def _key(conn, kind, account, day, reports, file_id=None) -> str:
    """What pairs a message the bank will write with the message once written
    (#155): ``GET /_mock/queue`` shows it before, the mailbox after.

    The type, the account, the day it books under, what it reports and - where
    there is one message per original file - the file. The row keeps it because
    it knows only the first two: ``due_at`` is when the booking ran, not the
    day booked, and three kinds of ``camt.054`` store the same columns.

    A second message of one kind for one day - a file posted on its own
    settlement day, after that day's notification went out - is ``#2``, so a
    key names one message. Asked before the message is written, this is the key
    it will get; asked again after, the next one.
    """
    base = "/".join([kind, account, day, reports.replace(" ", "-")]
                    + (["file-%d" % file_id] if file_id else []))
    used = db.one(conn, "SELECT COUNT(*) AS n FROM message WHERE key = ?"
                        " OR substr(key, 1, ?) = ?",
                  (base, len(base) + 1, base + "#"))["n"]
    return "%s#%d" % (base, used + 1) if used else base


def _queue_return_file(conn, account, rows, day, file_id, now) -> None:
    """A NACHA account's returns for one day and one original file, as the
    return file NACHA sends where ISO 20022 sends a pacs.004 (#54)."""
    for row in rows:
        # The account the payment was made to, as the file named it: a held
        # account's domestic number, since resolve gave the payment its IBAN.
        held = accounts.by_iban(conn, row.get("creditor_iban") or "")
        row["creditor_number"] = (held["account_number"] if held
                                  else row.get("creditor_iban") or "")
    sequence = db.next_value(conn, "nacha.return:" + account["id"])
    modifier = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"[(sequence - 1) % 36]
    body = nacha.return_file(accounts.ROUTING, account, rows, day, now, modifier)
    conn.execute(
        "INSERT INTO message (type, account, file_id, due_at, body, key)"
        " VALUES (?,?,?,?,?,?)",
        (nacha.RETURN, account["id"], file_id, db.stamp(now), body,
         _key(conn, nacha.RETURN, account["id"], day.isoformat(), RETURNED, file_id)))


def upcoming(conn, file_id) -> List[Dict[str, Any]]:
    """The ``camt.054`` s a file's accepted, unbooked payments will bring, and
    when: one per account per settlement date."""
    rows = db.rows(conn, "SELECT DISTINCT account_id, settlement_date FROM payment"
                         " WHERE file_id = ? AND status = ? AND booked_at IS NULL"
                         " AND account_id IS NOT NULL ORDER BY settlement_date, account_id",
                   (file_id, accounts.ACCEPTED))
    return [{"type": messages.CAMT054.name, "account": r["account_id"],
             "due_on": r["settlement_date"],
             "key": _key(conn, messages.CAMT054.name, r["account_id"],
                         r["settlement_date"], SETTLING)} for r in rows]


def queued(conn, clock, kind="") -> List[Dict[str, Any]]:
    """What the bank is going to send and has not released, soonest first (#155).

    Reads and changes nothing: nothing is released, nothing is taken. Two kinds
    of entry, told apart by ``written``:

    - a message already **written** and waiting for its time, which today is a
      status report held back by ``--status-delay-ms``. It has an ``id``, and
      ``GET /_mock/mailbox/<id>`` shows it;
    - a message the bank **will write** when something books, which is most of
      what a caller means by "about to send": the ``camt.054`` for payments
      accepted and not yet settled, the one for money arriving, and what a
      return brings - a ``pacs.004`` or a NACHA return file per original file,
      and a ``camt.054`` credit where money comes back. These have no ``id``
      yet, and are due at the start of their day in bank time, which is when
      an advance to that day books them. ``reports`` says which of those it
      is, because a debit notification and a return's credit for one account
      on one day are two messages of one type.

    Every entry has a ``key``, and the message is in the mailbox under the same
    one (``_key``, ``key_of``): that is what pairs the two.

    A statement is not here. It is not owed for anything that has happened: one
    is written for every open account each time the clock is advanced past the
    end of a business day, so listing "the next one" would be listing the
    calendar. ``kind`` is the mailbox's prefix filter.
    """
    out = [{"id": row["id"], "key": key_of(row), "type": row["type"],
            "account": row["account"],
            "fileId": row["file_id"], "msgId": row["msg_id"], "dueAt": row["due_at"],
            "written": True, "reports": "a file's status"}
           for row in db.rows(conn, "SELECT message.*, file.msg_id FROM message"
                                    " LEFT JOIN file ON file.id = message.file_id"
                                    " WHERE released_at IS NULL ORDER BY message.id")]
    expected = set()
    for row in db.rows(conn, "SELECT account_id, settlement_date FROM payment"
                             " WHERE status = ? AND booked_at IS NULL"
                             " AND account_id IS NOT NULL", (accounts.ACCEPTED,)):
        expected.add((row["settlement_date"], messages.CAMT054.name, row["account_id"],
                      None, SETTLING))
    for row in db.rows(conn, "SELECT credit.account_id, booking_date FROM credit"
                             " JOIN account ON account.id = credit.account_id"
                             " WHERE booked_at IS NULL AND account.closed = 0"):
        expected.add((row["booking_date"], messages.CAMT054.name, row["account_id"],
                      None, ARRIVING))
    for row in db.rows(conn, "SELECT payment.*, account.format FROM payment"
                             " JOIN account ON account.id = payment.account_id"
                             " WHERE return_due IS NOT NULL AND returned_at IS NULL"
                             " AND (payment.status = ? OR (payment.status = ?"
                             " AND booked_at IS NOT NULL))",
                       (accounts.REJECTED, accounts.ACCEPTED)):
        answer = nacha.RETURN if row["format"] == "nacha" else messages.PACS004.name
        expected.add((row["return_due"], answer, row["account_id"], row["file_id"],
                      RETURNED))
        if row["status"] == accounts.ACCEPTED:
            # Money comes back only for a payment that was debited (#144).
            expected.add((row["return_due"], messages.CAMT054.name, row["account_id"],
                          None, RETURNED))
    names = {row["id"]: row["msg_id"] for row in db.rows(conn, "SELECT id, msg_id FROM file")}
    for day, kind_, account, file_id, reports in expected:
        start = datetime.datetime.combine(datetime.date.fromisoformat(day),
                                          datetime.time(0, 0), tzinfo=clock.zone)
        out.append({"id": None, "key": _key(conn, kind_, account, day, reports, file_id),
                    "type": kind_, "account": account, "fileId": file_id,
                    "msgId": names.get(file_id), "dueAt": db.stamp(start),
                    "written": False, "reports": reports})
    out = [entry for entry in out if entry["type"].startswith(kind)]
    return sorted(out, key=lambda e: (e["dueAt"], not e["written"], e["type"],
                                      e["account"] or "", e["fileId"] or 0, e["reports"]))


def release_due(conn, now, today, clock) -> List[Dict[str, Any]]:
    """Book what has come due, write its ``camt.054``, and release every
    queued message whose time has come. Returns what was released.

    Payments from a ``return-later`` account are scheduled to come back as
    they book, which needs the ``clock`` for its business days, and those
    whose day has come are credited back with a ``pacs.004`` and a
    ``camt.054`` credit (``_release_returns``). The clock is required rather
    than optional: a caller that left it out would book payments whose
    returns then silently never happened."""
    booked = accounts.book_due(conn, today, commit=False)
    accounts.schedule_returns(conn, booked, clock)
    _release_returns(conn, now, today)
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
            "INSERT INTO message (type, account, due_at, body, key) VALUES (?,?,?,?,?)",
            (messages.CAMT054.name, account_id, stamp, body.decode("utf-8"),
             _key(conn, messages.CAMT054.name, account_id, day, SETTLING)))
    _release_credits(conn, now, today, clock)
    released = db.rows(conn, "SELECT id, type, account, due_at FROM message"
                             " WHERE released_at IS NULL AND due_at <= ? ORDER BY id",
                       (stamp,))
    conn.execute("UPDATE message SET released_at = ? WHERE released_at IS NULL"
                 " AND due_at <= ?", (stamp, stamp))
    conn.commit()
    return released


def _release_credits(conn, now, today, clock):
    """Book the money arriving today, and say so: a ``camt.054`` credit per
    account and booking day, the same granularity as the debits (#91)."""
    stamp = db.stamp(now)
    by_day: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in credits.book_due(conn, today, clock):
        by_day.setdefault((row["account_id"], row["booking_date"]), []).append(
            dict(row, incoming=True))
    for (account_id, day), rows in sorted(by_day.items()):
        account = accounts.require(conn, account_id)
        sequence = db.next_value(conn, "camt.054:" + account_id)
        body = messages.write_camt054(account, rows, datetime.date.fromisoformat(day),
                                      "MB-C054-%s-%d" % (account_id[:18], sequence), now)
        conn.execute(
            "INSERT INTO message (type, account, due_at, body, key) VALUES (?,?,?,?,?)",
            (messages.CAMT054.name, account_id, stamp, body.decode("utf-8"),
             _key(conn, messages.CAMT054.name, account_id, day, ARRIVING)))


def _release_returns(conn, now, today):
    """Credit back what is due to come back, and tell the client twice: a
    ``pacs.004`` per account, day and original file, and a ``camt.054`` credit
    per account and day - the same granularity as the debits."""
    returned = accounts.book_returns(conn, today)
    stamp = db.stamp(now)
    by_file: Dict[tuple, List[Dict[str, Any]]] = {}
    by_day: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in returned:
        by_file.setdefault((row["account_id"], row["return_due"], row["file_id"]), []).append(row)
        by_day.setdefault((row["account_id"], row["return_due"]), []).append(row)
    # A NACHA account's rejected payments come back too, as return entries,
    # though nothing is credited: they were never debited (#54).
    for row in accounts.rejected_returns_due(conn, today):
        by_file.setdefault((row["account_id"], row["return_due"], row["file_id"]), []).append(row)
    for (account_id, day, file_id), rows in sorted(by_file.items()):
        account = accounts.require(conn, account_id)
        if account["format"] == "nacha":
            _queue_return_file(conn, account, rows, datetime.date.fromisoformat(day),
                               file_id, now)
            continue
        sequence = db.next_value(conn, "pacs.004:" + account_id)
        body = messages.write_pacs004(account, rows, datetime.date.fromisoformat(day),
                                      "MB-P004-%s-%d" % (account_id[:18], sequence), now)
        conn.execute(
            "INSERT INTO message (type, account, file_id, due_at, body, key)"
            " VALUES (?,?,?,?,?,?)",
            (messages.PACS004.name, account_id, file_id, stamp, body.decode("utf-8"),
             _key(conn, messages.PACS004.name, account_id, day, RETURNED, file_id)))
    for (account_id, day), rows in sorted(by_day.items()):
        account = accounts.require(conn, account_id)
        sequence = db.next_value(conn, "camt.054:" + account_id)
        body = messages.write_camt054(account, [dict(r, credit=True) for r in rows],
                                      datetime.date.fromisoformat(day),
                                      "MB-C054-%s-%d" % (account_id[:18], sequence), now)
        conn.execute(
            "INSERT INTO message (type, account, due_at, body, key) VALUES (?,?,?,?,?)",
            (messages.CAMT054.name, account_id, stamp, body.decode("utf-8"),
             _key(conn, messages.CAMT054.name, account_id, day, RETURNED)))
    return returned


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
    sql = ("SELECT id, type, account, file_id, due_at, released_at, body, key"
           " FROM message WHERE released_at IS NOT NULL AND taken_at IS NULL")
    params: List[Any] = []
    if kind:
        # A literal prefix, not a LIKE pattern. LIKE would make `_` and `%` in
        # the caller's own input into wildcards - and every message type is full
        # of dots and would soon have had an `_` in it - and LIKE is
        # case-insensitive for ASCII, so `PAIN.002` would match too. A filter
        # that quietly matches more than it was given is worse than no filter.
        sql += " AND substr(type, 1, length(?)) = ?"
        params.extend([kind, kind])
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


def key_of(row) -> str:
    """A message's key (#155): the one it was written under, which
    ``GET /_mock/queue`` showed before it existed, or ``m<id>`` for a message
    that was a row all along - a status report, a statement, a report - and
    for one written before the key was kept."""
    return row["key"] or "m%d" % row["id"]


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
    return [{"id": row["id"], "key": key_of(row), "type": row["type"],
             "account": row["account"],
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


def _statement_body(account, day, number, opening, closing, shown, now, clock):
    """(message type, body) for one account's statement, in its own format.

    A NACHA-format account is sent BAI2 and everything else a ``camt.053``. The
    two writers take the same statement data, which is what makes this a choice
    of renderer rather than a second statement: the balances and the entries are
    already worked out above and neither writer computes them.

    ``statement-gap`` needs nothing here. It drops an entry from ``shown``
    before this is called, so both formats leave the same entry out and both
    keep the balances true - which is the point of the behaviour, and would stop
    being true if either writer recomputed a total from the entries it was given.
    """
    if account.get("format") == "nacha":
        # The receiver is left to default to the account id. An earlier version
        # passed the account's name, which is a display string where BAI2 wants
        # an identification - and the 02's originator and ultimate receiver are
        # already the thing #57 has to settle against an outside sample, so this
        # is not the place to add a third guess.
        return bai2.STATEMENT, bai2.write_statement(
            account, day, number, opening, closing, shown, created_at=now)
    msg_id = "MB-C053-%s-%d" % (account["id"][:18], number)
    return messages.CAMT053.name, messages.write_camt053(
        account, day, number, opening, closing, shown, msg_id, now,
        clock.zone).decode("utf-8")


def _position(conn, account, day):
    """(opening, closing, entries) for one account's ``day``, as the
    statement and the intraday report both state them.

    The closing is the account's balance now with every movement booked after
    ``day`` undone, and the opening is the closing with the day's own
    movements undone too. For today, nothing has booked after it, so the
    closing is the balance now: what a report calls the interim balance.
    """
    when = day.isoformat()
    # Debits booked after the day took money out since; returns that
    # came back after it put money in. Undo both to reach the day's end.
    # A return only put money in if the payment was debited in the first
    # place. A NACHA account's rejections are given a return as well (#54,
    # option (a)) and nothing is credited for those, because nothing was ever
    # booked - so `booked_at IS NOT NULL` is what separates the two, and it is
    # the same condition `accounts.book_returns` credits on (#144).
    later_debits = db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM payment"
                                " WHERE account_id = ? AND booked_at IS NOT NULL"
                                " AND settlement_date > ?",
                          (account["id"], when))["total"]
    later_credits = db.one(conn, "SELECT COALESCE(SUM(amount), 0) AS total FROM payment"
                                 " WHERE account_id = ? AND returned_at IS NOT NULL"
                                 " AND booked_at IS NOT NULL AND return_due > ?",
                           (account["id"], when))["total"]
    booked = db.rows(conn, "SELECT payment.*, file.msg_id FROM payment"
                           " JOIN file ON file.id = payment.file_id"
                           " WHERE account_id = ? AND booked_at IS NOT NULL"
                           " AND settlement_date = ? ORDER BY payment.id",
                     (account["id"], when))
    came_back = db.rows(conn, "SELECT payment.*, file.msg_id FROM payment"
                              " JOIN file ON file.id = payment.file_id"
                              " WHERE account_id = ? AND returned_at IS NOT NULL"
                              " AND booked_at IS NOT NULL"
                              " AND return_due = ? ORDER BY payment.id",
                        (account["id"], when))
    booked += [dict(p, credit=True) for p in came_back]
    # Money that arrived from somebody else (#91): on the day it booked,
    # and undone, like a return, for any day before it.
    later_credits += credits.booked_after(conn, account["id"], when)
    booked += [dict(c, incoming=True, credit=True)
               for c in credits.booked_on(conn, account["id"], when)]
    closing = account["balance"] + int(later_debits) - int(later_credits)
    opening = closing + sum(-p["amount"] if p.get("credit") else p["amount"]
                            for p in booked)
    return opening, closing, booked


def issue_statements(conn, clock, days, now) -> List[Dict[str, Any]]:
    """A ``camt.053`` for every open account for each of ``days``, in order,
    released at once. A day already issued for an account is skipped, so
    issuing is safe to repeat.

    The balances are computed, not carried: the closing balance of a day is
    the account's balance now with every movement after that day undone - the
    debits booked and the returns credited since - and the opening is the
    closing with that day's own movements undone. So closing is opening
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
            opening, closing, booked = _position(conn, account, day)
            shown = booked[:-1] if account["behaviour"] == "statement-gap" else booked
            # One counter for both formats, keyed on the camt.053 name it was
            # created under: an account that changes format keeps counting where
            # it left off rather than restarting at 1 and colliding with the
            # statements it has already been sent.
            number = db.next_value(conn, "camt.053:" + account["id"])
            kind, text = _statement_body(account, day, number, opening, closing,
                                         shown, now, clock)
            cursor = conn.execute(
                "INSERT INTO message (type, account, due_at, released_at, body)"
                " VALUES (?,?,?,?,?)",
                (kind, account["id"], stamp, stamp, text))
            conn.execute(
                "INSERT INTO statement (account, day, number, opening, closing, entries,"
                " message_id) VALUES (?,?,?,?,?,?,?)",
                (account["id"], day.isoformat(), number, opening, closing, len(shown),
                 cursor.lastrowid))
            issued.append({"account": account["id"], "day": day.isoformat(),
                           "number": number, "message_id": cursor.lastrowid})
    conn.commit()
    return issued


class Unreportable(ValueError):
    """An account the bank will not report on: closed."""


def issue_report(conn, clock, account, now) -> Dict[str, Any]:
    """A ``camt.052`` for ``account`` as of ``now``, released at once (#132).

    The same position the day's statement will state, so far: the opening,
    the balance now, and every entry booked today. ``statement-gap`` is not
    applied - it is a fault in the statement, and a report that shows the
    entry the statement then leaves out is what lets a reconciler find it.
    Reports are numbered on their own counter, apart from the statements'.
    """
    if account["closed"]:
        raise Unreportable("account %r is closed, and the bank does not report on "
                           "a closed account" % account["id"])
    today = clock.today()
    opening, interim, booked = _position(conn, account, today)
    number = db.next_value(conn, "camt.052:" + account["id"])
    msg_id = "MB-C052-%s-%d" % (account["id"][:18], number)
    body = messages.write_camt052(account, today, number, opening, interim, booked,
                                  msg_id, now, clock.zone).decode("utf-8")
    stamp = db.stamp(now)
    cursor = conn.execute(
        "INSERT INTO message (type, account, due_at, released_at, body)"
        " VALUES (?,?,?,?,?)",
        (messages.CAMT052.name, account["id"], stamp, stamp, body))
    conn.commit()
    return {"account": account["id"], "day": today.isoformat(), "number": number,
            "opening": opening, "interim": interim, "entries": len(booked),
            "message_id": cursor.lastrowid}


def statements(conn, account_id) -> List[Dict[str, Any]]:
    """What ``GET /_mock/accounts/<id>/statements`` answers, oldest first."""
    return db.rows(conn, "SELECT number, day, opening, closing, entries, message_id"
                         " FROM statement WHERE account = ? ORDER BY number", (account_id,))
