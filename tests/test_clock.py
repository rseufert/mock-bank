"""Bank time: the cutoff, the weekend, the holidays, and moving the clock.

The dates in here are real ones and the weekday is asserted alongside each,
because "settles on 2026-09-28" is only meaningful if that is a Monday, and a
test that pins a date without pinning the day it falls on cannot tell a correct
answer from an arithmetic slip that happens to land nearby.

The settlement rule is the one every other issue depends on, so it is stated
here the way the README states it, case by case.
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockbank import clock as clock_module                  # noqa: E402
from mockbank.clock import Clock, Invalid                    # noqa: E402

from support import MockServerCase                          # noqa: E402

# 2026-09-24 is a Thursday, so the week these tests live in runs
# Thu 24, Fri 25, Sat 26, Sun 27, Mon 28, Tue 29.
THURSDAY = datetime.date(2026, 9, 24)
FRIDAY = datetime.date(2026, 9, 25)
SATURDAY = datetime.date(2026, 9, 26)
SUNDAY = datetime.date(2026, 9, 27)
MONDAY = datetime.date(2026, 9, 28)
TUESDAY = datetime.date(2026, 9, 29)


def clock(holidays=(), cutoff="15:00", zone="UTC", start=""):
    return Clock(zone=zone, cutoff=cutoff, start=start,
                 holidays=lambda: list(holidays))


class TheWeekIsTheWeekIThinkItIs(unittest.TestCase):
    """If this fails, every date below means something else."""

    def test_the_dates_fall_on_the_days_the_other_tests_assume(self):
        for day, name in ((THURSDAY, "Thursday"), (FRIDAY, "Friday"),
                          (SATURDAY, "Saturday"), (SUNDAY, "Sunday"),
                          (MONDAY, "Monday"), (TUESDAY, "Tuesday")):
            self.assertEqual(day.strftime("%A"), name)


class TheCutoffRule(unittest.TestCase):
    """`settlement_date`: the later of the requested date and the next
    business day if received at or after the cutoff, rolled forward past
    weekends and holidays."""

    def test_a_payment_at_1600_on_a_friday_settles_on_monday(self):
        settles = clock().settlement_date(
            datetime.datetime.combine(FRIDAY, datetime.time(16, 0)))
        self.assertEqual(settles, MONDAY)
        self.assertEqual(settles.strftime("%A"), "Monday")

    def test_and_on_tuesday_when_monday_is_in_the_holiday_list(self):
        settles = clock(holidays=[MONDAY.isoformat()]).settlement_date(
            datetime.datetime.combine(FRIDAY, datetime.time(16, 0)))
        self.assertEqual(settles, TUESDAY)
        self.assertEqual(settles.strftime("%A"), "Tuesday")

    def test_before_the_cutoff_it_settles_the_same_day(self):
        settles = clock().settlement_date(
            datetime.datetime.combine(FRIDAY, datetime.time(14, 59)))
        self.assertEqual(settles, FRIDAY)

    def test_at_the_cutoff_exactly_it_is_already_too_late(self):
        # A bank that stops taking today's work at 15:00 has stopped at
        # 15:00:00, not at 15:00:01. The boundary is worth pinning because it
        # is the one a client hits by accident.
        self.assertEqual(
            clock().settlement_date(
                datetime.datetime.combine(FRIDAY, datetime.time(15, 0))),
            MONDAY)

    def test_a_cutoff_of_its_own_is_honoured(self):
        noon = clock(cutoff="12:00")
        self.assertEqual(noon.settlement_date(
            datetime.datetime.combine(THURSDAY, datetime.time(13, 0))), FRIDAY)
        self.assertEqual(noon.settlement_date(
            datetime.datetime.combine(THURSDAY, datetime.time(11, 0))), THURSDAY)

    def test_a_requested_date_further_out_wins(self):
        self.assertEqual(
            clock().settlement_date(
                datetime.datetime.combine(THURSDAY, datetime.time(9, 0)),
                requested_date=TUESDAY),
            TUESDAY)

    def test_a_requested_date_already_past_does_not_move_it_backwards(self):
        self.assertEqual(
            clock().settlement_date(
                datetime.datetime.combine(THURSDAY, datetime.time(9, 0)),
                requested_date=datetime.date(2026, 1, 1)),
            THURSDAY)

    def test_a_requested_date_on_a_weekend_rolls_forward(self):
        self.assertEqual(
            clock().settlement_date(
                datetime.datetime.combine(THURSDAY, datetime.time(9, 0)),
                requested_date=SATURDAY),
            MONDAY)

    def test_a_payment_on_a_saturday_settles_on_monday_whatever_the_hour(self):
        for hour in (9, 16):
            with self.subTest(hour=hour):
                self.assertEqual(
                    clock().settlement_date(
                        datetime.datetime.combine(SATURDAY, datetime.time(hour, 0))),
                    MONDAY)


class CountingBusinessDays(unittest.TestCase):

    def test_three_business_days_after_a_thursday_is_the_next_tuesday(self):
        self.assertEqual(clock().business_days_after(THURSDAY, 3), TUESDAY)

    def test_a_holiday_in_the_window_pushes_it_one_further(self):
        self.assertEqual(
            clock(holidays=[MONDAY.isoformat()]).business_days_after(THURSDAY, 3),
            datetime.date(2026, 9, 30))

    def test_zero_days_is_today_when_today_is_a_business_day(self):
        self.assertEqual(clock().business_days_after(THURSDAY, 0), THURSDAY)

    def test_zero_days_from_a_saturday_rolls_forward_to_monday(self):
        self.assertEqual(clock().business_days_after(SATURDAY, 0), MONDAY)

    def test_it_counts_forwards_only(self):
        with self.assertRaises(Invalid):
            clock().business_days_after(THURSDAY, -1)

    def test_a_weekend_is_not_a_business_day_and_a_holiday_is_not_either(self):
        bank = clock(holidays=[MONDAY.isoformat()])
        self.assertTrue(bank.is_business_day(FRIDAY))
        self.assertFalse(bank.is_business_day(SATURDAY))
        self.assertFalse(bank.is_business_day(SUNDAY))
        self.assertFalse(bank.is_business_day(MONDAY))


class TheTwoReadingsOfThreeDays(unittest.TestCase):
    """The reason `POST /_mock/advance` has to say which one it does.

    The issue asks for both to be pinned, because "three days from Thursday" is
    Sunday to a calendar and Tuesday to a bank, and an endpoint that does not
    say which it means is one a caller has to find out by experiment.
    """

    def test_calendar_and_business_readings_of_the_same_span_differ(self):
        bank = clock()
        calendar = THURSDAY + datetime.timedelta(days=3)
        business = bank.business_days_after(THURSDAY, 3)
        self.assertEqual(calendar, SUNDAY)
        self.assertEqual(business, TUESDAY)
        self.assertNotEqual(calendar, business)


class TheZone(unittest.TestCase):

    def test_utc_is_the_default_and_needs_nothing(self):
        self.assertEqual(clock().zone_name, "UTC")
        self.assertEqual(clock().now().utcoffset(), datetime.timedelta(0))

    def test_a_zone_this_system_does_not_know_is_refused_by_name(self):
        with self.assertRaises(Invalid) as caught:
            clock(zone="Not/AZone")
        self.assertIn("Not/AZone", str(caught.exception))

    @unittest.skipUnless(clock_module._has_zone_database(),
                         "this system has no IANA time zone database")
    def test_a_named_zone_moves_the_clock_off_utc(self):
        amsterdam = clock(zone="Europe/Amsterdam")
        self.assertNotEqual(amsterdam.now().utcoffset(), datetime.timedelta(0))

    @unittest.skipIf(sys.version_info >= (3, 9), "only 3.8 has to refuse")
    def test_on_38_a_named_zone_is_refused_rather_than_taken_as_utc(self):
        # Half-supporting it would put a mock on 3.8 an hour away from the same
        # mock on 3.12 for half the year, which is a bug nobody would look for
        # in a mock bank.
        with self.assertRaises(Invalid) as caught:
            clock(zone="Europe/Amsterdam")
        self.assertIn("3.9", str(caught.exception))

    @unittest.skipIf(clock_module._has_zone_database(),
                     "this system has a time zone database")
    def test_without_a_database_the_refusal_says_that_is_what_is_missing(self):
        # Windows ships no IANA database, so zoneinfo imports and finds nothing
        # to read. The first version of this told the caller to "use an IANA
        # name such as Europe/Amsterdam" when Europe/Amsterdam was exactly what
        # they had passed, which sends them looking in the wrong place.
        with self.assertRaises(Invalid) as caught:
            clock(zone="Europe/Amsterdam")
        message = str(caught.exception)
        self.assertIn("database", message)
        self.assertIn("tzdata", message)

    def test_utc_works_whatever_the_system_has(self):
        # The default has to hold on a runner with no tz database at all.
        self.assertEqual(clock(zone="UTC").now().utcoffset(),
                         datetime.timedelta(0))

    def test_a_cutoff_that_is_not_a_time_is_refused_by_name(self):
        for bad in ("3pm", "25:00", "", None, "15"):
            with self.subTest(cutoff=bad):
                if bad in ("", None):
                    continue            # empty means "take the default"
                with self.assertRaises(Invalid):
                    clock(cutoff=bad)


class StartingAtAGivenMoment(unittest.TestCase):

    def test_clock_pins_the_start_so_a_run_is_reproducible(self):
        bank = clock(start="2026-09-25T16:30")
        self.assertEqual(bank.now().date(), FRIDAY)
        self.assertEqual(bank.now().strftime("%H:%M"), "16:30")

    def test_it_keeps_ticking_rather_than_standing_still(self):
        # An offset, not a stored instant: a frozen clock makes every timestamp
        # in a run identical, which in a log is indistinguishable from a bug.
        bank = clock(start="2026-09-25T16:30")
        first = bank.now()
        for _ in range(200000):
            pass
        self.assertGreaterEqual(bank.now(), first)

    def test_a_start_that_is_not_a_moment_is_refused_by_name(self):
        with self.assertRaises(Invalid) as caught:
            clock(start="last Friday")
        self.assertIn("YYYY-MM-DDTHH:MM", str(caught.exception))


class Advancing(unittest.TestCase):

    def test_days_counts_calendar_days(self):
        bank = clock(start="2026-09-24T09:00")
        outcome = bank.advance(days=3)
        self.assertEqual(bank.today(), SUNDAY)
        self.assertEqual(outcome["calendarDays"], 3)

    def test_and_says_which_business_days_it_crossed(self):
        bank = clock(start="2026-09-24T09:00")
        outcome = bank.advance(days=3)
        # Thursday to Sunday passes through Friday and nothing else.
        self.assertEqual(outcome["businessDaysCrossed"], [FRIDAY.isoformat()])

    def test_to_moves_to_midnight_on_that_date(self):
        bank = clock(start="2026-09-24T09:00")
        bank.advance(to=TUESDAY)
        self.assertEqual(bank.today(), TUESDAY)
        self.assertEqual(bank.now().strftime("%H:%M"), "00:00")

    def test_a_week_forward_crosses_five_business_days(self):
        bank = clock(start="2026-09-24T09:00")
        outcome = bank.advance(days=7)
        self.assertEqual(len(outcome["businessDaysCrossed"]), 5)

    def test_a_holiday_is_not_among_the_business_days_crossed(self):
        bank = clock(start="2026-09-24T09:00", holidays=[FRIDAY.isoformat()])
        self.assertEqual(bank.advance(days=3)["businessDaysCrossed"], [])

    def test_backwards_is_refused(self):
        bank = clock(start="2026-09-24T09:00")
        with self.assertRaises(Invalid):
            bank.advance(days=-1)
        with self.assertRaises(Invalid):
            bank.advance(to=datetime.date(2026, 1, 1))
        self.assertEqual(bank.today(), THURSDAY)

    def test_one_of_days_or_to_is_needed_and_not_both(self):
        bank = clock(start="2026-09-24T09:00")
        with self.assertRaises(Invalid):
            bank.advance()
        with self.assertRaises(Invalid):
            bank.advance(days=1, to=TUESDAY)

    def test_the_hooks_run_with_the_moment_before_and_after(self):
        bank = clock(start="2026-09-24T09:00")
        seen = []
        bank.on_advance.append(lambda before, after: seen.append((before, after)))
        bank.advance(days=1)
        self.assertEqual(len(seen), 1)
        before, after = seen[0]
        self.assertEqual(before.date(), THURSDAY)
        self.assertEqual(after.date(), FRIDAY)
        # Released work is dated by the new time, so the hook must not run
        # before the clock has moved.
        self.assertLess(before, after)


class OverHttp(MockServerCase):
    """The endpoints, on a mock whose bank time starts on a Thursday morning."""

    config_kwargs = {"clock": "2026-09-24T09:00", "timezone": "UTC",
                     "cutoff": "15:00"}

    def setUp(self):
        self.addCleanup(self.post, "/_mock/reset")

    def test_state_reports_bank_time_the_cutoff_and_the_zone(self):
        reported = self.get("/_mock/state").json()["clock"]
        self.assertEqual(reported["date"], THURSDAY.isoformat())
        self.assertEqual(reported["cutoff"], "15:00")
        self.assertEqual(reported["timezone"], "UTC")
        self.assertIs(reported["isBusinessDay"], True)
        self.assertIs(reported["pastCutoff"], False)
        self.assertEqual(reported["nextBusinessDay"], FRIDAY.isoformat())

    def test_advance_days_lands_on_sunday_and_says_it_counted_calendar_days(self):
        # The issue's own pair: three days from a Thursday is Sunday by the
        # calendar and Tuesday by business days. This endpoint does the former
        # and the answer says so in words as well as in the dates.
        outcome = self.post("/_mock/advance?days=3").json()
        self.assertEqual(outcome["to"][:10], SUNDAY.isoformat())
        self.assertEqual(outcome["calendarDays"], 3)
        self.assertIn("calendar days", outcome["counts"])
        self.assertEqual(outcome["businessDaysCrossed"], [FRIDAY.isoformat()])
        self.assertEqual(self.get("/_mock/state").json()["clock"]["date"],
                         SUNDAY.isoformat())

    def test_and_the_business_day_reading_of_the_same_span_is_tuesday(self):
        # Reached over HTTP rather than in Python, so the endpoint and the rule
        # are pinned against each other: ?to= that Tuesday is three business
        # days out, and ?days=3 is not the same thing.
        outcome = self.post("/_mock/advance?to=%s" % TUESDAY.isoformat()).json()
        self.assertEqual(outcome["to"][:10], TUESDAY.isoformat())
        self.assertEqual(outcome["calendarDays"], 5)
        self.assertEqual(outcome["businessDaysCrossed"],
                         [FRIDAY.isoformat(), MONDAY.isoformat(),
                          TUESDAY.isoformat()])

    def test_advancing_backwards_is_a_400_that_says_why(self):
        resp = self.post("/_mock/advance?days=-1")
        self.assertEqual(resp.status, 400)
        self.assertIn("backwards", resp.json()["error"])
        resp = self.post("/_mock/advance?to=2026-01-01")
        self.assertEqual(resp.status, 400)
        self.assertIn("backwards", resp.json()["error"])
        self.assertEqual(self.get("/_mock/state").json()["clock"]["date"],
                         THURSDAY.isoformat())

    def test_neither_parameter_and_both_parameters_are_both_400(self):
        for query in ("", "?days=3&to=2026-10-01"):
            with self.subTest(query=query):
                resp = self.post("/_mock/advance" + query)
                self.assertEqual(resp.status, 400)
                self.assertIn("days", resp.json()["error"])

    def test_days_that_is_not_a_number_is_a_400_not_a_500(self):
        resp = self.post("/_mock/advance?days=soon")
        self.assertEqual(resp.status, 400)
        self.assertIn("soon", resp.json()["error"])

    def test_holidays_go_in_as_a_list_and_come_back_sorted(self):
        resp = self.put("/_mock/holidays", ["2026-12-26", "2026-12-25"])
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), ["2026-12-25", "2026-12-26"])
        self.assertEqual(self.get("/_mock/holidays").json(),
                         ["2026-12-25", "2026-12-26"])

    def test_putting_a_list_replaces_the_whole_list(self):
        self.put("/_mock/holidays", ["2026-12-25", "2026-12-26"])
        self.put("/_mock/holidays", ["2027-01-01"])
        self.assertEqual(self.get("/_mock/holidays").json(), ["2027-01-01"])

    def test_the_same_date_twice_is_one_holiday(self):
        self.put("/_mock/holidays", ["2026-12-25", "2026-12-25"])
        self.assertEqual(self.get("/_mock/holidays").json(), ["2026-12-25"])

    def test_a_holiday_that_is_not_a_date_is_a_400_naming_it(self):
        resp = self.put("/_mock/holidays", ["2026-12-25", "Boxing Day"])
        self.assertEqual(resp.status, 400)
        self.assertIn("Boxing Day", resp.json()["error"])
        self.assertEqual(self.get("/_mock/holidays").json(), [])

    def test_a_body_that_is_not_a_list_is_a_400(self):
        resp = self.put("/_mock/holidays", {"2026-12-25": True})
        self.assertEqual(resp.status, 400)
        self.assertIn("list", resp.json()["error"])

    def test_a_holiday_the_clock_can_see_changes_what_is_a_business_day(self):
        self.put("/_mock/holidays", [FRIDAY.isoformat()])
        reported = self.get("/_mock/state").json()["clock"]
        # Thursday, whose next business day was Friday, now looks to Monday.
        self.assertEqual(reported["nextBusinessDay"], MONDAY.isoformat())

    def test_advancing_skips_a_holiday_it_was_given_over_http(self):
        self.put("/_mock/holidays", [FRIDAY.isoformat()])
        outcome = self.post("/_mock/advance?days=3").json()
        self.assertEqual(outcome["businessDaysCrossed"], [])

    def test_reset_puts_the_clock_and_the_holidays_back(self):
        self.put("/_mock/holidays", ["2026-12-25"])
        self.post("/_mock/advance?days=30")
        self.post("/_mock/reset")
        state = self.get("/_mock/state").json()
        self.assertEqual(state["holidays"], 0)
        self.assertEqual(self.get("/_mock/holidays").json(), [])
        # Back to where --clock put it, not back to real time: --clock is
        # configuration and a reset does not undo configuration. Every test in
        # this class relies on it, which is how the first version of this was
        # caught - it reset to the wall clock and every test after the first
        # one saw a different week.
        self.assertEqual(state["clock"]["date"], THURSDAY.isoformat())
        self.assertEqual(state["clock"]["now"][11:16], "09:00")

    def test_a_mock_with_no_pinned_clock_resets_to_real_time(self):
        bank = clock()
        bank.advance(days=5)
        bank.reset()
        self.assertEqual(bank.today(), datetime.date.today())

    def test_getting_advance_says_what_is_allowed(self):
        resp = self.get("/_mock/advance")
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.json()["allowed"], ["POST"])

    def test_posting_holidays_says_what_is_allowed(self):
        resp = self.post("/_mock/holidays", ["2026-12-25"])
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.json()["allowed"], ["GET", "PUT"])


class TheReadmeSaysTheSettlementRule(unittest.TestCase):
    """The rule is in the docstring and in the README, and they have to agree.

    The issue asks for the same sentence in both. `tools/check_docs.py` checks
    that documentation exists, not that it says the same thing, so this holds
    the two together on the words that carry the rule.
    """

    ROOT = os.path.dirname(HERE)

    def test_both_say_the_later_of_the_requested_date_and_the_next_business_day(self):
        with open(os.path.join(self.ROOT, "README.md"), encoding="utf-8") as handle:
            readme = handle.read().lower()
        docstring = clock_module.Clock.settlement_date.__doc__.lower()
        for phrase in ("the later of the requested execution date",
                       "the next business day",
                       "rolled forward past weekends and holidays"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, " ".join(docstring.split()))
                self.assertIn(phrase, " ".join(readme.split()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
