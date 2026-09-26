"""The HTTP surface.

The control plane a tester reaches first (``/_mock/health``, ``/_mock/state``,
``POST /_mock/reset``), the ISO 20022 dictionary (``/_mock/dictionary``), the
accounts the bank holds, and an index page.
Everything the plan promises and this release has not built yet answers 404
with a body naming what *is* supported, which is the rule the sibling mocks
follow: refuse by name rather than half-implement.

Two things about the shape:

**One lock over everything that touches the database.** SQLite copes with
several threads on one connection; the mock's read-modify-write sequences do
not - two payments arriving at once must not both read the same balance and
both find it sufficient. Every route runs under it.

**The request log is written before the answer goes out.** A client that asks
``/_mock/requests`` the moment its answer arrives is what a test does, and a row
written after the response is a row that test can lose on a slow runner.
"""
from __future__ import annotations

import html
import json
import sqlite3
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple

from . import __version__, accounts, db, schema, validate
from .accounts import BEHAVIOURS

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
    "POST /_mock/validate",
]

# The endpoints the plan commits to, so a 404 can say what is coming.
PLANNED = [
    "POST /payments",
    "GET /_mock/mailbox",
    "POST /_mock/advance",
    "GET /_mock/holidays", "PUT /_mock/holidays",
    "GET /_mock/requests",
]

# A line of explanation for the endpoints that are not self-evident from their
# path; the rest of the index just lists them.
NOTES = {
    "GET /_mock/dictionary": "the ISO 20022 declarations the mock reads and "
                             "writes by",
    "GET /_mock/accounts": "the accounts the bank holds, with their balances "
                           "and their behaviours",
    "PATCH /_mock/accounts/<id>": "change a behaviour, a balance or the "
                                  "closed flag while it runs",
    "POST /_mock/reset": "back to the four seeded accounts",
    "POST /_mock/validate": "send a pain.001, get its findings as prose, one "
                            "line each; nothing is stored",
}

# A request body larger than this is refused rather than read into memory. A
# pain.001 with a thousand payments is a few megabytes; this is generous.
MAX_BODY = 32 * 1024 * 1024


class Config:
    """Everything the server can be told, with the defaults it runs with."""

    def __init__(self, host="127.0.0.1", port=8080, db_path=":memory:", quiet=False):
        self.host = host
        self.port = port
        self.db_path = db_path
        self.quiet = quiet


