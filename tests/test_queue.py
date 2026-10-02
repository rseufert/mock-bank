"""GET /_mock/queue: what the bank is going to send, and when (#155).

Two kinds of entry. One is a message already written and held back, which a
status delay produces. The other is a message the bank will write when something
books: a `camt.054` at settlement, the one for money arriving, and what a return
brings. Each test queues one, reads it with its `dueAt`, moves the clock, and
sees it leave the listing and arrive in the mailbox.
"""
import datetime
import os
import time

from support import MockServerCase
from test_payments import UMBRELLA, pain001

ACME = "NL41MOCK0000000001"
THURSDAY = "2026-10-01T09:00"


class QueueCase(MockServerCase):
    config_kwargs = {"clock": THURSDAY}

    def setUp(self):
        self.post("/_mock/reset")

    def queue(self, query=""):
        resp = self.get("/_mock/queue" + query)
        self.assertEqual(resp.status, 200, resp.body)
        return resp.json()

    def mailbox(self, kind):
        return self.get("/_mock/mailbox?leave&type=" + kind).json()

    def pay(self, msg_id, when):
        resp = self.post("/payments", body=pain001(msg_id, ACME, [("P-" + msg_id, 1000, UMBRELLA)],
                                                   when=when))
        self.assertEqual(resp.json()["accepted"], 1, resp.body)
        return resp.json()

    def file_of(self, msg_id):
        """The bank's own id for a file, which a reset does not start again."""
        return self.get("/_mock/payments/P-" + msg_id).json()["file_id"]


class AMessageWrittenAndHeldBack(QueueCase):
    config_kwargs = {"clock": THURSDAY, "status_delay_ms": 60000}

    def test_it_is_listed_with_its_id_and_due_time_until_it_is_released(self):
        answer = self.pay("M1", datetime.date(2026, 10, 1))
        [status] = self.queue("?type=pain.002")
        self.assertEqual(status, {
            "id": answer["queued"][0]["id"], "key": "m%d" % answer["queued"][0]["id"],
            "type": "pain.002.001.10", "account": "ACME",
            "fileId": self.file_of("M1"), "msgId": "M1",
            "queuedAt": "2026-10-01T09:00:00Z", "dueAt": "2026-10-01T09:01:00Z",
            "written": True,
            "reports": "a file's status"})
        self.assertEqual(self.mailbox("pain.002"), [])
        # Its id is enough to read it before it is sent.
        self.assertIn("<OrgnlMsgId>M1</OrgnlMsgId>",
                      self.get("/_mock/mailbox/%d" % status["id"]).body.decode("utf-8"))
        self.post("/_mock/advance?days=1")
        self.assertEqual(self.queue("?type=pain.002"), [])
        self.assertEqual([(m["id"], m["key"]) for m in self.mailbox("pain.002")],
                         [(status["id"], status["key"])])

    def test_reading_it_twice_changes_nothing(self):
        self.pay("M1", datetime.date(2026, 10, 6))
        before = self.get("/_mock/state").json()["messages"]
        first, second = self.queue(), self.queue()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2, first)
        self.assertEqual(self.get("/_mock/state").json()["messages"], before)
        self.assertEqual(before["queued"], 1, "the listing released nothing")


class AMessageWhoseTimeHasCome(QueueCase):
    config_kwargs = {"clock": THURSDAY, "status_delay_ms": 1000}

    def test_listing_it_does_not_release_it(self):
        # Every mailbox path releases what is due before it answers. This one
        # must not: it is asked what is about to be sent.
        self.pay("M1", datetime.date(2026, 10, 1))
        time.sleep(1.2)
        [late] = self.queue("?type=pain.002")
        self.assertTrue(late["written"])
        self.assertEqual(self.get("/_mock/state").json()["messages"]["queued"], 1)
        self.assertEqual(len(self.mailbox("pain.002")), 1, "the mailbox is what releases it")
        self.assertEqual(self.queue("?type=pain.002"), [])


