"""What the bank decided about the direct debits it was asked to collect (#131)."""
from __future__ import annotations

from .. import direct_debit
from . import route


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
