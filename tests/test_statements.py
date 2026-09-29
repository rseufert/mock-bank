"""camt.053: the end-of-day statement, balances that reconcile, and statement-gap.

Every balance here is checked against numbers the test works out for itself:
the account's balance before the file, and the amounts read out of the file
with a regex. A statement is never checked against itself.
"""
import datetime

# test_messages brings in support, which puts the checkout on sys.path
from test_messages import MessageCase
from test_payments import ACME, UMBRELLA, TODAY, BANK_START, amounts, pain001, sample

from support import FileDatabaseCase
from mockbank import schema

CAMT053 = schema.MESSAGES["camt.053.001.08"]
M = {"m": CAMT053.namespace}
FRIDAY = TODAY + datetime.timedelta(days=1)
MONDAY = TODAY + datetime.timedelta(days=4)
TUESDAY = TODAY + datetime.timedelta(days=5)


def minor(text):
    """A wire amount as minor units, by hand: '1250.00' -> 125000."""
    whole, _, cents = text.partition(".")
    return int(whole) * 100 + int((cents + "00")[:2])


def read_statement(root):
    """What a test needs from a camt.053, read straight off the XML."""
    stmt = root.find("m:BkToCstmrStmt/m:Stmt", M)
    balances = {}
    for bal in stmt.findall("m:Bal", M):
        amount = minor(bal.findtext("m:Amt", namespaces=M))
        sign = -1 if bal.findtext("m:CdtDbtInd", namespaces=M) == "DBIT" else 1
        balances[bal.findtext("m:Tp/m:CdOrPrtry/m:Cd", namespaces=M)] = sign * amount
    entries = stmt.findall("m:Ntry", M)
    return {
        "iban": stmt.findtext("m:Acct/m:Id/m:IBAN", namespaces=M),
        "number": int(stmt.findtext("m:ElctrncSeqNb", namespaces=M)),
        "day": stmt.findtext("m:FrToDt/m:FrDtTm", namespaces=M)[:10],
        "opening": balances["OPBD"], "closing": balances["CLBD"],
        "entries": [(e.findtext("m:NtryDtls/m:TxDtls/m:Refs/m:EndToEndId", namespaces=M),
                     minor(e.findtext("m:Amt", namespaces=M))) for e in entries],
        # Who each entry paid. Added by #129: the two renderings of one statement
        # disagreed about the payee's name through 0.3 and 0.4, and nothing
        # compared them, because `entries` carried the reference and the amount
        # and stopped there.
        # Whichever party the entry names: a payment out names its payee as
        # `Cdtr`, and money arriving names its payer as `Dbtr`. Reading only
        # `Cdtr` left an incoming credit with nothing to compare, which is the
        # same gap one level down.
        "payees": [e.findtext("m:NtryDtls/m:TxDtls/m:RltdPties/m:Cdtr/m:Pty/m:Nm",
                              namespaces=M)
                   or e.findtext("m:NtryDtls/m:TxDtls/m:RltdPties/m:Dbtr/m:Pty/m:Nm",
                                 namespaces=M)
                   for e in entries],
        "summary": stmt.findtext("m:TxsSummry/m:TtlNtries/m:NbOfNtries", namespaces=M),
        "tags": [schema.split_tag(child.tag)[1] for child in stmt],
    }


class StatementCase(MessageCase):

    def advance(self, to):
        resp = self.post("/_mock/advance?to=" + to.isoformat())
        self.assertEqual(resp.status, 200, resp.body)

    def statements_for(self, iban):
        return [s for s in (read_statement(r) for r in self.of_type(self.mailbox(), CAMT053))
                if s["iban"] == iban]


