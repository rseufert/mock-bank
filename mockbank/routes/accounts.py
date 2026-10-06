"""The accounts the bank holds, the statements issued for them, and the
intraday report a test can ask for."""
from __future__ import annotations

from .. import accounts, outbox
from ..accounts import BEHAVIOURS
from . import json_body, route


@route("GET", "/_mock/accounts",
       note=("the accounts the bank holds, with their balances and their "
            "behaviours"))
def listing(h) -> None:
    h.json(200, accounts.listing(h.state.conn))


@route("POST", "/_mock/accounts")
def create(h) -> None:
    payload = json_body(h.body)
    if payload is None:
        return h.json(400, {
            "error": "the body has to be a JSON object describing "
                     "the account, as in {\"id\": \"OTHER\", "
                     "\"iban\": \"...\"}"})
    identifier = payload.pop("id", "")
    try:
        row = accounts.create(h.state.conn, identifier, payload)
    except accounts.Invalid as error:
        return h.json(400, {"error": str(error)})
    h.json(201, row)


@route("GET", "/_mock/accounts/<id>")
def account(h, identifier: str) -> None:
    row = accounts.get(h.state.conn, identifier)
    if row is None:
        return unknown_account(h, identifier)
    h.json(200, row)


@route("PATCH", "/_mock/accounts/<id>",
       note=("change a behaviour, a balance or the closed flag while it "
            "runs"))
def update(h, identifier: str) -> None:
    payload = json_body(h.body)
    if payload is None:
        return h.json(400, {
            "error": "the body has to be a JSON object of the fields to "
                     "change, as in {\"behaviour\": \"closed-account\"}",
            "fields": sorted(accounts.FIELDS)})
    try:
        row = accounts.update(h.state.conn, identifier, payload)
    except accounts.UnknownAccount:
        return unknown_account(h, identifier)
    except accounts.Invalid as error:
        return h.json(400, {"error": str(error),
                            "behaviours": sorted(BEHAVIOURS)})
    h.json(200, row)


@route("GET", "/_mock/accounts/<id>/statements",
       note="the camt.053 statements issued for an account, oldest first")
def statements(h, identifier: str) -> None:
    if accounts.get(h.state.conn, identifier) is None:
        return unknown_account(h, identifier)
    h.json(200, outbox.statements(h.state.conn, identifier))


@route("POST", "/_mock/accounts/<id>/report",
       note=("a camt.052 intraday report for an account now: the day so far, "
             "released to the mailbox at once"))
def report(h, identifier: str) -> None:
    row = accounts.get(h.state.conn, identifier)
    if row is None:
        return unknown_account(h, identifier)
    # Book whatever has come due first, so the report is the day as it stands.
    h.state.release()
    try:
        issued = outbox.issue_report(h.state.conn, h.state.clock,
                                     accounts.get(h.state.conn, identifier),
                                     h.state.now())
    except outbox.Unreportable as refusal:
        return h.json(409, {"error": str(refusal)})
    h.state.deliver()
    h.json(201, issued)


def unknown_account(h, identifier: str) -> None:
    h.json(404, {
        "error": "no account %r" % identifier,
        "accounts": [row["id"] for row in accounts.listing(h.state.conn)],
    })
