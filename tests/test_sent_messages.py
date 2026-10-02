"""`GET /_mock/messages`: what the bank has sent, collected or not (#186).

The mailbox forgets a message once a client collects it. Somebody watching that
client - a capture of a run whose own code collects the `pain.002` - could then
not find it: the listing no longer showed it, and `GET /_mock/mailbox/<id>`
serves a body to a caller who already knows the id. This is the mailbox's
listing with the collected ones still in it, and it can take nothing.
"""
from support import MockServerCase
from test_mailbox import CAMT054, PAIN002, MailboxCase
from test_payments import sample


class WhatWasSent(MailboxCase):

    def sent(self, query=""):
        resp = self.get("/_mock/messages" + query)
        self.assertEqual(resp.status, 200, resp.body)
        return resp.json()

    def test_a_collected_message_is_still_listed_and_says_when_it_was_taken(self):
        before = self.sent()
        self.assertEqual([(m["id"], m["takenAt"]) for m in before],
                         [(self.given[0], None), (self.given[1], None)])
        self.get("/_mock/mailbox?type=pain.002")            # the client collects one
        self.assertEqual(self.ids(self.get("/_mock/mailbox?leave").json()), self.given[1:])
        taken, waiting = self.sent()
        self.assertEqual((taken["id"], waiting["id"], waiting["takenAt"]),
                         (self.given[0], self.given[1], None))
        # The bank clock's moment, as releasedAt is: the mock started at 09:00.
        self.assertEqual(taken["takenAt"][:13], "2026-10-01T09")

    def test_it_has_the_mailboxs_fields_and_one_more(self):
        peeked = self.get("/_mock/mailbox?leave").json()
        listed = self.sent()
        self.assertEqual([dict(m, takenAt=None) for m in peeked], listed)
        self.assertEqual(set(listed[0]) - set(peeked[0]), {"takenAt"})

    def test_reading_it_takes_nothing_however_it_is_asked(self):
        # The flags that mean something on the mailbox mean nothing here, and
        # none of them can take a message.
        for query in ("", "?leave=0", "?raw", "?type=pain.002", "?all", "?take"):
            self.sent(query)
        self.assertEqual(self.get("/_mock/state").json()["messages"]["taken"], 0)
        self.assertEqual(self.ids(self.get("/_mock/mailbox").json()), self.given)

    def test_the_type_filter_is_the_mailboxs_prefix(self):
        self.get("/_mock/mailbox")
        self.assertEqual(self.ids(self.sent("?type=pain.002")), self.given[:1])
        self.assertEqual(self.ids(self.sent("?type=camt")), self.given[1:])
        self.assertEqual(self.sent("?type=PAIN.002"), [], "a prefix, and it is literal")
        self.assertEqual(self.sent("?type=p_in.002"), [], "not a LIKE pattern")

    def test_a_message_not_yet_released_is_the_queues_to_list(self):
        held = self.put_message(PAIN002, "ACME", "<Document>held</Document>", released=False)
        self.assertNotIn(held, self.ids(self.sent()))
        self.assertIn(held, [e["id"] for e in self.get("/_mock/queue").json()])

    def test_one_put_back_is_untaken_again(self):
        self.get("/_mock/mailbox")
        self.post("/_mock/mailbox/%d/unread" % self.given[0])
        self.assertEqual([m["takenAt"] is None for m in self.sent()], [True, False])

    def test_oldest_first(self):
        third = self.put_message(CAMT054, "ACME", "<Document>three</Document>")
        self.get("/_mock/mailbox?type=camt")
        self.assertEqual(self.ids(self.sent()), self.given + [third])


class ItDoesNotReleaseEither(MockServerCase):
    """An observer reading between a client's calls changes nothing the client
    will see: a status report whose time has come stays unreleased until the
    client asks the mailbox, as it would have with nobody watching."""

    config_kwargs = {"clock": "2026-10-01T09:00", "status_delay_ms": 60000}

    def test_a_report_whose_time_has_come_is_not_released_by_reading(self):
        self.post("/_mock/reset")
        self.post("/payments", body=sample("pain001_four_payments.xml"))
        with self.httpd.state.lock:                         # the minute passes
            self.httpd.state.conn.execute(
                "UPDATE message SET due_at = '2026-10-01T08:59:00Z'")
            self.httpd.state.conn.commit()
        self.assertEqual(self.get("/_mock/messages?type=pain.002").json(), [])
        self.assertEqual(self.get("/_mock/state").json()["messages"]["queued"], 1)
        [report] = self.get("/_mock/mailbox?type=pain.002").json()
        [listed] = self.get("/_mock/messages?type=pain.002").json()
        self.assertEqual(listed["id"], report["id"])
        self.assertIsNotNone(listed["takenAt"])


class WhatRetentionHasRemovedIsNotListed(MockServerCase):
    """It lists what the bank still holds. Retention removes collected messages
    as they age, and the listing does not pretend otherwise."""

    config_kwargs = {"clock": "2025-01-01T09:00", "retention_days": 30}

    def test_a_collected_message_goes_when_retention_takes_it(self):
        self.post("/_mock/reset")
        self.post("/payments", body=sample("pain001_four_payments.xml"))
        collected = [m["id"] for m in self.get("/_mock/mailbox").json()]
        self.assertTrue(collected)
        listed = [m["id"] for m in self.get("/_mock/messages").json()]
        self.assertEqual(listed, collected)
        self.post("/_mock/advance?days=90")
        left = [m["id"] for m in self.get("/_mock/messages").json()]
        self.assertEqual(set(left) & set(collected), set())
        self.assertGreater(
            self.get("/_mock/state").json()["retention"]["pruned"].get("message", 0), 0)
