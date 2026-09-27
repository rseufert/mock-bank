"""The payments door, `POST /payments`, and what the bank decided."""
from __future__ import annotations

from .. import accounts
from . import route


@route("POST", "/payments",
       note=("send a pain.001: the bank decides each payment, books what "
            "is due and answers with a JSON summary"))
def payments_in(h) -> None:
    """POST /payments: the pipeline, and its answer as JSON.

    The work is `State.receive`, which the drop directory calls with the
    same bytes; this is the HTTP door onto it and nothing more.
    """
    answer, decision, _findings = h.state.receive(
        h.body, h.headers.get("Content-Type") or "")
    h.json(422 if decision.rejected_outright else 202, answer)


@route("GET", "/_mock/payments",
       note="every payment the bank decided on, newest first")
def listing(h) -> None:
    h.json(200, accounts.payments(h.state.conn))


@route("GET", "/_mock/payments/<EndToEndId>")
def payment(h, end_to_end_id: str) -> None:
    conn = h.state.conn
    found = accounts.payments(conn, end_to_end_id)
    if not found:
        return h.json(404, {"error": "no payment with EndToEndId %r" % end_to_end_id,
                            "known": sorted({p["end_to_end_id"] for p in
                                             accounts.payments(conn)
                                             if p["end_to_end_id"]})})
    # Always one shape: the newest payment with that id, or with
    # ?all every one, newest first.
    h.json(200, found if "all" in h.query else found[0])
