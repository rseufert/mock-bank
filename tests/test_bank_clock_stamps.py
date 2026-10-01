"""Which control-plane timestamps are bank time and which are real time (#147).

A payment's ``booked_at`` recorded real time while the bank clock decided the
booking, so after ``POST /_mock/advance?days=3`` a payment booked on the bank's
Thursday carried the real Monday. Rick's option 1: a stamp that records a moment
**the bank clock decided** comes from the bank clock, as a message's
``releasedAt`` always has. A stamp that records when the *process* did something
stays on the real clock.

The test that matters is not "equals the pinned time" - a pinned clock and the
real one can agree by accident, and an equality that holds for the wrong reason
proves nothing. It is that **the stamp moves with the bank clock**: two payments
booked either side of an advance are days apart on the bank clock and
microseconds apart on the real one. That invariant cannot go vacuous as the real
date moves, which is what #148 and #151 were both about.
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from support import MockServerCase                                   # noqa: E402
from test_payments import ACME, GLOBEX, UMBRELLA, pain001            # noqa: E402

# A Thursday, before the 15:00 cutoff, and deliberately not the real date: every
# assertion below is against the bank's own reported clock rather than against
# this literal, so moving it does not break them.
PINNED = "2026-09-24T11:30"
FIRST = datetime.date(2026, 9, 24)
DUE = datetime.date(2026, 9, 25)          # the Friday: one business day on
LATER = datetime.date(2026, 9, 28)        # the Monday: four calendar days on


def real_utc_date():
    return datetime.datetime.now(datetime.timezone.utc).date()


class ClockCase(MockServerCase):
    config_kwargs = {"clock": PINNED}

    def setUp(self):
        self.post("/_mock/reset")

    def bank(self):
        """The bank's clock as it reports itself."""
        return self.get("/_mock/state").json()["clock"]

    def advance_to(self, day):
        resp = self.post("/_mock/advance?to=" + day.isoformat())
        self.assertEqual(resp.status, 200, resp.body)

    def payment(self, end_to_end_id):
        resp = self.get("/_mock/payments/" + end_to_end_id)
        self.assertEqual(resp.status, 200, resp.body)
        return resp.json()

    def assert_on_the_bank_clock(self, stamp, what):
        """``stamp`` is the bank's moment, not the host's.

        Checked against the bank's own ``/_mock/state``, to the hour, and the
        date to the day. The guard afterwards is the point: if the bank clock
        and the real clock happen to agree to the hour, this test cannot tell
        them apart and says so instead of passing.
        """
        clock = self.bank()
        self.assertIsNotNone(stamp, "%s was never stamped" % what)
        self.assertEqual(stamp[:13], clock["now"][:13], what)
        self.assertEqual(stamp[:10], clock["date"], what)
        self.assertNotEqual(
            clock["date"], real_utc_date().isoformat(),
            "the bank clock is on the real date, so this test cannot tell the "
            "two apart - move PINNED")


