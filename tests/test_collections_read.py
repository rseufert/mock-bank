"""pain.008: a direct debit initiation, read and validated (#131, step a).

The account holder asks the bank to collect from debtors under their mandates.
This step reads the file and says what is wrong with it; what the bank then
decides is `tests/test_collections_decide.py`'s. Two files from
outside the project are the ground truth for the reading, with every value
below read off them by eye rather than out of the reader.
"""
import datetime
import os
from decimal import Decimal

from support import MockServerCase

from mockbank import messages, schema

EXTERNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "external")
PAIN008 = schema.MESSAGES["pain.008.001.08"]


def external(name):
    with open(os.path.join(EXTERNAL, name), "rb") as handle:
        return handle.read()


def pain008(collections, msg_id="DD-1", when="2026-01-20", ccy="EUR", nb=None, total=None,
            creditor="NL41MOCK0000000001", account_ccy="EUR", debtors=None, mandate=True):
    """A pain.008 from ACME collecting [(EndToEndId, minor units)] in one batch.

    `debtors` names a debtor account by EndToEndId, for the ones not at another
    bank; `account_ccy` of None leaves the creditor account's currency unstated;
    and `mandate` of False leaves the mandate out.
    """
    amount = sum(minor for _, minor in collections)
    account = {"Id": {"IBAN": creditor}}
    if account_ccy:
        account["Ccy"] = account_ccy
    mandated = ({"MndtRltdInf": {"MndtId": "M-%s", "DtOfSgntr": "2025-06-01"}}
                if mandate else {})
    return schema.serialize(PAIN008, {"CstmrDrctDbtInitn": {
        "GrpHdr": {"MsgId": msg_id, "CreDtTm": datetime.datetime(2026, 1, 2, 9, 0, tzinfo=datetime.timezone.utc),
                   "NbOfTxs": nb if nb is not None else len(collections),
                   "CtrlSum": total if total is not None else str(Decimal(amount).scaleb(-2)),
                   "InitgPty": {"Nm": "ACME"}},
        "PmtInf": [{"PmtInfId": msg_id + "-B1", "PmtMtd": "DD",
                    "PmtTpInf": {"SeqTp": "RCUR"}, "ReqdColltnDt": when,
                    "Cdtr": {"Nm": "ACME"},
                    "CdtrAcct": account,
                    "CdtrAgt": {"FinInstnId": {"BICFI": "MOCKNL2A"}},
                    "DrctDbtTxInf": [
                        {"PmtId": {"EndToEndId": e2e},
                         "InstdAmt": schema.Amount(minor, ccy),
                         "DrctDbtTx": {key: dict(value, MndtId=value["MndtId"] % e2e)
                                       for key, value in mandated.items()},
                         "DbtrAgt": {"FinInstnId": {"BICFI": "MOCKNL2A"}},
                         "Dbtr": {"Nm": "Customer " + e2e},
                         "DbtrAcct": {"Id": {"IBAN": (debtors or {}).get(
                             e2e, "NL30MOCK0000000005")}}}
                        for e2e, minor in collections]}]}})


