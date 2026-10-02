"""`queuedAt`: when the bank came to owe a message (#185).

An entry in `GET /_mock/queue` says when it is due. This is when it entered the
queue, on the bank clock, whatever caused it: a file arriving, money arriving,
a payment booking with its return scheduled, the debtor's bank refusing a
collection. The message carries the same value into the mailbox, so one read at
the end can say when each message was promised.

The rule is held directly in `TheRule`: an entry's `queuedAt` is never later
than the bank clock at the first read that shows it, it does not change while
the entry waits, and the message arrives with it.
"""
import datetime
import os

from test_collections_read import pain008
from test_nacha_collection_returns import ReturningCase
from test_nacha_collections import FRIDAY, MONDAY
from test_queue import QueueCase

HERE = os.path.dirname(os.path.abspath(__file__))
GLOBEX = "NL14MOCK0000000002"
RETURN_LATER = {"behaviour": "return-later", "parameters": {"days": 2}}


def moment(stamp):
    """A stamp or the state's clock reading, as a moment to compare."""
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class QueuedAtCase(QueueCase):

    def clock(self):
        return moment(self.get("/_mock/state").json()["clock"]["now"])

    def advance(self, to):
        self.assertEqual(self.post("/_mock/advance?to=" + to).status, 200)

    def collect(self, collections, when, msg_id="DD-1"):
        resp = self.post("/payments", body=pain008(collections, msg_id=msg_id, when=when))
        self.assertEqual(resp.status, 202, resp.body)

    def received(self, msg_id):
        """When the bank received a payment file, by its payment."""
        return self.get("/_mock/payments/P-" + msg_id).json()["received_at"]

    def by_key(self, kind=""):
        return {m["key"]: m for m in self.mailbox(kind)}

    def arrives_as_promised(self, entries):
        """Each of these queue entries is now a message with its `queuedAt`."""
        arrived = self.by_key()
        for entry in entries:
            self.assertIn(entry["key"], arrived)
            self.assertEqual(arrived[entry["key"]]["queuedAt"], entry["queuedAt"], entry["key"])


class APaymentNotYetSettled(QueuedAtCase):

    def test_it_was_queued_when_its_file_arrived(self):
        self.pay("M1", datetime.date(2026, 10, 6))
        [entry] = self.queue()
        self.assertEqual(entry["queuedAt"], "2026-10-01T09:00:00Z")
        self.assertEqual(entry["queuedAt"], self.received("M1"))
        self.advance("2026-10-06")
        self.arrives_as_promised([entry])

    def test_a_second_file_for_the_same_day_does_not_move_it(self):
        self.pay("M1", datetime.date(2026, 10, 6))
        self.advance("2026-10-02")
        self.pay("M2", datetime.date(2026, 10, 6))
        [entry] = self.queue()
        self.assertEqual(entry["queuedAt"], self.received("M1"))
        self.assertLess(self.received("M1"), self.received("M2"))
        self.advance("2026-10-06")
        self.arrives_as_promised([entry])


class AReturn(QueuedAtCase):

    def test_it_was_queued_when_the_payment_booked_not_when_the_file_arrived(self):
        self.request("PATCH", "/_mock/accounts/ACME", body=RETURN_LATER)
        self.pay("M1", datetime.date(2026, 10, 6))
        self.assertEqual([e["reports"] for e in self.queue()], ["payments settling"],
                         "no return is owed until the payment books")
        self.advance("2026-10-06")                      # books; back on Thursday
        booked = self.get("/_mock/payments/P-M1").json()["booked_at"]
        entries = self.queue()
        self.assertEqual([(e["type"][:8], e["queuedAt"]) for e in entries],
                         [("camt.054", booked), ("pacs.004", booked)])
        self.assertEqual(booked[:10], "2026-10-06")
        self.assertLess(self.received("M1"), booked)
        self.advance("2026-10-08")
        self.arrives_as_promised(entries)

    def test_one_scheduled_and_due_in_the_same_advance_was_queued_then(self):
        # One advance books the payment, schedules its return and sends it back,
        # so the return is never seen waiting. It was queued in that advance.
        self.request("PATCH", "/_mock/accounts/ACME", body=RETURN_LATER)
        self.pay("M1", datetime.date(2026, 10, 6))
        self.advance("2026-10-09")
        booked = self.get("/_mock/payments/P-M1").json()["booked_at"]
        self.assertEqual(booked[:10], "2026-10-09")
        back = [m["queuedAt"] for key, m in self.by_key().items() if "payments-returned" in key]
        self.assertEqual(back, [booked, booked])

    def test_a_nacha_accounts_rejection_was_queued_when_its_file_arrived(self):
        self.request("PATCH", "/_mock/accounts/ACME", body={"format": "nacha", "currency": "USD"})
        with open(os.path.join(HERE, "samples", "nacha_four_payments_to_the_seed.ach"),
                  "rb") as handle:
            self.post("/payments", body=handle.read())
        [entry] = self.queue("?type=nacha.return")
        self.assertEqual(entry["queuedAt"], "2026-10-01T09:00:00Z")
        self.advance("2026-10-02")
        self.arrives_as_promised([entry])


