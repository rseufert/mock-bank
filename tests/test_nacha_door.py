"""NACHA through the payments door: recognised, resolved, decided, acknowledged (#53).

The milestone's promise is that a NACHA file is decided by the same engine as
a pain.001, so the test that matters most sends the NACHA twin of the sample
and asks for the same decisions, payment for payment. Everything else here is
what that needs: the accounts named by routing and account number, the
account's `format`, and the acknowledgement a NACHA account is sent.
"""
import os
import shutil
import tempfile

from support import MockServerCase
from test_payments import BANK_START

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")
ACK = "nacha.ack"


def sample(name):
    with open(os.path.join(SAMPLES, name), "rb") as handle:
        return handle.read()


TWIN = "nacha_four_payments_to_the_seed.ach"


def pain001_paying(creditor_account, clearing_id, debtor=None, msg_id="DOM-1"):
    """A pain.001 that names accounts the way a NACHA file does: numbers, not IBANs."""
    debtor_account = ("<IBAN>NL41MOCK0000000001</IBAN>" if debtor is None
                      else "<Othr><Id>%s</Id></Othr>" % debtor)
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"><CstmrCdtTrfInitn>
<GrpHdr><MsgId>%s</MsgId><CreDtTm>2026-10-01T08:00:00</CreDtTm><NbOfTxs>1</NbOfTxs>
<CtrlSum>10.00</CtrlSum><InitgPty><Nm>ACME</Nm></InitgPty></GrpHdr>
<PmtInf><PmtInfId>%s-B1</PmtInfId><PmtMtd>TRF</PmtMtd><NbOfTxs>1</NbOfTxs>
<CtrlSum>10.00</CtrlSum><ReqdExctnDt><Dt>2026-10-01</Dt></ReqdExctnDt>
<Dbtr><Nm>ACME</Nm></Dbtr><DbtrAcct><Id>%s</Id></DbtrAcct>
<DbtrAgt><FinInstnId><BICFI>MOCKNL2A</BICFI></FinInstnId></DbtrAgt>
<CdtTrfTxInf><PmtId><EndToEndId>DOM-E2E-1</EndToEndId></PmtId>
<Amt><InstdAmt Ccy="EUR">10.00</InstdAmt></Amt>
<CdtrAgt><FinInstnId><ClrSysMmbId><ClrSysId><Cd>USABA</Cd></ClrSysId>
<MmbId>%s</MmbId></ClrSysMmbId></FinInstnId></CdtrAgt>
<Cdtr><Nm>Creditor</Nm></Cdtr><CdtrAcct><Id><Othr><Id>%s</Id></Othr></Id></CdtrAcct>
</CdtTrfTxInf></PmtInf></CstmrCdtTrfInitn></Document>
""" % (msg_id, msg_id, debtor_account, clearing_id, creditor_account)).encode("utf-8")


class DoorCase(MockServerCase):
    config_kwargs = {"clock": BANK_START}          # Thursday 09:00, before the cutoff

    def setUp(self):
        self.post("/_mock/reset")

    def nacha_acme(self, **more):
        fields = dict({"format": "nacha", "currency": "USD"}, **more)
        resp = self.request("PATCH", "/_mock/accounts/ACME", body=fields)
        self.assertEqual(resp.status, 200, resp.body)

    def decisions(self, answer):
        return [(p["end_to_end_id"], p["outcome"], p["reason"], p["settlement_date"])
                for p in answer["payments"]]

    def mailbox(self, query=""):
        return self.get("/_mock/mailbox" + query).json()


class TheSameDecisions(DoorCase):

    def test_the_nacha_twin_is_decided_exactly_as_the_pain001_sample(self):
        iso = self.post("/payments", body=sample("pain001_four_payments.xml")).json()
        self.post("/_mock/reset")
        self.nacha_acme()
        ach = self.post("/payments", body=sample(TWIN)).json()
        self.assertEqual((iso["format"], ach["format"]), ("iso20022", "nacha"))
        self.assertEqual((ach["status"], ach["accepted"], ach["rejected"]),
                         (iso["status"], iso["accepted"], iso["rejected"]))
        self.assertEqual(self.decisions(ach), self.decisions(iso))
        # And it is the full set: GLOBEX paid, INITECH closed, EURODIS's bank
        # unknown, Umbrella at another bank - read from the account numbers.
        self.assertEqual([(e, o, r) for e, o, r, _ in self.decisions(ach)], [
            ("INV-2026-0101", "accepted", None),
            ("INV-2026-0102", "rejected", "AC04"),
            ("INV-2026-0103", "rejected", "RC01"),
            ("INV-2026-0104", "accepted", None)])

    def test_the_same_nacha_file_twice_is_dupl(self):
        self.nacha_acme()
        self.post("/payments", body=sample(TWIN))
        again = self.post("/payments", body=sample(TWIN))
        self.assertEqual((again.status, again.json()["reason"]), (422, "DUPL"))

    def test_a_nacha_account_not_in_dollars_rejects_each_payment_am03(self):
        # A NACHA file is in dollars. An account that trades NACHA and is held
        # in euros is decided by the rule that already exists for a currency
        # mismatch, payment by payment, and says so.
        self.nacha_acme(currency="EUR")
        answer = self.post("/payments", body=sample(TWIN)).json()
        self.assertEqual({(o, r) for _, o, r, _ in self.decisions(answer)},
                         {("rejected", "AM03")})

    def test_a_past_effective_date_executes_on_the_next_business_day(self):
        # DT01 is the reader's warning; this is what the bank then does with it.
        self.nacha_acme()
        past = sample(TWIN).decode("ascii").replace(
            "SUPPLIERS       261001", "SUPPLIERS       260925")
        answer = self.post("/payments", body=past).json()
        self.assertIn("DT01", [f["code"] for f in answer["findings"]])
        # Received Thursday 1 October before the cutoff: that day, not the past one.
        self.assertEqual({s for _, o, _, s in self.decisions(answer) if o == "accepted"},
                         {"2026-10-01"})


class Resolution(DoorCase):
    """Accounts named by number, in either format, found by the bank (#53)."""

    def test_a_pain001_creditor_named_by_routing_and_number_is_the_held_account(self):
        # INITECH's number at this bank: closed, so AC04. Had it not been
        # resolved it would be an account at another bank, and accepted.
        answer = self.post("/payments", body=pain001_paying("0000000003", "999999992")).json()
        self.assertEqual([(o, r) for _, o, r, _ in self.decisions(answer)],
                         [("rejected", "AC04")])

    def test_the_same_number_at_another_bank_is_not_the_held_account(self):
        answer = self.post("/payments", body=pain001_paying("0000000003", "021000021")).json()
        self.assertEqual([o for _, o, _, _ in self.decisions(answer)], ["accepted"])

    def test_a_debtor_named_by_account_number_is_the_held_account(self):
        answer = self.post("/payments", body=pain001_paying(
            "0000000002", "999999992", debtor="0000000001")).json()
        self.assertEqual([(o, r) for _, o, r, _ in self.decisions(answer)],
                         [("accepted", None)])

    def test_a_debtor_number_the_bank_does_not_hold_is_ac02(self):
        answer = self.post("/payments", body=pain001_paying(
            "0000000002", "999999992", debtor="0000000099")).json()
        self.assertEqual([r for _, _, r, _ in self.decisions(answer)], ["AC02"])


class TheAccountNumber(DoorCase):

    def test_the_seed_gives_each_account_its_number_and_format(self):
        found = {a["id"]: (a["account_number"], a["format"])
                 for a in self.get("/_mock/accounts").json()}
        self.assertEqual(found, {"ACME": ("0000000001", "iso20022"),
                                 "GLOBEX": ("0000000002", "iso20022"),
                                 "INITECH": ("0000000003", "iso20022"),
                                 "EURODIS": ("0000000004", "iso20022")})

    def test_one_number_names_one_account(self):
        resp = self.request("PATCH", "/_mock/accounts/GLOBEX",
                            body={"account_number": "0000000001"})
        self.assertEqual(resp.status, 400)
        self.assertIn("'ACME'", resp.json()["error"])

    def test_no_number_is_not_a_clash(self):
        for account in ("GLOBEX", "INITECH"):
            resp = self.request("PATCH", "/_mock/accounts/" + account,
                                body={"account_number": ""})
            self.assertEqual(resp.status, 200, resp.body)

    def test_a_number_is_digits_and_a_format_is_one_the_bank_writes(self):
        for fields in ({"account_number": "12-34"}, {"account_number": "1" * 18},
                       {"format": "bai2"}):
            with self.subTest(fields):
                resp = self.request("PATCH", "/_mock/accounts/ACME", body=fields)
                self.assertEqual(resp.status, 400)


class TheAcknowledgement(DoorCase):

    def ack(self):
        acks = [m for m in self.mailbox("?type=" + ACK)]
        self.assertEqual(len(acks), 1, acks)
        return acks[0]

    def test_a_nacha_account_is_sent_an_acknowledgement_not_a_pain002(self):
        self.nacha_acme()
        queued = self.post("/payments", body=sample(TWIN)).json()["queued"]
        self.assertEqual(queued[0]["type"], ACK)
        message = self.ack()
        self.assertEqual(message["account"], "ACME")
        lines = message["body"].splitlines()
        self.assertTrue(lines[0].startswith("ACKNOWLEDGEMENT MB-ACK-"), lines)
        self.assertIn("FILE 0000000001-2609300930A", lines)
        self.assertIn("STATUS PART", lines)
        entries = [line.split()[:6] for line in lines if line.startswith("ENTRY")]
        self.assertEqual(entries, [
            ["ENTRY", "999999990000001", "INV-2026-0101", "1250.00", "ACCEPTED", "2026-10-01"],
            ["ENTRY", "999999990000002", "INV-2026-0102", "3400.50", "REJECTED", "AC04"],
            ["ENTRY", "999999990000003", "INV-2026-0103", "980.25", "REJECTED", "RC01"],
            ["ENTRY", "999999990000004", "INV-2026-0104", "15000.00", "ACCEPTED", "2026-10-01"]])
        self.assertEqual([m for m in self.mailbox() if m["type"].startswith("pain.002")], [])

    def test_an_iso20022_account_still_gets_its_pain002(self):
        self.post("/payments", body=sample("pain001_four_payments.xml"))
        types = [m["type"] for m in self.mailbox()]
        self.assertIn("pain.002.001.10", types)
        self.assertNotIn(ACK, types)

    def test_a_duplicate_is_acknowledged_as_refused(self):
        self.nacha_acme()
        self.post("/payments", body=sample(TWIN))
        self.mailbox()
        self.post("/payments", body=sample(TWIN))
        self.assertIn("STATUS RJCT DUPL", self.ack()["body"])

    def test_one_message_is_served_as_text(self):
        self.nacha_acme()
        message_id = self.post("/payments", body=sample(TWIN)).json()["queued"][0]["id"]
        resp = self.get("/_mock/mailbox/%d" % message_id)
        self.assertIn("text/plain", resp.headers["Content-Type"])
        self.assertTrue(resp.body.startswith(b"ACKNOWLEDGEMENT"))

    def test_raw_refuses_to_mix_text_into_xml_and_takes_nothing(self):
        self.nacha_acme()
        self.post("/payments", body=sample(TWIN))          # an ack and a camt.054
        resp = self.get("/_mock/mailbox?raw")
        self.assertEqual(resp.status, 409)
        self.assertIn("?raw&type=%s" % ACK, resp.json()["error"])
        # Nothing was collected by the refusal; asked apart, both come.
        text = self.get("/_mock/mailbox?raw&type=" + ACK)
        self.assertIn("text/plain", text.headers["Content-Type"])
        self.assertTrue(text.body.startswith(b"ACKNOWLEDGEMENT"))
        xml = self.get("/_mock/mailbox?raw&type=camt.")
        self.assertIn(b"<?xml", xml.body)


class ThroughTheFolder(DoorCase):
    """The drop directory is the other door, and takes NACHA the same way."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="mock-bank-nacha-")
        cls.drop = os.path.join(cls.root, "in")
        cls.pickup = os.path.join(cls.root, "out")
        cls.config_kwargs = dict(DoorCase.config_kwargs, drop_dir=cls.drop,
                                 pickup_dir=cls.pickup, drop_settle_ms=0)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_a_dropped_nacha_file_is_processed_and_acknowledged_as_text(self):
        self.nacha_acme()
        with open(os.path.join(self.drop, "payroll.ach"), "wb") as handle:
            handle.write(sample(TWIN))
        found = self.post("/_mock/drop/scan").json()["files"]
        self.assertEqual((found[0]["name"], found[0]["ok"]), ("payroll.ach", True))
        written = sorted(os.listdir(self.pickup))
        acks = [name for name in written if name.startswith(ACK)]
        self.assertEqual(len(acks), 1, written)
        self.assertTrue(acks[0].startswith(ACK + "-ACME-") and acks[0].endswith(".txt"), acks)
        with open(os.path.join(self.pickup, acks[0]), encoding="utf-8") as handle:
            self.assertIn("NAME payroll.ach", handle.read())
