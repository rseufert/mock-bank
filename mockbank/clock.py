"""Bank time: the cutoff, the weekend, the holidays, and a clock a test moves.

Nothing in the mock waits. A payment that settles tomorrow settles when a test
says ``POST /_mock/advance?days=1``, and a return three business days out is a
test line rather than a three-day wait. So there is one clock, every date the
mock computes is computed against it, and it can be moved.

Three things worth knowing:

**It holds an offset, not an instant.** ``now()`` is real time plus an offset,
so the clock keeps ticking between advances and two messages written a moment
apart still sort in the order they were written. A stored instant would freeze,
and a frozen clock makes every timestamp in a run identical - which is
indistinguishable, in a log, from a bug.

**Bank time is one time zone.** Chosen with ``--timezone``, UTC by default. A
bank has business days and a cutoff hour, and both are meaningless without a
zone; taking the host's would mean the same test lands on a different
settlement date on a laptop than in CI.

**It knows nothing about payments.** It answers questions about dates. What to
do when a date arrives belongs to the pipeline, which registers a callback in
``on_advance``; releasing statements and returns is that code, not this.
"""
from __future__ import annotations

import datetime
import sys
from typing import Callable, Dict, List, Optional, Sequence

# The hour a real bank stops taking today's payments for today. 15:00 is the
# common one for euro credit transfers and it is only a default: a bank that
# cuts off at noon is `--cutoff 12:00`.
DEFAULT_CUTOFF = "15:00"

# Saturday and Sunday. A calendar rather than a constant because the weekend is
# not Saturday and Sunday everywhere - Friday and Saturday in much of the Gulf -
# and when somebody needs that, this is the one place it changes.
WEEKEND = (5, 6)


class Invalid(ValueError):
    """A clock the mock could not keep, and why. Answered 400, or refused at startup."""


def timezone(name: str) -> datetime.tzinfo:
    """Bank time's zone, by name.

    ``zoneinfo`` is 3.9 and later. On 3.8 the mock has no way to know what
    ``Europe/Amsterdam`` means - it will not grow a dependency to find out, and
    guessing would put a mock on 3.8 an hour away from the same mock on 3.12 for
    half the year, which is a bug nobody would look for here. So anything but
    UTC is refused by name on 3.8 rather than quietly treated as UTC.
    """
    if name.upper() in ("UTC", "Z", ""):
        return datetime.timezone.utc
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:
        raise Invalid(
            "--timezone %s needs zoneinfo, which arrived in Python 3.9; this is "
            "%d.%d, where the mock can only keep bank time in UTC. Run it on "
            "3.9 or newer for a named zone, or leave --timezone at UTC."
            % (name, sys.version_info[0], sys.version_info[1])) from None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if not _has_zone_database():
            # Windows ships no IANA database, so `zoneinfo` imports and then
            # finds nothing to read. The fix is the `tzdata` package, which
            # this mock will not require of anyone: a mock you cannot install
            # in a locked-down image is a mock nobody runs. So it says what is
            # missing and who can supply it, and keeps bank time in UTC.
            raise Invalid(
                "--timezone %s needs an IANA time zone database, and this "
                "system has none - which is normal on Windows. `pip install "
                "tzdata` provides one; mock-bank will not depend on it, "
                "because it takes no dependencies. Until then bank time can "
                "only be UTC." % name) from None
        raise Invalid(
            "--timezone %s is not a zone this system knows; use an IANA name "
            "such as Europe/Amsterdam, or UTC." % name) from None
    except ValueError:
        raise Invalid(
            "--timezone %r is not the shape of an IANA zone name; use one like "
            "Europe/Amsterdam, or UTC." % name) from None


