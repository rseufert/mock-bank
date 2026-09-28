"""A holiday declared after something was due on it (#107).

A payment's settlement date, a return's due date and a credit's booking date
are fixed when the bank accepts them. If that day becomes a holiday afterwards,
they used to book on it anyway: no statement is issued for a holiday, so the
entries appeared on none, and the next statement opened at a balance its
predecessor did not close at. Now whatever is still due moves to the next
business day when the holiday is declared, payments and credits by one rule.
"""
from test_payments import ACME, GLOBEX, TODAY, UMBRELLA, pain001
from test_statements import (CAMT053, FRIDAY, MONDAY, TUESDAY, StatementCase,
                             read_statement)


class DeclaredOnADayWithThingsDue(StatementCase):

    def statements(self, collected, iban):
        return sorted((s for s in (read_statement(r) for r in self.of_type(collected, CAMT053))
                       if s["iban"] == iban), key=lambda s: s["number"])

    def assert_chains(self, statements, balance):
        for before, after in zip(statements, statements[1:]):
            self.assertEqual(after["opening"], before["closing"],
                             "%s opens where %s closed" % (after["day"], before["day"]))
        self.assertEqual(statements[-1]["closing"], balance)

    def test_a_payment_a_return_and_a_credit_move_and_every_statement_chains(self):
        # Thursday: ACME pays R1 today, and return-later sends it back on
        # Friday; GLOBEX pays P1 on Friday; money arrives in ACME on Friday.
        self.patch_account("ACME", behaviour="return-later", parameters={"days": 1})
        self.patch_account("GLOBEX", behaviour="accept", balance=100000)
        for answer in (self.send(pain001("H-1", ACME, [("R1", 10000, UMBRELLA)])),
                       self.send(pain001("H-2", GLOBEX, [("P1", 20000, UMBRELLA)],
                                         when=FRIDAY))):
            self.assertEqual([p["outcome"] for p in answer.json()["payments"]],
                             ["accepted"], answer.body)
        arrived = self.post("/_mock/credits", body={
            "account": "ACME", "amount": 30000, "value_date": FRIDAY.isoformat(),
            "end_to_end_id": "C1"})
        self.assertEqual(arrived.status, 201, arrived.body)
        acme, globex = self.balance("ACME"), self.balance("GLOBEX")

        # Then Friday is declared a holiday.
        resp = self.request("PUT", "/_mock/holidays", body=[FRIDAY.isoformat()])
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(self.get("/_mock/payments/P1").json()["settlement_date"],
                         MONDAY.isoformat())
        self.assertEqual(self.get("/_mock/credits").json()[0]["booking_date"],
                         MONDAY.isoformat())

        self.advance(TUESDAY)
        collected = self.mailbox()
        for iban, balance, moved in ((ACME, acme + 10000 + 30000, {"R1", "C1"}),
                                     (GLOBEX, globex - 20000, {"P1"})):
            with self.subTest(account=iban):
                statements = self.statements(collected, iban)
                self.assertEqual([s["day"] for s in statements],
                                 [TODAY.isoformat(), MONDAY.isoformat()])
                self.assert_chains(statements, balance)
                self.assertEqual({e for e, _ in statements[-1]["entries"]} & moved, moved)

    def test_today_is_refused_once_something_has_booked_on_it(self):
        self.send(pain001("H-3", ACME, [("T1", 10000, UMBRELLA)]))       # books now
        resp = self.request("PUT", "/_mock/holidays", body=[TODAY.isoformat()])
        self.assertEqual(resp.status, 409, resp.body)
        self.assertIn("T1 settled on ACME", resp.json()["error"])
        self.assertEqual(self.get("/_mock/holidays").json(), [])

    def test_today_is_declared_when_nothing_has_booked_on_it(self):
        resp = self.request("PUT", "/_mock/holidays", body=[TODAY.isoformat()])
        self.assertEqual(resp.status, 200, resp.body)
        # A payment now settles on the next business day.
        answer = self.send(pain001("H-4", ACME, [("T2", 10000, UMBRELLA)])).json()
        self.assertEqual(answer["payments"][0]["settlement_date"], FRIDAY.isoformat())

    def test_a_day_already_past_moves_nothing(self):
        self.send(pain001("H-5", ACME, [("T3", 10000, UMBRELLA)]))
        self.advance(FRIDAY)
        resp = self.request("PUT", "/_mock/holidays", body=[TODAY.isoformat()])
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(self.get("/_mock/payments/T3").json()["settlement_date"],
                         TODAY.isoformat())