class MoneyArriving(QueuedAtCase):

    def test_it_was_queued_when_the_bank_heard_of_it(self):
        self.advance("2026-10-02")
        made = self.request("POST", "/_mock/credits", body={
            "account": "GLOBEX", "amount": 700, "value_date": "2026-10-06"})
        [entry] = self.queue()
        self.assertEqual(entry["queuedAt"], made.json()["received_at"])
        self.assertEqual(entry["queuedAt"][:10], "2026-10-02")
        self.advance("2026-10-06")
        self.arrives_as_promised([entry])


class ACollection(QueuedAtCase):

    def test_it_was_queued_when_its_file_arrived(self):
        self.collect([("C1", 1000)], "2026-10-06")
        [entry] = self.queue()
        self.assertEqual((entry["reports"], entry["queuedAt"]),
                         ("collections settling", "2026-10-01T09:00:00Z"))
        self.advance("2026-10-06")
        self.arrives_as_promised([entry])

    def test_its_return_was_queued_when_the_debtors_bank_refused_it(self):
        # Settled on Friday, refused on Saturday: the money goes back on Monday.
        # The return was owed from Saturday, which neither the file's arrival
        # nor the booking says.
        self.collect([("C1", 1000)], "2026-10-02")
        self.advance("2026-10-02")
        self.advance("2026-10-03")
        refused = self.post("/_mock/collections/C1/refuse", body={"reason": "MD01"}).json()
        self.assertEqual((refused["refused_at"][:10], refused["return_due"]),
                         ("2026-10-03", "2026-10-05"))
        self.assertLess(refused["booked_at"], refused["refused_at"])
        entries = self.queue()
        self.assertEqual([(e["type"][:8], e["reports"], e["queuedAt"]) for e in entries],
                         [("camt.054", "collections returned", refused["refused_at"]),
                          ("pacs.004", "collections returned", refused["refused_at"])])
        self.advance("2026-10-05")
        self.arrives_as_promised(entries)

    def test_a_held_debtors_return_was_queued_when_the_collection_booked(self):
        # GLOBEX sends back what is taken from it, by itself, as it books.
        self.request("PATCH", "/_mock/accounts/GLOBEX", body=RETURN_LATER)
        resp = self.post("/payments", body=pain008([("C1", 1000)], when="2026-10-06",
                                                   debtors={"C1": GLOBEX}))
        self.assertEqual(resp.status, 202, resp.body)
        self.advance("2026-10-06")
        collection = self.get("/_mock/collections/C1").json()
        entries = [e for e in self.queue() if e["reports"] == "collections returned"]
        self.assertEqual([(e["type"][:8], e["queuedAt"]) for e in entries],
                         [("camt.054", collection["booked_at"]),
                          ("pacs.004", collection["booked_at"])])
        self.assertEqual((collection["booked_at"][:10], collection["refused_at"]),
                         ("2026-10-06", None))
        self.advance("2026-10-08")
        self.arrives_as_promised(entries)

    def test_one_refused_before_it_settles_says_when_and_so_does_its_pain002(self):
        self.collect([("C1", 1000)], "2026-10-06")
        self.advance("2026-10-02")
        self.get("/_mock/mailbox")
        refused = self.post("/_mock/collections/C1/refuse", body={"reason": "MD01"}).json()
        self.assertEqual((refused["status"], refused["refused_at"][:10]),
                         ("rejected", "2026-10-02"))
        [report] = self.mailbox("pain.002")
        self.assertEqual(report["queuedAt"], refused["refused_at"])

    def test_a_return_due_at_once_was_queued_as_it_was_refused(self):
        # Refused on a business day before the cutoff, so it goes back now and
        # is never seen waiting. It was queued at the refusal all the same.
        self.collect([("C1", 1000)], "2026-10-02")
        self.advance("2026-10-05")
        self.get("/_mock/mailbox")
        refused = self.post("/_mock/collections/C1/refuse", body={"reason": "MD01"}).json()
        self.assertEqual(self.queue(), [])
        arrived = self.by_key()
        self.assertEqual(sorted(k.split("/")[0] for k in arrived),
                         ["camt.054.001.08", "pacs.004.001.09"])
        for message in arrived.values():
            self.assertEqual(message["queuedAt"], refused["refused_at"])

    def test_one_never_refused_says_so(self):
        self.collect([("C1", 1000)], "2026-10-02")
        self.assertIsNone(self.get("/_mock/collections/C1").json()["refused_at"])


