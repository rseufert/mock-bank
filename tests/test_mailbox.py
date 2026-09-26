"""The mailbox, and the request log: how a client gets what the bank sent.

`GET /_mock/mailbox` is the surface a tester spends the most time on, so the
things asserted here are the ones they will rely on without thinking: a message
is handed over once, peeking does not consume it, the raw form carries the XML
and nothing else, and filtering by type does not require knowing the version.

Messages are put in the table directly where the point is the mailbox's own
behaviour, and sent through the real pipeline where the point is that the two
agree. A test that only ever inserted rows could not tell that the mailbox and
the writers disagree about a column.
"""
import urllib.parse
from xml.etree import ElementTree as ET

# test_payments brings in support, which puts the checkout on sys.path
from test_payments import PipelineCase, sample

from mockbank import db, schema

PAIN002 = "pain.002.001.10"
CAMT054 = "camt.054.001.08"


class MailboxCase(PipelineCase):
    """A mock with two messages already released, put there directly."""

    def setUp(self):
        super().setUp()                     # PipelineCase resets the mock
        self.given = [self.put_message(PAIN002, "ACME", "<Document>one</Document>"),
                      self.put_message(CAMT054, "GLOBEX", "<Document>two</Document>")]

    def put_message(self, kind, account, body, released=True, taken=False):
        """A released, untaken message in the table, as the writers would leave it."""
        now = db.now()
        with self.httpd.state.lock:
            cursor = self.httpd.state.conn.execute(
                "INSERT INTO message (type, account, file_id, due_at, released_at,"
                " taken_at, body) VALUES (?,?,?,?,?,?,?)",
                (kind, account, None, now, now if released else None,
                 now if taken else None, body))
            self.httpd.state.conn.commit()
            return cursor.lastrowid

    def ids(self, collected):
        return [item["id"] for item in collected]


class CollectingOnce(MailboxCase):

    def test_a_message_is_handed_over_once(self):
        first = self.get("/_mock/mailbox").json()
        self.assertEqual(self.ids(first), self.given)
        # Gone on the second call: that is what makes a second collect mean
        # "what has arrived since" rather than "everything, again".
        self.assertEqual(self.get("/_mock/mailbox").json(), [])

    def test_leave_looks_without_taking(self):
        peeked = self.get("/_mock/mailbox?leave").json()
        self.assertEqual(self.ids(peeked), self.given)
        # Still there, twice over.
        self.assertEqual(self.ids(self.get("/_mock/mailbox?leave").json()), self.given)
        self.assertEqual(self.ids(self.get("/_mock/mailbox").json()), self.given)

    def test_leave_equals_zero_means_take_it(self):
        # `?leave=0` says what it says, rather than being true because the
        # parameter is present.
        self.assertEqual(self.ids(self.get("/_mock/mailbox?leave=0").json()),
                         self.given)
        self.assertEqual(self.get("/_mock/mailbox").json(), [])

    def test_oldest_first(self):
        third = self.put_message(PAIN002, "ACME", "<Document>three</Document>")
        self.assertEqual(self.ids(self.get("/_mock/mailbox").json()),
                         sorted(self.given + [third]))

    def test_the_listing_says_what_each_message_is(self):
        item = self.get("/_mock/mailbox").json()[0]
        self.assertEqual(item["type"], PAIN002)
        self.assertEqual(item["account"], "ACME")
        self.assertIn(PAIN002, item["summary"])
        self.assertIn("ACME", item["summary"])
        self.assertEqual(item["bytes"], len(item["body"].encode("utf-8")))
        self.assertIsNotNone(item["releasedAt"])


class Raw(MailboxCase):

    def test_two_messages_come_back_as_two_bodies_and_nothing_else(self):
        resp = self.get("/_mock/mailbox?raw")
        self.assertEqual(resp.status, 200)
        self.assertIn("application/xml", resp.headers["Content-Type"])
        body = resp.body.decode("utf-8")
        self.assertIn("<Document>one</Document>", body)
        self.assertIn("<Document>two</Document>", body)
        # Nothing else: no JSON wrapper, no invented root element holding the
        # two. See outbox.RAW_SEPARATOR on why it is a sequence.
        self.assertEqual(body.strip().splitlines(),
                         ["<Document>one</Document>", "<Document>two</Document>"])

    def test_raw_takes_them_like_the_json_form_does(self):
        self.get("/_mock/mailbox?raw")
        self.assertEqual(self.get("/_mock/mailbox").json(), [])

    def test_raw_with_leave_does_not_take_them(self):
        self.get("/_mock/mailbox?raw&leave")
        self.assertEqual(self.ids(self.get("/_mock/mailbox?leave").json()), self.given)

    def test_an_empty_mailbox_is_empty_rather_than_malformed(self):
        self.get("/_mock/mailbox")
        resp = self.get("/_mock/mailbox?raw")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body.decode("utf-8").strip(), "")


