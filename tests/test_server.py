"""The control plane every other test relies on: health, state, reset.

Plus the default port, which is one number repeated in the Dockerfile, the
README and both examples (#203).
"""
import os
import re
import unittest

from support import MockServerCase

from mockbank import __version__, db
from mockbank.accounts import BEHAVIOURS
from mockbank.__main__ import build_parser
from mockbank.server import Config


class ControlPlane(MockServerCase):

    def test_health_names_the_version_and_the_accounts_it_holds(self):
        resp = self.get("/_mock/health")
        self.assertEqual(resp.status, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["version"], __version__)
        # A liveness probe that says "ok" for a bank holding no accounts has
        # answered the wrong question.
        self.assertEqual(body["accounts"], len(db.SEED))

    def test_state_counts_requests_and_lists_behaviours(self):
        before = self.get("/_mock/state").json()["requests"]
        self.get("/_mock/health")
        state = self.get("/_mock/state").json()
        self.assertEqual(state["requests"], before + 2)
        self.assertEqual(state["behaviours"], sorted(BEHAVIOURS))

    def test_reset_starts_the_count_again(self):
        self.get("/_mock/health")
        resp = self.post("/_mock/reset")
        self.assertEqual(resp.status, 200)
        state = self.get("/_mock/state").json()
        self.assertEqual(state["requests"], 1)
        self.assertGreaterEqual(state["resets"], 1)

    def test_index_page_is_html(self):
        resp = self.get("/")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.headers["Content-Type"])
        self.assertIn(b"mock-bank", resp.body)

    def test_a_path_it_does_not_have_refuses_by_naming_what_it_does(self):
        # This used to reach for /_mock/requests as the example of something
        # not built yet. With the mailbox and the request log in, every
        # endpoint the 0.1 plan promised answers, so `planned` is empty and
        # the thing worth testing is the refusal naming what is there.
        resp = self.get("/_mock/nonesuch")
        self.assertEqual(resp.status, 404)
        body = resp.json()
        self.assertEqual(body["planned"], [])
        self.assertIn("GET /_mock/health", body["supported"])
        self.assertIn("GET /_mock/mailbox", body["supported"])
        self.assertIn("/_mock/nonesuch", body["path"])

    # Three answers the route table (#44) changed, each a quirk of the old
    # chain of ifs. A path the mock does not have is a 404 whatever the method,
    # and only /_mock itself is the control plane.

    def test_a_path_that_only_starts_with_mock_is_not_the_control_plane(self):
        resp = self.get("/_mockxyz/health")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.json()["error"], "not found")

    def test_a_dictionary_path_too_long_to_exist_is_404_for_any_method(self):
        for method in ("GET", "POST"):
            resp = self.request(method, "/_mock/dictionary/pain.001.001.09/extra")
            self.assertEqual(resp.status, 404, method)

    def test_a_payments_path_too_long_to_exist_is_404_for_any_method(self):
        for method in ("GET", "POST"):
            resp = self.request(method, "/_mock/payments/E2E-1/extra")
            self.assertEqual(resp.status, 404, method)

    def test_the_index_does_not_show_an_empty_list_of_promises(self):
        page = self.get("/").body.decode("utf-8")
        self.assertIn("What this build answers", page)
        self.assertNotIn("answering 404 until it lands", page)

    def test_the_state_it_reports_is_the_state_it_has(self):
        state = self.get("/_mock/state").json()
        listed = self.get("/_mock/accounts").json()
        self.assertEqual(state["accounts"], len(listed))
        self.assertEqual(state["balances"],
                         {"EUR": sum(row["balance"] for row in listed)})
        self.assertEqual(state["schemaVersion"], db.SCHEMA_VERSION)


class TheDefaultPort(unittest.TestCase):
    """The number a reader gets with no flag, held to everywhere it is repeated.

    It moved from 8080 to 8090 (#203) because mock-edi defaults to 8080 as
    well, so the two could not both be started without a flag and the second
    one died on a bind error. Moving it meant editing the Dockerfile, the
    README, the tour and the example client as well as the code - six copies of
    one number, which is the shape that drifts back one place at a time. The
    `Config` default is the one that decides; the rest have to agree with it.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def read(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_the_flag_and_its_help_say_what_the_config_default_is(self):
        port = Config().port
        # Not only "they agree": 8080 is mock-edi's, and taking it back is the
        # bug this number moved to fix.
        self.assertEqual(port, 8090)
        action, = [action for action in build_parser()._actions
                   if "--port" in action.option_strings]
        self.assertEqual(action.default, port)
        self.assertIn(str(port), action.help)

    def test_the_image_publishes_the_port_its_entrypoint_binds(self):
        dockerfile = self.read("Dockerfile")
        port = str(Config().port)
        self.assertIn("EXPOSE %s" % port, dockerfile)
        # The README documents `docker run -p 8090:8090`, which works only if
        # the entrypoint binds what EXPOSE publishes.
        self.assertIn('"--port", "%s"' % port, dockerfile)

    def test_the_readme_flag_table_shows_the_default(self):
        row = re.search(r"^\| `--port` \| `(\d+)` \|", self.read("README.md"), re.M)
        self.assertIsNotNone(row, "the README's flag table has no --port row")
        self.assertEqual(int(row.group(1)), Config().port)

    def test_the_tour_and_the_example_client_look_where_it_listens(self):
        # Both default their base URL instead of being told the mock's, so a
        # reader who starts the mock with no flag and runs either one gets a
        # connection refused as soon as these drift.
        port = str(Config().port)
        self.assertIn('BASE="${BASE:-http://127.0.0.1:%s}"' % port,
                      self.read("examples", "demo.sh"))
        self.assertIn('"BASE", "http://127.0.0.1:%s"' % port,
                      self.read("examples", "client.py"))
