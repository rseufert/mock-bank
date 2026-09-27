"""The control plane every other test relies on: health, state, reset."""
from support import MockServerCase

from mockbank import __version__, db
from mockbank.accounts import BEHAVIOURS


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
