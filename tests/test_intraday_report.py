"""camt.052: the intraday report a test asks for (#132).

`POST /_mock/accounts/<id>/report` states the day so far for one account: the
opening booked balance, the booked balance now and every entry booked today.
Every balance is checked against numbers the test works out itself, and the
report is then held to the statement the same day ends with, which is the
point of having one: a reconciler compares the two.
"""
import datetime

# test_messages brings in support, which puts the checkout on sys.path
from test_messages import MessageCase
from test_payments import ACME, UMBRELLA, pain001
from test_statements import CAMT053, FRIDAY, read_statement

from mockbank import schema

CAMT052 = schema.MESSAGES["camt.052.001.08"]
M = {"m": CAMT052.namespace}
SATURDAY = FRIDAY + datetime.timedelta(days=1)


def minor(text):
    whole, _, cents = text.partition(".")
    return int(whole) * 100 + int((cents + "00")[:2])


def read_report(root):
    """What a test needs from a camt.052, read straight off the XML."""
    report = root.find("m:BkToCstmrAcctRpt/m:Rpt", M)
    balances = {}
    for bal in report.findall("m:Bal", M):
        sign = -1 if bal.findtext("m:CdtDbtInd", namespaces=M) == "DBIT" else 1
        balances[bal.findtext("m:Tp/m:CdOrPrtry/m:Cd", namespaces=M)] = \
            sign * minor(bal.findtext("m:Amt", namespaces=M))
    return {
        "iban": report.findtext("m:Acct/m:Id/m:IBAN", namespaces=M),
        "number": int(report.findtext("m:ElctrncSeqNb", namespaces=M)),
        "legal": report.findtext("m:LglSeqNb", namespaces=M),
        "from": report.findtext("m:FrToDt/m:FrDtTm", namespaces=M),
        "to": report.findtext("m:FrToDt/m:ToDtTm", namespaces=M),
        "balances": balances,
        "entries": [(e.findtext("m:NtryDtls/m:TxDtls/m:Refs/m:EndToEndId", namespaces=M),
                     minor(e.findtext("m:Amt", namespaces=M)),
                     e.findtext("m:CdtDbtInd", namespaces=M))
                    for e in report.findall("m:Ntry", M)],
    }


class ReportCase(MessageCase):

    def report(self, account="ACME", status=201):
        resp = self.post("/_mock/accounts/%s/report" % account)
        self.assertEqual(resp.status, status, resp.body)
        return resp.json()

    def reports(self, collected, iban=ACME):
        return [r for r in (read_report(root) for root in self.of_type(collected, CAMT052))
                if r["iban"] == iban]

    def advance(self, to):
        resp = self.post("/_mock/advance?to=" + to.isoformat())
        self.assertEqual(resp.status, 200, resp.body)


class TheDaySoFar(ReportCase):

    def test_it_states_what_has_booked_and_the_balance_now(self):
        before = self.balance("ACME")
        self.send(pain001("R-1", ACME, [("D1", 12500, UMBRELLA), ("D2", 40000, UMBRELLA)]))
        arrived = self.post("/_mock/credits", body={"account": "ACME", "amount": 7000,
                                                    "end_to_end_id": "C1"})
        self.assertEqual(arrived.status, 201, arrived.body)
        answer = self.report()
        [report] = self.reports(self.mailbox())
        # Worked out here, not read back from the mock.
        now = before - 12500 - 40000 + 7000
        self.assertEqual(report["balances"], {"OPBD": before, "ITBD": now})
        self.assertEqual(self.balance("ACME"), now)
        self.assertEqual(report["entries"], [("D1", 12500, "DBIT"), ("D2", 40000, "DBIT"),
                                             ("C1", 7000, "CRDT")])
        self.assertEqual((answer["opening"], answer["interim"], answer["entries"]),
                         (before, now, 3))
        # The day so far: from midnight to the bank's now, and no legal number.
        self.assertEqual((report["from"], report["to"]),
                         ("2026-10-01T00:00:00+00:00", "2026-10-01T09:00:00+00:00"))
        self.assertIsNone(report["legal"])

    def test_it_agrees_with_the_statement_the_day_ends_with(self):
        self.send(pain001("R-2", ACME, [("D3", 9900, UMBRELLA)]))
        self.report()
        collected = self.mailbox()
        self.advance(FRIDAY)
        [report] = self.reports(collected)
        [statement] = [s for s in (read_statement(r) for r in
                                   self.of_type(self.mailbox(), CAMT053)) if s["iban"] == ACME]
        self.assertEqual(report["balances"]["OPBD"], statement["opening"])
        self.assertEqual(report["balances"]["ITBD"], statement["closing"])
        self.assertEqual([(e, a) for e, a, _ in report["entries"]], statement["entries"])

    def test_a_day_that_came_while_nobody_asked_is_booked_before_it_is_reported(self):
        # Bank time runs on between requests. Friday's payment comes due at
        # midnight whether or not anything calls in, so the report books it
        # first rather than stating a Friday with Friday's payment missing.
        before = self.balance("ACME")
        self.send(pain001("R-6", ACME, [("F1", 4400, UMBRELLA)], when=FRIDAY))
        self.httpd.state.clock.offset += datetime.timedelta(days=1)   # no request
        self.report()
        [report] = self.reports(self.mailbox())
        self.assertTrue(report["from"].startswith(FRIDAY.isoformat()), report["from"])
        self.assertEqual(report["entries"], [("F1", 4400, "DBIT")])
        self.assertEqual(report["balances"], {"OPBD": before, "ITBD": before - 4400})

    def test_a_payment_for_a_later_day_is_not_in_it(self):
        before = self.balance("ACME")
        self.send(pain001("R-3", ACME, [("L1", 5000, UMBRELLA)], when=FRIDAY))
        self.report()
        [report] = self.reports(self.mailbox())
        self.assertEqual(report["entries"], [])
        self.assertEqual(report["balances"], {"OPBD": before, "ITBD": before})


