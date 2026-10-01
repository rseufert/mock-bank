"""A rejected payment was never debited, so nothing is credited back (#144).

Option (a) on #54 answers a NACHA account's rejections as return entries the
next business day and credits nothing back, because nothing was debited.
``outbox._position`` did not know that: both of its queries took every payment
with ``returned_at`` set as money that came back. So through 0.3.0, 0.4.0 and
0.5.0 a NACHA account's statement understated both balances on the settlement
day by the rejected total, and then booked that same total as credits on the day
the returns went out - ``165`` in the first two releases and ``257`` from 0.5.0,
where #127 settled the real type codes. The two errors cancel, which is why the account's
own balance and every later statement were right.

The file reconciles against itself either way - opening less the entries is the
closing, and the trailers tie out - so nothing that reads a statement back can
find this. Every number here is therefore worked out from the account's balance
and from the bank's own decisions on the payments, and no statement is checked
against another statement of the same kind.
"""
import datetime
import os
import sys
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from support import MockServerCase                                   # noqa: E402
from test_payments import BANK_START                                 # noqa: E402
from test_statements import read_statement                           # noqa: E402
from test_intraday_report import read_report                         # noqa: E402

from mockbank import accounts, bai2, messages, nacha                 # noqa: E402

TWIN = os.path.join(HERE, "samples", "nacha_four_payments_to_the_seed.ach")
FRIDAY = "2026-10-02"          # the business day after BANK_START's Thursday
MONDAY = "2026-10-05"          # far enough that both statements have been issued


def twin():
    with open(TWIN, "rb") as handle:
        return handle.read()


class RejectionCase(MockServerCase):
    config_kwargs = {"clock": BANK_START}      # Thursday 09:00, before the cutoff

    def setUp(self):
        self.post("/_mock/reset")

    def acme(self, **fields):
        """ACME in USD, as NACHA unless a test says otherwise."""
        resp = self.request("PATCH", "/_mock/accounts/ACME",
                            body=dict({"format": "nacha", "currency": "USD"}, **fields))
        self.assertEqual(resp.status, 200, resp.body)

    def balance(self, account="ACME"):
        return self.get("/_mock/accounts/" + account).json()["balance"]

    def advance(self, to):
        resp = self.post("/_mock/advance?to=" + to)
        self.assertEqual(resp.status, 200, resp.body)

    def decided(self):
        """The bank's own decisions: {status: {end_to_end_id: amount}}.

        Read from the payments endpoint, not from any statement, so a test's
        expected arithmetic never comes from the thing it is checking.
        """
        by_status = {}
        for payment in self.get("/_mock/payments").json():
            by_status.setdefault(payment["status"], {})[payment["end_to_end_id"]] = \
                payment["amount"]
        return by_status

    def a_rejection_happened(self, decided):
        """Guard: these tests say nothing unless the file really had one rejected
        and one accepted. Four separator tests went vacuous this way on #57."""
        self.assertTrue(decided.get("rejected"), decided)
        self.assertTrue(decided.get("accepted"), decided)
        return sum(decided["accepted"].values()), sum(decided["rejected"].values())

    def bai2_days(self):
        """[(day, Parsed)] for ACME's BAI2 statements, oldest first."""
        days = [row["day"] for row in
                self.get("/_mock/accounts/ACME/statements").json()]
        bodies = [m["body"] for m in self.get("/_mock/mailbox?leave&type=" + bai2.STATEMENT)
                  .json() if m["account"] == "ACME"]
        self.assertEqual(len(bodies), len(days), (len(bodies), days))
        out = []
        for day, body in zip(days, bodies):
            self.assertEqual(bai2.trailers_agree(body), [], day)
            [parsed] = bai2.statements(body)
            out.append((day, parsed))
        return out

    def returned_references(self):
        """The end-to-end ids in every return file, each file read back clean."""
        found = []
        for message in self.get("/_mock/mailbox?leave&type=" + nacha.RETURN).json():
            file_, findings = nacha.inspect(message["body"].encode("ascii"),
                                            datetime.date(2026, 10, 2))
            self.assertEqual(findings, [], message["id"])
            found += [r.end_to_end_id for r in file_.returns]
        return sorted(found)