class State:
    """What survives between requests: the database, and the lock over it."""

    def __init__(self, config: Config):
        self.config = config
        # Reentrant, because a route that takes the lock may call something
        # that takes it again.
        self.lock = threading.RLock()
        self.conn = db.connect(config.db_path)
        db.seed(self.conn)
        self.started = db.utcnow()
        self.resets = 0

    def reset(self) -> None:
        """Back to the seeded bank, without restarting the process.

        The tables are emptied and reseeded rather than the file replaced, so a
        mock on ``--db`` keeps being the same mock at the same path.
        """
        with self.lock:
            for table in ("request_log", "holiday", "account"):
                self.conn.execute("DELETE FROM %s" % table)
            self.conn.commit()
            db.seed(self.conn)
            self.resets += 1

    def close(self) -> None:
        """Close the database, under the lock every request takes.

        Closing SQLite while a statement is running is a use-after-free in C:
        it takes the interpreter with it instead of raising.
        """
        with self.lock:
            self.conn.close()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "version": __version__,
                "db": self.config.db_path,
                "schemaVersion": db.SCHEMA_VERSION,
                "started": db.stamp(self.started),
                "requests": db.count(self.conn, "request_log"),
                "resets": self.resets,
                "accounts": db.count(self.conn, "account"),
                # Per currency, in minor units. See accounts.totals.
                "balances": accounts.totals(self.conn),
                "holidays": db.count(self.conn, "holiday"),
                "behaviours": sorted(BEHAVIOURS),
                "supported": SUPPORTED,
                "planned": PLANNED,
            }


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-bank/" + __version__
    state: State  # set on the class by make_server

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: D401 - BaseHTTPRequestHandler API
        if not self.state.config.quiet:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    _head = False

    def _handle(self, method: str) -> None:
        path, query = _split(self.path)
        self._head = method == "HEAD"
        self._begin_log(method, path)
        try:
            body = self._read_body()
        except _BodyError as error:
            self.close_connection = True
            self._text(error.status, str(error))
            return
        try:
            with self.state.lock:
                self._route("GET" if self._head else method, path, query, body)
        except BrokenPipeError:            # pragma: no cover - client hung up
            return
        except Exception as error:         # pragma: no cover - last resort
            # A bug, then, and the one place it can be seen: the access log has
            # only the status line, so the traceback goes to stderr. Anything
            # the route wrote before it failed is undone, so that the request
            # log's own commit cannot commit half of it.
            traceback.print_exc()
            try:
                with self.state.lock:
                    self.state.conn.rollback()
            except sqlite3.Error:          # the database is already closed
                pass
            self._json(500, {"error": str(error), "type": type(error).__name__})

    # -- routes -----------------------------------------------------------

    def _route(self, method: str, path: str, query: Dict[str, List[str]],
               body: bytes) -> None:
        if path in ("/", "/index.html"):
            if method != "GET":
                return self._method_not_allowed(method, ["GET"])
            return self._text(200, index_page(), "text/html; charset=utf-8")
        if path.startswith("/_mock"):
            return self._control(method, path, query, body)
        return self._not_found()

    def _control(self, method: str, path: str, query: Dict[str, List[str]],
                 body: bytes) -> None:
        parts = _segments(path)[1:]                     # drop "_mock"
        head = parts[0] if parts else ""
        rest = parts[1:]
        conn = self.state.conn

        if head == "health" and not rest:
            if method != "GET":
                return self._method_not_allowed(method, ["GET"])
            return self._json(200, {
                "status": "ok",
                "version": __version__,
                "accounts": db.count(conn, "account"),
            })

        if head == "state" and not rest:
            if method != "GET":
                return self._method_not_allowed(method, ["GET"])
            return self._json(200, self.state.snapshot())


        if head == "reset" and not rest:
            if method != "POST":
                return self._method_not_allowed(method, ["POST"])
            self.state.reset()
            return self._json(200, {"reset": True,
                                    "accounts": db.count(conn, "account")})

        if head == "behaviours" and not rest:
            if method != "GET":
                return self._method_not_allowed(method, ["GET"])
            return self._json(200, BEHAVIOURS)

        if head == "dictionary":
            if method != "GET":
                return self._method_not_allowed(method, ["GET"])
            if not rest:
                return self._json(200, schema.dictionary_index())
            if len(rest) > 1:
                return self._not_found()
            return self._dictionary(rest[0])

        if head == "accounts":
            return self._accounts(method, rest, body)

        if head == "validate" and not rest:
            if method != "POST":
                return self._method_not_allowed(method, ["POST"])
            return self._validate(body)

        return self._not_found()

    def _accounts(self, method: str, rest: List[str], body: bytes) -> None:
        conn = self.state.conn
        if not rest:
            if method == "GET":
                return self._json(200, accounts.listing(conn))
            if method == "POST":
                payload = _json_body(body)
                if payload is None:
                    return self._json(400, {
                        "error": "the body has to be a JSON object describing "
                                 "the account, as in {\"id\": \"OTHER\", "
                                 "\"iban\": \"...\"}"})
                identifier = payload.pop("id", "")
                try:
                    row = accounts.create(conn, identifier, **payload)
                except accounts.Invalid as error:
                    return self._json(400, {"error": str(error)})
                return self._json(201, row)
            return self._method_not_allowed(method, ["GET", "POST"])

        identifier = rest[0]
        if len(rest) > 1:
            return self._not_found()
        if method == "GET":
            row = accounts.get(conn, identifier)
            if row is None:
                return self._unknown_account(identifier)
            return self._json(200, row)
        if method == "PATCH":
            payload = _json_body(body)
            if payload is None:
                return self._json(400, {
                    "error": "the body has to be a JSON object of the fields to "
                             "change, as in {\"behaviour\": \"closed-account\"}",
                    "fields": sorted(accounts.FIELDS)})
            try:
                row = accounts.update(conn, identifier, **payload)
            except accounts.UnknownAccount:
                return self._unknown_account(identifier)
            except accounts.Invalid as error:
                return self._json(400, {"error": str(error),
                                        "behaviours": sorted(BEHAVIOURS)})
            return self._json(200, row)
        return self._method_not_allowed(method, ["GET", "PATCH"])

    def _validate(self, body: bytes) -> None:
        """POST /_mock/validate: the findings as prose, nothing stored."""
        payment_file, findings = validate.inspect(body, self.headers.get("Content-Type"))
        status = 422 if validate.errors(findings) else 200
        if "application/json" in (self.headers.get("Accept") or ""):
            return self._json(status, {
                "file": payment_file.to_json() if payment_file else None,
                "findings": [f._asdict() for f in findings],
            })
        lines = [validate.render(f) for f in findings]
        if payment_file is not None:
            batches, payments = len(payment_file.batches), len(payment_file.payments)
            lines.insert(0, "%s %s: %d batch%s, %d payment%s, %d finding%s" % (
                payment_file.message, payment_file.msg_id or "(no MsgId)",
                batches, "" if batches == 1 else "es",
                payments, "" if payments == 1 else "s",
                len(findings), "" if len(findings) == 1 else "s"))
        return self._text(status, "\n".join(lines) + "\n")

    def _dictionary(self, name: str) -> None:
        message = schema.MESSAGES.get(name)
        if message is None:
            return self._json(404, {
                "error": "the mock does not speak %s" % name,
                "messages": sorted(schema.MESSAGES),
            })
        return self._json(200, message.to_json())

    # -- answers ----------------------------------------------------------

    def _not_found(self) -> None:
        self._json(404, {
            "error": "not found",
            "path": self.path,
            "supported": SUPPORTED,
            "planned": PLANNED,
        })

    def _unknown_account(self, identifier: str) -> None:
        self._json(404, {
            "error": "no account %r" % identifier,
            "accounts": [row["id"] for row in accounts.listing(self.state.conn)],
        })

    def _method_not_allowed(self, method: str, allowed: List[str]) -> None:
        self._json(405, {"error": "%s is not allowed here" % method,
                         "allowed": allowed})

    def _json(self, status: int, body: Any) -> None:
        self._raw(status, json.dumps(body, indent=2).encode("utf-8") + b"\n",
                  "application/json; charset=utf-8")

    def _text(self, status: int, body: str,
              content_type: str = "text/plain; charset=utf-8") -> None:
        self._raw(status, body.encode("utf-8"), content_type)

    def _raw(self, status: int, payload: bytes, content_type: str) -> None:
        """Every answer leaves through here, so every answer is logged first."""
        self._log_before_answering(status)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not self._head:
            self.wfile.write(payload)

    # -- the body ---------------------------------------------------------

    def _read_body(self) -> bytes:
        """The request body, or a `_BodyError` saying why not.

        Chunked bodies are refused by name rather than half-read: nothing the
        mock's own clients send is chunked, and a body read wrongly would be
        parsed as XML and rejected for the wrong reason.
        """
        if (self.headers.get("Transfer-Encoding", "").lower().strip()
                == "chunked"):
            raise _BodyError(411, "this mock reads a Content-Length body; send "
                                  "the file without chunked transfer encoding")
        raw = self.headers.get("Content-Length")
        if not raw:
            return b""
        try:
            length = int(raw)
        except ValueError:
            raise _BodyError(400, "Content-Length %r is not a number" % raw) from None
        if length < 0:
            raise _BodyError(400, "Content-Length %d is negative" % length)
        if length > MAX_BODY:
            raise _BodyError(413, "a body of %d bytes is larger than this mock "
                                  "reads (%d)" % (length, MAX_BODY))
        body = self.rfile.read(length)
        if len(body) != length:
            raise _BodyError(400, "the body stopped after %d of %d bytes"
                                  % (len(body), length))
        return body

    # -- the request log --------------------------------------------------

    # One request's row, in the making. A handler object can serve more than
    # one request, so these are reset for each.
    _log_method = ""
    _log_path = ""
    _logged = True

    def _begin_log(self, method: str, path: str) -> None:
        self._log_method, self._log_path, self._logged = method, path, False

    def _log_before_answering(self, status: int) -> None:
        if self._logged:
            return
        self._logged = True
        try:
            with self.state.lock:
                self.state.conn.execute(
                    "INSERT INTO request_log (method, path, status, at)"
                    " VALUES (?,?,?,?)",
                    (self._log_method, self._log_path, status, db.now()))
                self.state.conn.commit()
        except sqlite3.Error:      # pragma: no cover - logging must not fail a request
            pass


