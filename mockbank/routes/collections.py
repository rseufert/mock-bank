"""What the bank decided about the direct debits it was asked to collect (#131)."""
from __future__ import annotations

from .. import direct_debit, outbox
from . import json_body, route


@route("GET", "/_mock/collections",
       note="every collection the bank decided on, newest first")
def listing(h) -> None:
    h.json(200, direct_debit.listing(h.state.conn))


@route("GET", "/_mock/collections/<EndToEndId>")
def collection(h, end_to_end_id: str) -> None:
    conn = h.state.conn
    found = direct_debit.listing(conn, end_to_end_id)
    if not found:
        return h.json(404, {"error": "no collection with EndToEndId %r" % end_to_end_id,
                            "known": sorted({c["end_to_end_id"] for c in
                                             direct_debit.listing(conn)
                                             if c["end_to_end_id"]})})
    # One shape, as /_mock/payments/<EndToEndId>: the newest, or with ?all
    # every one, newest first.
    h.json(200, found if "all" in h.query else found[0])


@route("POST", "/_mock/collections/<EndToEndId>/refuse",
       note=('the debtor\'s bank says no, with {"reason": "MD01"}: before '
             "settlement the collection is rejected with a further pain.002, "
             "after it the money goes back with a pacs.004"))
def refuse(h, end_to_end_id: str) -> None:
    state = h.state
    try:
        collection, before = direct_debit.refuse(state.conn, state.clock, state.now(),
                                                 end_to_end_id, json_body(h.body))
    except direct_debit.Refused as refusal:
        return h.json(refusal.status, {"error": str(refusal)})
    if before:
        outbox.queue_refusal(state.conn, collection, state.now(),
                             state.config.status_delay_ms)
    # Released at once if it is due today, so the answer is waiting already.
    state.release()
    h.json(200, direct_debit.listing(state.conn, end_to_end_id)[0])
