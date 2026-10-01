"""Input the bank takes and then cannot write an answer for (#166).

Two faults, six cases. The first is that a value is accepted which a later
message cannot hold; the second is that one unwritable message used to make
every advance and every mailbox read a 500 until the mock was reset. These tests
are the first fault, and every one of them also asserts the second symptom is
absent - `still_working`, below - because that is what made these expensive:
not a bad answer to one call, but a bank that stopped answering at all.

Three of the six do not behave the way the issue's table first said, and the
tests say which, because a test that passes for a reason nobody checked is the
thing this repository keeps being bitten by:

* b's creditor account is **not** refused. An account given as `Othr/Id` is
  valid input and the bank accepted it; the fault was the writer putting it in an
  `IBAN` element. It is written back the way it came.
* c needs the file in the account's own currency, or it is rejected for its
  currency and never reaches the return the trace number breaks.
* f is only reachable through a return already scheduled on a payment. Switching
  the format while the *account's* parameters hold a NACHA reason has been
  refused since #54; the reason on the payment row is the one nothing checked.

`d` and `e` cannot be built through `schema.serialize` at all - it refuses a
36-character `MsgId` and an 18-digit amount itself - so those two files are
serialized valid and then edited, which is what a real client's file is anyway.
"""
import datetime
import os
import re
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from support import MockServerCase                                   # noqa: E402

from mockbank import accounts, messages, nacha, schema                # noqa: E402

PAIN001 = schema.MESSAGES["pain.001.001.09"]
ACME, GLOBEX = "NL41MOCK0000000001", "NL14MOCK0000000002"
UMBRELLA = "NL30MOCK0000000005"
SPACED = "NL30 MOCK 0000 0000 05"       # the same account, as a sender may paste it
SPARE_IBAN = "NL19MOCK0000000009"      # valid, and not one the seed holds
PINNED = "2026-10-01T09:00"
WHEN = datetime.date(2026, 10, 1)


def pain001(msg_id, payments, cdtr_acct=None, instr_id=None, ccy="EUR"):
    """A pain.001 with the one knob each case needs."""
    total = str(Decimal(sum(p[1] for p in payments)).scaleb(-2))
    transactions = []
    for e2e, minor, creditor in payments:
        ids = {"EndToEndId": e2e}
        if instr_id:
            ids["InstrId"] = instr_id
        transactions.append({"PmtId": ids,
                             "Amt": {"InstdAmt": schema.Amount(minor, ccy)},
                             "Cdtr": {"Nm": "Creditor %s" % e2e},
                             "CdtrAcct": cdtr_acct or {"Id": {"IBAN": creditor}}})
    return schema.serialize(PAIN001, {"CstmrCdtTrfInitn": {
        "GrpHdr": {"MsgId": msg_id,
                   "CreDtTm": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0),
                   "NbOfTxs": len(payments), "CtrlSum": total,
                   "InitgPty": {"Nm": "Test"}},
        "PmtInf": [{"PmtInfId": msg_id + "-B1", "PmtMtd": "TRF",
                    "NbOfTxs": len(payments), "CtrlSum": total,
                    "ReqdExctnDt": {"Dt": WHEN}, "Dbtr": {"Nm": "Debtor"},
                    "DbtrAcct": {"Id": {"IBAN": ACME}},
                    "DbtrAgt": {"FinInstnId": {"BICFI": "MOCKNL2A"}},
                    "CdtTrfTxInf": transactions}]}})


def bend(xml, pattern, replacement):
    """Edit serialized XML past the writer's own validation.

    For the two cases the writer will not build: the door is what is under test,
    not the builder, and a client's file is text.
    """
    out, count = re.subn(pattern, replacement, xml.decode(), count=1)
    assert count == 1, pattern
    return out.encode()


class DoorCase(MockServerCase):
    config_kwargs = {"clock": PINNED}

    def setUp(self):
        self.post("/_mock/reset")

    def still_working(self, days=5):
        """The release path still answers - the second fault's symptom, absent.

        Walked rather than probed once: a return scheduled days out only fails
        when the clock reaches it, and checking one advance is how this looked
        like "does not reproduce" the first time it was tried.

        Collecting the mailbox takes what is in it, so a test that also asserts
        on a message has to read it before calling this.
        """
        for day in range(days):
            advance = self.post("/_mock/advance?days=1")
            self.assertEqual(advance.status, 200,
                             "advance stopped working %d day(s) on: %s"
                             % (day + 1, advance.body[:200]))
            mailbox = self.get("/_mock/mailbox")
            self.assertEqual(mailbox.status, 200,
                             "the mailbox stopped working %d day(s) on: %s"
                             % (day + 1, mailbox.body[:200]))

    def mailbox_of(self, kind):
        answer = self.get("/_mock/mailbox?raw&type=" + kind)
        self.assertEqual(answer.status, 200, answer.body[:200])
        return answer.body if isinstance(answer.body, str) else answer.body.decode()