class WhereARejectedPaymentAppears(RejectionCase):

    def test_it_is_in_the_return_file_and_on_no_statement_on_any_day(self):
        self.acme()
        self.post("/payments", body=twin())
        self.advance(MONDAY)
        decided = self.decided()
        self.a_rejection_happened(decided)
        rejected = sorted(decided["rejected"])
        # The return file is where a rejected payment is answered, and the
        # whole of it.
        self.assertEqual(self.returned_references(), rejected)
        shown = [entry.reference for _, parsed in self.bai2_days()
                 for entry in parsed.entries]
        self.assertEqual([r for r in shown if r in rejected], [],
                         "a rejected payment is on a statement: %s" % shown)
        # And every entry that is shown is one the bank accepted.
        self.assertEqual(sorted(shown), sorted(decided["accepted"]))

    def test_no_type_code_for_money_coming_back_is_written_at_all(self):
        # The wrong credits were `257`, a payment returned. Nothing came back,
        # so the only type code in the file is `447`, a payment sent.
        self.acme()
        self.post("/payments", body=twin())
        self.advance(MONDAY)
        self.a_rejection_happened(self.decided())
        codes = {entry.type_code for _, parsed in self.bai2_days()
                 for entry in parsed.entries}
        self.assertEqual(codes, {"447"})


class TheStatementAgainstTheAccount(RejectionCase):
    """The check the project did not have: a statement held to the account it
    describes, rather than to its own arithmetic."""

    def test_every_days_bai2_closing_is_the_balance_that_day_ended_on(self):
        self.acme()
        before = self.balance()
        self.post("/payments", body=twin())
        self.advance(MONDAY)
        debited, rejected_total = self.a_rejection_happened(self.decided())
        # Only the accepted payments moved money, which the account agrees with.
        self.assertEqual(self.balance(), before - debited)
        days = self.bai2_days()
        self.assertEqual([day for day, _ in days], ["2026-10-01", FRIDAY])
        # Day one opens where the account stood and closes where it stands now.
        self.assertEqual(days[0][1].opening, before)
        self.assertEqual(days[0][1].closing, before - debited)
        # Nothing moves after it, so every later day states that balance twice.
        for day, parsed in days[1:]:
            self.assertEqual((parsed.opening, parsed.closing),
                             (before - debited, before - debited), day)
        # Each day opens where the day before closed.
        for (_, earlier), (day, later) in zip(days, days[1:]):
            self.assertEqual(later.opening, earlier.closing, day)
        # Naming the defect: both balances were low by exactly the rejected
        # total on the settlement day, and the day the returns went out opened
        # there and credited its way back up.
        self.assertNotEqual(days[0][1].closing, before - debited - rejected_total)
        self.assertNotEqual(days[1][1].opening, before - debited - rejected_total)

    def test_the_camt052_report_states_the_same_position(self):
        # Asked on the day the returns go out, which is when `returned_at` is
        # set and so the day the report was wrong. The interim balance was
        # right and the opening was not, because the phantom credits were
        # undone out of one and not the other.
        self.acme()
        before = self.balance()
        self.post("/payments", body=twin())
        self.advance(FRIDAY)
        debited, _ = self.a_rejection_happened(self.decided())
        answer = self.post("/_mock/accounts/ACME/report")
        self.assertEqual(answer.status, 201, answer.body)
        bodies = [m["body"] for m in self.get("/_mock/mailbox?leave&type="
                                              + messages.CAMT052.name).json()
                  if m["account"] == "ACME"]
        self.assertEqual(len(bodies), 1, bodies)
        report = read_report(ET.fromstring(bodies[0].encode("utf-8")))
        self.assertEqual(report["balances"], {"OPBD": before - debited,
                                              "ITBD": before - debited})
        self.assertEqual(report["entries"], [])
        self.assertEqual((answer.json()["opening"], answer.json()["interim"],
                          answer.json()["entries"]),
                         (before - debited, before - debited, 0))


