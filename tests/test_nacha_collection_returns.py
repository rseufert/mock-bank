"""NACHA: a collection comes back as a return file (#176, PR 2).

A NACHA account is answered in NACHA's terms. A collection the receiver's bank
sends back is a return entry - `26`, `36` or `46`, the returned debit - with an
addenda `99` carrying an `R` code, in a file the mock's own reader takes with no
finding. One refused before it settled comes back the same way and moves no
money, because NACHA has no message that rejects one entry of an accepted file.
"""
import datetime
from xml.etree import ElementTree as ET

from test_nacha_collections import (FRIDAY, GLOBEX_NUMBER, MONDAY, OURS, THEIRS,
                                    CollectingCase, debit_file)
from test_nacha_door import ACK

from mockbank import bai2, nacha, schema

TUESDAY = datetime.date(2026, 10, 6)
RETURN = "nacha.return"


class ReturningCase(CollectingCase):

    def setUp(self):
        super().setUp()
        self.nacha_acme()

    def advance(self, day):
        self.assertEqual(self.post("/_mock/advance?to=" + day.isoformat()).status, 200)

    def refuse(self, end_to_end_id, reason, status=200):
        resp = self.post("/_mock/collections/%s/refuse" % end_to_end_id, body={"reason": reason})
        self.assertEqual(resp.status, status, resp.body)
        return resp.json()

    def return_files(self):
        """Each return file waiting, read by the mock's own reader: its key and
        [(transaction code, EndToEndId, cents, account, reason, original trace,
        original receiving DFI)], with the batch's SEC code and service class."""
        out = []
        for message in self.get("/_mock/mailbox?type=" + RETURN).json():
            read, findings = nacha.inspect(message["body"].encode("ascii"))
            self.assertEqual(findings, [], message["body"])
            lines = message["body"].splitlines()
            out.append((message["key"], lines[1][50:53], lines[1][1:4], [
                (r.transaction_code, r.end_to_end_id, r.amount, r.account, r.reason,
                 r.original_trace, r.original_receiving_dfi) for r in read.returns],
                lines[-1 - sum(1 for line in lines if line == nacha.PADDING)]))
        return out


class AfterItSettled(ReturningCase):

    def test_it_comes_back_as_a_returned_debit_with_its_r_code(self):
        before = self.balance("ACME")
        self.collect([{"id": "INV-1", "cents": 1250}, {"id": "INV-2", "cents": 300}], sec="CCD")
        self.advance(MONDAY)                          # settled on Friday
        self.get("/_mock/mailbox")
        file_id = self.collection("INV-1")["file_id"]
        returned = self.refuse("INV-1", "R10")
        self.assertEqual((returned["status"], returned["return_reason"], returned["return_due"]),
                         ("returned", "R10", MONDAY.isoformat()))
        self.assertEqual(self.balance("ACME"), before + 300)
        [(key, sec, service, entries, control)] = self.return_files()
        self.assertEqual(key, "nacha.return/ACME/%s/collections-returned/file-%d"
                         % (MONDAY.isoformat(), file_id))
        self.assertEqual((sec, service), ("CCD", "225"))
        self.assertEqual(entries, [("26", "INV-1", 1250, "12345678", "R10",
                                    "121042880000001", THEIRS[:8])])
        # A returned debit counts in the debit total, as moov's own file counts it.
        self.assertEqual((int(control[31:43]), int(control[43:55])), (1250, 0))
        self.assertEqual(self.get("/_mock/mailbox?type=pacs.004").json(), [])

    def test_the_notification_and_the_statement_say_it_in_their_own_terms(self):
        before = self.balance("ACME")
        self.collect([{"id": "INV-1", "cents": 1250}])
        self.advance(MONDAY)
        # A NACHA account's notifications stay camt.054, for a collection that
        # settles as for a payment that books.
        [settled] = self.get("/_mock/mailbox?type=camt.054").json()
        self.assertIn("<SubFmlyCd>ESDD</SubFmlyCd>", settled["body"])
        self.refuse("INV-1", "R10")
        self.advance(TUESDAY)
        [note] = [m for m in self.get("/_mock/mailbox?leave&type=camt.054").json()
                  if "<CdtDbtInd>DBIT</CdtDbtInd>" in m["body"]]
        # The camt.054 is ISO 20022, so the R code is said as its ISO reason.
        self.assertIn("<Rsn><Cd>MD01</Cd></Rsn>", note["body"])
        self.assertIn("<SubFmlyCd>UPDD</SubFmlyCd>", note["body"])
        root = ET.fromstring(note["body"].encode("utf-8"))
        self.assertEqual(schema.check(schema.identify(root), root), [])
        monday = [m["body"] for m in self.get("/_mock/mailbox?type=bai2").json()][-1]
        [statement] = bai2.statements(monday)
        self.assertEqual([tuple(e) for e in statement.entries],
                         [("557", 1250, "INV-1", "Receiver INV-1")])
        self.assertEqual((statement.opening, statement.closing), (before + 1250, before))

    def test_a_savings_and_a_ledger_debit_come_back_as_36_and_46(self):
        self.collect([{"id": "S-1", "cents": 100, "code": "37"},
                      {"id": "G-1", "cents": 200, "code": "47"}], sec="WEB")
        self.advance(MONDAY)
        self.refuse("S-1", "R01")
        self.refuse("G-1", "R08")
        # Each refusal is answered as it arrives, so two files, and the second
        # for the same day and original file has a key of its own (#155).
        first, second = self.return_files()
        self.assertEqual((first[1], second[1]), ("WEB", "WEB"))
        self.assertEqual([(e[0], e[1], e[4]) for e in first[3] + second[3]],
                         [("36", "S-1", "R01"), ("46", "G-1", "R08")])
        self.assertEqual(second[0], first[0] + "#2")

    def test_the_reasons_are_r_codes_and_the_error_lists_them(self):
        self.collect([{"id": "INV-1", "cents": 1250}])
        error = self.refuse("INV-1", "MD01", status=400)["error"]
        self.assertIn('{"reason": "R10"}', error)
        self.assertIn("R01, R02, R03, R05, R07, R08, R10, R29", error)
        self.assertEqual(sorted(nacha.DEBIT_RETURN_REASONS),
                         ["R01", "R02", "R03", "R05", "R07", "R08", "R10", "R29"])