class _BodyError(Exception):
    """A request body that cannot be read, and the status that says why."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _split(target: str) -> Tuple[str, Dict[str, List[str]]]:
    parsed = urllib.parse.urlsplit(target)
    # `keep_blank_values` matters: the flags are written `?raw`, `?leave`, with
    # no value at all, and the default parse drops them - so every flag would
    # silently read as false.
    return (parsed.path,
            urllib.parse.parse_qs(parsed.query, keep_blank_values=True))


def _segments(path: str) -> List[str]:
    """The segments of a still-encoded path, each percent-decoded once."""
    return [urllib.parse.unquote(part) for part in path.split("/") if part]


def _json_body(body: bytes):
    """The body as a JSON object, `{}` when there is none, `None` when it is not one."""
    if not body.strip():
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _item(endpoint: str) -> str:
    # `<id>` and `<message>` are placeholders, not markup: escaped, or the
    # browser swallows them and the index lists an endpoint with a hole in it.
    note = NOTES.get(endpoint)
    return ("  <li><code>%s</code>%s</li>"
            % (html.escape(endpoint), ": " + html.escape(note) if note else ""))


def index_page() -> str:
    """The front page: everything this build answers, and everything it will."""
    return INDEX_TEMPLATE % {
        "version": __version__,
        "supported": "\n".join(_item(line) for line in SUPPORTED),
        "planned": "\n".join(_item(line) for line in PLANNED),
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
<h2>Planned, and answering 404 until it lands</h2>
<ul>
%(planned)s
</ul>
<p>Version %(version)s. <a href="https://github.com/rseufert/mock-bank">Source and issues</a>.</p>
"""


class _Server(ThreadingHTTPServer):
    """A server that closes its database when it closes its socket.

    `make_server` binds only after the database has opened, so a `--db` file
    this version cannot read is refused before the port is taken - a mock that
    is listening but cannot answer is worse than one that did not start.
    """

    daemon_threads = True
    state: State

    def server_close(self):
        super().server_close()
        self.state.close()


def make_server(config: Config) -> _Server:
    """A server bound and ready; the caller runs ``serve_forever``."""
    state = State(config)
    handler = type("BoundHandler", (Handler,), {"state": state})
    try:
        httpd = _Server((config.host, config.port), handler)
    except BaseException:
        state.close()
        raise
    httpd.state = state
    return httpd