class FilteringByType(MailboxCase):

    def test_a_prefix_matches_without_naming_the_version(self):
        # `pain.002`, not `pain.002.001.10`: a caller who had to write the
        # version would have to change when the mock writes a newer one, and
        # the version is not what they meant.
        collected = self.get("/_mock/mailbox?type=pain.002").json()
        self.assertEqual([item["type"] for item in collected], [PAIN002])

    def test_the_full_name_matches_too(self):
        collected = self.get("/_mock/mailbox?type=%s" % CAMT054).json()
        self.assertEqual([item["type"] for item in collected], [CAMT054])

    def test_what_did_not_match_is_still_waiting(self):
        self.get("/_mock/mailbox?type=pain.002")
        left = self.get("/_mock/mailbox").json()
        self.assertEqual([item["type"] for item in left], [CAMT054])

    def test_the_type_filter_is_a_literal_prefix_too(self):
        for pattern in ("pain%", "pain_002", "PAIN.002", "%"):
            with self.subTest(type=pattern):
                self.assertEqual(
                    self.get("/_mock/mailbox?leave&type=%s"
                             % urllib.parse.quote(pattern)).json(), [],
                    "%r matched something" % pattern)
        # Nothing was taken while all that was being refused.
        self.assertEqual(len(self.get("/_mock/mailbox?leave").json()), 2)

    def test_a_type_nothing_matches_is_an_empty_list_not_an_error(self):
        resp = self.get("/_mock/mailbox?type=camt.053")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), [])

    def test_it_combines_with_raw_and_leave(self):
        resp = self.get("/_mock/mailbox?type=pain.002&raw&leave")
        self.assertEqual(resp.body.decode("utf-8").strip(),
                         "<Document>one</Document>")
        self.assertEqual(len(self.get("/_mock/mailbox?leave").json()), 2)


class OneMessageById(MailboxCase):

    def test_it_comes_back_raw(self):
        resp = self.get("/_mock/mailbox/%d" % self.given[0])
        self.assertEqual(resp.status, 200)
        self.assertIn("application/xml", resp.headers["Content-Type"])
        self.assertEqual(resp.body.decode("utf-8").strip(),
                         "<Document>one</Document>")

    def test_asking_for_one_does_not_take_the_others(self):
        self.get("/_mock/mailbox/%d" % self.given[0])
        self.assertEqual(self.ids(self.get("/_mock/mailbox").json()), self.given)

    def test_a_message_already_taken_can_still_be_read(self):
        # A tester who has collected and wants to look again is the ordinary
        # case; a 404 for something the bank is still holding would be a lie.
        self.get("/_mock/mailbox")
        resp = self.get("/_mock/mailbox/%d" % self.given[1])
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body.decode("utf-8").strip(),
                         "<Document>two</Document>")

    def test_an_id_that_is_not_there_is_a_404_naming_what_is_waiting(self):
        resp = self.get("/_mock/mailbox/9999")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.json()["waiting"], self.given)

    def test_an_id_that_is_not_a_number_is_a_404_not_a_500(self):
        resp = self.get("/_mock/mailbox/not-a-number")
        self.assertEqual(resp.status, 404)
        self.assertIn("not-a-number", resp.json()["error"])


class PuttingOneBack(MailboxCase):

    def test_an_unread_message_can_be_collected_again(self):
        self.get("/_mock/mailbox")
        resp = self.post("/_mock/mailbox/%d/unread" % self.given[0])
        self.assertEqual(resp.status, 200)
        self.assertIs(resp.json()["wasTaken"], True)
        self.assertEqual(self.ids(self.get("/_mock/mailbox").json()),
                         [self.given[0]])

    def test_a_message_that_was_never_taken_is_a_no_op_that_says_so(self):
        resp = self.post("/_mock/mailbox/%d/unread" % self.given[0])
        self.assertEqual(resp.status, 200)
        self.assertIs(resp.json()["wasTaken"], False)
        # And it is still collectable exactly once, not twice.
        self.assertEqual(self.ids(self.get("/_mock/mailbox").json()), self.given)

    def test_unreading_something_that_is_not_there_is_a_404(self):
        resp = self.post("/_mock/mailbox/9999/unread")
        self.assertEqual(resp.status, 404)

    def test_the_state_counts_move_with_it(self):
        self.get("/_mock/mailbox")
        before = self.get("/_mock/state").json()["messages"]
        self.assertEqual(before["waiting"], 0)
        self.assertEqual(before["taken"], 2)
        self.post("/_mock/mailbox/%d/unread" % self.given[0])
        after = self.get("/_mock/state").json()["messages"]
        self.assertEqual(after["waiting"], 1)
        self.assertEqual(after["taken"], 1)


class Methods(MailboxCase):

    def test_posting_to_the_mailbox_says_what_is_allowed(self):
        resp = self.post("/_mock/mailbox")
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.json()["allowed"], ["GET"])

    def test_getting_unread_says_what_is_allowed(self):
        resp = self.get("/_mock/mailbox/%d/unread" % self.given[0])
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.json()["allowed"], ["POST"])

    def test_a_path_under_a_message_that_means_nothing_is_a_404(self):
        resp = self.get("/_mock/mailbox/%d/nonesuch" % self.given[0])
        self.assertEqual(resp.status, 404)


