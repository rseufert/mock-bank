"""POST /payments: read, validate, decide, book - and the precedence of the rules.

Each test in Precedence pins one line of the rule list in
``accounts.decide``'s docstring, and is written so that removing that line
changes the answer.
"""
import datetime
import os
import re
from decimal import Decimal

from support import MockServerCase

from mockbank import schema

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
PAIN001 = schema.MESSAGES["pain.001.001.09"]

ACME, GLOBEX = "NL41MOCK0000000001", "NL14MOCK0000000002"
INITECH, EURODIS = "NL84MOCK0000000003", "NL57MOCK0000000004"
UMBRELLA = "NL30MOCK0000000005"          # at another bank: not held

TODAY = datetime.datetime.now(datetime.timezone.utc).date()


def sample(name, when=TODAY):
    """A sample file, asking for execution on `when` (today unless told)."""
    with open(os.path.join(SAMPLES, name), encoding="utf-8") as handle:
        return handle.read().replace("2026-10-01", when.isoformat())


def amounts(text):
    """The instructed amounts of a file in minor units, read with a regex
    rather than the mock's reader, so the test does not agree with itself."""
    return [int(whole) * 100 + int(cents) for whole, cents in
            re.findall(r'<InstdAmt Ccy="EUR">(\d+)\.(\d\d)</InstdAmt>', text)]


def pain001(msg_id, debtor, payments, when=TODAY, ccy="EUR"):
    """A pain.001 from `debtor` paying [(EndToEndId, minor units, creditor IBAN)]."""
    total = str(Decimal(sum(p[1] for p in payments)).scaleb(-2))
    transactions = [{"PmtId": {"EndToEndId": e2e},
                     "Amt": {"InstdAmt": schema.Amount(minor, ccy)},
                     "Cdtr": {"Nm": "Creditor %s" % e2e},
                     "CdtrAcct": {"Id": {"IBAN": creditor}}}
                    for e2e, minor, creditor in payments]
    return schema.serialize(PAIN001, {"CstmrCdtTrfInitn": {
        "GrpHdr": {"MsgId": msg_id,
                   "CreDtTm": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0),
                   "NbOfTxs": len(payments), "CtrlSum": total,
                   "InitgPty": {"Nm": "Test"}},
        "PmtInf": [{"PmtInfId": msg_id + "-B1", "PmtMtd": "TRF",
                    "NbOfTxs": len(payments), "CtrlSum": total,
                    "ReqdExctnDt": {"Dt": when}, "Dbtr": {"Nm": "Debtor"},
                    "DbtrAcct": {"Id": {"IBAN": debtor}},
                    "DbtrAgt": {"FinInstnId": {"BICFI": "MOCKNL2A"}},
                    "CdtTrfTxInf": transactions}]}})


class PipelineCase(MockServerCase):

    def setUp(self):
        self.post("/_mock/reset")

    def send(self, body):
        return self.post("/payments", body=body)

    def balance(self, account):
        return self.get("/_mock/accounts/" + account).json()["balance"]

    def patch_account(self, account, **fields):
        resp = self.request("PATCH", "/_mock/accounts/" + account, body=fields)
        self.assertEqual(resp.status, 200, resp.body)

    def outcomes(self, answer):
        return [(p["end_to_end_id"], p["outcome"], p["reason"]) for p in answer["payments"]]