class AnAccountNameTheMessagesCannotCarry(DoorCase):
    """Case a. Every message for an account carries its name."""

    def test_an_empty_name_is_refused_at_the_door(self):
        answer = self.patch("/_mock/accounts/ACME", body={"name": ""})
        self.assertEqual(answer.status, 400, answer.body)
        # Names the element, so a reader knows what the bank could not write.
        self.assertIn("Nm", answer.json()["error"])
        self.assertEqual(self.get("/_mock/accounts/ACME").json()["name"],
                         "ACME Corporation", "the name was changed anyway")

    def test_a_name_of_whitespace_is_refused_too(self):
        # `Nm is empty` is about the value being blank, not about its length.
        self.assertEqual(self.patch("/_mock/accounts/ACME",
                                    body={"name": "   "}).status, 400)

    def test_a_long_name_is_written_rather_than_refused(self):
        # The done-when is "refused or written": the writers truncate to the
        # standard's length, so this one is written, and refusing it would be
        # wrong. Asserted so the door is not quietly tightened into refusing
        # every name a message has to shorten.
        self.assertEqual(self.patch("/_mock/accounts/ACME",
                                    body={"name": "x" * 200}).status, 200)
        self.post("/payments", body=pain001("A1", [("A-1", 1000, GLOBEX)]))
        self.post("/_mock/advance?days=0")
        # Read before the walk: collecting the mailbox takes what is in it, so
        # `still_working` would empty it first.
        self.assertIn("<Nm>" + "x" * 140 + "</Nm>", self.mailbox_of("camt.054"))
        self.still_working()

    def test_creating_an_account_is_the_same_door(self):
        """`POST /_mock/accounts`, which part 1 left open.

        `create` falls back to the id when the name is empty, so `""` was already
        safe - but a name of only spaces is truthy, so it survived the fallback and
        every message for that account was then unwritable. Found while doing part
        2: this made every later `POST /_mock/advance` a 500 with no payment
        involved at all, because the end-of-day `camt.053` names the account.
        """
        made = self.post("/_mock/accounts",
                         body={"id": "WS", "iban": SPARE_IBAN, "name": "   ",
                               "bic": "MOCKNL2A"})
        self.assertEqual(made.status, 400, made.body)
        self.assertIn("Nm", made.json()["error"])
        self.assertEqual(self.get("/_mock/accounts/WS").status, 404)
        self.still_working()

    def test_creating_one_with_no_name_still_takes_the_id(self):
        # The fallback that made `""` safe is kept: this is not "every account must
        # be named", it is "the name has to be writable".
        made = self.post("/_mock/accounts",
                         body={"id": "NONAME", "iban": SPARE_IBAN, "bic": "MOCKNL2A"})
        self.assertEqual(made.status, 201, made.body)
        self.assertEqual(made.json()["name"], "NONAME")
        self.still_working()

    def test_the_bank_still_answers_after_a_refused_name(self):
        self.patch("/_mock/accounts/ACME", body={"name": ""})
        self.post("/payments", body=pain001("A2", [("A-2", 1000, GLOBEX)]))
        self.still_working()


class ACreditorAccountThatIsNotAnIban(DoorCase):
    """Case b. Valid input; the writer was putting it in the wrong element."""

    def file(self, msg_id, e2e):
        return pain001(msg_id, [(e2e, 1000, GLOBEX)],
                       cdtr_acct={"Id": {"Othr": {"Id": SPACED}}})

    def test_it_is_accepted_and_written_back_the_way_it_came(self):
        answer = self.post("/payments", body=self.file("B1", "B-1"))
        self.assertEqual(answer.status, 202, answer.body)
        self.assertEqual(answer.json()["status"], "ACCP")
        self.post("/_mock/advance?days=0")
        camt = self.mailbox_of("camt.054")
        self.assertIn("<Othr><Id>%s</Id></Othr>" % SPACED, camt)
        self.assertNotIn("<IBAN>%s</IBAN>" % SPACED, camt,
                         "a spaced value was forced into an IBAN element")
        self.still_working()

    def test_validate_says_nothing_about_it_because_there_is_nothing_wrong(self):
        answer = self.post("/_mock/validate", body=self.file("B2", "B-2"))
        self.assertEqual(answer.status, 200, answer.body)
        self.assertIn("0 findings", answer.body if isinstance(answer.body, str)
                      else answer.body.decode())

    def test_a_strict_iban_still_goes_in_the_iban_element(self):
        # The other half of the rule: this is not "always use Othr".
        self.post("/payments", body=pain001("B3", [("B-3", 1000, GLOBEX)]))
        self.post("/_mock/advance?days=0")
        camt = self.mailbox_of("camt.054")
        self.assertIn("<IBAN>%s</IBAN>" % GLOBEX, camt)

    def test_the_choice_is_the_xsds_pattern_and_not_the_forgiving_check(self):
        # `iban_is_valid` forgives spaces on the way in, on purpose. Using it on
        # the way out is what wrote a spaced value into `IBAN` and made the bank
        # unanswerable, so the two tests have to disagree here or this case comes
        # back.
        self.assertTrue(schema.iban_is_valid(SPACED))
        self.assertEqual(messages.party_account(SPACED), {"Othr": {"Id": SPACED}})
        self.assertEqual(messages.party_account(UMBRELLA), {"IBAN": UMBRELLA})