class BeforeItSettles(ReturningCase):

    def test_it_is_rejected_and_comes_back_on_the_day_it_would_have_settled(self):
        before = self.balance("ACME")
        self.collect([{"id": "INV-1", "cents": 1250}, {"id": "INV-2", "cents": 300}],
                     effective=MONDAY)
        self.get("/_mock/mailbox")
        refused = self.refuse("INV-1", "R07")
        self.assertEqual((refused["status"], refused["reason"], refused["settlement_date"],
                          refused["return_due"], refused["return_reason"], refused["booked_at"]),
                         ("rejected", "R07", None, MONDAY.isoformat(), "R07", None))
        # No pain.002: NACHA has no message that rejects one entry of a file.
        self.assertEqual(self.get("/_mock/mailbox?leave").json(), [])
        self.assertEqual([(e["type"], e["reports"], e["dueAt"][:10])
                          for e in self.get("/_mock/queue").json()],
                         [("camt.054.001.08", "collections settling", MONDAY.isoformat()),
                          (RETURN, "collections returned", MONDAY.isoformat())])
        self.refuse("INV-1", "R07", status=409)
        self.advance(TUESDAY)
        # Nothing settled for it, so nothing is debited: only the other booked.
        self.assertEqual(self.balance("ACME"), before + 300)
        [(_key, _sec, _service, entries, control)] = self.return_files()
        self.assertEqual([(e[0], e[1], e[2], e[4]) for e in entries],
                         [("26", "INV-1", 1250, "R07")])
        monday = [m["body"] for m in self.get("/_mock/mailbox?type=bai2").json()][-1]
        [statement] = bai2.statements(monday)
        self.assertEqual([tuple(e) for e in statement.entries],
                         [("165", 300, "INV-2", "Receiver INV-2")])
        self.assertEqual((statement.opening, statement.closing), (before, before + 300))
        self.assertEqual(self.get("/_mock/state").json()["collections"],
                         {"accepted": 1, "rejected": 1, "returned": 0, "booked": 1})


