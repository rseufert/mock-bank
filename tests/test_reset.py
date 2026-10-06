"""`POST /_mock/reset` is a new bank, its row ids included (#159).

`State.reset` empties every table and reseeds, and it clears the `counter`
table so that a statement number and a `camt.054` `MsgId` start again at 1.
What it did not clear was SQLite's `AUTOINCREMENT` sequences, which a `DELETE`
leaves where they are. So the same file sent after a reset came back with
payment ids 5-8 instead of 1-4, message ids 3-4 instead of 1-2,
`MB-P002-000002` instead of `MB-P002-000001` - the `pain.002`'s `MsgId` is
built from the file row's id - and pickup file names ending `-3` and `-4`,
because a released message is written as `<type>-<account>-<id>`. A test that
resets between cases and asserts on any of those passed alone and failed in a
suite.

Asked of both kinds of bank, because the sequences live in the database: a
throwaway `:memory:` one, and a `--db` file - where a *restart* has to keep
carrying on, since that is the difference between a new bank and the old one
picking up where it left off, and the fix must not take it away. Two files
sent without a reset between them must not share ids either, which is the
other way to get this wrong.
"""
import datetime
import os
import re
import shutil
import tempfile

from test_payments import sample

from support import FileDatabaseCase, MockServerCase

# The clock the sample asks to be executed on, so the payments settle at once
# and both messages are released and written to the pickup directory. Pinned,
# because these tests compare one run with another and a day boundary in the
# middle would be a different file.
DAY = datetime.date(2026, 10, 1)
CLOCK = "2026-10-01T09:00"


class SameInputTwice:
    """Send the shipped file and read back everything built from a row id."""

    def fingerprint(self):
        answer = self.post("/payments", body=sample("pain001_four_payments.xml",
                                                    when=DAY))
        self.assertEqual(answer.status, 202, answer.body)
        payments = sorted(row["id"] for row in self.get("/_mock/payments").json())
        messages = self.get("/_mock/mailbox").json()
        return {
            "payment ids": payments,
            "message ids": sorted(message["id"] for message in messages),
            # Only the bank's own `MsgId`s: the file's are the client's, and
            # they are echoed rather than numbered.
            "MsgIds": sorted(re.findall(
                r"<MsgId>(MB-[^<]+)</MsgId>",
                "".join(message.get("body") or "" for message in messages))),
            "pickup files": sorted(os.listdir(self.pickup)),
        }

    def test_the_same_file_after_a_reset_gives_what_a_fresh_bank_gives(self):
        fresh = self.fingerprint()
        # Everything here has to be non-empty, or two empty answers would agree
        # and this test would pass against a bank that did nothing.
        self.assertEqual(fresh["payment ids"], [1, 2, 3, 4])
        self.assertEqual(fresh["message ids"], [1, 2])
        self.assertEqual(fresh["MsgIds"], ["MB-C054-ACME-1", "MB-P002-000001"])
        self.assertEqual(fresh["pickup files"],
                         ["camt.054.001.08-ACME-2.xml", "pain.002.001.10-ACME-1.xml"])

        self.assertEqual(self.post("/_mock/reset").status, 200)
        self.clear_pickup()
        self.assertEqual(self.fingerprint(), fresh)

    def test_two_files_with_no_reset_between_them_share_nothing(self):
        self.fingerprint()
        # A second file: the bank refuses an `MsgId` it has already seen.
        second = self.post("/payments", body=sample("pain001_four_payments.xml",
                                                    when=DAY)
                           .replace("ACME-20261001-0001", "ACME-20261001-0002"))
        self.assertEqual(second.status, 202, second.body)
        # `/_mock/messages` rather than the mailbox, which has collected the
        # first file's two already.
        messages = self.get("/_mock/messages").json()
        ids = sorted(row["id"] for row in self.get("/_mock/payments").json())
        msgids = re.findall(r"<MsgId>(MB-P002-\d+)</MsgId>",
                            "".join(m.get("body") or "" for m in messages))
        self.assertEqual(ids, [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(sorted(m["id"] for m in messages), [1, 2, 3, 4])
        self.assertEqual(sorted(msgids), ["MB-P002-000001", "MB-P002-000002"])

    def clear_pickup(self):
        for name in os.listdir(self.pickup):
            os.unlink(os.path.join(self.pickup, name))


class OnAThrowawayBank(SameInputTwice, MockServerCase):
    """`:memory:`, which is what every other test in the suite runs on."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp(prefix="mock-bank-reset-")
        cls.drop = os.path.join(cls.directory, "in")
        cls.pickup = os.path.join(cls.directory, "out")
        cls.config_kwargs = dict(cls.config_kwargs, clock=CLOCK,
                                 drop_dir=cls.drop, pickup_dir=cls.pickup,
                                 drop_settle_ms=0)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self):
        self.post("/_mock/reset")
        self.clear_pickup()


class OnADatabaseFile(SameInputTwice, FileDatabaseCase):
    """A `--db` file, where the sequences are in the file and outlive a restart."""

    start_on_setup = False

    def setUp(self):
        super().setUp()
        self.drop = os.path.join(self.directory, "in")
        self.pickup = os.path.join(self.directory, "out")
        self.config_kwargs = dict(self.config_kwargs, clock=CLOCK,
                                  drop_dir=self.drop, pickup_dir=self.pickup,
                                  drop_settle_ms=0)
        self.start()

    def test_a_restart_carries_on_where_the_old_mock_left_off(self):
        self.fingerprint()
        self.clear_pickup()
        self.restart()
        # Not a new bank: the same file would be `DUPL`, so this sends another
        # one and asks what ids it got. A restart that started again at 1 would
        # be the fix for a reset applied where it does not belong.
        again = self.post("/payments", body=sample("pain001_four_payments.xml",
                                                   when=DAY)
                          .replace("ACME-20261001-0001", "ACME-20261001-0002"))
        self.assertEqual(again.status, 202, again.body)
        ids = sorted(row["id"] for row in self.get("/_mock/payments").json())
        self.assertEqual(ids, [1, 2, 3, 4, 5, 6, 7, 8])
