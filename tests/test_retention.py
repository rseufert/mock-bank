"""What a mock left running for weeks keeps, and what it lets go of.

A shared staging bank that nobody restarts has a request log and a message table
that grow without end, and the only remedy was `POST /_mock/reset`, which throws
the accounts away with them. So two flags bound it.

The other half of this is the indexes, which already existed by the time the
issue came up: the tests here hold them in place by name, against the query
plans, so that a schema change cannot quietly drop one and leave a mock that
works and gets slower every week.
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockbank import db                                        # noqa: E402

from support import MockServerCase                             # noqa: E402
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

    def test_keeping_everything_is_the_default(self):
        self.add_request(self.old(400))
        self.conn.commit()
        self.assertEqual(db.prune(self.conn), {})
        self.assertEqual(db.count(self.conn, "request_log"), 1)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
