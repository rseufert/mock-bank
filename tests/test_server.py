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

    def test_unbuilt_surfaces_refuse_by_name(self):
        resp = self.get("/_mock/mailbox")
        self.assertEqual(resp.status, 404)
        body = resp.json()
        self.assertIn("GET /_mock/mailbox", body["planned"])
        self.assertIn("GET /_mock/health", body["supported"])

    def test_the_state_it_reports_is_the_state_it_has(self):
        state = self.get("/_mock/state").json()
        listed = self.get("/_mock/accounts").json()
        self.assertEqual(state["accounts"], len(listed))
        self.assertEqual(state["balances"],
                         {"EUR": sum(row["balance"] for row in listed)})
        self.assertEqual(state["schemaVersion"], db.SCHEMA_VERSION)
