"""A NACHA account's end-of-day statement is BAI2, not camt.053 (#57).

The wiring, over HTTP, against a real mock: the clock hook writing BAI2 for an
account whose `format` is NACHA, numbered from the same counter, reaching the
mailbox with its own type and the pickup directory with its own extension, and
`statement-gap` leaving one detail record out while the balances stay true.

Every balance here is checked against numbers this test works out for itself,
and the BAI2 statement is checked against the `camt.053` the *other* accounts in
the same file are sent, so neither format is ever checked only against itself.
"""
import datetime
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from support import MockServerCase                                   # noqa: E402
from test_payments import BANK_START                                 # noqa: E402
from test_statements import read_statement                           # noqa: E402

from mockbank import bai2, messages, outbox                          # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
TWIN = "nacha_four_payments_to_the_seed.ach"
FRIDAY = "2026-10-02"


def sample(name):
    with open(os.path.join(SAMPLES, name), "rb") as handle:
        return handle.read()


class StatementCase(MockServerCase):
    config_kwargs = {"clock": BANK_START}          # Thursday 09:00, before the cutoff

    def nacha(self, account="ACME", **fields):
        resp = self.request("PATCH", "/_mock/accounts/" + account,
                            body=dict({"format": "nacha", "currency": "USD"},
                                      **fields))
        self.assertEqual(resp.status, 200, resp.body)

    def run_a_day(self, **fields):
        self.post("/_mock/reset")
        self.nacha(**fields)
        self.post("/payments", body=sample(TWIN))
        self.assertEqual(self.post("/_mock/advance?to=" + FRIDAY).status, 200)
        return self.get("/_mock/mailbox?leave").json()

    def bai2_for(self, collected, account="ACME"):
        found = [m for m in collected
                 if m["type"] == bai2.STATEMENT and m["account"] == account]
        self.assertEqual(len(found), 1, [m["type"] for m in collected])
        return found[0]

    def camt_for(self, collected, account):
        found = [m for m in collected
                 if m["type"] == messages.CAMT053.name and m["account"] == account]
        self.assertEqual(len(found), 1, [m["type"] for m in collected])
        return read_statement(ET.fromstring(found[0]["body"].encode("utf-8")))


class ANachaAccountIsSentBai2(StatementCase):

    def test_its_statement_is_bai2_and_not_camt053(self):
        collected = self.run_a_day()
        mine = [m["type"] for m in collected if m["account"] == "ACME"]
        self.assertIn(bai2.STATEMENT, mine)
        self.assertNotIn(messages.CAMT053.name, mine)

    def test_the_other_accounts_still_get_camt053(self):
        # The choice is per account, not per file: one payment file produces a
        # BAI2 statement for the NACHA debtor and camt.053 for everyone else.
        collected = self.run_a_day()
        others = {m["account"] for m in collected
                  if m["type"] == messages.CAMT053.name}
        self.assertNotIn("ACME", others)
        self.assertTrue(others, [m["type"] for m in collected])

    def test_it_parses_as_bai2_with_its_trailers_agreeing(self):
        body = self.bai2_for(self.run_a_day())["body"]
        self.assertEqual(bai2.trailers_agree(body), [])
        self.assertEqual(len(bai2.statements(body)), 1)

    def test_the_balances_and_entries_are_the_camt053s_for_the_same_day(self):
        # ACME is NACHA so it has no camt.053 to compare against. GLOBEX in the
        # same file does, and both statements are for the same business day, so
        # this checks the renderer choice did not change what a statement says -
        # the day, the number's source and the shape of the arithmetic.
        collected = self.run_a_day()
        mine = bai2.statements(self.bai2_for(collected)["body"])[0]
        theirs = self.camt_for(collected, "GLOBEX")
        self.assertEqual(mine.currency, "USD")
        # Both statements are for the same business day, and both reconcile:
        # opening less the entries is closing, to the cent.
        self.assertEqual(mine.opening - sum(e.amount for e in mine.entries),
                         mine.closing)
        self.assertEqual(theirs["opening"] - sum(a for _, a in theirs["entries"]),
                         theirs["closing"])

    def test_the_statement_row_records_the_bai2_message(self):
        collected = self.run_a_day()
        message_id = self.bai2_for(collected)["id"]
        rows = self.get("/_mock/accounts/ACME/statements").json()
        self.assertEqual([r["message_id"] for r in rows], [message_id])
        self.assertEqual(rows[0]["entries"], 2)


