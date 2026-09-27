"""The server: its configuration, and how it is put together.

`Config` is everything the server can be told. `make_server` opens the
database into a `State` (`mockbank/state.py`), binds a `Handler`
(`mockbank/handler.py`) to it, and returns the server bound and ready. The
endpoints are in `mockbank/routes/`, one module per surface, and a request
finds its function through the route table there.
"""
from __future__ import annotations

import socketserver
from http.server import ThreadingHTTPServer
from typing import Optional

from . import clock as clock_module
from .handler import Handler
from .routes.control import PLANNED, SUPPORTED  # noqa: F401 - read by the tests
from .state import State


class Config:
    """Everything the server can be told, with the defaults it runs with."""

    def __init__(self, host="127.0.0.1", port=8080, db_path=":memory:", quiet=False,
                 timezone="UTC", cutoff=clock_module.DEFAULT_CUTOFF, clock="",
                 allow_duplicates=False, status_delay_ms=0, auth="",
                 keep_requests=5000, retention_days=0.0,
                 drop_dir="", pickup_dir="", drop_settle_ms=250,
                 drop_interval_ms=1000):
        self.host = host
        # A MsgId seen before is DUPL unless this is set, as at a real bank.
        self.allow_duplicates = allow_duplicates
        # How long after receipt the pain.002 is due; 0 so a test sees it at once.
        self.status_delay_ms = status_delay_ms
        # "user:password", or empty for a mock anyone who can reach the port
        # may reset. Checked by Handler._authorised.
        self.auth = auth
        # Retention, for a mock left running on a file database. Pruned at
        # startup, after every advance, and every PRUNE_EVERY requests.
        self.keep_requests = keep_requests      # newest rows kept; 0 keeps all
        self.retention_days = retention_days    # older records go; 0 keeps all
        # The second door: a directory the bank reads files from and one it
        # writes released messages into. Empty means no file work at all.
        self.drop_dir = drop_dir
        self.pickup_dir = pickup_dir
        self.drop_settle_ms = drop_settle_ms
        self.drop_interval_ms = drop_interval_ms
        self.port = port
        self.db_path = db_path
        self.quiet = quiet
        self.timezone = timezone
        self.cutoff = cutoff
        self.clock = clock


class _Server(ThreadingHTTPServer):
    """A server that closes its database when it closes its socket.

    `make_server` binds only after the database has opened, so a `--db` file
    this version cannot read is refused before the port is taken - a mock that
    is listening but cannot answer is worse than one that did not start.
    """

    daemon_threads = True

    # None until `make_server` sets it, and that is not only tidiness: when the
    # bind fails - a port already in use - `socketserver.TCPServer.__init__`
    # calls `server_close()` on the way out, before `make_server` gets to
    # assign. Without this the real error ("Address already in use") was
    # replaced by `AttributeError: '_Server' object has no attribute 'state'`,
    # which says nothing about the port.
    state: Optional[State] = None

    def server_bind(self):
        """Bind without asking DNS what this machine is called.

        ``HTTPServer.server_bind`` sets ``server_name`` from
        ``socket.getfqdn(host)``, a reverse lookup on the bind address. On a
        host with no reverse record for what it is binding - a CI runner, a
        container on a network with no resolver for ``0.0.0.0`` - that lookup
        waits for DNS to time out, and the mock does not finish starting until
        it does. It cost a minute on the macOS runner, which is how it was
        found: the exposure warning appeared and the "listening on" banner did
        not, with only the bind between them.

        Nothing here needs the FQDN. ``server_name`` and ``server_port`` are
        read by the CGI handlers in ``http.server``, which this does not use, so
        the address as given is both cheaper and more honest: it is what the
        mock was told to bind.
        """
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def server_close(self):
        super().server_close()
        if self.state is not None:
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
    if state.dropbox is not None:
        # Started after the socket is bound, so a port already in use fails
        # before a poller exists to have to stop again.
        state.dropbox.start()
    return httpd