class TheTwoRenderingsOfOneDay(RejectionCase):
    """The comparison that would have caught this on its own.

    What existed before put ACME's BAI2 beside *GLOBEX's* `camt.053` and checked
    each against itself, which a fault in the shared arithmetic survives: a
    NACHA account has no `camt.053` of its own to compare with. So this runs the
    same file into the same account twice, changing nothing but the format the
    account is held in, and compares the two statements of the same day.
    """

    def one_day(self, **fields):
        self.post("/_mock/reset")
        self.acme(**fields)
        self.post("/payments", body=twin())
        self.advance(MONDAY)
        decided = self.decided()
        self.a_rejection_happened(decided)
        return decided

    def test_bai2_and_camt053_agree_on_the_balances_and_the_entries(self):
        self.one_day()
        [(day, mine)] = self.bai2_days()[:1]
        mine_entries = [(e.reference, e.amount) for e in mine.entries]
        # The same file again, with the account left on ISO 20022. Its
        # rejections get no return at all - a pain.002 is their answer - so its
        # statement was right through all three releases.
        decided = self.one_day(format="iso20022")
        theirs = [s for s in (read_statement(ET.fromstring(m["body"].encode("utf-8")))
                              for m in self.get("/_mock/mailbox?leave&type="
                                                + messages.CAMT053.name).json()
                              if m["account"] == "ACME")
                  if s["day"] == day]
        self.assertEqual(len(theirs), 1, [s["day"] for s in theirs])
        # Both renderings of one day: the same two balances and the same entries.
        self.assertEqual((mine.opening, mine.closing),
                         (theirs[0]["opening"], theirs[0]["closing"]))
        self.assertEqual(mine_entries, theirs[0]["entries"])
        # And the day really did have a rejection in it, in both runs.
        self.assertEqual(len(decided["rejected"]), 2)
        self.assertEqual(len(mine_entries), len(decided["accepted"]))


class NothingIsStillToArrive(RejectionCase):
    """`accounts.still_to_arrive` is the same assumption one file over: it is
    what a balance has to leave room for, and it counted these returns too."""

    def test_a_rejected_payments_return_leaves_no_room_to_reserve(self):
        # Asked before the return day, while the return is scheduled and not
        # yet sent, which is the window `still_to_arrive` looks at. Nothing
        # will be credited, so the balance may go all the way to the limit.
        self.acme()
        self.post("/payments", body=twin())
        _, rejected_total = self.a_rejection_happened(self.decided())
        self.assertTrue(rejected_total, "nothing scheduled to come back wrongly")
        resp = self.request("PATCH", "/_mock/accounts/ACME",
                            body={"balance": accounts.MAX_BALANCE})
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(self.balance(), accounts.MAX_BALANCE)
        # And the clock still moves: the returns go out, crediting nothing, and
        # the statement that follows is one the writer can still write.
        self.advance(MONDAY)
        self.assertEqual(self.balance(), accounts.MAX_BALANCE)
        self.assertEqual(self.returned_references(), sorted(self.decided()["rejected"]))

    def test_a_return_that_will_be_credited_is_still_counted(self):
        # The guard is not simply switched off: a `return-later` payment was
        # booked, so its return will credit, and the room still has to be kept.
        self.acme(behaviour="return-later", parameters={"days": 3})
        self.post("/payments", body=twin())
        accepted = sum(self.decided()["accepted"].values())
        self.assertTrue(accepted)
        too_much = self.request("PATCH", "/_mock/accounts/ACME",
                                body={"balance": accounts.MAX_BALANCE - accepted + 1})
        self.assertEqual(too_much.status, 400, too_much.body)
        self.assertIn("returns due back", too_much.json()["error"])