class AMessageTheBankWillWrite(QueueCase):

    def test_the_camt054_for_a_payment_not_yet_settled(self):
        self.pay("M1", datetime.date(2026, 10, 6))
        self.assertEqual(self.queue(), [
            {"id": None, "key": "camt.054.001.08/ACME/2026-10-06/payments-settling",
             "type": "camt.054.001.08", "account": "ACME", "fileId": None,
             "msgId": None, "queuedAt": "2026-10-01T09:00:00Z",
             "dueAt": "2026-10-06T00:00:00Z", "written": False,
             "reports": "payments settling"}])
        self.assertEqual(self.mailbox("camt.054"), [])
        self.post("/_mock/advance?to=2026-10-06")
        self.assertEqual(self.queue(), [])
        self.assertEqual(len(self.mailbox("camt.054")), 1)

    def test_two_payments_for_one_day_are_one_notification_and_two_days_are_two(self):
        for msg_id, day in (("M1", 6), ("M2", 6), ("M3", 7)):
            self.pay(msg_id, datetime.date(2026, 10, day))
        self.assertEqual([e["dueAt"] for e in self.queue()],
                         ["2026-10-06T00:00:00Z", "2026-10-07T00:00:00Z"])

    def test_money_arriving_is_listed_until_it_books(self):
        made = self.request("POST", "/_mock/credits", body={
            "account": "GLOBEX", "amount": 700, "value_date": "2026-10-05"})
        self.assertEqual(made.status, 201, made.body)
        [entry] = self.queue()
        self.assertEqual((entry["type"], entry["account"], entry["dueAt"], entry["reports"]),
                         ("camt.054.001.08", "GLOBEX", "2026-10-05T00:00:00Z",
                          "money arriving"))
        self.post("/_mock/advance?to=2026-10-05")
        self.assertEqual(self.queue(), [])

    def test_money_waiting_for_a_closed_account_is_not_coming(self):
        # A credit does not book into an account closed while it waited, so
        # no notification is due for it.
        self.request("POST", "/_mock/credits", body={
            "account": "GLOBEX", "amount": 700, "value_date": "2026-10-05"})
        self.request("PATCH", "/_mock/accounts/GLOBEX", body={"closed": True})
        self.assertEqual(self.queue(), [])

    def test_a_return_brings_a_pacs004_for_its_file_and_a_credit(self):
        self.request("PATCH", "/_mock/accounts/ACME",
                     body={"behaviour": "return-later", "parameters": {"days": 2}})
        self.pay("M1", datetime.date(2026, 10, 1))        # settles today, back on Monday
        self.assertEqual([(e["type"], e["fileId"], e["msgId"], e["dueAt"], e["reports"])
                          for e in self.queue()],
                         [("camt.054.001.08", None, None, "2026-10-05T00:00:00Z",
                           "payments returned"),
                          ("pacs.004.001.09", self.file_of("M1"), "M1", "2026-10-05T00:00:00Z",
                           "payments returned")])
        self.post("/_mock/advance?to=2026-10-05")
        self.assertEqual(self.queue(), [])
        self.assertEqual(len(self.mailbox("pacs.004")), 1)

    def test_a_debit_and_a_return_on_one_day_are_two_notifications(self):
        self.request("PATCH", "/_mock/accounts/ACME",
                     body={"behaviour": "return-later", "parameters": {"days": 2}})
        self.pay("M1", datetime.date(2026, 10, 1))
        self.pay("M2", datetime.date(2026, 10, 5))
        self.assertEqual([(e["type"][:8], e["reports"]) for e in self.queue()],
                         [("camt.054", "payments returned"), ("camt.054", "payments settling"),
                          ("pacs.004", "payments returned")])

    def test_a_nacha_accounts_rejection_brings_a_return_file_and_no_credit(self):
        # Nothing was debited, so nothing comes back but the return file (#144).
        self.request("PATCH", "/_mock/accounts/ACME", body={"format": "nacha", "currency": "USD"})
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples",
                               "nacha_four_payments_to_the_seed.ach"), "rb") as handle:
            answer = self.post("/payments", body=handle.read()).json()
        self.assertEqual((answer["accepted"], answer["rejected"]), (2, 2), answer)
        self.assertEqual([(e["type"], e["dueAt"], e["reports"]) for e in self.queue()],
                         [("nacha.return", "2026-10-02T00:00:00Z", "payments returned")])
        self.post("/_mock/advance?to=2026-10-02")
        self.assertEqual(self.queue(), [], "sent, so no longer coming")
        self.assertEqual(len(self.mailbox("nacha.return")), 1)

    def test_the_type_filter_is_a_prefix_as_the_mailboxes_is(self):
        self.pay("M1", datetime.date(2026, 10, 6))
        self.assertEqual(len(self.queue("?type=camt.054")), 1)
        self.assertEqual(len(self.queue("?type=camt")), 1)
        self.assertEqual(self.queue("?type=pain.002"), [])
        self.assertEqual(self.queue("?type=CAMT"), [], "a prefix, and case matters")


