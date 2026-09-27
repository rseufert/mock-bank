"""The request handler: plumbing, and nothing about any endpoint.

A request is split into its path and query, authenticated, has its body read,
and is looked up in the route table (`mockbank.routes`). The function found
there answers it through `json`, `text` or `raw`. A path the table does not
have is a 404 naming what it does have. A path it has only with other methods
is a 405 naming those.

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

import base64
import binascii
import hmac
import json
import sqlite3
import sys
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

from . import __version__, db, routes
from .routes.control import PLANNED, SUPPORTED

if TYPE_CHECKING:                          # pragma: no cover
    from .state import State

# How many requests between prunes. Often enough that a long-running mock stays
# bounded, rarely enough that the cost is invisible: a prune is two DELETEs.
PRUNE_EVERY = 500

# A request body larger than this is refused rather than read into memory. A
# pain.001 with a thousand payments is a few megabytes; this is generous.
MAX_BODY = 32 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-bank/" + __version__
    state: "State"  # set on the class by make_server

    # What the route reads: the query and the body of the request being answered.
    query: Dict[str, List[str]] = {}
    body = b""

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
        path, self.query = _split(self.path)
        self._head = method == "HEAD"
        self._begin_log(method, path)
        if not self._authorised():
            # Before the body is read, so an unauthenticated POST /payments
            # does not get its file parsed, and before routing, so there is no
            # endpoint whose existence an unauthenticated caller can confirm.
            return self._challenge()
        try:
            self.body = self._read_body()
        except _BodyError as error:
            self.close_connection = True
            self.text(error.status, str(error))
            return
        try:
            with self.state.lock:
                self._route("GET" if self._head else method, path)
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
            self.json(500, {"error": str(error), "type": type(error).__name__})

    def _route(self, method: str, path: str) -> None:
        function, arguments, allowed = routes.find(method, path)
        if function is not None:
            return function(self, *arguments)
        if allowed:
            return self.method_not_allowed(method, allowed)
        return self.not_found()

    # -- answers ----------------------------------------------------------

    def not_found(self) -> None:
        self.json(404, {
            "error": "not found",
            "path": self.path,
            "supported": SUPPORTED,
            "planned": PLANNED,
        })

    def method_not_allowed(self, method: str, allowed: List[str]) -> None:
        self.json(405, {"error": "%s is not allowed here" % method,
                        "allowed": allowed})

    def json(self, status: int, body: Any) -> None:
        self.raw(status, json.dumps(body, indent=2).encode("utf-8") + b"\n",
                 "application/json; charset=utf-8")

    def text(self, status: int, body: str,
             content_type: str = "text/plain; charset=utf-8") -> None:
        self.raw(status, body.encode("utf-8"), content_type)

    def raw(self, status: int, payload: bytes, content_type: str) -> None:
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

    # -- who is asking ----------------------------------------------------

    def _authorised(self) -> bool:
        """Whether the request carries the credentials `--auth` asked for.

        Every endpoint, including `/_mock/health` and the index: a mock that
        answers an unauthenticated probe has told whoever is probing that it is
        there and which version it is, and the whole point of the flag is that
        the port is reachable by people who should not be reaching it.
        """
        expected = self.state.config.auth
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        # The scheme name is case-insensitive (RFC 7235), and some clients send
        # it lowercase.
        if header[:6].lower() != "basic ":
            return False
        try:
            given = base64.b64decode(header[6:], validate=True)
        except (binascii.Error, ValueError):
            return False
        # Bytes, not text, and this is not only tidiness: `compare_digest`
        # *raises* TypeError on a str containing non-ASCII rather than
        # returning False, and it is called outside the handler's try. So a
        # curl with an accented username used to drop the connection, and an
        # --auth with one could never be satisfied by anything - the operator
        # believing the port was guarded while nothing could get in, which is
        # the failure check_auth exists to prevent.
        #
        # Constant time over the whole `user:password`, so the comparison does
        # not leak how much of the credential was right. The stakes here are
        # low; the one-line version of the right answer costs nothing.
        return hmac.compare_digest(given, expected.encode("utf-8"))

    def _challenge(self) -> None:
        """A 401 that says how to authenticate, and nothing about the endpoint.

        The request body is deliberately never read - that is the point of
        refusing before reading it - which leaves bytes on the socket that a
        kept-alive connection would take for the next request. So the
        connection is closed rather than reused. Today the server speaks
        HTTP/1.0 and closes anyway; this does not depend on that staying true.
        """
        self.close_connection = True
        self._log_before_answering(401)
        payload = json.dumps({
            "error": "this mock-bank was started with --auth, so every request "
                     "needs HTTP basic credentials",
        }, indent=2).encode("utf-8") + b"\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="mock-bank"')
        self.send_header("Connection", "close")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not self._head:
            self.wfile.write(payload)

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
                self.state.requests_since_prune += 1
                if self.state.requests_since_prune >= PRUNE_EVERY:
                    self.state.prune()
        except sqlite3.Error:      # pragma: no cover - logging must not fail a request
            pass


class _BodyError(Exception):
    """A request body that cannot be read, and the status that says why."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _split(target: str) -> Tuple[str, Dict[str, List[str]]]:
    parsed = urllib.parse.urlsplit(target)
    # `keep_blank_values` matters: the flags are written `?raw`, `?leave`, with
    # no value at all, and the default parse drops them - so every flag would
    # silently read as false.
    return (parsed.path,
            urllib.parse.parse_qs(parsed.query, keep_blank_values=True))
