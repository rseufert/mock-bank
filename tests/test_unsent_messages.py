"""One message the bank cannot write must not stop the others (#166, part 2).

Part 1 shut the doors: a value a later message cannot hold is refused when it
arrives. This is the other half, and it is needed precisely because the doors
cannot be complete. A `--db` file carries rows written by an older mock, before
a guard existed - the same reason a database upgraded from 0.5.0 keeps real-time
stamps on the rows it already had (#147). So the bank has to cope with state it
would not accept today.

What it does, and what these tests pin:

* the money still moves - a booking that happened stays happened;
* the message is given up on, with the writer's own complaint kept;
* **nothing retries it**, because the value will not fix itself and a retry would
  add a row per advance for ever;
* every other account's message for the same day is still written;
* `POST /_mock/advance`, the mailbox and later payments keep answering.

The input is a real one rather than a forced failure: an account whose name no
message can carry, in the database before the mock opens it. Part 1 refuses such a
name at both doors now - `POST /_mock/accounts` and `PATCH` - and the test for
that is in `test_unwritable_inputs.py`; this file is about the row that is already
there.
"""
import json
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from support import FileDatabaseCase                                 # noqa: E402

from mockbank import messages                                        # noqa: E402

# A valid IBAN whose check digits agree, for an account the seed does not hold.
SPARE_IBAN = "NL19MOCK0000000009"


class AnAccountNoMessageCanName(FileDatabaseCase):
    """A row from before the guard: the account's name is only spaces.

    Written straight into the file with the mock stopped, which is what a `--db`
    from an older version is. `PATCH` and `POST /_mock/accounts` both refuse this
    name now, so there is no way to reach this state through the control plane -
    which is the point: the bank still has to answer.
    """

    config_kwargs = {"clock": "2026-10-01T09:00"}

    def setUp(self):
        super().setUp()
        self.stop()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance, behaviour,"
            " parameters, closed, format, account_number)"
            " VALUES ('OLD', '   ', ?, 'MOCKNL2A', 'EUR', 5000, 'accept', '{}', 0,"
            " 'iso20022', '')", (SPARE_IBAN,))
        conn.commit()
        conn.close()
        self.start()

    def unsent(self):
        answer = self.get("/_mock/unsent")
        self.assertEqual(answer.status, 200, answer.body)
        return answer.json()

    def counted(self):
        return self.get("/_mock/state").json()["messages"]["unsent"]

    def test_the_release_path_keeps_answering(self):
        for day in range(4):
            advance = self.post("/_mock/advance?days=1")
            self.assertEqual(advance.status, 200,
                             "advance stopped on day %d: %s" % (day + 1, advance.body[:200]))
            self.assertEqual(self.get("/_mock/mailbox").status, 200)
        self.assertEqual(self.get("/_mock/payments").status, 200)

    def test_what_it_could_not_write_is_kept_with_the_reason(self):
        self.post("/_mock/advance?days=1")
        [row] = [r for r in self.unsent() if r["account"] == "OLD"]
        self.assertEqual(row["type"], messages.CAMT053.name)
        self.assertEqual(row["day"], "2026-10-01")
        # The writer's own complaint, with the element it refused, so a reader
        # knows what to change rather than that "something went wrong".
        self.assertIn("Nm", row["problem"])
        self.assertIn("Ownr", row["problem"])
        self.assertTrue(row["at"], row)
        self.assertEqual(self.counted(), len(self.unsent()))

    def test_nothing_retries_it(self):
        """One row per day it could not write, not one per advance.

        Stated as an invariant rather than a count: four advances of a day each
        from a Thursday cross a weekend, and no statement is issued for a Saturday
        or a Sunday, so the number is a fact about the calendar. Asserting `4` here
        failed for exactly that reason, and a number pinned to the seeded clock
        would rot the moment the clock moved.
        """
        self.post("/_mock/advance?days=1")
        self.assertEqual(self.counted(), 1, self.unsent())
        for _ in range(3):
            self.post("/_mock/advance?days=1")
        rows = self.unsent()
        days = [row["day"] for row in rows]
        self.assertEqual(len(days), len(set(days)),
                         "the same day was given up on more than once: %s" % days)
        # And exactly the days the bank recorded as issued without a message.
        issued = [row["day"] for row in
                  self.get("/_mock/accounts/OLD/statements").json()
                  if row["message_id"] is None]
        self.assertEqual(sorted(days), sorted(issued))
        self.assertGreater(len(days), 1, "the clock did not reach a second day")

    def test_the_other_accounts_still_get_that_days_statement(self):
        self.post("/_mock/advance?days=1")
        for account in ("ACME", "GLOBEX"):
            issued = self.get("/_mock/accounts/%s/statements" % account).json()
            self.assertTrue(issued, "%s got no statement" % account)
            self.assertIsNotNone(issued[0]["message_id"],
                                 "%s's statement was not written either" % account)
        # And the one that failed is recorded as issued with no message, which is
        # how a reader tells the two apart without reading /_mock/unsent.
        [old] = self.get("/_mock/accounts/OLD/statements").json()
        self.assertIsNone(old["message_id"])

    def test_a_payment_still_goes_through_afterwards(self):
        self.post("/_mock/advance?days=1")
        answer = self.post("/payments", body=open(
            os.path.join(HERE, "samples", "pain001_four_payments.xml"), "rb").read())
        self.assertIn(answer.status, (202, 422), answer.body)
        self.assertEqual(self.get("/_mock/state").status, 200)

    def test_a_reset_is_a_new_bank_and_forgets_it(self):
        self.post("/_mock/advance?days=1")
        self.assertEqual(self.counted(), 1)
        self.post("/_mock/reset")
        self.assertEqual(self.counted(), 0)
        self.assertEqual(self.unsent(), [])

    def test_it_survives_a_restart_because_the_row_does(self):
        # On --db it is state, not a note in memory: a tester who restarts the
        # mock to look again must still see what it could not send.
        self.post("/_mock/advance?days=1")
        self.assertEqual(self.counted(), 1)
        self.restart()
        self.assertEqual(self.counted(), 1)
        self.assertIn("Nm", self.unsent()[0]["problem"])


class WhenNothingIsUnsent(FileDatabaseCase):
    """The ordinary case, so the two are told apart."""

    config_kwargs = {"clock": "2026-10-01T09:00"}

    def test_a_healthy_bank_reports_none(self):
        for _ in range(3):
            self.assertEqual(self.post("/_mock/advance?days=1").status, 200)
        self.assertEqual(self.get("/_mock/unsent").json(), [])
        self.assertEqual(self.get("/_mock/state").json()["messages"]["unsent"], 0)
        # And the statements that were written all have a message.
        issued = self.get("/_mock/accounts/ACME/statements").json()
        self.assertTrue(issued)
        self.assertTrue(all(row["message_id"] is not None for row in issued), issued)


if __name__ == "__main__":
    unittest.main()
