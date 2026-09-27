"""Bank time: moving it, and the days the bank does not settle on."""
from __future__ import annotations

import json

from .. import clock as clock_module
from . import first, route


@route("POST", "/_mock/advance",
       note="move bank time: ?days=N (calendar days) or ?to=YYYY-MM-DD")
def advance(h) -> None:
    """Move bank time, then let whatever came due happen.

    `?days=N` counts calendar days and `?to=YYYY-MM-DD` moves to midnight on
    that date; the answer says which it did and lists the business days the
    move passed through, because the two readings differ and a caller should
    not have to guess which one it got.
    """
    raw_days, raw_to = first(h.query, "days"), first(h.query, "to")
    if bool(raw_days) == bool(raw_to):
        return h.json(400, {
            "error": "advance takes ?days=N or ?to=YYYY-MM-DD, and one of "
                     "them", "given": h.path})
    try:
        if raw_days:
            try:
                days = int(raw_days)
            except ValueError:
                # Not a whole number, or not a number: float() first so
                # that 0.5, nan and inf each get the message that fits
                # them rather than one about digits.
                try:
                    days = float(raw_days)
                except ValueError:
                    raise clock_module.Invalid(
                        "days is a whole number of days, and %r is not a "
                        "number at all" % raw_days) from None
            outcome = h.state.clock.advance(days=days)
        else:
            outcome = h.state.clock.advance(
                to=clock_module.parse_date(raw_to, "to"))
    except clock_module.Invalid as error:
        return h.json(400, {"error": str(error)})
    h.json(200, outcome)


@route("GET", "/_mock/holidays")
def holidays(h) -> None:
    h.json(200, h.state.clock.snapshot()["holidays"])


@route("PUT", "/_mock/holidays",
       note="a JSON list of YYYY-MM-DD dates the bank does not settle on")
def set_holidays(h) -> None:
    """The days the bank does not settle on, as a JSON list of dates.

    A whole list, replaced whole: a holiday calendar is one thing a tester
    sets, not a collection they add to one date at a time, and PUT is the
    method that says so.
    """
    try:
        given = json.loads(h.body.decode("utf-8")) if h.body.strip() else []
    except (ValueError, UnicodeDecodeError):
        given = None
    if not isinstance(given, list):
        return h.json(400, {
            "error": "the body has to be a JSON list of dates, as in "
                     '["2026-12-25", "2026-12-26"]'})
    try:
        days = sorted({clock_module.parse_date(value, "holiday").isoformat()
                       for value in given})
    except clock_module.Invalid as error:
        return h.json(400, {"error": str(error)})
    conn = h.state.conn
    conn.execute("DELETE FROM holiday")
    conn.executemany("INSERT INTO holiday (day) VALUES (?)",
                     [(day,) for day in days])
    conn.commit()
    h.json(200, days)
