"""What a mock left running for weeks keeps, and what it lets go of.

A shared staging bank that nobody restarts has a request log and a message table
that grow without end, and the only remedy was `POST /_mock/reset`, which throws
the accounts away with them. So two flags bound it.

The other half of this is the indexes, which already existed by the time the
issue came up: the tests here hold them in place by name, against the query
plans, so that a schema change cannot quietly drop one and leave a mock that
works and gets slower every week.

The records of messages the bank could not write grow the same way, one per
business day for as long as an account the writer refuses is in the database,
and retention never touched them (#214). They do now, by age alone.
"""
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockbank import db                                        # noqa: E402

from support import FileDatabaseCase, MockServerCase           # noqa: E402
from test_payments import sample                               # noqa: E402


class TheRequestLogIsBounded(MockServerCase):
    config_kwargs = {"keep_requests": 20}

    def setUp(self):
        self.addCleanup(self.post, "/_mock/reset")

    def rows(self):
        return self.get("/_mock/state").json()["requests"]

    def test_more_requests_than_the_limit_leave_about_the_limit(self):
        for _ in range(45):
            self.get("/_mock/health")
        self.assertGreater(self.rows(), 40, "nothing was logged to prune")
        # Pruned on an advance, which is when a long-running mock ages.
        self.post("/_mock/advance?days=1")
        # *About* the limit, and the word is deliberate: the prune leaves
        # exactly 20, and then the advance's own row is logged after it. The
        # promise is that the table stays bounded near N, not that it never
        # exceeds N for an instant - between prunes it can exceed it by up to
        # PRUNE_EVERY, which is the design and is what the README now says.
        self.assertLessEqual(self.rows(), 25,
                             "the request log is not bounded by --keep-requests")

    def test_what_is_kept_is_the_newest(self):
        for _ in range(45):
            self.get("/_mock/health")
        self.get("/_mock/behaviours")          # the newest thing that happened
        self.post("/_mock/advance?days=1")
        paths = [row["path"] for row in self.get("/_mock/requests").json()]
        self.assertIn("/_mock/behaviours", paths)

    def test_state_says_what_it_removed(self):
        for _ in range(45):
            self.get("/_mock/health")
        self.post("/_mock/advance?days=1")
        retention = self.get("/_mock/state").json()["retention"]
        self.assertEqual(retention["keepRequests"], 20)
        self.assertGreater(retention["pruned"].get("request_log", 0), 0)


class ByDefaultNothingIsPruned(MockServerCase):
    """The ordinary in-memory mock must not lose anything under a test's feet."""

    def test_the_default_keeps_what_a_test_does(self):
        before = self.get("/_mock/state").json()["requests"]
        for _ in range(30):
            self.get("/_mock/health")
        self.post("/_mock/advance?days=1")
        after = self.get("/_mock/state").json()["requests"]
        # 5000 by default, so thirty requests are all still there.
        self.assertGreater(after, before + 25)

    def test_retention_days_is_off_by_default(self):
        retention = self.get("/_mock/state").json()["retention"]
        self.assertEqual(retention["retentionDays"], 0.0)
        self.assertEqual(retention["pruned"], {})


