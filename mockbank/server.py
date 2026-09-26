"""The HTTP surface.

This is the skeleton the 0.1 issues build on: the control plane a tester
reaches first (``/_mock/health``, ``/_mock/state``, ``POST /_mock/reset``)
and an index page.  Everything the plan promises and this release has not
built yet answers 404 with a body naming what *is* supported, which is the
rule the sibling mocks follow: refuse by name rather than half-implement.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .accounts import BEHAVIOURS

# The endpoints the plan commits to, so a 404 can say what is coming.
PLANNED = [
    "POST /payments",
    "GET /_mock/mailbox",
    "GET /_mock/accounts", "POST /_mock/accounts", "PATCH /_mock/accounts/<id>",
    "POST /_mock/advance",
    "POST /_mock/validate",
]


class Config:
    """Everything the server can be told, with the defaults it runs with."""

    def __init__(self, host="127.0.0.1", port=8080, db_path=":memory:", quiet=False):
        self.host = host
        self.port = port
        self.db_path = db_path
        self.quiet = quiet


class State:
    """What survives between requests. SQLite arrives with the accounts issue."""

    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.requests = 0
            self.resets = getattr(self, "resets", -1) + 1

    def snapshot(self):
        with self.lock:
            return {
                "version": __version__,
                "db": self.config.db_path,
                "requests": self.requests,
                "resets": self.resets,
                "behaviours": sorted(BEHAVIOURS),
                "planned": PLANNED,
            }


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-bank/" + __version__
    state: State  # set on the class by make_server

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: D401 - BaseHTTPRequestHandler API
        if not self.state.config.quiet:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, status, body):
        raw = json.dumps(body, indent=2).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _text(self, status, body, content_type="text/plain; charset=utf-8"):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _not_found(self):
        self._json(404, {
            "error": "not found",
            "path": self.path,
            "supported": ["GET /", "GET /_mock/health", "GET /_mock/state",
                          "POST /_mock/reset"],
            "planned": PLANNED,
        })

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        with self.state.lock:
            self.state.requests += 1
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._text(200, INDEX, "text/html; charset=utf-8")
        if path == "/_mock/health":
            return self._json(200, {"status": "ok", "version": __version__})
        if path == "/_mock/state":
            return self._json(200, self.state.snapshot())
        return self._not_found()

    do_HEAD = do_GET

    def do_POST(self):
        with self.state.lock:
            self.state.requests += 1
        path = self.path.split("?", 1)[0]
        if path == "/_mock/reset":
            self.state.reset()
            return self._json(200, {"reset": True})
        return self._not_found()

    def do_PATCH(self):
        with self.state.lock:
            self.state.requests += 1
        return self._not_found()


INDEX = """<!doctype html>
<meta charset="utf-8">
<title>mock-bank</title>
<h1>mock-bank</h1>
<p>A mock bank. Send it a <code>pain.001</code> and it answers with a
<code>pain.002</code>, <code>camt.054</code>, <code>camt.053</code> and,
when asked, a <code>pacs.004</code>.</p>
<ul>
  <li><code>GET /_mock/health</code></li>
  <li><code>GET /_mock/state</code></li>
  <li><code>POST /_mock/reset</code></li>
</ul>
<p>Version %s. <a href="https://github.com/rseufert/mock-bank">Source and issues</a>.</p>
""" % __version__


def make_server(config: Config) -> ThreadingHTTPServer:
    """A server bound and ready; the caller runs ``serve_forever``."""
    handler = type("BoundHandler", (Handler,), {"state": State(config)})
    httpd = ThreadingHTTPServer((config.host, config.port), handler)
    httpd.daemon_threads = True
    return httpd