class WhichIdentifierThe03Carries(StatementCase):
    """Named by a test, because #113's reader accepts either and a reader that
    accepts either cannot tell anyone which one it got.

    The account number for an account that banks in NACHA, the IBAN otherwise.
    The field is the 03's "customer account number"; a NACHA account is named by
    its routing and account number everywhere else in this mock, its payment
    files carry that and no IBAN, and a US treasury system reading this has no
    use for one. The seed gives every account both, which is what made writing
    the IBAN unnoticeable rather than right.
    """

    def account_record(self, body):
        return [l for l in body.splitlines() if l.startswith("03,")][0]

    def test_a_nacha_account_is_named_by_its_account_number(self):
        body = self.bai2_for(self.run_a_day())["body"]
        account = self.get("/_mock/accounts/ACME").json()
        self.assertTrue(account["account_number"])
        self.assertEqual(self.account_record(body).split(",")[1],
                         account["account_number"])

    def test_it_is_not_the_iban(self):
        # The assertion that would have failed before this was decided.
        body = self.bai2_for(self.run_a_day())["body"]
        account = self.get("/_mock/accounts/ACME").json()
        self.assertNotIn(account["iban"], self.account_record(body))

    def test_an_iso20022_account_written_as_bai2_is_named_by_its_iban(self):
        # bai2.write_statement is not restricted to NACHA accounts, and an
        # account with no NACHA format has an IBAN as its identifier.
        from mockbank import bai2 as writer
        import datetime
        account = self.get("/_mock/accounts/GLOBEX").json()
        self.assertEqual(account["format"], "iso20022")
        body = writer.write_statement(
            account, datetime.date(2026, 10, 1), 1, 1000, 900, [],
            created_at=datetime.datetime(2026, 10, 2, tzinfo=datetime.timezone.utc))
        self.assertEqual(self.account_record(body).split(",")[1], account["iban"])

    def test_a_nacha_account_with_no_number_falls_back_to_the_iban(self):
        # Rather than writing an empty field, which would shift nothing but
        # would name the account as nothing.
        from mockbank import bai2 as writer
        import datetime
        account = dict(self.get("/_mock/accounts/GLOBEX").json(),
                       format="nacha", account_number="")
        body = writer.write_statement(
            account, datetime.date(2026, 10, 1), 1, 1000, 900, [],
            created_at=datetime.datetime(2026, 10, 2, tzinfo=datetime.timezone.utc))
        self.assertEqual(self.account_record(body).split(",")[1], account["iban"])


class NumberedFromTheSameCounter(StatementCase):
    """An account that changes format keeps counting.

    The counter is keyed on the camt.053 name it was created under, deliberately:
    a per-format counter would restart at 1 and collide with the statements the
    account had already been sent.
    """

    def test_a_format_change_does_not_restart_the_numbering(self):
        self.post("/_mock/reset")
        self.post("/payments", body=sample(TWIN))       # ACME still iso20022
        self.assertEqual(self.post("/_mock/advance?to=" + FRIDAY).status, 200)
        first = self.get("/_mock/accounts/ACME/statements").json()
        self.assertTrue(first)

        self.nacha()                                    # now NACHA
        self.assertEqual(self.post("/_mock/advance?days=1").status, 200)
        after = self.get("/_mock/accounts/ACME/statements").json()
        numbers = [r["number"] for r in after]
        self.assertEqual(numbers, sorted(set(numbers)), "a number repeated")
        self.assertGreater(len(after), len(first))

    def test_the_bai2_file_carries_the_statement_number(self):
        body = self.bai2_for(self.run_a_day())["body"]
        number = self.get("/_mock/accounts/ACME/statements").json()[0]["number"]
        header = body.splitlines()[0].rstrip("/").split(",")
        self.assertEqual(header[5], str(number))    # file identification number