class WhatTheBankClockDecided(ClockCase):

    def test_a_payments_booked_at_moves_with_the_clock_that_booked_it(self):
        # The invariant that cannot go stale: one payment settles on the bank's
        # today, the other four days later, and they are booked by two
        # different advances. On the bank clock their stamps are four days
        # apart; on the real clock they would be a moment apart.
        self.post("/payments", body=pain001("CL-1", ACME, [("N1", 1000, UMBRELLA)],
                                            when=FIRST))
        self.post("/payments", body=pain001("CL-2", ACME, [("N2", 2000, UMBRELLA)],
                                            when=LATER))
        self.post("/_mock/advance?days=0")
        first = self.payment("N1")["booked_at"]
        self.advance_to(LATER)
        second = self.payment("N2")["booked_at"]
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        apart = (datetime.date.fromisoformat(second[:10])
                 - datetime.date.fromisoformat(first[:10]))
        self.assertEqual(apart, LATER - FIRST,
                         "booked_at did not move with the bank clock: %r then %r"
                         % (first, second))
        # And each one is the day its own settlement fell on, not the day the
        # test ran.
        self.assertEqual(first[:10], FIRST.isoformat())
        self.assertEqual(second[:10], LATER.isoformat())

    def test_a_payments_booked_at_is_the_banks_moment(self):
        self.post("/payments", body=pain001("CL-3", ACME, [("N3", 1500, UMBRELLA)],
                                            when=FIRST))
        self.post("/_mock/advance?days=0")
        self.assert_on_the_bank_clock(self.payment("N3")["booked_at"], "booked_at")

    def test_a_credits_two_stamps_agree_about_which_clock_they_are_on(self):
        # `received_at` was already bank time and `booked_at` was not, so one
        # credit carried both clocks - and `received_at` could read later than
        # the `booked_at` that followed it.
        arrived = self.post("/_mock/credits",
                            body={"account": "GLOBEX", "amount": 5000,
                                  "end_to_end_id": "C-1"})
        self.assertEqual(arrived.status, 201, arrived.body)
        self.post("/_mock/advance?days=0")
        [credit] = [c for c in self.get("/_mock/credits").json()
                    if c["end_to_end_id"] == "C-1"]
        self.assert_on_the_bank_clock(credit["received_at"], "a credit's received_at")
        self.assert_on_the_bank_clock(credit["booked_at"], "a credit's booked_at")
        self.assertLessEqual(credit["received_at"], credit["booked_at"],
                             "a credit booked before it arrived")

    def test_a_returned_payments_returned_at_is_the_day_it_came_back(self):
        # `return-later`: the balance moves because the bank clock reached
        # `return_due`, so the stamp is that day and not the day of the run.
        self.patch("/_mock/accounts/ACME", body={"behaviour": "return-later",
                                                 "parameters": {"days": 2}})
        self.post("/payments", body=pain001("CL-4", ACME, [("N4", 2500, UMBRELLA)],
                                            when=FIRST))
        self.post("/_mock/advance?days=0")
        booked = self.payment("N4")
        self.advance_to(LATER)
        came_back = self.payment("N4")
        self.assertEqual(came_back["status"], "returned")
        self.assert_on_the_bank_clock(came_back["returned_at"], "returned_at")
        self.assertEqual(came_back["returned_at"][:10], came_back["return_due"],
                         "returned_at is not the day the return was due")
        self.assertLess(booked["booked_at"], came_back["returned_at"])

    def test_a_rejected_nacha_payments_returned_at_is_its_return_day(self):
        # The other return path (#54, option (a)): nothing is credited, and the
        # stamp still records the bank's day. #144 made `booked_at IS NOT NULL`
        # load-bearing next to this, so both paths are pinned.
        self.patch("/_mock/accounts/ACME", body={"format": "nacha",
                                                 "currency": "USD"})
        with open(os.path.join(HERE, "samples",
                               "nacha_four_payments_to_the_seed.ach"), "rb") as handle:
            self.post("/payments", body=handle.read())
        # To the due day itself, so the day it went out and the day it was due
        # are the same and the stamp can be held to a known date. They are not
        # the same thing: `return_due <= today`, so a return due on the Friday
        # and processed on the Monday is stamped the Monday, which is when it
        # happened. Asserting equality after advancing past the due day is what
        # my first version of this did, and it failed - correctly.
        self.advance_to(DUE)
        rejected = [p for p in self.get("/_mock/payments").json()
                    if p["status"] == "rejected" and p["returned_at"]]
        self.assertTrue(rejected, "no rejected payment was returned")
        for payment in rejected:
            self.assert_on_the_bank_clock(payment["returned_at"], "a rejection's returned_at")
            self.assertEqual(payment["return_due"], DUE.isoformat())
            self.assertEqual(payment["returned_at"][:10], DUE.isoformat())
            self.assertIsNone(payment["booked_at"], "a rejection was never booked")

    def test_a_return_that_waits_is_stamped_the_day_it_went_out(self):
        # The same two facts pulled apart: due on the Friday, processed on the
        # Monday because nothing advanced the clock in between. `returned_at`
        # records what happened, so it is the Monday - still the bank's day,
        # which is the claim this file makes about it.
        self.patch("/_mock/accounts/ACME", body={"format": "nacha",
                                                 "currency": "USD"})
        with open(os.path.join(HERE, "samples",
                               "nacha_four_payments_to_the_seed.ach"), "rb") as handle:
            self.post("/payments", body=handle.read())
        self.advance_to(LATER)
        rejected = [p for p in self.get("/_mock/payments").json()
                    if p["status"] == "rejected" and p["returned_at"]]
        self.assertTrue(rejected, "no rejected payment was returned")
        for payment in rejected:
            self.assertEqual(payment["return_due"], DUE.isoformat())
            self.assertEqual(payment["returned_at"][:10], LATER.isoformat())
            self.assert_on_the_bank_clock(payment["returned_at"], "a late return's stamp")