class RejectedWhenItArrived(ReturningCase):

    def test_a_rejection_with_an_r_code_comes_back_the_next_business_day(self):
        before = self.balance("ACME")
        answer = self.collect([
            {"id": "OVER", "cents": 5000, "routing": OURS, "account": GLOBEX_NUMBER},
            {"id": "FINE", "cents": 300}])
        self.assertEqual(self.outcomes(answer), [("OVER", "rejected", "AM04"),
                                                 ("FINE", "accepted", None)])
        [ack] = self.get("/_mock/mailbox?type=" + ACK).json()
        self.assertIn("ENTRY 121042880000001 OVER 50.00 REJECTED R01", ack["body"])
        over = self.collection("OVER")
        self.assertEqual((over["return_due"], over["return_reason"]),
                         (FRIDAY.isoformat(), "R01"))
        self.advance(MONDAY)
        [(_key, _sec, _service, entries, _control)] = self.return_files()
        # The held debtor is named as the file named it: by its account number.
        self.assertEqual(entries, [("26", "OVER", 5000, GLOBEX_NUMBER, "R01",
                                    "121042880000001", OURS[:8])])
        self.assertEqual(self.balance("ACME"), before + 300, "a rejection debits nothing")

    def test_an_iso20022_accounts_rejection_is_in_its_pain002_and_nowhere_else(self):
        self.request("PATCH", "/_mock/accounts/ACME", body={"format": "iso20022"})
        self.collect([{"id": "OVER", "cents": 5000, "routing": OURS, "account": GLOBEX_NUMBER}])
        self.assertIsNone(self.collection("OVER")["return_due"])
        self.advance(MONDAY)
        self.assertEqual(self.get("/_mock/mailbox?type=" + RETURN).json(), [])


class ADebtorThisBankHolds(ReturningCase):

    def held(self, reason=None):
        parameters = dict({"days": 1}, **({"reason": reason} if reason else {}))
        self.request("PATCH", "/_mock/accounts/GLOBEX",
                     body={"behaviour": "return-later", "parameters": parameters})
        self.collect([{"id": "INV-1", "cents": 1250, "routing": OURS, "account": GLOBEX_NUMBER}])
        self.advance(FRIDAY)
        return self.collection("INV-1")

    def test_return_later_sends_it_back_in_a_return_file(self):
        before = self.balance("ACME")
        settled = self.held()
        # The debtor's default reason is ISO 20022's AC04; in a return file
        # that is R02, account closed.
        self.assertEqual((settled["return_due"], settled["return_reason"]),
                         (MONDAY.isoformat(), "R02"))
        self.get("/_mock/mailbox")
        self.advance(TUESDAY)
        self.assertEqual(self.balance("ACME"), before)
        [(_key, _sec, _service, entries, _control)] = self.return_files()
        self.assertEqual([(e[0], e[1], e[3], e[4]) for e in entries],
                         [("26", "INV-1", GLOBEX_NUMBER, "R02")])

    def test_a_reason_with_no_r_code_is_account_closed(self):
        self.assertEqual(self.held("MD07")["return_reason"], "R02")

    def test_insufficient_funds_is_r01(self):
        self.assertEqual(self.held("AM04")["return_reason"], "R01")


class AnIso20022AccountThatSentANachaFile(CollectingCase):

    def test_it_is_answered_with_a_pacs004_that_fits_the_dictionary(self):
        # The account's format decides the answer, not the file's.
        self.request("PATCH", "/_mock/accounts/ACME", body={"currency": "USD"})
        self.collect([{"id": "INV-1", "cents": 1250}])
        self.post("/_mock/advance?to=" + MONDAY.isoformat())
        resp = self.post("/_mock/collections/INV-1/refuse", body={"reason": "MD01"})
        self.assertEqual(resp.status, 200, resp.body)
        [returned] = self.get("/_mock/mailbox?type=pacs.004").json()
        for wanted in ("<OrgnlMsgNmId>NACHA</OrgnlMsgNmId>", "<Cd>MD01</Cd>",
                       "<OrgnlEndToEndId>INV-1</OrgnlEndToEndId>",
                       "<Othr><Id>12345678</Id></Othr>"):
            self.assertIn(wanted, returned["body"])
        root = ET.fromstring(returned["body"].encode("utf-8"))
        self.assertEqual(schema.check(schema.identify(root), root), [])
        self.assertEqual(self.get("/_mock/mailbox?type=" + RETURN).json(), [])