class StatementGapInBai2(StatementCase):
    """Exactly as for camt.053: one detail out, the balances still true."""

    def test_one_detail_record_is_missing_and_the_balances_are_not(self):
        clean = bai2.statements(self.bai2_for(self.run_a_day())["body"])[0]
        gapped = bai2.statements(
            self.bai2_for(self.run_a_day(behaviour="statement-gap"))["body"])[0]

        self.assertEqual(len(gapped.entries), len(clean.entries) - 1)
        # The balances are the same as the clean run's: the gap is in what is
        # shown, not in what the account did.
        self.assertEqual((gapped.opening, gapped.closing),
                         (clean.opening, clean.closing))

    def test_the_balances_no_longer_reconcile_by_exactly_the_missing_entry(self):
        clean = bai2.statements(self.bai2_for(self.run_a_day())["body"])[0]
        gapped = bai2.statements(
            self.bai2_for(self.run_a_day(behaviour="statement-gap"))["body"])[0]
        missing = [e for e in clean.entries if e not in gapped.entries]
        self.assertEqual(len(missing), 1)
        short = gapped.opening - sum(e.amount for e in gapped.entries) - gapped.closing
        self.assertEqual(short, missing[0].amount)

    def test_the_trailers_still_agree_with_what_is_shown(self):
        # The gap must not make the file internally inconsistent: it is a bank
        # that left an entry off, not a bank that wrote a broken file.
        body = self.bai2_for(self.run_a_day(behaviour="statement-gap"))["body"]
        self.assertEqual(bai2.trailers_agree(body), [])


class TheMailboxAndThePickupDirectory(StatementCase):

    def test_the_mailbox_serves_it_as_text_not_xml(self):
        self.run_a_day()
        resp = self.get("/_mock/mailbox?leave&raw&type=" + bai2.STATEMENT)
        self.assertEqual(resp.status, 200)
        self.assertIn("text/plain", resp.headers.get("Content-Type", ""))
        self.assertTrue(resp.body.decode("utf-8").startswith("01,"))

    def test_it_can_be_filtered_out_by_type_like_any_other(self):
        self.run_a_day()
        only = self.get("/_mock/mailbox?leave&type=" + bai2.STATEMENT).json()
        self.assertEqual([m["type"] for m in only], [bai2.STATEMENT])

    def test_the_type_is_in_the_shared_map_of_what_is_not_xml(self):
        # The four callers ask "is this XML, and if not what does it end in".
        # They used to ask nacha.TEXT_TYPES, which was the wrong question once a
        # second text format existed.
        self.assertEqual(outbox.TEXT_TYPES[bai2.STATEMENT], "bai2")
        self.assertIn("nacha.ack", outbox.TEXT_TYPES)


class ThePickupDirectory(StatementCase):

    def setUp(self):
        self.pickup = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.pickup, ignore_errors=True)

    def test_it_is_written_with_its_own_extension(self):
        # A fresh mock, because the pickup directory is set at startup.
        from mockbank.server import Config
        from mockbank import server as server_module
        import threading
        httpd = server_module.make_server(Config(
            host="127.0.0.1", port=0, db_path=":memory:", quiet=True,
            clock=BANK_START, pickup_dir=self.pickup))
        self.addCleanup(httpd.server_close)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(httpd.shutdown)
        base = "http://127.0.0.1:%d" % httpd.server_address[1]

        import json
        import urllib.request
        def call(method, path, body=None, content_type="application/json"):
            data = body if isinstance(body, bytes) else (
                json.dumps(body).encode() if body is not None else None)
            req = urllib.request.Request(
                base + path, data=data, method=method,
                headers={"Content-Type": content_type} if data else {})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status

        self.assertEqual(call("PATCH", "/_mock/accounts/ACME",
                              {"format": "nacha", "currency": "USD"}), 200)
        call("POST", "/payments", sample(TWIN), "text/plain")
        call("POST", "/_mock/advance?to=" + FRIDAY)

        written = sorted(os.listdir(self.pickup))
        statements = [n for n in written if n.startswith(bai2.STATEMENT)]
        self.assertEqual(len(statements), 1, written)
        self.assertTrue(statements[0].endswith(".bai2"), statements[0])
        with open(os.path.join(self.pickup, statements[0]), encoding="utf-8") as f:
            body = f.read()
        self.assertEqual(bai2.trailers_agree(body), [])
        # The other accounts' statements are still XML beside it.
        self.assertTrue([n for n in written if n.endswith(".xml")], written)


if __name__ == "__main__":
    unittest.main(verbosity=2)