class AnInstructionIdANachaReturnCannotCarry(DoorCase):
    """Case c. A NACHA account's rejections come back as returns (#54)."""

    def nacha_account(self):
        answer = self.patch("/_mock/accounts/ACME",
                            body={"format": "nacha", "currency": "USD",
                                  "behaviour": "insufficient-funds"})
        self.assertEqual(answer.status, 200, answer.body)

    def test_an_instrid_over_the_trace_number_is_rejected_with_the_reason(self):
        self.nacha_account()
        answer = self.post("/payments", body=pain001(
            "C1", [("C-1", 100000000, GLOBEX)],
            instr_id="I" * (accounts.NACHA_TRACE_WIDTH + 1), ccy="USD"))
        self.assertEqual(answer.status, 202, answer.body)
        [decided] = answer.json()["payments"]
        self.assertEqual(decided["outcome"], accounts.REJECTED, decided)
        self.assertEqual(decided["reason"], schema.STRUCTURAL, decided)
        self.assertIn("InstrId", decided["reason_text"])
        self.assertIn("trace number", decided["reason_text"])
        self.still_working()

    def test_one_that_fits_is_decided_on_its_merits(self):
        # The guard is about the width, not about having an InstrId at all: this
        # one is rejected by the behaviour, with the behaviour's own code, and the
        # return for it is written.
        self.nacha_account()
        answer = self.post("/payments", body=pain001(
            "C2", [("C-2", 100000000, GLOBEX)],
            instr_id="I" * accounts.NACHA_TRACE_WIDTH, ccy="USD"))
        [decided] = answer.json()["payments"]
        self.assertEqual(decided["outcome"], accounts.REJECTED, decided)
        self.assertEqual(decided["reason"], "AM04", decided)
        self.still_working()

    def test_the_same_long_instrid_is_fine_on_an_iso20022_account(self):
        # Nothing carries a trace number there, so refusing it would be refusing
        # input the bank can answer for.
        answer = self.post("/payments", body=pain001(
            "C3", [("C-3", 1000, GLOBEX)],
            instr_id="I" * (accounts.NACHA_TRACE_WIDTH + 1)))
        [decided] = answer.json()["payments"]
        self.assertEqual(decided["outcome"], accounts.ACCEPTED, decided)
        self.still_working()

    def test_the_width_is_the_declarations_and_not_a_number_typed_here(self):
        self.assertEqual(
            accounts.NACHA_TRACE_WIDTH,
            next(f.width for f in nacha.RETURN_ADDENDA[1]
                 if f.name == "original entry trace number"))


class AFormatSwitchUnderAScheduledReturn(DoorCase):
    """Case f. The reason is stored on the payment when it books, not read
    from the account when the return goes out."""

    def schedule_a_nacha_return(self):
        self.assertEqual(self.patch("/_mock/accounts/ACME", body={
            "format": "nacha", "currency": "USD", "behaviour": "return-later",
            "parameters": {"days": 2, "reason": "R02"}}).status, 200)
        self.post("/payments", body=pain001("F1", [("F-1", 1000, GLOBEX)], ccy="USD"))
        self.post("/_mock/advance?days=0")
        row = self.get("/_mock/payments/F-1").json()
        self.assertEqual(row["return_reason"], "R02", row)
        self.assertIsNotNone(row["return_due"], row)
        return row

    def test_the_switch_is_refused_while_the_return_is_waiting(self):
        row = self.schedule_a_nacha_return()
        answer = self.patch("/_mock/accounts/ACME",
                            body={"format": "iso20022", "currency": "EUR",
                                  "parameters": {"days": 2, "reason": "AC04"}})
        self.assertEqual(answer.status, 400, answer.body)
        error = answer.json()["error"]
        # How many, which reason, and until when: enough to act on.
        self.assertIn("1 return", error)
        self.assertIn("R02", error)
        self.assertIn(row["return_due"], error)
        self.still_working()

    def test_it_is_allowed_once_the_return_has_gone_back(self):
        # The refusal is "not yet", not "never": the door has to open again or a
        # NACHA account could never be switched back at all.
        self.schedule_a_nacha_return()
        for _ in range(6):
            self.post("/_mock/advance?days=1")
        self.assertEqual(self.get("/_mock/payments/F-1").json()["status"], "returned")
        self.assertEqual(self.patch("/_mock/accounts/ACME",
                                    body={"format": "iso20022", "currency": "EUR",
                                          "parameters": {"days": 2, "reason": "AC04"}}
                                    ).status, 200)

    def test_a_switch_with_nothing_scheduled_is_untouched(self):
        self.assertEqual(self.patch("/_mock/accounts/ACME",
                                    body={"format": "nacha", "currency": "USD"}).status, 200)
        self.assertEqual(self.patch("/_mock/accounts/ACME",
                                    body={"format": "iso20022", "currency": "EUR"}).status, 200)