class EndOfDay(StatementCase):

    def test_the_readme_file_closes_at_opening_less_its_entries(self):
        before = self.balance("ACME")
        text = sample("pain001_four_payments.xml")
        self.send(text)
        self.mailbox()
        self.advance(FRIDAY)
        [statement] = self.statements_for(ACME)
        paid = amounts(text)
        self.assertEqual(statement["day"], TODAY.isoformat())
        self.assertEqual(statement["entries"], [("INV-2026-0101", paid[0]),
                                                ("INV-2026-0104", paid[3])])
        # both sides worked out independently of the statement
        self.assertEqual(statement["opening"], before)
        self.assertEqual(statement["closing"], before - paid[0] - paid[3])
        self.assertEqual(statement["closing"], self.balance("ACME"))
        self.assertEqual(statement["summary"], "2")
        # the standard's order: balances before entries
        self.assertLess(statement["tags"].index("Bal"), statement["tags"].index("Ntry"))

    def test_three_business_days_chain_through_an_empty_one(self):
        before = self.balance("ACME")
        self.send(pain001("CH-1", ACME, [("T1", 1000, UMBRELLA), ("T2", 2500, UMBRELLA)]))
        self.send(pain001("CH-2", ACME, [("M1", 4000, UMBRELLA)], when=MONDAY))
        self.mailbox()
        self.advance(TUESDAY)                  # ends Thursday, Friday and Monday
        statements = self.statements_for(ACME)
        self.assertEqual([s["day"] for s in statements],
                         [TODAY.isoformat(), FRIDAY.isoformat(), MONDAY.isoformat()])
        self.assertEqual([s["number"] for s in statements], [1, 2, 3])
        self.assertEqual([len(s["entries"]) for s in statements], [2, 0, 1])
        self.assertEqual(statements[0]["opening"], before)
        for statement in statements:
            self.assertEqual(statement["closing"],
                             statement["opening"] - sum(a for _, a in statement["entries"]))
        for earlier, later in zip(statements, statements[1:]):
            self.assertEqual(later["opening"], earlier["closing"])
        self.assertEqual(statements[-1]["closing"], before - 7500)

    def test_every_open_account_gets_one_and_a_closed_one_none(self):
        self.advance(FRIDAY)
        listed = {a["id"]: self.get("/_mock/accounts/%s/statements" % a["id"]).json()
                  for a in self.get("/_mock/accounts").json()}
        self.assertEqual({k: len(v) for k, v in listed.items()},
                         {"ACME": 1, "GLOBEX": 1, "EURODIS": 1, "INITECH": 0})
        self.assertEqual(listed["ACME"][0]["day"], TODAY.isoformat())

    def test_a_statement_is_never_issued_twice(self):
        self.advance(FRIDAY)
        self.post("/_mock/advance?days=0")
        self.post("/_mock/advance?to=" + FRIDAY.isoformat())
        self.assertEqual(len(self.get("/_mock/accounts/ACME/statements").json()), 1)

    def test_the_statements_endpoint_refuses_an_unknown_account(self):
        self.assertEqual(self.get("/_mock/accounts/NOPE/statements").status, 404)


class APatchedBalance(StatementCase):

    def test_the_next_opening_shows_the_jump_and_each_statement_still_adds_up(self):
        # A balance set by PATCH is a change no entry explains: the statement
        # after it opens at the new balance, not where the last one closed,
        # and still closes at its own opening less its own entries.
        self.send(pain001("PB-1", ACME, [("P1", 1000, UMBRELLA)]))
        self.advance(FRIDAY)
        [thursday] = self.statements_for(ACME)
        self.patch_account("ACME", balance=5000000)
        self.send(pain001("PB-2", ACME, [("P2", 2000, UMBRELLA)]))
        self.advance(MONDAY)
        [friday] = self.statements_for(ACME)
        self.assertEqual(friday["opening"], 5000000)
        self.assertNotEqual(friday["opening"], thursday["closing"])
        for statement in (thursday, friday):
            self.assertEqual(statement["closing"],
                             statement["opening"] - sum(a for _, a in statement["entries"]))
        self.assertEqual(friday["closing"], 5000000 - 2000)


class StatementGap(StatementCase):

    def test_one_entry_is_missing_and_the_balances_still_tell_the_truth(self):
        self.patch_account("ACME", behaviour="statement-gap")
        before = self.balance("ACME")
        self.send(pain001("GAP-1", ACME, [("G1", 1100, UMBRELLA), ("G2", 2200, UMBRELLA),
                                         ("G3", 3300, UMBRELLA)]))
        self.mailbox()
        self.advance(FRIDAY)
        [statement] = self.statements_for(ACME)
        self.assertEqual(len(statement["entries"]), 2)
        self.assertEqual(statement["opening"], before)
        self.assertEqual(statement["closing"], before - 6600)       # all three
        shown = sum(a for _, a in statement["entries"])
        missing = statement["opening"] - shown - statement["closing"]
        self.assertEqual(missing, 3300)
        self.assertNotIn("G3", [e for e, _ in statement["entries"]])

    def test_an_empty_day_has_nothing_to_leave_out(self):
        self.patch_account("ACME", behaviour="statement-gap")
        self.advance(FRIDAY)
        [statement] = self.statements_for(ACME)
        self.assertEqual((statement["entries"], statement["opening"] - statement["closing"]),
                         ([], 0))


class AcrossARestart(FileDatabaseCase):
    config_kwargs = {"clock": BANK_START}

    def test_a_restart_neither_renumbers_nor_reissues(self):
        self.post("/_mock/advance?to=" + FRIDAY.isoformat())
        self.restart()                       # bank time is back at Thursday 09:00
        self.post("/_mock/advance?to=" + MONDAY.isoformat())
        listed = self.get("/_mock/accounts/ACME/statements").json()
        self.assertEqual([(s["number"], s["day"]) for s in listed],
                         [(1, TODAY.isoformat()), (2, FRIDAY.isoformat())])