class TheKeyThatPairsAnEntryWithItsMessage(QueueCase):
    """mock-films pairs "was waiting" with "arrived" by key, and a message the
    bank has not written has no id to pair on (#155)."""

    def keys(self, kind):
        return [m["key"] for m in self.mailbox(kind)]

    def test_the_message_arrives_under_the_key_its_entry_had(self):
        answer = self.pay("M1", datetime.date(2026, 10, 6))
        [entry] = self.queue()
        self.assertIsNone(entry["id"])
        # The answer to the file names it too, for a caller that kept it.
        self.assertEqual([q["key"] for q in answer["queued"]],
                         ["m%d" % answer["queued"][0]["id"], entry["key"]])
        # Past the day, so the message's own dueAt is not the day in the key.
        self.post("/_mock/advance?to=2026-10-08")
        [message] = self.mailbox("camt.054")
        self.assertEqual(message["key"], entry["key"])
        self.assertEqual(message["dueAt"][:10], "2026-10-08")

    def test_two_messages_of_one_type_for_one_account_on_one_day(self):
        # A debit notification and a return's credit: both camt.054, both for
        # ACME, both on Monday. Each arrives under its own entry's key.
        self.request("PATCH", "/_mock/accounts/ACME",
                     body={"behaviour": "return-later", "parameters": {"days": 2}})
        self.pay("M1", datetime.date(2026, 10, 1))
        self.pay("M2", datetime.date(2026, 10, 5))
        self.get("/_mock/mailbox")                       # today's, collected
        waiting = {e["reports"]: e["key"] for e in self.queue("?type=camt.054")}
        self.assertEqual(waiting, {
            "payments returned": "camt.054.001.08/ACME/2026-10-05/payments-returned",
            "payments settling": "camt.054.001.08/ACME/2026-10-05/payments-settling"})
        [pacs] = self.queue("?type=pacs.004")
        self.assertEqual(pacs["key"], "pacs.004.001.09/ACME/2026-10-05/payments-returned"
                                      "/file-%d" % self.file_of("M1"))
        self.post("/_mock/advance?to=2026-10-05")
        self.assertEqual(sorted(self.keys("camt.054")), sorted(waiting.values()))
        self.assertEqual(self.keys("pacs.004"), [pacs["key"]])
        # Which is which is in the message: the return's credit is the CRDT.
        by_key = {m["key"]: m["body"] for m in self.mailbox("camt.054")}
        self.assertIn("<CdtDbtInd>CRDT</CdtDbtInd>", by_key[waiting["payments returned"]])
        self.assertIn("<CdtDbtInd>DBIT</CdtDbtInd>", by_key[waiting["payments settling"]])

    def test_a_second_notification_for_a_day_has_a_key_of_its_own(self):
        # A file posted on its settlement day books at once, after that day's
        # notification went out: a second camt.054 for the same day, never in
        # the queue. One key, one message.
        self.pay("M1", datetime.date(2026, 10, 6))
        self.post("/_mock/advance?to=2026-10-06")
        self.pay("M2", datetime.date(2026, 10, 6))
        self.assertEqual(self.keys("camt.054"), [
            "camt.054.001.08/ACME/2026-10-06/payments-settling",
            "camt.054.001.08/ACME/2026-10-06/payments-settling#2"])
        self.assertEqual(self.queue(), [])

    def test_money_arriving_and_a_nacha_return_file_keep_their_keys_too(self):
        self.request("POST", "/_mock/credits", body={
            "account": "GLOBEX", "amount": 700, "value_date": "2026-10-05"})
        self.request("PATCH", "/_mock/accounts/ACME", body={"format": "nacha", "currency": "USD"})
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples",
                               "nacha_four_payments_to_the_seed.ach"), "rb") as handle:
            self.post("/payments", body=handle.read())
        self.get("/_mock/mailbox")
        waiting = [e["key"] for e in self.queue()]
        self.assertEqual([k.split("/")[:2] + k.split("/")[3:4] for k in waiting],
                         [["nacha.return", "ACME", "payments-returned"],
                          ["camt.054.001.08", "GLOBEX", "money-arriving"]])
        self.post("/_mock/advance?to=2026-10-05")
        arrived = [m["key"] for m in self.get("/_mock/mailbox?leave").json()]
        for key in waiting:
            self.assertIn(key, arrived)
