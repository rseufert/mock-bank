"""What the bank has sent, and what the client has sent it."""
from __future__ import annotations

from typing import Any, List

from .. import db, nacha, outbox
from . import first, flag, route

# How many request-log rows `GET /_mock/requests` hands back. The issue asks
# for a hundred; a tester reading what their client just sent wants the last
# few, and a mock left running for a day has thousands.
REQUEST_LOG_PAGE = 100

# Every path here releases first, `?leave` included: whether a caller is
# collecting or peeking, the question they are asking is "what is waiting for
# me", and a `pain.002` held back by `--status-delay-ms` is waiting once its
# time has come. Peeking should not show a different bank from collecting.


@route("GET", "/_mock/mailbox",
       note=("?leave to peek, ?raw for the XML, ?type=pain.002 to filter: "
            "the messages the bank has sent and you have not collected, "
            "oldest first; collecting takes them"))
def mailbox(h) -> None:
    """What the bank has sent and the client has not taken."""
    h.state.release()
    kind = first(h.query, "type")
    if flag(h.query, "raw"):
        # `?raw` is a sequence of XML documents, and a client splits it on
        # their declarations. A NACHA acknowledgement is plain text, and in
        # among them it would be read as the tail of the XML before it. So a
        # mix is refused, before anything is taken, and says how to ask.
        waiting = {row["type"] for row in outbox.collect(
            h.state.conn, h.state.now(), leave=True, kind=kind)}
        text = waiting & set(outbox.TEXT_TYPES)
        if text and len(waiting) > 1:
            return h.json(409, {
                "error": "?raw is a sequence of XML documents, and NACHA "
                         "acknowledgements and return files are plain text; collect "
                         "each kind apart, with ?raw&type=%s, ?raw&type=%s and "
                         "?raw&type=pain. or ?raw&type=camt."
                         % (nacha.ACK, nacha.RETURN),
                "waiting": sorted(waiting)})
    rows = outbox.collect(h.state.conn, h.state.now(),
                          leave=flag(h.query, "leave"), kind=kind)
    if flag(h.query, "raw"):
        # The bodies and nothing else. See outbox.RAW_SEPARATOR on why
        # this is a sequence of documents rather than one document.
        text = rows and all(row["type"] in outbox.TEXT_TYPES for row in rows)
        return h.text(200, outbox.raw(rows), "text/plain; charset=utf-8" if text
                      else "application/xml; charset=utf-8")
    h.json(200, outbox.as_json(rows))


@route("GET", "/_mock/mailbox/<id>")
def message(h, identifier: str) -> None:
    h.state.release()
    row = outbox.message(h.state.conn, identifier)
    if row is None:
        return unknown_message(h, identifier)
    h.text(200, row["body"].strip() + "\n",
           "text/plain; charset=utf-8" if row["type"] in outbox.TEXT_TYPES
           else "application/xml; charset=utf-8")


@route("POST", "/_mock/mailbox/<id>/unread")
def unread(h, identifier: str) -> None:
    outcome = outbox.unread(h.state.conn, identifier)
    if not outcome:
        return unknown_message(h, identifier)
    h.json(200, outcome)


def unknown_message(h, given: str) -> None:
    h.json(404, {
        "error": "no message %r" % given,
        "waiting": outbox.waiting_ids(h.state.conn),
    })


@route("GET", "/_mock/requests")
def requests(h) -> None:
    """The newest rows of the request log, so a tester can see what they sent.

    Newest first and bounded, because the point is the last few things that
    happened and a mock left running has thousands. `?path=` matches on a
    prefix, so `?path=/payments` finds the file posts without the caller
    writing out a query string they did not keep.
    """
    sql = "SELECT id, method, path, status, at FROM request_log"
    params: List[Any] = []
    wanted = first(h.query, "path")
    if wanted:
        # A literal prefix, not a LIKE pattern. With LIKE, `_` and `%` in
        # the caller's input are wildcards, so `?path=/%mock` matched
        # `/xmock/zzz` - and every `/_mock` path contains an `_`, so the
        # filter could show requests the client never sent. LIKE ignores
        # ASCII case too. The request log exists to answer "what did my
        # client actually send", and a filter that answers with more than
        # was asked for defeats the whole point of it.
        sql += " WHERE substr(path, 1, length(?)) = ?"
        params.extend([wanted, wanted])
    params.append(REQUEST_LOG_PAGE)
    h.json(200, db.rows(conn=h.state.conn,
                        sql=sql + " ORDER BY id DESC LIMIT ?", params=params))
