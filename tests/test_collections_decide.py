"""pain.008: what the bank decides about a file of collections (#131, step b1).

The account holder is the creditor here. Each test sends a file collecting for
ACME and reads the bank's answer three ways: the JSON it returns, the pain.002
it sends, and the collection it recorded. Nothing books in this step, which the
first class holds: a decided collection moves no balance yet.

The seed is what makes the held-debtor cases cheap. GLOBEX is
`insufficient-funds` with 12.50, INITECH is closed, and EURODIS is `bad-bank-id`.
"""
import datetime
from xml.etree import ElementTree as ET

from support import MockServerCase
from test_collections_read import pain008
from test_drop import DropCase
from test_payments import EURODIS, INITECH, UMBRELLA, pain001

from mockbank import accounts, direct_debit

ACME, GLOBEX = "NL41MOCK0000000001", "NL14MOCK0000000002"
NS = {"s": "urn:iso:std:iso:20022:tech:xsd:pain.002.001.10"}
FRIDAY_MORNING = "2026-01-02T09:00"


class CollectingCase(MockServerCase):
    config_kwargs = {"clock": FRIDAY_MORNING}

    def setUp(self):
        self.post("/_mock/reset")

    def collect(self, collections, **kw):
        return self.post("/payments", body=pain008(collections, **kw))

    def outcomes(self, answer):
        return [(c["end_to_end_id"], c["outcome"], c["reason"])
                for c in answer.json()["collections"]]

    def behave(self, account, **fields):
        resp = self.request("PATCH", "/_mock/accounts/" + account, body=fields)
        self.assertEqual(resp.status, 200, resp.body)

    def balance(self, account):
        return self.get("/_mock/accounts/" + account).json()["balance"]

    def status_reports(self):
        return [ET.fromstring(m["body"].encode("utf-8"))
                for m in self.get("/_mock/mailbox?leave&type=pain.002").json()]


class AFileOfCollectionsIsDecided(CollectingCase):

    def test_it_is_accepted_recorded_and_not_yet_booked(self):
        before = self.balance("ACME")
        answer = self.collect([("C1", 1000), ("C2", 2500)])
        self.assertEqual(answer.status, 202, answer.body)
        body = answer.json()
        self.assertEqual((body["format"], body["msg_id"], body["status"], body["accepted"],
                          body["rejected"], body["payments"]),
                         ("iso20022", "DD-1", "ACCP", 2, 0, []))
        self.assertEqual(body["collections"], [
            {"end_to_end_id": e2e, "pmt_inf_id": "DD-1-B1", "outcome": "accepted",
             "reason": None, "reason_text": None, "amount": minor, "currency": "EUR",
             "settlement_date": "2026-01-20", "booked": False}
            for e2e, minor in (("C1", 1000), ("C2", 2500))])
        kept = self.get("/_mock/collections/C1").json()
        self.assertEqual({k: kept[k] for k in (
            "msg_id", "account_id", "creditor_iban", "amount", "debtor_name", "debtor_iban",
            "mandate_id", "mandate_signed", "sequence_type", "status", "collection_date",
            "settlement_date", "booked_at")},
            {"msg_id": "DD-1", "account_id": "ACME", "creditor_iban": ACME, "amount": 1000,
             "debtor_name": "Customer C1", "debtor_iban": UMBRELLA, "mandate_id": "M-C1",
             "mandate_signed": "2025-06-01", "sequence_type": "RCUR", "status": "accepted",
             "collection_date": "2026-01-20", "settlement_date": "2026-01-20",
             "booked_at": None})
        # Decided, not booked: that is the next step's, and the README says so.
        self.assertEqual(self.balance("ACME"), before)
        self.assertEqual(self.get("/_mock/state").json()["collections"],
                         {"accepted": 2, "rejected": 0, "booked": 0})

    def test_the_pain002_reports_the_file_as_the_creditors_bank(self):
        self.collect([("C1", 1000), ("C2", 2500)], debtors={"C2": INITECH})
        [report] = self.status_reports()
        header = report.find("s:CstmrPmtStsRpt/s:GrpHdr", NS)
        # The bank is the agent of whoever sent the file.
        self.assertIsNotNone(header.find("s:CdtrAgt", NS))
        self.assertIsNone(header.find("s:DbtrAgt", NS))
        group = report.find("s:CstmrPmtStsRpt/s:OrgnlGrpInfAndSts", NS)
        self.assertEqual([group.findtext("s:" + tag, namespaces=NS) for tag in (
            "OrgnlMsgId", "OrgnlMsgNmId", "OrgnlNbOfTxs", "OrgnlCtrlSum", "GrpSts")],
            ["DD-1", "pain.008.001.08", "2", "35.00", "PART"])
        self.assertEqual(
            [(tx.findtext("s:OrgnlEndToEndId", namespaces=NS),
              tx.findtext("s:TxSts", namespaces=NS),
              tx.findtext("s:StsRsnInf/s:Rsn/s:Cd", namespaces=NS))
             for tx in report.iter("{%s}TxInfAndSts" % NS["s"])],
            [("C1", "ACCP", None), ("C2", "RJCT", "AC04")])

    def test_a_msgid_is_a_duplicate_whichever_kind_of_file_used_it(self):
        self.assertEqual(self.collect([("C1", 1000)]).status, 202)
        again = self.collect([("C9", 500)])
        self.assertEqual((again.status, again.json()["reason"]), (422, "DUPL"))
        # One `file` table: a pain.001 cannot reuse a pain.008's MsgId either.
        payment = self.post("/payments", body=pain001("DD-1", ACME, [("P1", 100, UMBRELLA)]))
        self.assertEqual((payment.status, payment.json()["reason"]), (422, "DUPL"))
        self.assertEqual([c["end_to_end_id"] for c in self.get("/_mock/collections").json()],
                         ["C1"])

    def test_the_listing_is_newest_first_and_a_miss_names_what_it_has(self):
        self.collect([("C1", 1000), ("C2", 2500)])
        self.assertEqual([c["end_to_end_id"] for c in self.get("/_mock/collections").json()],
                         ["C2", "C1"])
        missing = self.get("/_mock/collections/NOPE")
        self.assertEqual((missing.status, missing.json()["known"]), (404, ["C1", "C2"]))

    def test_a_reset_forgets_them(self):
        self.collect([("C1", 1000)])
        self.post("/_mock/reset")
        self.assertEqual(self.get("/_mock/collections").json(), [])
        self.assertEqual(self.collect([("C1", 1000)]).status, 202, "the MsgId is free again")


