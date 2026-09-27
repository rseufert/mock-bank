"""The control plane a tester reaches first, the dictionary, and the index page.

Everything the plan promises and this release has not built yet answers 404
with a body naming what *is* supported, which is the rule the sibling mocks
follow: refuse by name rather than half-implement. `SUPPORTED` and `PLANNED`
are that body, and the index page lists them.
"""
from __future__ import annotations

import html
from typing import List

from .. import __version__, db, schema
from ..accounts import BEHAVIOURS
from . import route

# What this release answers, so a 404 can say so and the index can list it.
SUPPORTED = [
    "GET /",
    "GET /_mock/health",
    "GET /_mock/state",
    "POST /_mock/reset",
    "GET /_mock/dictionary", "GET /_mock/dictionary/<message>",
    "GET /_mock/behaviours",
    "GET /_mock/accounts", "POST /_mock/accounts",
    "GET /_mock/accounts/<id>", "PATCH /_mock/accounts/<id>",
    "GET /_mock/accounts/<id>/statements",
    "POST /_mock/advance",
    "GET /_mock/holidays", "PUT /_mock/holidays",
    "POST /_mock/validate",
    "POST /payments",
    "GET /_mock/payments", "GET /_mock/payments/<EndToEndId>",
    "GET /_mock/mailbox", "GET /_mock/mailbox/<id>",
    "POST /_mock/mailbox/<id>/unread",
    "GET /_mock/requests",
    "GET /_mock/drop", "POST /_mock/drop/scan",
]

# The endpoints the plan commits to, so a 404 can say what is coming.
# Empty, and that is the news: with the mailbox and the request log, every
# endpoint the 0.1 plan committed to now answers. What 0.2 adds - returns, and
# the drop and pickup directories - arrives as messages in the mailbox and as
# command-line flags, not as new endpoints, so there is nothing here to promise.
# Kept rather than deleted because the shape of the 404 body is something a
# client may read, and because 0.3's NACHA and BAI2 will fill it again.
PLANNED: List[str] = []

# A line of explanation for the endpoints that are not self-evident from their
# path; the rest of the index just lists them.
NOTES = {
    "GET /_mock/dictionary": "the ISO 20022 declarations the mock reads and "
                             "writes by",
    "GET /_mock/accounts": "the accounts the bank holds, with their balances "
                           "and their behaviours",
    "PATCH /_mock/accounts/<id>": "change a behaviour, a balance or the "
                                  "closed flag while it runs",
    "POST /_mock/advance": "move bank time: ?days=N (calendar days) or "
                           "?to=YYYY-MM-DD",
    "PUT /_mock/holidays": "a JSON list of YYYY-MM-DD dates the bank does "
                           "not settle on",
    "POST /_mock/drop/scan": "read the drop directory now, instead of waiting "
                             "for the next poll",
    "POST /_mock/reset": "back to the four seeded accounts",
    "POST /_mock/validate": "send a pain.001, get its findings as prose, one "
                            "line each; nothing is stored",
    "POST /payments": "send a pain.001: the bank decides each payment, books "
                      "what is due and answers with a JSON summary",
    "GET /_mock/payments": "every payment the bank decided on, newest first",
    "GET /_mock/accounts/<id>/statements": "the camt.053 statements issued "
                                           "for an account, oldest first",
    "GET /_mock/mailbox": "?leave to peek, ?raw for the XML, ?type=pain.002 to "
                          "filter: the messages the bank has sent and you have not "
                          "collected, oldest first; collecting takes them",
}


@route("GET", "/")
@route("GET", "/index.html")
def index(h) -> None:
    h.text(200, index_page(), "text/html; charset=utf-8")


@route("GET", "/_mock/health")
def health(h) -> None:
    h.json(200, {
        "status": "ok",
        "version": __version__,
        "accounts": db.count(h.state.conn, "account"),
    })


@route("GET", "/_mock/state")
def state(h) -> None:
    h.json(200, h.state.snapshot())


@route("POST", "/_mock/reset")
def reset(h) -> None:
    h.state.reset()
    h.json(200, {"reset": True, "accounts": db.count(h.state.conn, "account")})


@route("GET", "/_mock/behaviours")
def behaviours(h) -> None:
    h.json(200, BEHAVIOURS)


@route("GET", "/_mock/dictionary")
def dictionary_index(h) -> None:
    h.json(200, schema.dictionary_index())


@route("GET", "/_mock/dictionary/<message>")
def dictionary(h, name: str) -> None:
    message = schema.MESSAGES.get(name)
    if message is None:
        return h.json(404, {
            "error": "the mock does not speak %s" % name,
            "messages": sorted(schema.MESSAGES),
        })
    h.json(200, message.to_json())


# ---------------------------------------------------------------------------
# The index page
# ---------------------------------------------------------------------------

def _item(endpoint: str) -> str:
    # `<id>` and `<message>` are placeholders, not markup: escaped, or the
    # browser swallows them and the index lists an endpoint with a hole in it.
    note = NOTES.get(endpoint)
    return ("  <li><code>%s</code>%s</li>"
            % (html.escape(endpoint), ": " + html.escape(note) if note else ""))


def index_page() -> str:
    """The front page: everything this build answers, and everything it will."""
    planned = ""
    if PLANNED:
        planned = ("<h2>Planned, and answering 404 until it lands</h2>\n<ul>\n%s\n</ul>"
                   % "\n".join(_item(line) for line in PLANNED))
    return INDEX_TEMPLATE % {
        "version": __version__,
        "supported": "\n".join(_item(line) for line in SUPPORTED),
        # Not an empty list under a heading, which reads as a page that failed
        # to load rather than as a mock with nothing left to promise.
        "planned": planned,
    }


INDEX_TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<title>mock-bank</title>
<h1>mock-bank</h1>
<p>A mock bank. Send it a <code>pain.001</code> and it answers with a
<code>pain.002</code>, <code>camt.054</code>, <code>camt.053</code> and,
when asked, a <code>pacs.004</code>.</p>
<h2>What this build answers</h2>
<ul>
%(supported)s
</ul>
%(planned)s
<p>Version %(version)s. <a href="https://github.com/rseufert/mock-bank">Source and issues</a>.</p>
"""
