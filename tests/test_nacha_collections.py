"""NACHA: a file of debit entries is a file of collections (#176, PR 1).

The company that sends the file is the creditor and each receiver a debtor, read
into the model a `pain.008` reads into and decided, recorded and booked by the
same code. What NACHA does not have, it does not get: there is no mandate in the
file, so none is asked for. The files here are built from `nacha.RECORDS` with
every count, hash and total computed, and one is from outside the project.
"""
import datetime
import os

from test_nacha_door import ACK, DoorCase, sample
from test_collections_read import pain008

from mockbank import bai2, nacha

ACME_NUMBER, GLOBEX_NUMBER, INITECH_NUMBER = "0000000001", "0000000002", "0000000003"
OURS, THEIRS = "999999992", "231380104"
FRIDAY, MONDAY = datetime.date(2026, 10, 2), datetime.date(2026, 10, 5)


def debit_file(entries, company=ACME_NUMBER, sec="PPD", effective=FRIDAY, service=225,
               modifier="A"):
    """A NACHA file of one batch. Each entry is a dict: `id`, `cents`, and
    optionally `code` (27), `account`, `routing`, `name` and `type`, the two
    characters of discretionary data."""
    lines, entry_hash, totals = [], 0, {"credit": 0, "debit": 0}
    for number, entry in enumerate(entries, start=1):
        code, routing = entry.get("code", "27"), entry.get("routing", THEIRS)
        entry_hash += int(routing[:8])
        totals[nacha.side(code)] += entry["cents"]
        lines.append(nacha.line(nacha.RECORDS["6"], **{
            "record type code": 6, "transaction code": code,
            "receiving DFI identification": routing[:8], "check digit": routing[8],
            "DFI account number": entry.get("account", "12345678"), "amount": entry["cents"],
            "individual identification number": entry["id"],
            "individual name": entry.get("name", "Receiver " + entry["id"]),
            "discretionary data": entry.get("type", ""), "addenda record indicator": 0,
            "trace number": "12104288%07d" % number}))
    control = {"entry/addenda count": len(lines), "entry hash": entry_hash % 10 ** 10,
               "total debit entry dollar amount": totals["debit"],
               "total credit entry dollar amount": totals["credit"]}
    records = [nacha.line(nacha.RECORDS["1"], **{
        "record type code": 1, "priority code": 1, "immediate destination": " " + OURS,
        "immediate origin": company.rjust(10), "file creation date": "261001",
        "file creation time": "0800", "file ID modifier": modifier, "record size": 94,
        "blocking factor": 10, "format code": 1, "immediate destination name": "MOCK BANK",
        "immediate origin name": "ACME"}),
        nacha.line(nacha.RECORDS["5"], **{
            "record type code": 5, "service class code": service, "company name": "ACME",
            "company identification": company, "standard entry class code": sec,
            "company entry description": "DUES", "effective entry date":
            effective.strftime("%y%m%d"), "originator status code": 1,
            "originating DFI identification": "12104288", "batch number": 1})]
    records += lines
    records.append(nacha.line(nacha.RECORDS["8"], **dict(control, **{
        "record type code": 8, "service class code": service,
        "company identification": company,
        "originating DFI identification": "12104288", "batch number": 1})))
    blocks = -(-(len(records) + 1) // 10)
    records.append(nacha.line(nacha.RECORDS["9"], **dict(control, **{
        "record type code": 9, "batch count": 1, "block count": blocks})))
    records += [nacha.PADDING] * (blocks * 10 - len(records))
    return ("\n".join(records) + "\n").encode("ascii")


class CollectingCase(DoorCase):

    def collect(self, entries, **kw):
        return self.post("/payments", body=debit_file(entries, **kw))

    def outcomes(self, answer):
        return [(c["end_to_end_id"], c["outcome"], c["reason"])
                for c in answer.json()["collections"]]

    def collection(self, end_to_end_id):
        return self.get("/_mock/collections/" + end_to_end_id).json()

    def balance(self, account):
        return self.get("/_mock/accounts/" + account).json()["balance"]


class ReadingAFileOfDebits(CollectingCase):

    def reading(self, body):
        resp = self.post("/_mock/validate", body=body, headers={"Accept": "application/json"})
        return resp.status, resp.json()

    def test_a_file_built_here_reads_with_no_finding(self):
        status, reading = self.reading(debit_file([{"id": "INV-1", "cents": 1250},
                                                   {"id": "INV-2", "cents": 300, "code": "37"}]))
        self.assertEqual((status, reading["findings"]), (200, []))

    def test_a_general_ledger_debit_from_outside_is_one_collection(self):
        # moov-io/ach's `gl-debit.ach`: service class 225, one 47. Until #176
        # its one finding was the debit.
        status, reading = self.reading(sample(os.path.join("external", "nacha-gl-debit.ach")))
        # Its one finding is a warning: the file is from 2019.
        self.assertEqual((status, [(f["level"], f["code"]) for f in reading["findings"]]),
                         (200, [("warning", "DT01")]))
        [batch] = reading["file"]["batches"]
        self.assertEqual((reading["file"]["message"], batch["creditor_account"],
                          batch["requested_collection_date"]),
                         ("NACHA", "121042882", "2019-06-25"))
        self.assertEqual(batch["collections"], [{
            "end_to_end_id": "NOTPROVIDED", "instruction_id": "121042880000001",
            "amount": 100000000, "currency": "USD", "sequence_type": None,
            # NACHA carries no mandate: the authorization is the originator's to hold.
            "mandate_id": None, "mandate_signed": None, "creditor_scheme_id": None,
            "debtor_name": "Receiver Account Name", "debtor_account": "12345678",
            "debtor_bic": None, "remittance": []}])

    def test_the_prose_summary_counts_collections(self):
        resp = self.post("/_mock/validate", body=debit_file([{"id": "INV-1", "cents": 1250}]))
        self.assertIn("1 collection", resp.body.decode("utf-8"))

    def test_the_entrys_code_decides_not_the_batchs_service_class(self):
        # A batch marked 200, mixed, whose entries are all debits.
        status, reading = self.reading(debit_file([{"id": "INV-1", "cents": 1250}], service=200))
        self.assertEqual((status, reading["findings"]), (200, []))
        self.assertEqual(len(reading["file"]["batches"][0]["collections"]), 1)

    def test_credits_and_debits_in_one_file_are_refused_and_it_says_why(self):
        status, reading = self.reading(debit_file(
            [{"id": "INV-1", "cents": 1250}, {"id": "PAY-1", "cents": 500, "code": "22"}],
            service=200))
        self.assertEqual(status, 422)
        [finding] = reading["findings"]
        self.assertEqual((finding["code"], finding["path"]),
                         ("FF01", "/line 3 (entry detail)/transaction code (columns 2-3)"))
        self.assertIn("1 credit entry and 1 debit entry", finding["text"])
        self.assertIn("a file of payments or a file of collections, not both", finding["text"])

    def test_what_is_not_read_is_named_for_what_it_is(self):
        for code, says in (("28", "a prenote of a debit"), ("23", "a prenote of a credit"),
                           ("29", "a zero-dollar entry"), ("55", "a loan debit"),
                           ("20", "not one NACHA defines")):
            with self.subTest(code=code):
                status, reading = self.reading(debit_file(
                    [{"id": "X-1", "cents": 0 if code != "55" else 100, "code": code}]))
                self.assertEqual(status, 422)
                [finding] = reading["findings"]
                self.assertIn("transaction code %s is %s" % (code, says), finding["text"])
        status, reading = self.reading(debit_file([{"id": "X-1", "cents": 0, "code": "28"}]))
        self.assertIn("no money would move", reading["findings"][0]["text"])

    def test_a_web_or_tel_debit_says_whether_it_recurs(self):
        for sec, kind, expected in (("WEB", "R", "RCUR"), ("WEB", "S", "OOFF"),
                                    ("TEL", "R ", "RCUR"), ("PPD", "R", None)):
            with self.subTest(sec=sec, kind=kind):
                _, reading = self.reading(debit_file(
                    [{"id": "INV-1", "cents": 1250, "type": kind}], sec=sec))
                [collection] = reading["file"]["batches"][0]["collections"]
                self.assertEqual(collection["sequence_type"], expected)

    def test_a_repeated_identification_number_is_am05(self):
        status, reading = self.reading(debit_file([{"id": "INV-1", "cents": 1250},
                                                   {"id": "INV-1", "cents": 300}]))
        self.assertEqual((status, [f["code"] for f in reading["findings"]]), (422, ["AM05"]))


class DecidingIt(CollectingCase):

    def test_a_nacha_account_collects_and_is_acknowledged(self):
        self.nacha_acme()
        answer = self.collect([{"id": "INV-1", "cents": 1250},
                               {"id": "INV-2", "cents": 300, "code": "37"}], sec="CCD")
        self.assertEqual(answer.status, 202, answer.body)
        body = answer.json()
        self.assertEqual((body["format"], body["status"], body["payments"]),
                         ("nacha", "ACCP", []))
        self.assertEqual(self.outcomes(answer), [("INV-1", "accepted", None),
                                                 ("INV-2", "accepted", None)])
        # No mandate, and no MD02 for the want of one.
        kept = self.collection("INV-2")
        self.assertEqual(
            (kept["account_id"], kept["debtor_iban"], kept["debtor_name"], kept["mandate_id"],
             kept["mandate_signed"], kept["entry_class"], kept["transaction_code"],
             kept["debtor_clearing_id"], kept["instruction_id"], kept["settlement_date"]),
            ("ACME", "12345678", "Receiver INV-2", None, None, "CCD", "37", THEIRS,
             "121042880000002", FRIDAY.isoformat()))
        [ack] = self.get("/_mock/mailbox?type=" + ACK).json()
        self.assertEqual(ack["body"].splitlines()[2:], [
            "FILE 0000000001-2610010800A", "STATUS ACCP",
            "ENTRY 121042880000001 INV-1 12.50 ACCEPTED 2026-10-02",
            "ENTRY 121042880000002 INV-2 3.00 ACCEPTED 2026-10-02"])

    def test_it_never_settles_before_the_business_day_after_receipt(self):
        self.nacha_acme()
        answer = self.collect([{"id": "INV-1", "cents": 1250}],
                              effective=datetime.date(2026, 10, 1))
        self.assertEqual(answer.json()["collections"][0]["settlement_date"],
                         FRIDAY.isoformat())

    def test_an_account_not_in_dollars_rejects_each_collection_am03(self):
        answer = self.collect([{"id": "INV-1", "cents": 1250}])     # ACME is in EUR
        self.assertEqual(self.outcomes(answer), [("INV-1", "rejected", "AM03")])
        # ...and an ISO 20022 account is answered with a pain.002, as its agent.
        [report] = self.get("/_mock/mailbox?type=pain.002").json()
        self.assertIn("<CdtrAgt>", report["body"])
        self.assertIn("<OrgnlMsgNmId>NACHA</OrgnlMsgNmId>", report["body"])

    def test_a_company_the_bank_does_not_hold_is_ac03(self):
        answer = self.post("/payments", body=sample(os.path.join("external",
                                                                 "nacha-gl-debit.ach")))
        self.assertEqual(self.outcomes(answer), [("NOTPROVIDED", "rejected", "AC03")])

    def test_a_debtor_this_bank_holds_decides_by_its_own_state(self):
        self.nacha_acme()
        answer = self.collect([
            {"id": "FITS", "cents": 1250, "routing": OURS, "account": GLOBEX_NUMBER},
            {"id": "OVER", "cents": 1, "routing": OURS, "account": GLOBEX_NUMBER},
            {"id": "SHUT", "cents": 100, "routing": OURS, "account": INITECH_NUMBER},
            # The same number at another bank is nobody this bank knows.
            {"id": "AWAY", "cents": 100, "routing": THEIRS, "account": INITECH_NUMBER}])
        self.assertEqual(self.outcomes(answer), [
            ("FITS", "accepted", None), ("OVER", "rejected", "AM04"),
            ("SHUT", "rejected", "AC04"), ("AWAY", "accepted", None)])
        self.assertEqual(self.collection("FITS")["debtor_iban"], "NL14MOCK0000000002")
        self.assertEqual(self.balance("GLOBEX"), 1250, "read, never changed")

    def test_the_same_file_twice_is_dupl(self):
        self.nacha_acme()
        self.collect([{"id": "INV-1", "cents": 1250}])
        again = self.collect([{"id": "INV-1", "cents": 1250}])
        self.assertEqual((again.status, again.json()["reason"]), (422, "DUPL"))

    def test_a_nacha_account_that_sends_a_pain008_is_acknowledged_too(self):
        # Noted on #154 and held by no test until now: the account's format
        # decides the answer, whichever format the file came in.
        self.nacha_acme()
        answer = self.post("/payments", body=pain008(
            [("C1", 1000)], when="2026-10-02", ccy="USD", account_ccy="USD"))
        self.assertEqual(answer.json()["status"], "ACCP", answer.body)
        [ack] = self.get("/_mock/mailbox?type=" + ACK).json()
        self.assertIn("ENTRY NOTPROVIDED C1 10.00 ACCEPTED 2026-10-02", ack["body"])
        self.assertEqual(self.get("/_mock/mailbox?type=pain.002").json(), [])


class BookingIt(CollectingCase):

    def test_it_credits_the_account_and_the_bai2_statement_says_165(self):
        self.nacha_acme()
        before = self.balance("ACME")
        self.collect([{"id": "INV-1", "cents": 1250}, {"id": "INV-2", "cents": 300}])
        self.assertEqual(self.balance("ACME"), before, "not before its day")
        self.post("/_mock/advance?to=" + MONDAY.isoformat())
        self.assertEqual(self.balance("ACME"), before + 1550)
        friday = [m["body"] for m in self.get("/_mock/mailbox?type=bai2").json()][-1]
        [statement] = bai2.statements(friday)
        self.assertEqual([tuple(e) for e in statement.entries],
                         [("165", 1250, "INV-1", "Receiver INV-1"),
                          ("165", 300, "INV-2", "Receiver INV-2")])
        self.assertEqual((statement.opening, statement.closing), (before, before + 1550))
        self.assertEqual(bai2.trailers_agree(friday), [])

    def test_an_iso20022_account_in_dollars_is_notified_in_camt054(self):
        # The file's format does not decide the answer's: the account's does.
        self.request("PATCH", "/_mock/accounts/ACME", body={"currency": "USD"})
        self.collect([{"id": "INV-1", "cents": 1250}])
        self.post("/_mock/advance?to=" + FRIDAY.isoformat())
        [note] = self.get("/_mock/mailbox?type=camt.054").json()
        for wanted in ("<CdtDbtInd>CRDT</CdtDbtInd>", "<SubFmlyCd>ESDD</SubFmlyCd>",
                       "<EndToEndId>INV-1</EndToEndId>", "<Othr><Id>12345678</Id></Othr>",
                       "<InstrId>121042880000001</InstrId>"):
            self.assertIn(wanted, note["body"])
        self.assertNotIn("<MndtId>", note["body"])

    def test_it_waits_in_the_queue_under_the_key_it_arrives_with(self):
        self.request("PATCH", "/_mock/accounts/ACME", body={"currency": "USD"})
        self.collect([{"id": "INV-1", "cents": 1250}])
        [waiting] = self.get("/_mock/queue").json()
        self.assertEqual(waiting["key"], "camt.054.001.08/ACME/2026-10-02/collections-settling")