def _has_zone_database() -> bool:
    """Whether `zoneinfo` has an IANA database to read, as opposed to just importing.

    Kept apart from `timezone()` so that a refusal can say which of the two
    things is missing. A test skips on a system without one rather than
    asserting that every runner has a tz database, which Windows does not.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return False
    try:
        ZoneInfo("Etc/UTC")
    except Exception:
        return False
    return True


def cutoff_time(text: str) -> datetime.time:
    """``HH:MM`` as a time, or an `Invalid` saying what was wanted."""
    try:
        hour, minute = text.split(":")
        return datetime.time(int(hour), int(minute))
    except (ValueError, TypeError):
        raise Invalid("--cutoff %r is not an HH:MM time of day, as in 15:00"
                      % text) from None


def parse_start(text: str) -> datetime.datetime:
    """``YYYY-MM-DDTHH:MM`` (or with seconds) as a naive bank-time moment."""
    for shape in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, shape)
        except ValueError:
            continue
    raise Invalid("--clock %r is not a moment in bank time; write it as "
                  "YYYY-MM-DDTHH:MM, as in 2026-09-25T14:30" % text)


def parse_date(text: str, what: str = "date") -> datetime.date:
    """``YYYY-MM-DD`` as a date, or an `Invalid` saying what was wanted."""
    try:
        return datetime.datetime.strptime(str(text), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise Invalid("%s %r is not a date; write it as YYYY-MM-DD"
                      % (what, text)) from None


class Clock:
    """Bank time, and the questions the pipeline asks of it.

    Holidays are not held here: they live in the ``holiday`` table, so that
    ``PUT /_mock/holidays`` and a restart on ``--db`` agree about them. The
    clock is given a callable that reads them, which keeps this module free of
    SQL and testable on its own.
    """

    def __init__(self, zone: str = "UTC", cutoff: str = DEFAULT_CUTOFF,
                 start: str = "", holidays: Optional[Callable[[], Sequence]] = None):
        self.zone_name = zone or "UTC"
        self.zone = timezone(self.zone_name)
        self.cutoff = cutoff_time(cutoff or DEFAULT_CUTOFF)
        self._holidays = holidays or (lambda: ())
        # Everything a test moves goes through this one offset; see the module
        # docstring on why it is an offset and not an instant.
        self.start = start
        self.offset = datetime.timedelta(0)
        self.started = self._real()
        if start:
            self.offset = parse_start(start).replace(tzinfo=self.zone) - self.started
        # Callbacks the pipeline issues register into: each is called with the
        # moment before and the moment after an advance, and does its own work.
        self.on_advance: List[Callable[[datetime.datetime, datetime.datetime], None]] = []

    # -- reading it -------------------------------------------------------

    def _real(self) -> datetime.datetime:
        return datetime.datetime.now(self.zone)

    def now(self) -> datetime.datetime:
        """The current moment in bank time, aware and in the bank's zone."""
        return self._real() + self.offset

    def today(self) -> datetime.date:
        return self.now().date()

    def holidays(self) -> List[datetime.date]:
        """The holiday list, as dates, whatever the source hands back."""
        out = []
        for value in self._holidays():
            out.append(value if isinstance(value, datetime.date)
                       else parse_date(str(value), "holiday"))
        return sorted(set(out))

    # -- the calendar -----------------------------------------------------

    def is_business_day(self, day: datetime.date) -> bool:
        return day.weekday() not in WEEKEND and day not in self.holidays()

    def next_business_day(self, day: datetime.date) -> datetime.date:
        """The first business day strictly after `day`."""
        day += datetime.timedelta(days=1)
        while not self.is_business_day(day):
            day += datetime.timedelta(days=1)
        return day

    def roll_forward(self, day: datetime.date) -> datetime.date:
        """`day` itself if it is a business day, else the first one after it."""
        while not self.is_business_day(day):
            day += datetime.timedelta(days=1)
        return day

    def business_days_after(self, day: datetime.date, count: int) -> datetime.date:
        """`count` business days after `day`.

        Counting days, not adding them: three business days after a Thursday is
        the following Tuesday, and further still if one of those days is a
        holiday. `count` of 0 rolls forward off a weekend, because "today, if
        today is a business day" is what a zero-day window means.
        """
        if count < 0:
            raise Invalid("business_days_after counts forwards; %d is negative"
                          % count)
        day = self.roll_forward(day)
        for _ in range(count):
            day = self.next_business_day(day)
        return day

    def business_days_between(self, start: datetime.date,
                              end: datetime.date) -> List[datetime.date]:
        """The business days after `start` and up to and including `end`.

        What an advance crossed, so the statement issue has the list of days it
        owes a `camt.053` for. Empty when the clock went forward within a day or
        only over a weekend.
        """
        days, day = [], start + datetime.timedelta(days=1)
        while day <= end:
            if self.is_business_day(day):
                days.append(day)
            day += datetime.timedelta(days=1)
        return days

    # -- the rule the whole pipeline hangs on ------------------------------

    def settlement_date(self, received_at: datetime.datetime,
                        requested_date: Optional[datetime.date] = None) -> datetime.date:
        """When a payment received at `received_at` settles.

        The later of the requested execution date and the day the bank can start
        on - which is the day of receipt before the cutoff and the next business
        day at or after it - rolled forward past weekends and holidays.

        At the cutoff, not after it: a bank that stops taking today's work at
        15:00 has stopped at 15:00:00. A payment received at 16:00 on a Friday
        settles on Monday, and on Tuesday if Monday is a holiday.
        """
        received_at = self._in_zone(received_at)
        earliest = received_at.date()
        if received_at.timetz().replace(tzinfo=None) >= self.cutoff:
            earliest = self.next_business_day(earliest)
        if requested_date is not None and requested_date > earliest:
            earliest = requested_date
        return self.roll_forward(earliest)

    def _in_zone(self, moment: datetime.datetime) -> datetime.datetime:
        """A moment as bank time reads it; a naive one is already bank time."""
        if moment.tzinfo is None:
            return moment.replace(tzinfo=self.zone)
        return moment.astimezone(self.zone)

    # -- moving it --------------------------------------------------------

    def advance(self, days: Optional[float] = None,
                to: Optional[datetime.date] = None) -> Dict[str, object]:
        """Move bank time forward, and say what was crossed.

        `days` is **calendar** days: three days from a Thursday is Sunday. That
        is what "advance the clock three days" means, and the answer also
        carries the business days crossed for a caller who wanted those - see
        `businessDaysCrossed`.

        `to` moves to 00:00 on that date. Backwards is refused: messages
        already queued for a date the clock had passed would come due a second
        time, and every timestamp written since would be in the future.
        """
        if (days is None) == (to is None):
            raise Invalid("advance takes either days=N or to=YYYY-MM-DD, "
                          "and needs one of them")
        before = self.now()
        if days is not None:
            if days < 0:
                raise Invalid(
                    "the clock does not go backwards: advancing %s days would "
                    "put it before %s, and whatever is queued for a date it had "
                    "already passed would come due a second time"
                    % (days, before.date().isoformat()))
            shift = datetime.timedelta(days=days)
        else:
            target = datetime.datetime.combine(to, datetime.time(0, 0),
                                               tzinfo=self.zone)
            if target < before:
                raise Invalid(
                    "the clock does not go backwards: it is %s and %s is behind "
                    "that, and whatever is queued for a date it had already "
                    "passed would come due a second time"
                    % (before.isoformat(timespec="minutes"), to.isoformat()))
            shift = target - before
        self.offset += shift
        after = self.now()

        crossed = self.business_days_between(before.date(), after.date())
        # The hooks run after the clock has moved, so anything they release is
        # dated by the new time, and before the answer is written, so a client
        # that collects the mailbox on the next line finds what came due.
        for hook in list(self.on_advance):
            hook(before, after)
        return {
            "from": before.isoformat(timespec="seconds"),
            "to": after.isoformat(timespec="seconds"),
            "timezone": self.zone_name,
            "calendarDays": (after.date() - before.date()).days,
            "businessDaysCrossed": [day.isoformat() for day in crossed],
            # Said out loud in the answer, because the two readings differ and
            # the endpoint has to commit to one: ?days=N is calendar days.
            "counts": "calendar days; businessDaysCrossed lists the business "
                      "days this advance passed through",
        }

    def reset(self) -> None:
        """Back to the moment the mock started at, not back to real time.

        With ``--clock`` that is the pinned moment, because ``--clock`` is
        configuration and a reset is not meant to undo configuration: a suite
        that pins bank time and resets between tests would otherwise get the
        pinned moment for its first test and the wall clock for every test
        after it, which is a difference that shows up as one flaky test
        somewhere else entirely. Without ``--clock`` it is real time.

        The holiday list is the database's to clear.
        """
        self.started = self._real()
        self.offset = (parse_start(self.start).replace(tzinfo=self.zone)
                       - self.started) if self.start else datetime.timedelta(0)

    def snapshot(self) -> Dict[str, object]:
        """What `/_mock/state` reports about bank time."""
        now = self.now()
        return {
            "now": now.isoformat(timespec="seconds"),
            "date": now.date().isoformat(),
            "timezone": self.zone_name,
            "cutoff": self.cutoff.strftime("%H:%M"),
            "offsetSeconds": int(self.offset.total_seconds()),
            "isBusinessDay": self.is_business_day(now.date()),
            "pastCutoff": now.timetz().replace(tzinfo=None) >= self.cutoff,
            "nextBusinessDay": self.next_business_day(now.date()).isoformat(),
            "holidays": [day.isoformat() for day in self.holidays()],
        }
