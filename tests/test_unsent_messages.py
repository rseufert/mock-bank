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
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from support import FileDatabaseCase                                 # noqa: E402

import datetime                                                       # noqa: E402
from decimal import Decimal                                          # noqa: E402

from mockbank import messages, schema                                # noqa: E402

PAIN001 = schema.MESSAGES["pain.001.001.09"]
# A valid IBAN whose check digits agree, for an account the seed does not hold.
SPARE_IBAN = "NL19MOCK0000000009"
ACME, GLOBEX = "NL41MOCK0000000001", "NL14MOCK0000000002"
WHEN = datetime.date(2026, 10, 1)


def pain001(msg_id, debtor, payments):
    """A pain.001 from `debtor` paying [(EndToEndId, minor units, creditor IBAN)]."""
    total = str(Decimal(sum(p[1] for p in payments)).scaleb(-2))
    return schema.serialize(PAIN001, {"CstmrCdtTrfInitn": {
        "GrpHdr": {"MsgId": msg_id,
                   "CreDtTm": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0),
                   "NbOfTxs": len(payments), "CtrlSum": total,
                   "InitgPty": {"Nm": "Test"}},
        "PmtInf": [{"PmtInfId": msg_id + "-B1", "PmtMtd": "TRF",
                    "NbOfTxs": len(payments), "CtrlSum": total,
                    "ReqdExctnDt": {"Dt": WHEN}, "Dbtr": {"Nm": "Debtor"},
                    "DbtrAcct": {"Id": {"IBAN": debtor}},
                    "DbtrAgt": {"FinInstnId": {"BICFI": "MOCKNL2A"}},
                    "CdtTrfTxInf": [{"PmtId": {"EndToEndId": e2e},
                                     "Amt": {"InstdAmt": schema.Amount(minor, "EUR")},
                                     "Cdtr": {"Nm": "Creditor %s" % e2e},
                                     "CdtrAcct": {"Id": {"IBAN": creditor}}}
                                    for e2e, minor, creditor in payments]}]}})


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

    def walk(self, days=6):
        """Advance a day at a time, insisting the bank answers every time."""
        for day in range(days):
            advance = self.post("/_mock/advance?days=1")
            self.assertEqual(advance.status, 200,
                             "advance stopped on day %d: %s" % (day + 1, advance.body[:200]))
            self.assertEqual(self.get("/_mock/mailbox").status, 200)

    def write_row(self, sql, parameters=()):
        """Change the OLD row from outside, with the mock stopped.

        The same premise as `setUp`: this is a row an older mock wrote. It has to
        be done this way rather than through `PATCH`, because part 1 refuses a
        `PATCH` on an account whose stored name no message can carry - see
        `test_patching_it_is_refused_until_the_name_is_fixed`.
        """
        self.stop()
        conn = sqlite3.connect(self.db_path)
        conn.execute(sql, parameters)
        conn.commit()
        conn.close()
        self.start()

    def unsent_of(self, kind, account="OLD"):
        return [row for row in self.unsent()
                if row["type"] == kind and row["account"] == account]

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

    def test_a_debit_from_it_books_and_only_its_notification_is_lost(self):
        """The money moves; the `camt.054` for it does not get written.

        This replaces a test that accepted 202 **or** 422 and so pinned almost
        nothing. @Dre had to check this path by hand on the PR, which is the
        argument for pinning it here.
        """
        answer = self.post("/payments", body=pain001("OLD-1", SPARE_IBAN,
                                                     [("O-1", 2500, GLOBEX)]))
        self.assertEqual(answer.status, 202, answer.body)
        self.assertEqual(answer.json()["status"], "ACCP")
        # Booked: the money moved, which is the half that must not be lost.
        paid = self.get("/_mock/payments/O-1").json()
        self.assertEqual(paid["status"], "accepted")
        self.assertIsNotNone(paid["booked_at"])
        self.assertEqual(self.get("/_mock/accounts/OLD").json()["balance"], 2500)
        # And exactly one notification could not be written for it.
        [notification] = self.unsent_of(messages.CAMT054.name)
        self.assertIn("Nm", notification["problem"])
        self.walk()

    def test_a_return_goes_back_with_both_of_its_messages_unsent(self):
        """`return-later` on the same account: the `pacs.004` and the credit.

        Two messages for one event, and each is given up on once - the path with
        the most to go wrong, and the one furthest from the statement the other
        tests use.
        """
        self.write_row("UPDATE account SET behaviour = 'return-later',"
                       " parameters = '{\"days\": 1}' WHERE id = 'OLD'")
        self.post("/payments", body=pain001("OLD-2", SPARE_IBAN,
                                            [("O-2", 2500, GLOBEX)]))
        self.walk()
        came_back = self.get("/_mock/payments/O-2").json()
        self.assertEqual(came_back["status"], "returned", came_back)
        # The money is back, so the return itself happened.
        self.assertEqual(self.get("/_mock/accounts/OLD").json()["balance"], 5000)
        self.assertEqual(len(self.unsent_of(messages.PACS004.name)), 1, self.unsent())
        # Three camt.054s could not be written: the debit, and the credit coming
        # back, and the day's statement is its own type.
        self.assertGreaterEqual(len(self.unsent_of(messages.CAMT054.name)), 2,
                                self.unsent())
        # Nothing is left queued waiting to be retried.
        self.assertEqual(self.get("/_mock/queue").json(), [])

    def test_patching_it_is_refused_until_the_name_is_fixed(self):
        """A side effect of part 1 worth stating rather than discovering.

        The trial write is on the account as it *would be*, so any `PATCH` of an
        account whose stored name no message can carry is refused - even one that
        has nothing to do with the name. That is the right answer, since the bank
        would not be able to write about the account afterwards either, and there
        is a way out: fix the name in the same call.
        """
        refused = self.patch("/_mock/accounts/OLD", body={"behaviour": "return-later"})
        self.assertEqual(refused.status, 400, refused.body)
        self.assertIn("Nm", refused.json()["error"])
        # The way out, in one call.
        fixed = self.patch("/_mock/accounts/OLD",
                           body={"name": "Old Account", "behaviour": "return-later"})
        self.assertEqual(fixed.status, 200, fixed.body)
        # And from here the bank can write about it again.
        self.post("/_mock/advance?days=1")
        self.assertEqual(self.unsent_of(messages.CAMT053.name), [],
                         "a statement was still given up on after the name was fixed")

    def test_paying_into_it_is_not_affected(self):
        # The account is only unwritable as the *subject* of a message. A payment
        # from a healthy account that merely names it as creditor is untouched.
        answer = self.post("/payments", body=pain001("IN-1", ACME,
                                                     [("I-1", 1000, SPARE_IBAN)]))
        self.assertEqual(answer.status, 202, answer.body)
        self.assertEqual(self.get("/_mock/payments/I-1").json()["status"], "accepted")
        self.assertEqual(self.unsent_of(messages.CAMT054.name, "ACME"), [])
        self.walk()

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