class WhatRetentionDaysRemoves(unittest.TestCase):
    """Driven on a connection, because it needs rows dated weeks ago.

    Moving the clock forward will not do it: these timestamps are the control
    plane's, in real UTC, and the clock is bank time. A test that advanced the
    clock a month would prove nothing about them.
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.seed(self.conn)
        self.addCleanup(self.conn.close)

    def old(self, days):
        return db.stamp(db.utcnow() - datetime.timedelta(days=days))

    def add_request(self, at):
        self.conn.execute("INSERT INTO request_log (method, path, status, at)"
                          " VALUES ('GET', '/x', 200, ?)", (at,))

    def add_message(self, taken_at, released=True):
        self.conn.execute(
            "INSERT INTO message (type, account, due_at, released_at, taken_at,"
            " body) VALUES ('pain.002.001.10', 'ACME', ?, ?, ?, '<Document/>')",
            (self.old(40), self.old(40) if released else None, taken_at))

    def test_old_requests_go_and_recent_ones_stay(self):
        self.add_request(self.old(40))
        self.add_request(self.old(1))
        self.conn.commit()
        removed = db.prune(self.conn, retention_days=7)
        self.assertEqual(removed.get("request_log"), 1)
        self.assertEqual(db.count(self.conn, "request_log"), 1)

    def test_a_message_the_client_took_long_ago_goes(self):
        self.add_message(taken_at=self.old(40))
        self.conn.commit()
        removed = db.prune(self.conn, retention_days=7)
        self.assertEqual(removed.get("message"), 1)

    def test_a_message_nobody_has_collected_stays_however_old(self):
        # The whole point of a mailbox is that it waits. A message aged out
        # before anyone read it is a message the mock lost.
        self.add_message(taken_at=None)
        self.conn.commit()
        db.prune(self.conn, retention_days=7)
        self.assertEqual(db.count(self.conn, "message"), 1)

    def test_what_the_bank_did_is_never_pruned(self):
        # payment and file are the evidence a failing test is read against.
        self.conn.execute("INSERT INTO file (msg_id, message, received_at, status)"
                          " VALUES ('OLD', 'pain.001.001.09', ?, 'ACCP')",
                          (self.old(400),))
        self.conn.execute(
            "INSERT INTO payment (file_id, end_to_end_id, account_id, amount,"
            " currency, status, settlement_date) VALUES (1, 'E2E', 'ACME', 5,"
            " 'EUR', 'accepted', '2020-01-01')")
        self.conn.commit()
        db.prune(self.conn, retention_days=1)
        self.assertEqual(db.count(self.conn, "file"), 1)
        self.assertEqual(db.count(self.conn, "payment"), 1)

    def test_the_accounts_and_the_counters_are_what_the_mock_is(self):
        self.conn.execute("INSERT INTO holiday (day) VALUES ('2020-01-01')")
        self.conn.commit()
        db.prune(self.conn, retention_days=1)
        self.assertEqual(db.count(self.conn, "account"), len(db.SEED))
        self.assertEqual(db.count(self.conn, "holiday"), 1)

    def add_unsent(self, at, day="2026-01-01"):
        """A message the bank owed and could not write (#166 part 2)."""
        self.conn.execute(
            "INSERT INTO unsent (type, account, day, problem, at)"
            " VALUES ('camt.053.001.08', 'OLD', ?, 'Nm is only spaces', ?)",
            (day, at))

    def test_an_old_record_of_a_message_never_written_goes(self):
        self.add_unsent(self.old(40))
        self.add_unsent(self.old(8))
        self.conn.commit()
        removed = db.prune(self.conn, retention_days=7)
        self.assertEqual(removed.get("unsent"), 2)
        self.assertEqual(db.count(self.conn, "unsent"), 0)

    def test_a_record_inside_the_window_stays(self):
        # Age alone decides, and this one is not old enough. Nothing else can
        # decide it: there is no message to collect and nothing retries it.
        self.add_unsent(self.old(6))
        self.conn.commit()
        self.assertIsNone(db.prune(self.conn, retention_days=7).get("unsent"))
        self.assertEqual(db.count(self.conn, "unsent"), 1)

    def test_keeping_everything_is_the_default(self):
        self.add_request(self.old(400))
        self.conn.commit()
        self.assertEqual(db.prune(self.conn), {})
        self.assertEqual(db.count(self.conn, "request_log"), 1)


class AgeIsMeasuredOnTheRightClock(MockServerCase):
    """Bank time for messages, real time for the request log.

    `message.taken_at` is written from the bank clock, which a test moves;
    `request_log.at` is when an HTTP request arrived, in real UTC. Measuring a
    message's age against real time deleted messages a client had collected
    seconds earlier - the senior reproduced it with `--clock 2025-01-01` and
    `--retention-days 30`, and four just-collected messages went.
    """

    config_kwargs = {"clock": "2025-01-01T09:00", "retention_days": 30}

    def test_a_message_collected_seconds_ago_is_not_pruned(self):
        self.post("/_mock/reset")
        self.post("/payments", body=sample("pain001_four_payments.xml"))
        collected = self.get("/_mock/mailbox").json()
        self.assertTrue(collected, "nothing to collect, so nothing is proved")
        self.post("/_mock/advance?days=1")
        state = self.get("/_mock/state").json()
        self.assertEqual(state["retention"]["pruned"].get("message", 0), 0,
                         "a message collected moments ago was pruned")
        self.assertGreater(state["messages"]["taken"], 0)

    def test_a_message_collected_long_ago_in_bank_time_does_go(self):
        # The flip side: once bank time has moved past the window, it goes.
        self.post("/_mock/reset")
        self.post("/payments", body=sample("pain001_four_payments.xml"))
        self.get("/_mock/mailbox")
        self.post("/_mock/advance?days=90")
        self.assertGreater(
            self.get("/_mock/state").json()["retention"]["pruned"].get("message", 0),
            0, "bank time moved three months and nothing aged out")


class AMockLeftRunningWithOneBadAccount(FileDatabaseCase):
    """A hundred days of statements nobody could write (#214).

    The input is `test_unsent_messages.py`'s: an account whose name no message
    can carry, written into a `--db` file with the mock stopped, which is what a
    database from before #166's guard is. The bank gives up on its statement
    once per business day, so the rows grow like the message table they stand in
    for - and until now nothing but a reset removed them.

    This is also where the clock is settled. Bank time starts in January and
    moves a hundred days; real time does not move at all while the test runs. A
    cutoff taken from real time would be months past every row here and would
    take the lot, the window's own included, so keeping the recent ones is what
    says `unsent.at` is read on the bank clock.
    """

    # A valid IBAN the seed does not hold, with check digits that agree.
    SPARE_IBAN = "NL19MOCK0000000009"
    WINDOW = 30
    config_kwargs = {"clock": "2026-01-01T09:00", "retention_days": WINDOW}

    def setUp(self):
        super().setUp()
        self.stop()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance,"
            " behaviour, parameters, closed, format, account_number)"
            " VALUES ('OLD', '   ', ?, 'MOCKNL2A', 'EUR', 5000, 'accept', '{}',"
            " 0, 'iso20022', '')", (self.SPARE_IBAN,))
        conn.commit()
        conn.close()
        self.start()

    def test_a_hundred_days_leaves_the_window_and_says_what_went(self):
        for _ in range(20):
            self.assertEqual(self.post("/_mock/advance?days=5").status, 200)
        state = self.get("/_mock/state").json()
        rows = self.get("/_mock/unsent").json()
        today = datetime.date.fromisoformat(state["clock"]["date"])
        self.assertTrue(rows, "no records at all, so nothing is measured here")
        # Business days only, so a hundred days is about seventy records; what
        # is left is the window's worth, and it is the recent ones.
        self.assertLess(len(rows), 30, "a hundred days of records are all kept")
        self.assertGreaterEqual(
            datetime.date.fromisoformat(min(row["at"] for row in rows)[:10]),
            today - datetime.timedelta(days=self.WINDOW),
            "a record older than the window is still there")
        self.assertGreater(state["retention"]["pruned"].get("unsent", 0), 40,
                           "retention removed none of them")
        self.assertEqual(state["messages"]["unsent"], len(rows),
                         "the count and the listing disagree")

    def test_the_day_just_given_up_on_is_not_taken(self):
        # The flip side of the clock: the newest record is seconds old in bank
        # time, and a window of thirty days keeps it.
        self.post("/_mock/advance?days=1")
        rows = self.get("/_mock/unsent").json()
        self.assertEqual([row["day"] for row in rows], ["2026-01-01"])
        self.assertEqual(
            self.get("/_mock/state").json()["retention"]["pruned"].get("unsent", 0),
            0, "a record the bank wrote moments ago was pruned")


