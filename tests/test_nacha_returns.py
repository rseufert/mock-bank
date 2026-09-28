"""NACHA returns: what a NACHA account is sent for payments that come back (#54).

Option (a), as the maintainer chose: a payment the bank rejects stays rejected
- the acknowledgement already says so - and is also answered the way ACH
answers it, as a return entry the next business day: R01 for insufficient
funds, R02 for a closed account, R03 for one that cannot be found. A payment
that settles and comes back under `return-later` is a return entry too, where
an ISO 20022 account gets a pacs.004.

The test that holds the writer to NACHA is the done-when: every return file
the mock writes reads back through the step 1 reader with no finding, so its
counts, hash, totals and padding are computed, not copied.
"""
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from support import MockServerCase  # noqa: E402
from test_payments import BANK_START  # noqa: E402

from mockbank import nacha  # noqa: E402

TWIN = os.path.join(HERE, "samples", "nacha_four_payments_to_the_seed.ach")
FRIDAY = "2026-10-02"                  # the business day after BANK_START's Thursday


def twin():
    with open(TWIN, "rb") as handle:
        return handle.read()


class ReturnsCase(MockServerCase):
    config_kwargs = {"clock": BANK_START}

    def setUp(self):
        self.post("/_mock/reset")

    def nacha_acme(self, **more):
        resp = self.request("PATCH", "/_mock/accounts/ACME",
                            body=dict({"format": "nacha", "currency": "USD"}, **more))
        self.assertEqual(resp.status, 200, resp.body)

    def return_files(self):
        """Every return file waiting, each read back: (the file, its findings)."""
        files = []
        for message in self.get("/_mock/mailbox?type=" + nacha.RETURN).json():
            self.assertEqual(message["account"], "ACME")
            files.append(nacha.inspect(message["body"].encode("ascii"),
                                       datetime.date(2026, 10, 2)))
        return files

    def returned(self):
        files = self.return_files()
        for _, findings in files:
            self.assertEqual(findings, [])
        return {r.end_to_end_id: r for f, _ in files for r in f.returns}


class RejectedAndReturned(ReturnsCase):

    def test_the_rejections_come_back_next_business_day_as_r02_and_r03(self):
        self.nacha_acme()
        balance = self.get("/_mock/accounts/ACME").json()["balance"]
        self.post("/payments", body=twin())
        self.assertEqual(self.return_files(), [], "not before the next business day")
        self.post("/_mock/advance?to=" + FRIDAY)
        returned = self.returned()
        self.assertEqual(sorted(returned), ["INV-2026-0102", "INV-2026-0103"])
        closed, unknown = returned["INV-2026-0102"], returned["INV-2026-0103"]
        self.assertEqual((closed.reason, unknown.reason), ("R02", "R03"))
        # What a client matches a return on: the original trace number, the
        # original receiving bank, and the account and amount it was sent to.
        self.assertEqual((closed.original_trace, closed.original_receiving_dfi,
                          closed.account, closed.amount, closed.transaction_code),
                         ("999999990000002", "99999999", "0000000003", 340050, "21"))
        # Rejected, so never debited, so nothing is credited back: the only
        # money that moved is the two accepted payments leaving.
        self.assertEqual(self.get("/_mock/accounts/ACME").json()["balance"],
                         balance - 125000 - 1500000)

    def test_insufficient_funds_is_r01(self):
        self.nacha_acme(behaviour="insufficient-funds", balance=100000)
        self.post("/payments", body=twin())
        self.post("/_mock/advance?to=" + FRIDAY)
        codes = {e2e: r.reason for e2e, r in self.returned().items()}
        # 1250.00 does not fit 1000.00; INITECH and EURODIS are rejected for
        # their own accounts first; 15000.00 does not fit either.
        self.assertEqual(codes, {"INV-2026-0101": "R01", "INV-2026-0102": "R02",
                                 "INV-2026-0103": "R03", "INV-2026-0104": "R01"})

    def test_an_iso20022_account_gets_no_return_file(self):
        self.post("/payments", body=open(os.path.join(HERE, "samples",
                                                      "pain001_four_payments.xml"), "rb").read())
        self.post("/_mock/advance?to=" + FRIDAY)
        self.assertEqual(self.get("/_mock/mailbox?type=" + nacha.RETURN).json(), [])


class ReturnLater(ReturnsCase):

    def test_a_settled_payment_comes_back_as_a_return_entry_not_a_pacs004(self):
        self.nacha_acme(behaviour="return-later",
                        parameters={"days": 1, "end_to_end_id": "INV-2026-0101"})
        self.post("/payments", body=twin())
        self.post("/_mock/advance?to=" + FRIDAY)
        returned = self.returned()
        # R02 is a NACHA account's default reason, as AC04 is an ISO one's.
        self.assertEqual(returned["INV-2026-0101"].reason, "R02")
        messages = self.get("/_mock/mailbox").json()
        self.assertNotIn("pacs.004.001.09", [m["type"] for m in messages])
        # The notification is still ISO 20022, so it says R02 as ISO does.
        credits = [m["body"] for m in messages if m["type"].startswith("camt.054")
                   and "<RtrInf>" in m["body"]]
        self.assertEqual(len(credits), 1, [m["type"] for m in messages])
        self.assertIn("<Cd>AC04</Cd>", credits[0].split("<RtrInf>")[1])
        # The money did come back, and the statement shows it.
        self.assertEqual(self.get("/_mock/payments/INV-2026-0101").json()["status"],
                         "returned")

    def test_a_return_reason_is_one_for_the_accounts_format(self):
        self.nacha_acme()
        for fields in ({"behaviour": "return-later", "parameters": {"reason": "AC04"}},):
            resp = self.request("PATCH", "/_mock/accounts/ACME", body=fields)
            self.assertEqual(resp.status, 400)
            self.assertIn("R01", resp.json()["error"])
        ok = self.request("PATCH", "/_mock/accounts/ACME", body={
            "behaviour": "return-later", "parameters": {"reason": "R03"}})
        self.assertEqual(ok.status, 200, ok.body)
        iso = self.request("PATCH", "/_mock/accounts/GLOBEX", body={
            "behaviour": "return-later", "parameters": {"reason": "R03"}})
        self.assertEqual(iso.status, 400)

    def test_switching_to_nacha_with_an_iso_reason_set_is_refused(self):
        self.request("PATCH", "/_mock/accounts/ACME", body={
            "behaviour": "return-later", "parameters": {"reason": "AC04"}})
        resp = self.request("PATCH", "/_mock/accounts/ACME", body={"format": "nacha"})
        self.assertEqual(resp.status, 400)


class AReturnFileIsNotAPaymentFile(ReturnsCase):

    def test_it_is_refused_at_the_payments_door_and_named_at_validate(self):
        self.nacha_acme()
        self.post("/payments", body=twin())
        self.post("/_mock/advance?to=" + FRIDAY)
        body = self.get("/_mock/mailbox?raw&type=" + nacha.RETURN).body
        refused = self.post("/payments", body=body)
        self.assertEqual(refused.status, 422)
        self.assertEqual(refused.json()["reason"], "FF01")
        self.assertIn("return file", refused.json()["reason_text"])
        said = self.post("/_mock/validate", body=body).body.decode()
        self.assertIn("the bank sends return files", said)