class TheSampleFile(PipelineCase):

    def test_against_the_seed_it_is_part_with_a_closed_and_an_unknown_bank(self):
        before = self.balance("ACME")
        text = sample("pain001_four_payments.xml")
        resp = self.send(text)
        self.assertEqual(resp.status, 202)
        answer = resp.json()
        self.assertEqual((answer["status"], answer["accepted"], answer["rejected"]), ("PART", 2, 2))
        self.assertEqual(self.outcomes(answer), [
            ("INV-2026-0101", "accepted", None), ("INV-2026-0102", "rejected", "AC04"),
            ("INV-2026-0103", "rejected", "RC01"), ("INV-2026-0104", "accepted", None)])
        paid = amounts(text)
        self.assertEqual(before - self.balance("ACME"), paid[0] + paid[3])

    def test_the_readme_example_three_accepted_and_one_ac04(self):
        self.patch_account("EURODIS", behaviour="accept")
        before = self.balance("ACME")
        text = sample("pain001_four_payments.xml")
        answer = self.send(text).json()
        self.assertEqual((answer["status"], answer["accepted"], answer["rejected"]), ("PART", 3, 1))
        self.assertEqual([p["reason"] for p in answer["payments"]], [None, "AC04", None, None])
        self.assertTrue(all(p["booked"] for p in answer["payments"] if p["outcome"] == "accepted"))
        paid = amounts(text)
        self.assertEqual(before - self.balance("ACME"), paid[0] + paid[2] + paid[3])

    def test_the_same_file_twice_is_dupl_and_nothing_books_twice(self):
        text = sample("pain001_four_payments.xml")
        self.send(text)
        after_first = self.balance("ACME")
        resp = self.send(text)
        self.assertEqual(resp.status, 422)
        self.assertEqual((resp.json()["status"], resp.json()["reason"]), ("RJCT", "DUPL"))
        self.assertEqual(resp.json()["payments"], [])
        self.assertEqual(self.balance("ACME"), after_first)
        self.assertEqual(self.get("/_mock/state").json()["payments"],
                         {"files": 2, "accepted": 2, "rejected": 2, "booked": 2})

    def test_the_2009_twin_is_decided_the_same(self):
        new = self.send(sample("pain001_four_payments.xml")).json()
        self.post("/_mock/reset")
        old = self.send(sample("pain001_four_payments_001_03.xml")).json()
        self.assertEqual(self.outcomes(old), self.outcomes(new))


class AllowingDuplicates(PipelineCase):
    config_kwargs = {"allow_duplicates": True}

    def test_the_same_file_twice_books_twice(self):
        text = pain001("DUP-1", ACME, [("D1", 1000, UMBRELLA)])
        before = self.balance("ACME")
        self.assertEqual(self.send(text).json()["status"], "ACCP")
        self.assertEqual(self.send(text).json()["status"], "ACCP")
        self.assertEqual(before - self.balance("ACME"), 2000)