class WhatTheProcessDid(ClockCase):
    """The other side of the rule, so it is a rule and not a direction."""

    def test_the_request_log_stays_on_the_real_clock(self):
        self.get("/_mock/health")
        rows = self.get("/_mock/requests?path=/_mock/health").json()
        self.assertTrue(rows, rows)
        self.assertEqual(rows[0]["at"][:10], real_utc_date().isoformat(),
                         "the request log is a log of calls to this process")
        self.assertNotEqual(rows[0]["at"][:10], self.bank()["date"])

    def test_the_mock_started_when_the_process_started(self):
        started = self.get("/_mock/state").json()["started"]
        self.assertEqual(started[:10], real_utc_date().isoformat())
        self.assertNotEqual(started[:10], self.bank()["date"])


class AFilesReceivedAt(ClockCase):
    """``accounts.decide`` judges the cutoff on a bank-clock moment of receipt and
    the row used to keep real time, so the decision was taken on one clock and
    recorded on the other.

    The row had no reader at all until #147: nothing served it, so the stamp
    could not be checked over HTTP and a client had no bank-clock receipt time
    for a file - the gap the report was actually about. It is served beside
    ``msgId`` on a payment now, so these go through the control plane.
    """

    def test_a_file_is_received_on_the_bank_clock(self):
        self.post("/payments", body=pain001("CL-F", ACME, [("F1", 1000, UMBRELLA)],
                                            when=FIRST))
        payment = self.payment("F1")
        self.assertEqual(payment["msg_id"], "CL-F")
        self.assert_on_the_bank_clock(payment["received_at"], "a file's received_at")
        self.assertEqual(payment["received_at"][:10], FIRST.isoformat())
        # The settlement date was decided from that same moment, so the stamp and
        # the decision agree rather than straddling two clocks.
        self.assertEqual(payment["settlement_date"], FIRST.isoformat())

    def test_it_is_the_day_the_file_arrived_and_not_the_day_it_booked(self):
        # A file received before the weekend for a payment that settles after it:
        # one stamp is receipt and the other is booking, and they are days apart
        # on the bank clock. Real time would have made both the day of the run.
        self.post("/payments", body=pain001("CL-G", ACME, [("G1", 1000, UMBRELLA)],
                                            when=LATER))
        self.assertEqual(self.payment("G1")["received_at"][:10], FIRST.isoformat())
        self.advance_to(LATER)
        booked = self.payment("G1")
        self.assertEqual(booked["received_at"][:10], FIRST.isoformat(),
                         "receipt moved when the clock did")
        self.assertEqual(booked["booked_at"][:10], LATER.isoformat())

    def test_every_payment_in_the_listing_carries_it_too(self):
        self.post("/payments", body=pain001("CL-H", ACME, [("H1", 1000, UMBRELLA),
                                                           ("H2", 2000, UMBRELLA)],
                                            when=FIRST))
        listed = self.get("/_mock/payments").json()
        self.assertEqual({p["end_to_end_id"] for p in listed}, {"H1", "H2"})
        for payment in listed:
            self.assertEqual(payment["received_at"], listed[0]["received_at"],
                             "one file, one moment of receipt")
            self.assert_on_the_bank_clock(payment["received_at"], "received_at")


if __name__ == "__main__":
    unittest.main()
