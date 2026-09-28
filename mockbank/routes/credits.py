"""Money arriving from somebody else: a credit the test makes arrive (#91)."""
from __future__ import annotations

from .. import credits
from . import json_body, route


@route("POST", "/_mock/credits",
       note=("make money arrive in an account the bank holds, from a payer you "
             "describe; it books on its value date and shows on the statement"))
def create(h) -> None:
    body = json_body(h.body)
    try:
        credit = credits.create(h.state.conn, h.state.clock, h.state.now(),
                                body if body is not None else h.body)
    except credits.Refused as refusal:
        return h.json(refusal.status, {"error": str(refusal)})
    # Released at once if it books today, so the camt.054 is waiting already.
    h.state.release()
    h.json(201, credits.get(h.state.conn, credit["id"]))


@route("GET", "/_mock/credits",
       note="every credit made to arrive, newest first, booked or waiting")
def listing(h) -> None:
    h.json(200, credits.listing(h.state.conn))