class Precedence(PipelineCase):
    """One test per line of decide()'s rule list."""

    # 1. a file-level finding rejects the whole file, whatever the behaviour
    def test_1_a_header_finding_rejects_the_file_outright(self):
        self.patch_account("ACME", behaviour="silent")
        before = self.balance("ACME")
        resp = self.send(sample("pain001_broken_ctrlsum.xml"))
        self.assertEqual(resp.status, 422)
        answer = resp.json()
        self.assertEqual((answer["status"], answer["reason"], answer["payments"]),
                         ("RJCT", "AM10", []))
        self.assertEqual(self.balance("ACME"), before)

    def test_1_a_structural_finding_inside_a_payment_rejects_the_file(self):
        answer = self.send(sample("pain001_broken_namespace.xml")).json()
        self.assertEqual((answer["status"], answer["reason"]), ("RJCT", "FF01"))

    def test_1_an_unreadable_body_is_rejected_and_not_recorded(self):
        resp = self.send("MsgId,Amount\n")
        self.assertEqual((resp.status, resp.json()["status"], resp.json()["reason"]),
                         (422, "RJCT", "FF01"))
        self.assertEqual(self.get("/_mock/state").json()["payments"]["files"], 0)

    def test_1_comes_before_2(self):
        broken = sample("pain001_broken_ctrlsum.xml")
        self.send(broken)
        self.assertEqual(self.send(broken).json()["reason"], "AM10")

    # 2. a MsgId seen before is DUPL
    def test_2_comes_before_3(self):
        text = pain001("TWICE", ACME, [("T1", 100, UMBRELLA)])
        self.send(text)
        self.patch_account("ACME", behaviour="reject-file")
        self.assertEqual(self.send(text).json()["reason"], "DUPL")

    # 3. reject-file
    def test_3_reject_file_rejects_at_group_level(self):
        self.patch_account("ACME", behaviour="reject-file")
        before = self.balance("ACME")
        resp = self.send(pain001("RF-1", ACME, [("R1", 100, UMBRELLA)]))
        self.assertEqual(resp.status, 422)
        self.assertEqual((resp.json()["status"], resp.json()["reason"]), ("RJCT", "FF01"))
        self.assertEqual(self.balance("ACME"), before)

    # 4a. the debtor account
    def test_4a_a_debtor_the_bank_does_not_hold_is_ac02_naming_what_it_holds(self):
        resp = self.send(pain001("NH-1", UMBRELLA, [("N1", 100, ACME), ("N2", 200, ACME)]))
        self.assertEqual(resp.status, 202)
        answer = resp.json()
        self.assertEqual((answer["status"], [p["reason"] for p in answer["payments"]]),
                         ("RJCT", ["AC02", "AC02"]))
        self.assertIn(ACME, answer["payments"][0]["reason_text"])

    def test_4a_a_closed_debtor_account_is_ac04(self):
        self.patch_account("ACME", closed=True)
        answer = self.send(pain001("CD-1", ACME, [("C1", 100, UMBRELLA)])).json()
        self.assertEqual(self.outcomes(answer), [("C1", "rejected", "AC04")])

    # 4b. a payment-level finding
    def test_4b_a_payment_level_finding_rejects_that_payment_with_its_code(self):
        # the broken IBAN is INITECH's, which would otherwise be AC04
        answer = self.send(sample("pain001_broken_iban.xml")).json()
        self.assertEqual(answer["status"], "PART")
        self.assertEqual(answer["payments"][1]["reason"], "AC01")
        # and the wrong currency is on EURODIS's, which would otherwise be RC01
        self.post("/_mock/reset")
        answer = self.send(sample("pain001_broken_currency.xml")).json()
        self.assertEqual(answer["payments"][2]["reason"], "AM03")

    # 4c. the held debtor account's currency
    def test_4c_a_payment_in_another_currency_than_the_account_is_am03(self):
        self.patch_account("ACME", currency="USD")
        answer = self.send(pain001("CC-1", ACME, [("X1", 100, INITECH)])).json()
        self.assertEqual(self.outcomes(answer), [("X1", "rejected", "AM03")])

    # 4d. a closed creditor account
    def test_4d_a_creditor_marked_closed_is_ac04_whatever_its_behaviour(self):
        self.patch_account("INITECH", behaviour="accept")      # still closed
        answer = self.send(pain001("CL-1", ACME, [("K1", 100, INITECH)])).json()
        self.assertEqual(self.outcomes(answer), [("K1", "rejected", "AC04")])

    def test_4d_a_creditor_with_closed_account_is_ac04_even_if_not_flagged(self):
        self.patch_account("INITECH", closed=False)            # still closed-account
        answer = self.send(pain001("CL-2", ACME, [("K2", 100, INITECH)])).json()
        self.assertEqual(self.outcomes(answer), [("K2", "rejected", "AC04")])

    # 4e. bad-bank-id on a held creditor, and nothing else
    def test_4e_bad_bank_id_is_rc01_and_another_bank_settles(self):
        answer = self.send(pain001("BB-1", ACME, [("B1", 100, EURODIS),
                                                  ("B2", 100, UMBRELLA)])).json()
        self.assertEqual(self.outcomes(answer), [("B1", "rejected", "RC01"),
                                                 ("B2", "accepted", None)])

    # 4f. insufficient funds
    def test_4f_insufficient_funds_skips_what_does_not_fit_and_takes_what_does(self):
        self.patch_account("GLOBEX", balance=10000)
        answer = self.send(pain001("IF-1", GLOBEX, [("F1", 6000, UMBRELLA),
                                                    ("F2", 6000, UMBRELLA),
                                                    ("F3", 3000, UMBRELLA)])).json()
        self.assertEqual(self.outcomes(answer), [("F1", "accepted", None),
                                                 ("F2", "rejected", "AM04"),
                                                 ("F3", "accepted", None)])
        self.assertEqual(self.balance("GLOBEX"), 1000)

    def test_4f_counts_what_is_accepted_and_not_yet_booked(self):
        self.patch_account("GLOBEX", balance=10000)
        later = TODAY + datetime.timedelta(days=5)
        first = self.send(pain001("IF-2", GLOBEX, [("L1", 8000, UMBRELLA)], when=later)).json()
        self.assertEqual(self.outcomes(first), [("L1", "accepted", None)])
        self.assertEqual(self.balance("GLOBEX"), 10000)      # not booked yet
        second = self.send(pain001("IF-3", GLOBEX, [("L2", 3000, UMBRELLA)])).json()
        self.assertEqual(self.outcomes(second), [("L2", "rejected", "AM04")])

    def test_4f_is_the_only_behaviour_that_looks_at_the_balance(self):
        self.patch_account("ACME", balance=100)
        answer = self.send(pain001("OD-1", ACME, [("O1", 500, UMBRELLA)])).json()
        self.assertEqual(self.outcomes(answer), [("O1", "accepted", None)])
        self.assertEqual(self.balance("ACME"), -400)

    # 5. silent
    def test_5_silent_is_decided_and_booked_but_not_reported(self):
        self.patch_account("ACME", behaviour="silent")
        before = self.balance("ACME")
        answer = self.send(pain001("SI-1", ACME, [("S1", 700, UMBRELLA)])).json()
        self.assertEqual((answer["status"], answer["reported"]), ("ACCP", False))
        self.assertEqual(before - self.balance("ACME"), 700)

    # 6. the rest accept here
    def test_6_the_other_debtor_behaviours_accept(self):
        for n, behaviour in enumerate(("accept", "statement-gap", "return-later",
                                       "duplicate-file")):
            with self.subTest(behaviour):
                self.patch_account("ACME", behaviour=behaviour)
                answer = self.send(pain001("OK-%d" % n, ACME, [("A%d" % n, 100, UMBRELLA)])).json()
                self.assertEqual((answer["status"], answer["reported"]), ("ACCP", True))