class TwoFilesFromOutside(MockServerCase):
    # Before either file's collection date, so neither is in the past.
    config_kwargs = {"clock": "2026-01-02T09:00"}

    def reading(self, body):
        resp = self.post("/_mock/validate", body=body, headers={"Accept": "application/json"})
        self.assertEqual(resp.status, 200, resp.body)
        return resp.json()

    def test_a_belgian_sepa_core_collection_reads_with_no_finding(self):
        answer = self.reading(external("pain.008.001.08-be-sdd-core.xml"))
        self.assertEqual(answer["findings"], [])
        reading = answer["file"]
        self.assertEqual((reading["message"], reading["msg_id"]),
                         ("pain.008.001.08", "TLN-DD-20260925"))
        [batch] = reading["batches"]
        self.assertEqual((batch["requested_collection_date"], batch["creditor_account"]),
                         ("2026-09-25", "BE66793820621243"))
        [collection] = batch["collections"]
        self.assertEqual(collection, {
            "end_to_end_id": "TLN-2026-09-000001", "instruction_id": None,
            "amount": 5499, "currency": "EUR", "sequence_type": "RCUR",
            "mandate_id": "TLN-MNDT-000456", "mandate_signed": "2024-11-20",
            # On the batch in this file, and carried down to the collection.
            "creditor_scheme_id": "BE68ZZZ0123456789",
            "debtor_name": "An Peeters", "debtor_account": "BE29795361022164",
            "debtor_bic": "VVLSBEF0", "remittance": []})

    def test_two_batches_a_first_and_a_recurring_collection(self):
        answer = self.reading(external("pain.008.001.08-direct-debit.xml"))
        self.assertEqual(answer["findings"], [])
        batches = answer["file"]["batches"]
        self.assertEqual([(b["requested_collection_date"],
                           [(c["end_to_end_id"], c["amount"], c["sequence_type"],
                             c["mandate_id"], c["creditor_scheme_id"])
                            for c in b["collections"]]) for b in batches],
                         [("2026-01-20", [("E2E-DD-0001", 9999, "RCUR", "MANDATE-001",
                                           "DE98ZZZ09999999999")]),
                          ("2026-01-22", [("E2E-DD-0002", 4999, "FRST", "MANDATE-002",
                                           "DE98ZZZ09999999999")])])

    def test_the_plain_summary_counts_collections(self):
        resp = self.post("/_mock/validate", body=external("pain.008.001.08-direct-debit.xml"))
        self.assertEqual(resp.body.decode("utf-8").splitlines(), [
            "pain.008.001.08 DD-20260116-0001: 2 batches, 2 collections, 0 findings"])


class WhatIsWrongWithOne(MockServerCase):
    """The checks a pain.001 gets, turned round to the creditor's side."""
    config_kwargs = {"clock": "2026-01-02T09:00"}

    def findings(self, body):
        resp = self.post("/_mock/validate", body=body, headers={"Accept": "application/json"})
        return [(f["level"], f["code"]) for f in resp.json()["findings"]]

    def test_a_clean_one_has_none(self):
        self.assertEqual(self.findings(pain008([("C1", 1000), ("C2", 2500)])), [])

    def test_the_count_and_the_sum_are_checked(self):
        self.assertIn(("error", "AM18"), self.findings(pain008([("C1", 1000)], nb=2)))
        self.assertIn(("error", "AM10"), self.findings(pain008([("C1", 1000)], total="10.01")))

    def test_a_repeated_end_to_end_id(self):
        self.assertIn(("error", "AM05"), self.findings(pain008([("C1", 1000), ("C1", 2000)])))

    def test_a_collection_date_already_past(self):
        self.assertIn(("warning", "DT01"), self.findings(pain008([("C1", 1000)],
                                                                 when="2026-01-01")))

    def test_an_amount_in_another_currency_than_the_creditor_account(self):
        self.assertIn(("error", "AM03"), self.findings(pain008([("C1", 1000)], ccy="USD")))


class ACollectionIsNotAPayment(MockServerCase):
    config_kwargs = {"clock": "2026-01-02T09:00"}

    def test_a_collection_with_no_mandate_is_a_finding(self):
        # The XSD leaves the mandate optional; a debtor's bank does not.
        resp = self.post("/_mock/validate", body=pain008([("C1", 1000)], mandate=False),
                         headers={"Accept": "application/json"})
        [finding] = resp.json()["findings"]
        self.assertEqual((finding["level"], finding["code"]), ("error", "MD02"))
        self.assertIn("no MndtId and no DtOfSgntr", finding["text"])

    def test_the_reader_tells_a_collection_from_a_payment(self):
        # A pain.001 reads as payments and a pain.008 as collections; the
        # dispatch is by the message, not by what the body happens to hold.
        self.assertIsInstance(messages.read_pain001(pain008([("C1", 1000)])),
                              messages.CollectionFile)