class AFileTheBankCannotEchoBack(DoorCase):
    """Cases d and e. The `pain.002` has to carry the file's own values back."""

    def test_a_msgid_too_long_for_the_status_report_is_refused(self):
        long_id = bend(pain001("D1", [("D-1", 1000, GLOBEX)]),
                       r"<MsgId>D1</MsgId>", "<MsgId>%s</MsgId>" % ("M" * 36))
        answer = self.post("/payments", body=long_id)
        self.assertEqual(answer.status, 422, answer.body)
        said = answer.json()
        self.assertEqual(said["status"], "RJCT")
        self.assertEqual(said["reason"], schema.STRUCTURAL)
        self.assertIn("status report", said["reason_text"])
        self.assertIn("OrgnlMsgId", said["reason_text"])
        # Nothing queued and nothing booked: the second fault was that the file
        # was taken first and the failure then arrived on the release path.
        self.assertEqual(said["queued"], [])
        self.assertEqual(self.get("/_mock/payments/D-1").status, 404)
        self.still_working()

    def test_an_amount_too_big_for_the_status_report_is_refused(self):
        huge = "9" * 18 + ".00"
        big = bend(pain001("E1", [("E-1", 1000, GLOBEX)]),
                   r'<InstdAmt Ccy="EUR">10\.00</InstdAmt>',
                   '<InstdAmt Ccy="EUR">%s</InstdAmt>' % huge)
        big = bend(big, r"<CtrlSum>10\.00</CtrlSum>", "<CtrlSum>%s</CtrlSum>" % huge)
        big = bend(big, r"<CtrlSum>10\.00</CtrlSum>", "<CtrlSum>%s</CtrlSum>" % huge)
        answer = self.post("/payments", body=big)
        self.assertEqual(answer.status, 422, answer.body)
        said = answer.json()
        self.assertEqual(said["reason"], schema.STRUCTURAL)
        self.assertIn("status report", said["reason_text"])
        self.assertEqual(said["queued"], [])
        self.still_working()

    def test_validate_already_said_so_and_still_does(self):
        # The door is not the only thing that answers: `/_mock/validate` found
        # these before #166 and the finding is what names the field.
        long_id = bend(pain001("D2", [("D-2", 1000, GLOBEX)]),
                       r"<MsgId>D2</MsgId>", "<MsgId>%s</MsgId>" % ("M" * 36))
        answer = self.post("/_mock/validate", body=long_id)
        self.assertEqual(answer.status, 422, answer.body)
        body = answer.body if isinstance(answer.body, str) else answer.body.decode()
        self.assertIn("FF01", body)
        self.assertIn("MsgId", body)

    def test_a_file_the_bank_can_answer_for_is_not_touched_by_any_of_this(self):
        answer = self.post("/payments", body=pain001("D3", [("D-3", 1000, GLOBEX)]))
        self.assertEqual(answer.status, 202, answer.body)
        self.assertTrue(answer.json()["queued"], "a good file queued nothing")
        self.still_working()


class TheBanksOwnAccountIsAlwaysWritable(unittest.TestCase):
    """Why the bank's own `Acct/Id/IBAN` sites were left as they are.

    `party_account` is for the *other* party, whose identifier arrives in a file
    and may be anything. The bank's own account goes through `accounts.check`,
    which normalises spaces away and refuses an IBAN that is not one - so those
    sites cannot be handed a value the `IBAN` element will not take. Asserted
    rather than assumed, because "it is validated elsewhere" is exactly the kind
    of claim that stops being true quietly.
    """

    def test_check_normalises_spaces_out_of_an_iban(self):
        self.assertEqual(accounts.check({"iban": SPACED})["iban"], UMBRELLA)

    def test_check_refuses_an_iban_that_is_not_one(self):
        with self.assertRaises(accounts.Invalid):
            accounts.check({"iban": "not an iban"})

    def test_so_a_held_accounts_iban_always_matches_the_strict_pattern(self):
        self.assertEqual(messages.party_account(accounts.check({"iban": SPACED})["iban"]),
                         {"IBAN": UMBRELLA})


if __name__ == "__main__":
    unittest.main()