class WhatAStatementLeavesOut(ReportCase):

    def test_statement_gap_leaves_the_entry_off_the_statement_and_not_the_report(self):
        self.patch_account("ACME", behaviour="statement-gap")
        self.send(pain001("R-4", ACME, [("G1", 1000, UMBRELLA), ("G2", 2000, UMBRELLA)]))
        self.report()
        collected = self.mailbox()
        self.advance(FRIDAY)
        [report] = self.reports(collected)
        [statement] = [s for s in (read_statement(r) for r in
                                   self.of_type(self.mailbox(), CAMT053)) if s["iban"] == ACME]
        self.assertEqual([e for e, _, _ in report["entries"]], ["G1", "G2"])
        self.assertEqual([e for e, _ in statement["entries"]], ["G1"])
        # The balances agree: only the entry is missing, which the report shows.
        self.assertEqual(report["balances"]["ITBD"], statement["closing"])


class Numbering(ReportCase):

    def test_reports_count_on_their_own_and_leave_the_statements_alone(self):
        self.report()
        self.report()
        self.advance(FRIDAY)
        self.report()
        collected = self.mailbox()
        self.assertEqual([r["number"] for r in self.reports(collected)], [1, 2, 3])
        [statement] = [s for s in (read_statement(r) for r in self.of_type(collected, CAMT053))
                       if s["iban"] == ACME]
        self.assertEqual(statement["number"], 1)

    def test_the_mailbox_filters_on_it(self):
        self.report()
        self.report("GLOBEX")
        resp = self.get("/_mock/mailbox?type=camt.052&leave")
        self.assertEqual([(m["type"], m["account"]) for m in resp.json()],
                         [(CAMT052.name, "ACME"), (CAMT052.name, "GLOBEX")])


class WhoGetsOne(ReportCase):

    def test_a_closed_account_is_refused_and_an_unknown_one_is_not_found(self):
        self.patch_account("ACME", closed=True)
        self.assertIn("closed", self.report(status=409)["error"])
        self.assertIn("NOSUCH", self.report("NOSUCH", status=404)["error"])
        self.assertEqual(self.get("/_mock/mailbox?type=camt.052").json(), [])

    def test_a_nacha_account_gets_one_too(self):
        resp = self.request("PATCH", "/_mock/accounts/ACME",
                            body={"format": "nacha", "currency": "USD"})
        self.assertEqual(resp.status, 200, resp.body)
        self.report()
        [report] = self.reports(self.mailbox())
        self.assertEqual(report["iban"], ACME)

    def test_on_a_day_the_bank_is_shut_nothing_has_booked(self):
        self.send(pain001("R-5", ACME, [("W1", 3000, UMBRELLA)]))
        self.advance(SATURDAY)
        balance = self.balance("ACME")
        self.report()
        [report] = self.reports(self.mailbox())
        self.assertEqual(report["entries"], [])
        self.assertEqual(report["balances"], {"OPBD": balance, "ITBD": balance})
        self.assertTrue(report["from"].startswith(SATURDAY.isoformat()), report["from"])


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