class ANachaCollectionRefusedBeforeItSettles(ReturningCase):

    def test_its_return_entry_was_queued_when_it_was_refused(self):
        # NACHA answers a refusal with a return entry on the day the collection
        # would have settled, so it waits - from the refusal, not from the file.
        self.collect([{"id": "INV-1", "cents": 1250}], effective=MONDAY)
        self.advance(FRIDAY)
        refused = self.refuse("INV-1", "R10")
        self.assertEqual((refused["refused_at"][:10], refused["return_due"]),
                         (FRIDAY.isoformat(), MONDAY.isoformat()))
        [entry] = self.get("/_mock/queue?type=nacha.return").json()
        self.assertEqual(entry["queuedAt"], refused["refused_at"])
        self.advance(MONDAY)
        [arrived] = self.get("/_mock/mailbox?leave&type=nacha.return").json()
        self.assertEqual((arrived["key"], arrived["queuedAt"]),
                         (entry["key"], refused["refused_at"]))


class AStatusReportHeldBack(QueuedAtCase):
    config_kwargs = dict(QueueCase.config_kwargs, status_delay_ms=60000)

    def test_it_was_queued_when_it_was_written(self):
        self.pay("M1", datetime.date(2026, 10, 6))
        [status] = self.queue("?type=pain.002")
        self.assertEqual((status["written"], status["queuedAt"], status["dueAt"]),
                         (True, "2026-10-01T09:00:00Z", "2026-10-01T09:01:00Z"))
        self.advance("2026-10-02")
        self.arrives_as_promised([status])


class WhatWasNeverInTheQueue(QueuedAtCase):

    def test_a_statement_has_none(self):
        self.advance("2026-10-02")
        statements = self.mailbox("camt.053")
        self.assertTrue(statements)
        self.assertEqual({m["queuedAt"] for m in statements}, {None})

    def test_a_status_report_sent_at_once_was_queued_as_it_was_sent(self):
        self.pay("M1", datetime.date(2026, 10, 6))
        [status] = self.mailbox("pain.002")
        self.assertEqual(status["queuedAt"], status["releasedAt"])


class TheRule(QueuedAtCase):
    """Held over everything the queue can list, one day at a time."""

    def setUp(self):
        super().setUp()
        self.first = {}

    def watch(self):
        """Read the queue as a client would, and hold each entry to the rule."""
        now = self.clock()
        for entry in self.queue():
            self.assertIsNotNone(entry["queuedAt"], entry["key"])
            if entry["key"] not in self.first:
                self.assertLessEqual(moment(entry["queuedAt"]), now, entry["key"])
                self.first[entry["key"]] = entry["queuedAt"]
            self.assertEqual(entry["queuedAt"], self.first[entry["key"]],
                             "%s moved while it waited" % entry["key"])
        for key, message in self.by_key().items():
            if key in self.first:
                self.assertEqual(message["queuedAt"], self.first[key], key)

    def test_no_entry_was_queued_after_it_was_first_seen_and_none_moves(self):
        self.request("PATCH", "/_mock/accounts/ACME", body=RETURN_LATER)
        self.pay("M1", datetime.date(2026, 10, 2))
        self.watch()
        self.request("POST", "/_mock/credits", body={
            "account": "GLOBEX", "amount": 700, "value_date": "2026-10-06"})
        self.collect([("C1", 1000), ("C2", 2500)], "2026-10-02")
        self.watch()
        self.advance("2026-10-02")
        self.pay("M2", datetime.date(2026, 10, 6))
        self.collect([("C3", 400)], "2026-10-07", msg_id="DD-2")
        self.watch()
        self.advance("2026-10-03")
        self.post("/_mock/collections/C1/refuse", body={"reason": "MD01"})
        self.watch()
        for day in range(4, 13):
            self.advance("2026-10-%02d" % day)
            self.watch()
        self.assertEqual(self.queue(), [])
        kinds = {key.split("/")[0][:8] + " " + key.split("/")[3] for key in self.first}
        self.assertEqual(kinds, {
            "camt.054 payments-settling", "camt.054 payments-returned",
            "pacs.004 payments-returned", "camt.054 money-arriving",
            "camt.054 collections-settling", "camt.054 collections-returned",
            "pacs.004 collections-returned"})
        # Every one of them arrived, under the time it was promised at.
        arrived = self.by_key()
        self.assertEqual(sorted(set(self.first) - set(arrived)), [])