class Booking(PipelineCase):

    def test_a_later_settlement_date_is_accepted_and_waits(self):
        later = TODAY + datetime.timedelta(days=3)
        before = self.balance("ACME")
        answer = self.send(pain001("LT-1", ACME, [("W1", 500, UMBRELLA)], when=later)).json()
        self.assertEqual(answer["payments"][0]["settlement_date"], later.isoformat())
        self.assertIs(answer["payments"][0]["booked"], False)
        self.assertEqual(self.balance("ACME"), before)
        stored = self.get("/_mock/payments/W1").json()
        self.assertEqual((stored["status"], stored["booked_at"]), ("accepted", None))

    def test_a_past_date_settles_today(self):
        past = TODAY - datetime.timedelta(days=10)
        answer = self.send(pain001("PD-1", ACME, [("P1", 500, UMBRELLA)], when=past)).json()
        self.assertEqual(answer["payments"][0]["settlement_date"], TODAY.isoformat())
        self.assertEqual([f["code"] for f in answer["findings"]], ["DT01"])

    def test_payments_can_be_looked_up_and_state_counts_them(self):
        self.send(sample("pain001_four_payments.xml"))
        listed = self.get("/_mock/payments").json()
        self.assertEqual(len(listed), 4)
        one = self.get("/_mock/payments/INV-2026-0102").json()
        self.assertEqual((one["status"], one["reason"], one["msg_id"]),
                         ("rejected", "AC04", "ACME-20261001-0001"))
        self.assertEqual(one["amount"], 340050)
        missing = self.get("/_mock/payments/NOPE")
        self.assertEqual(missing.status, 404)
        self.assertIn("INV-2026-0101", missing.json()["known"])

    def test_reset_forgets_payments_and_files(self):
        self.send(sample("pain001_four_payments.xml"))
        self.post("/_mock/reset")
        self.assertEqual(self.get("/_mock/payments").json(), [])
        self.assertEqual(self.send(sample("pain001_four_payments.xml")).json()["status"], "PART")

    def test_payments_only_takes_post(self):
        self.assertEqual(self.get("/payments").status, 405)
