"""The control plane a tester reaches first, the dictionary, and the index page.

Everything the plan promises and this release has not built yet answers 404
with a body naming what *is* supported, which is the rule the sibling mocks
follow: refuse by name rather than half-implement. `routes.SUPPORTED` and
`PLANNED` are that body, and the index page lists them.
"""
from __future__ import annotations

import html
from typing import List

from .. import __version__, db, routes, schema
from ..accounts import BEHAVIOURS
from . import route


# The endpoints the plan commits to, so a 404 can say what is coming.
# Empty, and that is the news: with the mailbox and the request log, every
# endpoint the 0.1 plan committed to now answers. What 0.2 adds - returns, and
# the drop and pickup directories - arrives as messages in the mailbox and as
# command-line flags, not as new endpoints, so there is nothing here to promise.
# Kept rather than deleted because the shape of the 404 body is something a
# client may read, and because 0.3's NACHA and BAI2 will fill it again.
PLANNED: List[str] = []


@route("GET", "/")
@route("GET", "/index.html", listed=False)
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


@route("POST", "/_mock/reset",
       note="back to the four seeded accounts")
def reset(h) -> None:
    h.state.reset()
    h.json(200, {"reset": True, "accounts": db.count(h.state.conn, "account")})


@route("GET", "/_mock/behaviours")
def behaviours(h) -> None:
    h.json(200, BEHAVIOURS)


@route("GET", "/_mock/dictionary",
       note="the ISO 20022 declarations the mock reads and writes by")
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
    note = routes.NOTES.get(endpoint)
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
        "supported": "\n".join(_item(line) for line in routes.SUPPORTED),
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
when asked, a <code>pacs.004</code> or a <code>camt.052</code>.</p>
<h2>What this build answers</h2>
<ul>
%(supported)s
</ul>
%(planned)s
<p>Version %(version)s. <a href="https://github.com/rseufert/mock-bank">Source and issues</a>.</p>
"""