class TheRealPipelineAgreesWithTheMailbox(PipelineCase):
    """Sent through `POST /payments`, not inserted: the two have to agree.

    A suite that only ever put rows in the table could not tell that the writers
    and the mailbox disagree about a column, which is the one bug this file
    exists to make impossible.
    """

    def test_the_pain002_the_bank_wrote_is_what_the_mailbox_hands_over(self):
        self.send(sample("pain001_four_payments.xml"))
        collected = self.get("/_mock/mailbox?type=pain.002").json()
        self.assertEqual(len(collected), 1)
        item = collected[0]
        # It parses, it is the message it says it is, and it passes the mock's
        # own dictionary - through the mailbox, not through the writer.
        root = ET.fromstring(item["body"].encode("utf-8"))
        message = schema.identify(root)
        self.assertEqual(message.name, item["type"])
        self.assertEqual(schema.check(message, root), [])

    def test_raw_gives_the_same_bytes_as_the_listing(self):
        self.send(sample("pain001_four_payments.xml"))
        peeked = self.get("/_mock/mailbox?leave").json()
        raw = self.get("/_mock/mailbox?raw").body.decode("utf-8")
        for item in peeked:
            self.assertIn(item["body"].strip(), raw)

    def test_a_message_by_id_is_the_same_bytes_again(self):
        self.send(sample("pain001_four_payments.xml"))
        item = self.get("/_mock/mailbox?leave").json()[0]
        alone = self.get("/_mock/mailbox/%d" % item["id"]).body.decode("utf-8")
        self.assertEqual(alone.strip(), item["body"].strip())

    def test_reset_empties_the_mailbox(self):
        self.send(sample("pain001_four_payments.xml"))
        self.assertTrue(self.get("/_mock/mailbox?leave").json())
        self.post("/_mock/reset")
        self.assertEqual(self.get("/_mock/mailbox").json(), [])
        self.assertEqual(self.get("/_mock/state").json()["messages"],
                         {"queued": 0, "waiting": 0, "taken": 0})


class TheRequestLog(PipelineCase):

    def test_it_shows_what_the_client_actually_sent(self):
        self.send(sample("pain001_four_payments.xml"))
        rows = self.get("/_mock/requests").json()
        posts = [row for row in rows if row["path"] == "/payments"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["method"], "POST")
        self.assertEqual(posts[0]["status"], 202)

    def test_newest_first(self):
        self.get("/_mock/health")
        self.get("/_mock/state")
        rows = self.get("/_mock/requests").json()
        self.assertEqual(rows[0]["path"], "/_mock/state")
        self.assertGreater(rows[0]["id"], rows[1]["id"])

    def test_the_path_filter_matches_a_prefix(self):
        self.send(sample("pain001_four_payments.xml"))
        self.get("/_mock/health")
        rows = self.get("/_mock/requests?path=/payments").json()
        self.assertEqual({row["path"] for row in rows}, {"/payments"})

    def test_the_filter_is_a_literal_prefix_not_a_pattern(self):
        # With LIKE, `_` and `%` in the caller's input are wildcards and case is
        # ignored, so `?path=/%mock` matched paths the client never sent - and
        # every /_mock path contains an `_`. The log exists to answer "what did
        # my client actually send"; a filter that answers with more than it was
        # given defeats the point of it.
        self.get("/_mock/health")
        self.send(sample("pain001_four_payments.xml"))
        for pattern in ("/%mock", "/_%", "/%", "/_ock", "/PAYMENTS", "/paymentsX"):
            with self.subTest(path=pattern):
                self.assertEqual(self.get("/_mock/requests?path=%s"
                                          % urllib.parse.quote(pattern)).json(), [],
                                 "%r matched something" % pattern)
        # And the literal one still works, underscore and all.
        rows = self.get("/_mock/requests?path=/_mock/health").json()
        self.assertEqual({row["path"] for row in rows}, {"/_mock/health"})

    def test_a_path_nothing_matches_is_an_empty_list(self):
        self.assertEqual(self.get("/_mock/requests?path=/nope").json(), [])

    def test_it_is_bounded_so_a_long_running_mock_stays_readable(self):
        for _ in range(105):
            self.get("/_mock/health")
        rows = self.get("/_mock/requests").json()
        self.assertEqual(len(rows), 100)

    def test_a_refused_request_is_in_it_too(self):
        # What a tester most wants to see is the request that did not work.
        self.get("/_mock/nonesuch")
        rows = self.get("/_mock/requests?path=/_mock/nonesuch").json()
        self.assertEqual(rows[0]["status"], 404)

    def test_reset_empties_it(self):
        self.get("/_mock/health")
        self.post("/_mock/reset")
        rows = self.get("/_mock/requests").json()
        # Only the reset and this request's predecessors survive it.
        self.assertTrue(all(row["path"] != "/_mock/health" for row in rows))

    def test_posting_to_it_says_what_is_allowed(self):
        resp = self.post("/_mock/requests")
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.json()["allowed"], ["GET"])