class WhenItSettles(CollectingCase):
    """On the requested date, rolled to a business day, and never before the
    business day after the bank can start on the file."""

    def settles(self, when):
        answer = self.collect([("C1", 1000)], when=when)
        return answer.json()["collections"][0]["settlement_date"]

    def test_on_the_requested_date(self):
        self.assertEqual(self.settles("2026-01-20"), "2026-01-20")

    def test_a_weekend_rolls_to_monday(self):
        self.assertEqual(self.settles("2026-01-24"), "2026-01-26")

    def test_never_before_the_business_day_after_receipt(self):
        # Received Friday morning and asked for that same Friday, and then for a
        # day already past: both are collected on Monday.
        self.assertEqual(self.settles("2026-01-02"), "2026-01-05")
        self.post("/_mock/reset")
        self.assertEqual(self.settles("2025-12-01"), "2026-01-05")


class AfterTheCutoff(CollectingCase):
    config_kwargs = {"clock": "2026-01-02T16:00"}

    def test_the_bank_starts_on_monday_so_it_collects_on_tuesday(self):
        answer = self.collect([("C1", 1000)], when="2026-01-02")
        self.assertEqual(answer.json()["collections"][0]["settlement_date"], "2026-01-06")


class TheCreditorAccount(CollectingCase):
    """The account the file collects for: held, open, and in the file's currency."""

    def test_one_the_bank_does_not_hold(self):
        answer = self.collect([("C1", 1000)], creditor=UMBRELLA)
        self.assertEqual((answer.json()["status"], self.outcomes(answer)),
                         ("RJCT", [("C1", "rejected", "AC03")]))

    def test_one_that_is_closed(self):
        answer = self.collect([("C1", 1000)], creditor=INITECH)
        self.assertEqual(self.outcomes(answer), [("C1", "rejected", "AC04")])
        self.assertIn("the creditor account", answer.json()["collections"][0]["reason_text"])

    def test_one_that_behaves_as_closed(self):
        self.behave("ACME", behaviour="closed-account")
        self.assertEqual(self.outcomes(self.collect([("C1", 1000)])),
                         [("C1", "rejected", "AC04")])

    def test_one_held_in_another_currency(self):
        # The file states no currency for the account, so no finding says it:
        # the account itself does.
        answer = self.collect([("C1", 1000)], ccy="USD", account_ccy=None)
        self.assertEqual(self.outcomes(answer), [("C1", "rejected", "AM03")])
        self.assertIn("is held in EUR", answer.json()["collections"][0]["reason_text"])

    def test_reject_file_rejects_the_file_it_sends(self):
        self.behave("ACME", behaviour="reject-file")
        answer = self.collect([("C1", 1000)])
        self.assertEqual((answer.status, answer.json()["reason"], answer.json()["collections"]),
                         (422, "FF01", []))

    def test_silent_decides_and_sends_no_status(self):
        self.behave("ACME", behaviour="silent")
        answer = self.collect([("C1", 1000)])
        self.assertEqual((answer.json()["reported"], self.outcomes(answer)),
                         (False, [("C1", "accepted", None)]))
        self.assertEqual(self.status_reports(), [])


