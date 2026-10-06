"""A business day that ends on the real clock gets its statement too (#157).

Bank time is real time plus an offset, so it passes by itself between advances.
Statements used to be issued for the days an advance crossed and no others: a
mock left running over midnight never issued that day's, and the day's debits
were then on no statement at all.

None of these sleep. The clock is handed its real time, and the night passes
when a test says so.
"""
import datetime

from support import MockServerCase
from test_payments import sample

from mockbank.clock import Clock

CAMT053 = "camt.053"


class NightCase(MockServerCase):
    """A mock a minute and ten seconds before midnight, on a Thursday."""

    config_kwargs = {"clock": "2026-10-01T23:58:50", "cutoff": "23:59"}

    def setUp(self):
        clock = self.httpd.state.clock
        started = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
        self.passed = datetime.timedelta(0)
        with self.httpd.state.lock:
            clock.real = lambda: started + self.passed
        self.post("/_mock/reset")

    def wait(self, **how_long):
        """Real time passes, and nobody tells the bank."""
        self.passed += datetime.timedelta(**how_long)

    def statements(self, account="ACME"):
        resp = self.get("/_mock/accounts/%s/statements" % account)
        self.assertEqual(resp.status, 200, resp.body)
        return resp.json()

    def days(self, account="ACME"):
        return [s["day"] for s in self.statements(account)]

    def pay(self):
        resp = self.post("/payments", body=sample("pain001_four_payments.xml"))
        self.assertEqual(resp.json()["accepted"], 2, resp.body)


class TheNightPasses(NightCase):

    def test_the_day_that_ended_is_on_a_statement_and_so_are_its_debits(self):
        # The issue's own case: two payments book on Thursday, the night
        # passes, and the first thing anyone does is advance.
        self.pay()
        self.wait(seconds=78)
        self.assertEqual(self.get("/_mock/state").json()["clock"]["date"], "2026-10-02")
        self.post("/_mock/advance?days=4")
        thursday, friday, monday = self.statements()
        self.assertEqual((thursday["day"], friday["day"], monday["day"]),
                         ("2026-10-01", "2026-10-02", "2026-10-05"))
        self.assertEqual((thursday["opening"], thursday["closing"], thursday["entries"]),
                         (12500000, 10875000, 2))
        self.assertEqual((friday["opening"], friday["entries"]), (10875000, 0))

    def test_it_is_issued_at_the_next_release_with_no_advance_at_all(self):
        self.pay()
        self.get("/_mock/mailbox")
        self.assertEqual(self.days(), [], "Thursday has not ended")
        self.wait(seconds=78)
        sent = self.get("/_mock/mailbox?type=" + CAMT053).json()
        self.assertEqual(sorted(m["account"] for m in sent),
                         ["ACME", "EURODIS", "GLOBEX"], "every open account")
        self.assertEqual(self.days(), ["2026-10-01"])
        # It is released as the bank noticed, which is after midnight.
        self.assertEqual({m["releasedAt"][:16] for m in sent}, {"2026-10-02T00:00"})

    def test_it_is_issued_once(self):
        self.wait(seconds=78)
        self.get("/_mock/mailbox")
        self.get("/_mock/mailbox")
        self.post("/_mock/advance?days=1")
        self.assertEqual(self.days(), ["2026-10-01", "2026-10-02"])

    def test_a_payment_sent_after_midnight_is_on_the_new_days_statement(self):
        self.wait(seconds=78)
        self.pay()                       # the release that issues Thursday's
        self.post("/_mock/advance?days=1")
        thursday, friday = self.statements()
        self.assertEqual((thursday["day"], thursday["entries"]), ("2026-10-01", 0))
        self.assertEqual((friday["day"], friday["entries"]), ("2026-10-02", 2))
        self.assertEqual(thursday["closing"], friday["opening"])

    def test_several_nights(self):
        self.wait(days=5)                # to Tuesday, over a weekend
        self.get("/_mock/mailbox")
        self.assertEqual(self.days(), ["2026-10-01", "2026-10-02", "2026-10-05"])

    def test_a_day_that_has_not_ended_gets_none(self):
        self.wait(seconds=60)            # 23:59:50
        self.get("/_mock/mailbox")
        self.assertEqual(self.days(), [])

    def test_a_day_is_looked_at_once_as_an_advance_looks_at_it_once(self):
        # A holiday when it ended, so it got no statement; taking the holiday
        # away afterwards does not send the bank back for it.
        self.put("/_mock/holidays", ["2026-10-01"])
        self.wait(seconds=78)
        self.get("/_mock/mailbox")
        self.put("/_mock/holidays", [])
        self.get("/_mock/mailbox")
        self.assertEqual(self.days(), [])

    def test_a_reset_is_a_new_bank_and_owes_nothing_for_the_old_ones_night(self):
        self.wait(seconds=78)
        self.post("/_mock/reset")        # back to 23:58:50 on Thursday
        self.get("/_mock/mailbox")
        self.assertEqual(self.days(), [])
        self.wait(seconds=78)
        self.get("/_mock/mailbox")
        self.assertEqual(self.days(), ["2026-10-01"])


class TheClockIsHandedItsRealTime(MockServerCase):

    def test_bank_time_follows_the_source_it_was_given(self):
        real = [datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)]
        bank = Clock(start="2026-10-01T23:58:50", real=lambda: real[0])
        self.assertEqual(bank.now().isoformat(), "2026-10-01T23:58:50+00:00")
        real[0] += datetime.timedelta(seconds=78)
        self.assertEqual(bank.now().isoformat(), "2026-10-02T00:00:08+00:00")
        bank.advance(days=1)
        self.assertEqual(bank.today(), datetime.date(2026, 10, 3))

    def test_a_source_in_another_zone_is_the_same_moment(self):
        east = datetime.timezone(datetime.timedelta(hours=5))
        real = datetime.datetime(2026, 1, 1, 17, 0, tzinfo=east)
        bank = Clock(zone="UTC", real=lambda: real)
        self.assertEqual(bank.now().isoformat(), "2026-01-01T12:00:00+00:00")

    def test_without_one_it_is_the_hosts_clock(self):
        before = datetime.datetime.now(datetime.timezone.utc)
        self.assertLess(abs(Clock().now() - before), datetime.timedelta(seconds=5))
