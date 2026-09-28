"""A balance a statement cannot write is refused where it would arise (#106).

A camt amount has 18 digits, and a balance in minor units uses every one. Past
that, the statement writer refuses the balance, and before this every
`POST /_mock/advance` answered 500 for as long as the account held it. The
balance is kept inside `accounts.MAX_BALANCE` at the doors that move it: set
directly by `PATCH` or at creation, lowered by a debit, and raised by a return
that was scheduled before a `PATCH` raised the balance under it.
"""
import datetime

from test_payments import ACME, TODAY, UMBRELLA, PipelineCase, pain001

from mockbank import accounts, db

MAX = accounts.MAX_BALANCE


class SetDirectly(PipelineCase):

    def test_a_balance_past_18_digits_is_refused_either_side_of_zero(self):
        for balance in (MAX + 1, -(MAX + 1), str(MAX + 1)):
            with self.subTest(balance=balance):
                resp = self.request("PATCH", "/_mock/accounts/ACME",
                                    body={"balance": balance})
                self.assertEqual(resp.status, 400, resp.body)
                self.assertIn("18 digits", resp.json()["error"])
        self.assertNotEqual(self.balance("ACME"), MAX + 1)

    def test_an_account_is_not_created_with_one_either(self):
        body = {"id": "BIG", "iban": db.iban("NL", "MOCK0000000006"), "balance": MAX + 1}
        resp = self.post("/_mock/accounts", body=body)
        self.assertEqual(resp.status, 400, resp.body)
        self.assertIn("18 digits", resp.json()["error"])
        self.assertEqual(self.get("/_mock/accounts/BIG").status, 404)
        # The same account with a balance that fits is made: the refusal was
        # the balance and nothing else.
        resp = self.post("/_mock/accounts", body=dict(body, balance=MAX))
        self.assertEqual(resp.status, 201, resp.body)

    def test_the_largest_balance_there_is_still_makes_a_statement(self):
        for balance in (MAX, -MAX):
            with self.subTest(balance=balance):
                self.post("/_mock/reset")
                self.patch_account("ACME", balance=balance)
                resp = self.post("/_mock/advance?days=1")
                self.assertEqual(resp.status, 200, resp.body)
                statements = [m for m in self.get("/_mock/mailbox?type=camt.053").json()
                              if m["account"] == "ACME"]
                self.assertEqual(len(statements), 1)


class RaisedByAReturnStillToCome(PipelineCase):

    def test_a_patch_counts_what_will_be_credited_back(self):
        self.patch_account("ACME", balance=100000, behaviour="return-later",
                           parameters={"days": 3})
        answer = self.send(pain001("RL-1", ACME, [("R1", 50000, UMBRELLA)])).json()
        self.assertEqual(self.outcomes(answer), [("R1", "accepted", None)])
        self.post("/_mock/advance?days=0")               # books today
        too_much = self.request("PATCH", "/_mock/accounts/ACME",
                                body={"balance": MAX - 50000 + 1})
        self.assertEqual(too_much.status, 400, too_much.body)
        self.assertIn("still to be returned", too_much.json()["error"])
        self.patch_account("ACME", balance=MAX - 50000)
        resp = self.post("/_mock/advance?days=7")
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(self.balance("ACME"), MAX)


class LoweredByADebit(PipelineCase):

    def test_a_debit_that_would_overdraw_past_it_is_rejected_am02(self):
        self.patch_account("ACME", balance=-MAX + 100)
        answer = self.send(pain001("OD-1", ACME, [("D1", 101, UMBRELLA),
                                                  ("D2", 100, UMBRELLA)])).json()
        self.assertEqual(self.outcomes(answer), [("D1", "rejected", "AM02"),
                                                 ("D2", "accepted", None)])
        self.assertEqual(self.balance("ACME"), -MAX)
        resp = self.post("/_mock/advance?days=1")
        self.assertEqual(resp.status, 200, resp.body)

    def test_two_debits_in_one_file_are_held_to_it_together(self):
        # Each fits alone; the second does not fit after the first.
        self.patch_account("ACME", balance=-MAX + 150)
        answer = self.send(pain001("OD-2", ACME, [("D1", 100, UMBRELLA),
                                                  ("D2", 100, UMBRELLA)])).json()
        self.assertEqual(self.outcomes(answer), [("D1", "accepted", None),
                                                 ("D2", "rejected", "AM02")])

    def test_one_accepted_for_later_counts_too(self):
        self.patch_account("ACME", balance=-MAX + 150)
        later = TODAY + datetime.timedelta(days=5)
        first = self.send(pain001("OD-3", ACME, [("L1", 100, UMBRELLA)], when=later)).json()
        self.assertEqual(self.outcomes(first), [("L1", "accepted", None)])
        second = self.send(pain001("OD-4", ACME, [("L2", 100, UMBRELLA)])).json()
        self.assertEqual(self.outcomes(second), [("L2", "rejected", "AM02")])