class ADebtorTheBankHolds(CollectingCase):
    """Rick's decision on #131: its state and behaviour decide the collection,
    and nothing is booked on it."""

    def test_a_closed_one_is_ac04_and_the_rest_of_the_file_goes_on(self):
        answer = self.collect([("C1", 1000), ("C2", 2500)], debtors={"C1": INITECH})
        self.assertEqual((answer.json()["status"], self.outcomes(answer)),
                         ("PART", [("C1", "rejected", "AC04"), ("C2", "accepted", None)]))
        self.assertIn("the debtor account", answer.json()["collections"][0]["reason_text"])

    def test_one_that_behaves_as_closed_is_ac04(self):
        self.behave("EURODIS", behaviour="closed-account")
        self.assertEqual(self.outcomes(self.collect([("C1", 1000)], debtors={"C1": EURODIS})),
                         [("C1", "rejected", "AC04")])

    def test_insufficient_funds_is_held_to_the_debtors_balance(self):
        # GLOBEX has 12.50. 10.00 fits; 5.00 does not fit what is left; 2.00 does.
        answer = self.collect([("C1", 1000), ("C2", 500), ("C3", 200)],
                              debtors={"C1": GLOBEX, "C2": GLOBEX, "C3": GLOBEX})
        self.assertEqual(self.outcomes(answer), [
            ("C1", "accepted", None), ("C2", "rejected", "AM04"), ("C3", "accepted", None)])
        self.assertIn("has available, 2.50", answer.json()["collections"][1]["reason_text"])

    def test_the_debtors_balance_is_read_and_not_changed(self):
        self.collect([("C1", 1000)], debtors={"C1": GLOBEX})
        self.assertEqual(self.balance("GLOBEX"), 1250)
        # So the next file is held to the same 12.50, not to what the last left.
        again = self.collect([("C2", 1000)], msg_id="DD-2", debtors={"C2": GLOBEX})
        self.assertEqual(self.outcomes(again), [("C2", "accepted", None)])

    def test_what_the_debtor_has_itself_sent_and_not_yet_paid_is_not_available(self):
        # GLOBEX sends 10.00 of its 12.50 for a later date: accepted, not booked.
        sent = self.post("/payments", body=pain001(
            "GLOBEX-1", GLOBEX, [("P1", 1000, UMBRELLA)], when=datetime.date(2026, 2, 2)))
        self.assertEqual(sent.json()["accepted"], 1, sent.body)
        answer = self.collect([("C1", 500)], debtors={"C1": GLOBEX})
        self.assertEqual(self.outcomes(answer), [("C1", "rejected", "AM04")])

    def test_every_other_behaviour_lets_the_collection_through(self):
        # EURODIS is bad-bank-id in the seed, which is about paying into it.
        for behaviour in sorted(direct_debit.NOT_ABOUT_A_DEBTOR) + ["return-later"]:
            with self.subTest(behaviour=behaviour):
                self.post("/_mock/reset")
                self.behave("EURODIS", behaviour=behaviour)
                self.assertEqual(
                    self.outcomes(self.collect([("C1", 1000)], debtors={"C1": EURODIS})),
                    [("C1", "accepted", None)])

    def test_every_behaviour_is_on_one_list_or_the_other(self):
        acting, not_acting = (set(direct_debit.DEBTOR_SIDE_OF_A_COLLECTION),
                              set(direct_debit.NOT_ABOUT_A_DEBTOR))
        self.assertEqual(acting & not_acting, set())
        self.assertEqual(acting | not_acting, set(accounts.BEHAVIOURS),
                         "a behaviour is on neither list: say what it means for the "
                         "debtor of a collection")


class WhatTheFileItselfGetsWrong(CollectingCase):

    def test_a_collection_with_no_mandate_is_md02(self):
        answer = self.collect([("C1", 1000)], mandate=False)
        self.assertEqual(self.outcomes(answer), [("C1", "rejected", "MD02")])

    def test_a_wrong_control_sum_rejects_the_file(self):
        answer = self.collect([("C1", 1000)], total="10.01")
        self.assertEqual((answer.status, answer.json()["reason"]), (422, "AM10"))
        self.assertEqual(self.get("/_mock/collections").json(), [])


class ThroughTheFolder(DropCase):
    """The other door onto the same pipeline."""

    def test_a_dropped_pain008_is_decided_and_answered(self):
        self.drop_file("collect.xml", pain008([("C1", 1000)]).decode("utf-8"))
        self.assertEqual(self.scan()["scanned"], 1)
        [kept] = self.get("/_mock/collections").json()
        self.assertEqual((kept["end_to_end_id"], kept["status"]), ("C1", "accepted"))
        self.assertEqual([name.split("-")[0] for name in self.listing(self.pickup)],
                         ["pain.002.001.10"])