class APrunedStatementMessage(unittest.TestCase):
    """A statement points at the camt.053 it was sent as.

    Pruning the message left `statement.message_id` naming a row that was gone,
    so `GET /_mock/accounts/<id>/statements` handed out an id that answered 404.
    The statement row is the record and it still reconciles; the reference is
    cleared instead.
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.seed(self.conn)
        self.addCleanup(self.conn.close)

    def test_the_reference_is_cleared_rather_than_left_dangling(self):
        old = db.stamp(db.utcnow() - datetime.timedelta(days=40))
        cursor = self.conn.execute(
            "INSERT INTO message (type, account, due_at, released_at, taken_at,"
            " body) VALUES ('camt.053.001.08', 'ACME', ?, ?, ?, '<Document/>')",
            (old, old, old))
        message_id = cursor.lastrowid
        self.conn.execute(
            "INSERT INTO statement (account, day, number, opening, closing,"
            " entries, message_id) VALUES ('ACME', '2026-01-01', 1, 0, 0, 0, ?)",
            (message_id,))
        self.conn.commit()

        db.prune(self.conn, retention_days=7)
        self.assertEqual(db.count(self.conn, "message"), 0)
        # The statement survives, and no longer points at nothing.
        statement = db.one(self.conn, "SELECT * FROM statement WHERE account = 'ACME'")
        self.assertIsNotNone(statement)
        self.assertIsNone(statement["message_id"])


class RetentionSettingsTheMockCannotActOn(unittest.TestCase):
    """Every one of these got through before.

    `inf` and `1e9` reached `timedelta` and came back as an OverflowError at
    startup. `nan` and `-3` were silently treated as "off", which is the worst
    of the three: the operator believes the mock is bounded and it is not.
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def refusal(self, **kwargs):
        with self.assertRaises(db.Unusable) as caught:
            db.prune(self.conn, **kwargs)
        return str(caught.exception)

    def test_a_window_that_is_not_a_number_is_refused(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(days=value):
                self.assertIn("is not one", self.refusal(retention_days=value))

    def test_a_negative_window_is_refused_rather_than_ignored(self):
        self.assertIn("cannot be negative", self.refusal(retention_days=-3))

    def test_an_absurd_window_is_refused_rather_than_overflowing(self):
        self.assertIn("at most", self.refusal(retention_days=1e9))

    def test_a_fraction_of_a_day_is_fine(self):
        # Twelve hours is a reasonable thing to ask for.
        db.prune(self.conn, retention_days=0.5)

    def test_the_century_bound_itself_is_fine(self):
        db.prune(self.conn, retention_days=db.MAX_RETENTION_DAYS)

    def test_a_negative_row_count_is_refused(self):
        self.assertIn("cannot be negative", self.refusal(keep_requests=-5))

    def test_a_row_count_that_is_not_whole_is_refused(self):
        self.assertIn("whole number", self.refusal(keep_requests=2.5))

    def test_the_mock_refuses_to_start_rather_than_pruning_wrongly(self):
        import subprocess
        import sys as _sys
        result = subprocess.run(
            [_sys.executable, "-m", "mockbank", "--host", "203.0.113.1",
             "--port", "8099", "--retention-days", "nan"],
            cwd=os.path.dirname(HERE), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("retention_days", result.stderr)
        self.assertNotIn("OverflowError", result.stderr)


class TheLookupsThePipelineDoesAreIndexed(unittest.TestCase):
    """The query plans, by index name.

    These already passed when the issue was written - #6, #7 and #9 added the
    indexes with the tables. What was missing was anything stopping a later
    schema change from dropping one, which would leave a mock that still works
    and gets slower every week. A plan naming the index is the cheapest thing
    that notices.
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.seed(self.conn)
        self.addCleanup(self.conn.close)

    def plan(self, sql, params=()):
        return " ".join(row[3] for row in
                        self.conn.execute("EXPLAIN QUERY PLAN " + sql, params))

    def test_the_duplicate_check_uses_the_msgid_index(self):
        plan = self.plan("SELECT id FROM file WHERE msg_id = ?", ("X",))
        self.assertIn("ix_file_msg_id", plan)
        self.assertNotIn("SCAN file", plan)

    def test_a_payment_by_end_to_end_id_uses_its_index(self):
        plan = self.plan("SELECT * FROM payment WHERE end_to_end_id = ?", ("X",))
        self.assertIn("ix_payment_end_to_end_id", plan)

    def test_the_mailbox_uses_its_index(self):
        plan = self.plan("SELECT id FROM message WHERE released_at IS NOT NULL"
                         " AND taken_at IS NULL")
        self.assertIn("ix_message_mailbox", plan)

    def test_an_account_by_iban_uses_its_index(self):
        plan = self.plan("SELECT * FROM account WHERE iban = ?", ("X",))
        self.assertIn("ix_account_iban", plan)

    def test_every_index_the_schema_declares_is_actually_created(self):
        made = {row["name"] for row in db.rows(
            self.conn, "SELECT name FROM sqlite_master WHERE type = 'index'")}
        for statement in db.INDEXES:
            name = statement.split("IF NOT EXISTS")[1].split(" ON ")[0].strip()
            with self.subTest(index=name):
                self.assertIn(name, made)


class RealPaymentsThroughAPrunedMock(MockServerCase):
    """Pruning must not break the bank, only bound what it remembers."""

    config_kwargs = {"keep_requests": 5}

    def test_the_pipeline_still_works_with_the_log_being_pruned_constantly(self):
        self.post("/_mock/reset")
        resp = self.post("/payments", body=sample("pain001_four_payments.xml"))
        self.assertEqual(resp.status, 202)
        self.post("/_mock/advance?days=1")
        # The payments and the messages survived; only the log was trimmed.
        self.assertEqual(len(self.get("/_mock/payments").json()), 4)
        self.assertTrue(self.get("/_mock/mailbox?leave").json())
        self.assertLessEqual(self.get("/_mock/state").json()["requests"], 5)


class AMessageTheFolderNeverReceived(unittest.TestCase):
    """The guard that only matters once the folder door and retention are both on.

    A client that polls a directory has no mailbox to fall back on: if
    retention ages out a message before it was written, that message is gone
    with nothing to say so. So with a pickup directory configured, a message is
    prunable only once `written_at` is set.

    Driven on a connection for the same reason as `WhatRetentionDaysRemoves`:
    these timestamps are real UTC and moving the bank clock would not age them.
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.seed(self.conn)
        self.addCleanup(self.conn.close)

    def old(self, days):
        return db.stamp(db.utcnow() - datetime.timedelta(days=days))

    def add_message(self, written):
        """Released and collected forty days ago; written, or not."""
        self.conn.execute(
            "INSERT INTO message (type, account, due_at, released_at, taken_at,"
            " written_at, body) VALUES ('pain.002.001.10', 'ACME', ?, ?, ?, ?,"
            " '<Document/>')",
            (self.old(40), self.old(40), self.old(40),
             self.old(40) if written else None))
        self.conn.commit()

    def test_an_unwritten_message_stays_however_old(self):
        self.add_message(written=False)
        db.prune(self.conn, retention_days=7, require_written=True)
        self.assertEqual(db.count(self.conn, "message"), 1)

    def test_once_it_has_been_written_it_ages_out_as_before(self):
        # The other half: the guard delays a message, it does not exempt it.
        self.add_message(written=True)
        removed = db.prune(self.conn, retention_days=7, require_written=True)
        self.assertEqual(removed.get("message"), 1)
        self.assertEqual(db.count(self.conn, "message"), 0)

    def test_without_the_guard_the_unwritten_message_goes(self):
        # Without this, the two tests above would also pass if `require_written`
        # did nothing at all and no mock ever wrote `written_at`.
        self.add_message(written=False)
        removed = db.prune(self.conn, retention_days=7)
        self.assertEqual(removed.get("message"), 1)

    def test_a_pruned_statement_message_still_clears_its_reference(self):
        # The guard narrows which messages go; it must not let one go while
        # leaving a statement pointing at it.
        self.add_message(written=True)
        message_id = db.one(self.conn, "SELECT id FROM message")["id"]
        self.conn.execute(
            "INSERT INTO statement (account, day, number, opening, closing,"
            " entries, message_id) VALUES ('ACME', '2026-01-02', 1, 0, 0, 0, ?)",
            (message_id,))
        self.conn.commit()
        db.prune(self.conn, retention_days=7, require_written=True)
        self.assertIsNone(
            db.one(self.conn, "SELECT message_id FROM statement")["message_id"])


class ThePickupDirectoryTurnsTheGuardOn(MockServerCase):
    """That the rule above is wired to the flag, not just available.

    Two mocks, one with a pickup directory and one without, and the same row
    put into each. A guard nothing switches on is the failure this catches.
    """

    @classmethod
    def setUpClass(cls):
        cls.pickup = tempfile.mkdtemp()
        cls.config_kwargs = dict(cls.config_kwargs, pickup_dir=cls.pickup,
                                 retention_days=7)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls.pickup, ignore_errors=True)

    def unwritten_message(self):
        """A message released and collected long ago that was never written."""
        state = self.httpd.state
        old = db.stamp(db.utcnow() - datetime.timedelta(days=40))
        with state.lock:
            state.conn.execute(
                "INSERT INTO message (type, account, due_at, released_at,"
                " taken_at, written_at, body) VALUES ('pain.002.001.10',"
                " 'ACME', ?, ?, ?, NULL, '<Document/>')", (old, old, old))
            state.conn.commit()
            return db.count(state.conn, "message")

    def test_it_is_not_pruned_while_the_folder_has_not_had_it(self):
        before = self.unwritten_message()
        self.httpd.state.prune()
        with self.httpd.state.lock:
            self.assertEqual(db.count(self.httpd.state.conn, "message"), before)


class WithNoPickupDirectoryNothingWaits(MockServerCase):
    """The same row on a mock with no pickup directory: it ages out.

    `written_at` is NULL on every message a mock without a pickup directory
    ever wrote, so a guard that ignored the flag would switch retention off
    entirely and this is what would notice.
    """

    config_kwargs = {"retention_days": 7}

    def test_an_old_collected_message_still_goes(self):
        state = self.httpd.state
        old = db.stamp(db.utcnow() - datetime.timedelta(days=40))
        with state.lock:
            state.conn.execute(
                "INSERT INTO message (type, account, due_at, released_at,"
                " taken_at, written_at, body) VALUES ('pain.002.001.10',"
                " 'ACME', ?, ?, ?, NULL, '<Document/>')", (old, old, old))
            state.conn.commit()
        state.prune()
        with state.lock:
            self.assertEqual(db.count(state.conn, "message"), 0)



if __name__ == "__main__":
    unittest.main(verbosity=2)
