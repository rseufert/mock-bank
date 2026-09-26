"""Shared harness for the end-to-end tests.

Every test in this suite talks to a real mock over real HTTP: MockServerCase
starts one in a background thread on an ephemeral port, and subclasses point
`config_kwargs` at whatever configuration the surface under test needs.
Nothing is stubbed; if the mock cannot be reached the way a client reaches
it, the test is wrong.
"""
from __future__ import annotations

import faulthandler
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mockbank.server import Config, make_server  # noqa: E402

# Every request a test makes gives up after this long. A mock that stops
# answering is then a failed test naming the request, not a run that blocks
# until CI kills it. Generous, because a slow Windows or macOS runner is not
# a hung one.
REQUEST_TIMEOUT = 30.0

# A watchdog over the whole run, armed only when asked for (CI asks): after
# MOCKBANK_TEST_WATCHDOG seconds, every thread's stack is written to stderr
# and the process exits, so a hang fails with the place it hung in the log.
_WATCHDOG = os.environ.get("MOCKBANK_TEST_WATCHDOG")
if _WATCHDOG:
    faulthandler.dump_traceback_later(float(_WATCHDOG), exit=True)


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class MockServerCase(unittest.TestCase):
    config_kwargs: dict = {}

    @classmethod
    def setUpClass(cls):
        kwargs = dict(host="127.0.0.1", port=0, db_path=":memory:", quiet=True)
        kwargs.update(cls.config_kwargs)
        cls.httpd = make_server(Config(**kwargs))
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(5)

    def request(self, method, path, body=None, headers=None) -> Response:
        data = body
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            headers = dict(headers or {}, **{"Content-Type": "application/json"})
        elif isinstance(body, str):
            data = body.encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return Response(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as err:
            with err:
                return Response(err.code, dict(err.headers), err.read())

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body, **kw)
