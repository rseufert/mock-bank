"""A --db file from an earlier version: opened and upgraded in place, or refused.

`--db` exists so that a bank's accounts and balances survive a restart, which
means they have to survive an upgrade too. The failure this guards against is
the one that costs a morning: a mock that starts, binds its port, and then
answers every request with `no such column: parameters`.
"""
import datetime
import hashlib
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockbank import db                                    # noqa: E402
from mockbank.server import Config, make_server            # noqa: E402

from support import FileDatabaseCase                        # noqa: E402
from test_collections_read import pain008                   # noqa: E402
from test_payments import UMBRELLA, pain001                 # noqa: E402

OLD_SCHEMA = os.path.join(HERE, "fixtures", "schema-v0.sql")
SCHEMA_V1 = os.path.join(HERE, "fixtures", "schema-v1.sql")
SCHEMA_V2 = os.path.join(HERE, "fixtures", "schema-v2.sql")
SCHEMA_V3 = os.path.join(HERE, "fixtures", "schema-v3.sql")
SCHEMA_V4 = os.path.join(HERE, "fixtures", "schema-v4.sql")
SCHEMA_V5 = os.path.join(HERE, "fixtures", "schema-v5.sql")
SCHEMA_V6 = os.path.join(HERE, "fixtures", "schema-v6.sql")
SCHEMA_V7 = os.path.join(HERE, "fixtures", "schema-v7.sql")
SCHEMA_V8 = os.path.join(HERE, "fixtures", "schema-v8.sql")
SCHEMA_V9 = os.path.join(HERE, "fixtures", "schema-v9.sql")


class FromAnOlderFile(FileDatabaseCase):
    """A file with the pre-version schema, and an account already on it."""

    start_on_setup = False

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        with open(OLD_SCHEMA, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        # A balance somebody moved, in minor units, on the old columns only.
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance,"
            " behaviour) VALUES ('ACME', 'Acme Distribution GmbH',"
            " 'NL41MOCK0000000001', 'MOCKDEFFXXX', 'EUR', 9999, 'accept')")
        conn.commit()
        conn.close()

    def test_the_balance_it_held_is_still_there(self):
        self.start()
        account = self.get("/_mock/accounts/ACME").json()
        self.assertEqual(account["balance"], 9999)

    def test_the_new_columns_take_the_defaults_the_schema_declares(self):
        self.start()
        account = self.get("/_mock/accounts/ACME").json()
        self.assertEqual(account["parameters"], {})
        self.assertIs(account["closed"], False)

    def test_the_seed_does_not_overwrite_a_file_that_has_been_used(self):
        self.start()
        listed = self.get("/_mock/accounts").json()
        self.assertEqual([row["id"] for row in listed], ["ACME"])

    def test_it_is_marked_with_the_current_version(self):
        self.start()
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)

    def test_the_upgrade_says_what_it_added_and_is_done_once(self):
        conn = sqlite3.connect(self.db_path)
        try:
            added = db.upgrade(conn, self.db_path)
            self.assertIn("account.parameters", added)
            self.assertIn("account.closed", added)
            self.assertEqual(db.upgrade(conn, self.db_path), [])
        finally:
            conn.close()

    def test_the_table_the_old_file_never_had_is_created(self):
        self.start()
        self.assertEqual(self.get("/_mock/state").json()["holidays"], 0)


class FromVersionOne(FileDatabaseCase):
    """A file written before the bank kept payments: it gains the tables, and
    what it held is still there and still works."""

    start_on_setup = False

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        with open(SCHEMA_V1, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance, behaviour)"
            " VALUES ('ACME', 'ACME Corporation', 'NL41MOCK0000000001', 'MOCKNL2A',"
            " 'EUR', 424242, 'accept')")
        conn.commit()
        conn.close()

    def test_it_gains_the_payment_tables_and_keeps_the_account(self):
        self.start()
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)
        state = self.get("/_mock/state").json()
        self.assertEqual(state["payments"], {"files": 0, "accepted": 0, "rejected": 0,
                                             "returned": 0, "booked": 0})
        self.assertEqual(self.get("/_mock/accounts/ACME").json()["balance"], 424242)
        self.assertEqual(self.get("/_mock/payments").json(), [])


class FromVersionTwo(FileDatabaseCase):
    """A file written before the bank queued messages: it gains the message
    table, and a payment it held is still there."""

    start_on_setup = False

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        with open(SCHEMA_V2, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance, behaviour)"
            " VALUES ('ACME', 'ACME Corporation', 'NL41MOCK0000000001', 'MOCKNL2A',"
            " 'EUR', 1000, 'accept')")
        conn.execute("INSERT INTO file (msg_id, message, received_at, status)"
                     " VALUES ('OLD-1', 'pain.001.001.09', '2026-09-01T09:00:00Z', 'ACCP')")
        conn.execute("INSERT INTO payment (file_id, end_to_end_id, account_id, amount,"
                     " currency, status, settlement_date, booked_at) VALUES (1, 'E2E-OLD',"
                     " 'ACME', 500, 'EUR', 'accepted', '2026-09-01', '2026-09-01T09:00:00Z')")
        conn.commit()
        conn.close()

    def test_it_gains_the_message_table_and_keeps_its_payments(self):
        self.start()
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)
        self.assertEqual(self.get("/_mock/payments/E2E-OLD").json()["amount"], 500)
        self.assertEqual(self.get("/_mock/state").json()["messages"],
                         {"queued": 0, "waiting": 0, "taken": 0})
        self.assertEqual(self.get("/_mock/mailbox").json(), [])


class FromVersionThree(FileDatabaseCase):
    """A file written before the bank issued statements: it gains the
    statement and counter tables, and a message it held is still there."""

    start_on_setup = False

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        with open(SCHEMA_V3, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance, behaviour)"
            " VALUES ('ACME', 'ACME Corporation', 'NL41MOCK0000000001', 'MOCKNL2A',"
            " 'EUR', 1000, 'accept')")
        conn.execute("INSERT INTO message (type, account, due_at, released_at, body)"
                     " VALUES ('pain.002.001.10', 'ACME', '2026-09-01T09:00:00Z',"
                     " '2026-09-01T09:00:00Z', '<Document/>')")
        conn.commit()
        conn.close()

    def test_it_gains_the_statement_tables_and_keeps_its_messages(self):
        self.start()
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)
        self.assertEqual(self.get("/_mock/accounts/ACME/statements").json(), [])
        self.assertEqual([m["type"] for m in self.get("/_mock/mailbox").json()],
                         ["pain.002.001.10"])


class FromVersionFour(FileDatabaseCase):
    """A 0.1.0 file, before returns: its payments gain the return columns,
    empty, and are still there."""

    start_on_setup = False

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        with open(SCHEMA_V4, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance, behaviour)"
            " VALUES ('ACME', 'ACME Corporation', 'NL41MOCK0000000001', 'MOCKNL2A',"
            " 'EUR', 1000, 'accept')")
        conn.execute("INSERT INTO file (msg_id, message, received_at, status)"
                     " VALUES ('OLD-4', 'pain.001.001.09', '2026-09-01T09:00:00Z', 'ACCP')")
        conn.execute("INSERT INTO payment (file_id, end_to_end_id, account_id, amount,"
                     " currency, status, settlement_date, booked_at) VALUES (1, 'E2E-V4',"
                     " 'ACME', 700, 'EUR', 'accepted', '2026-09-01', '2026-09-01T09:00:00Z')")
        conn.commit()
        conn.close()

    def test_its_payments_gain_the_return_columns_and_survive(self):
        self.start()
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)
        payment = self.get("/_mock/payments/E2E-V4").json()
        self.assertEqual((payment["amount"], payment["status"]), (700, "accepted"))
        self.assertEqual((payment["return_due"], payment["return_reason"],
                          payment["returned_at"]), (None, None, None))


class FromVersionFive(FileDatabaseCase):
    """A file written before the mock recorded what it had put in the pickup
    directory: it gains the column, and a message it had already delivered is
    not delivered again."""

    start_on_setup = False

    def setUp(self):
        super().setUp()
        self.pickup = os.path.join(os.path.dirname(self.db_path), "out")
        os.makedirs(self.pickup, exist_ok=True)
        self.config_kwargs = dict(self.config_kwargs, pickup_dir=self.pickup)
        conn = sqlite3.connect(self.db_path)
        with open(SCHEMA_V5, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance, behaviour)"
            " VALUES ('ACME', 'ACME Corporation', 'NL41MOCK0000000001', 'MOCKNL2A',"
            " 'EUR', 1000, 'accept')")
        # Released and collected, before written_at existed.
        conn.execute("INSERT INTO message (type, account, due_at, released_at,"
                     " taken_at, body) VALUES ('pain.002.001.10', 'ACME',"
                     " '2026-09-01T09:00:00Z', '2026-09-01T09:00:00Z',"
                     " '2026-09-01T09:05:00Z', '<Document/>')")
        conn.commit()
        conn.close()

    def test_it_gains_the_column_and_delivers_the_old_message_once(self):
        # An upgraded row has written_at NULL, so the message *is* written once
        # after the upgrade - the mock cannot know whether a file it has no
        # record of is still in the directory, and writing it once is the safe
        # side of that. What must not happen is writing it again on every
        # release, which is what the in-memory set did after any restart.
        self.start()
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)
        # Nothing is written until something releases; the first advance does.
        self.request("POST", "/_mock/advance?days=1")
        self.assertEqual(self.pain002_files(), ["pain.002.001.10-ACME-1.xml"])
        # And then never again, which is the whole point: the in-memory set
        # rewrote every released message on every restart. The directory does
        # keep growing - each advance closes another business day and writes
        # its camt.053 - so the claim is about *this* message, not the count.
        for _ in range(3):
            self.request("POST", "/_mock/advance?days=1")
        self.assertEqual(self.pain002_files(), ["pain.002.001.10-ACME-1.xml"])

    def pain002_files(self):
        return sorted(name for name in os.listdir(self.pickup)
                      if name.startswith("pain.002"))


class FromVersionSix(FileDatabaseCase):
    """A file written before an account had a format or a domestic account
    number (#53): its accounts gain both, as ISO 20022 accounts with no
    number, which is what they were."""

    start_on_setup = False

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        with open(SCHEMA_V6, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        for identifier, iban in (("ACME", "NL41MOCK0000000001"),
                                 ("GLOBEX", "NL14MOCK0000000002")):
            conn.execute(
                "INSERT INTO account (id, name, iban, bic, currency, balance, behaviour)"
                " VALUES (?, ?, ?, 'MOCKNL2A', 'EUR', 1000, 'accept')",
                (identifier, identifier, iban))
        conn.commit()
        conn.close()

    def test_its_accounts_gain_a_format_and_no_number(self):
        self.start()
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)
        found = {a["id"]: (a["format"], a["account_number"], a["balance"])
                 for a in self.request("GET", "/_mock/accounts").json()}
        # No number is invented for an account the seed did not make: a NACHA
        # file cannot name it until one is set, and then only that one.
        self.assertEqual(found, {"ACME": ("iso20022", "", 1000),
                                 "GLOBEX": ("iso20022", "", 1000)})
        for identifier, number in (("ACME", "1234"), ("GLOBEX", "")):
            resp = self.request("PATCH", "/_mock/accounts/" + identifier,
                                body={"account_number": number})
            self.assertEqual(resp.status, 200, resp.body)
        clash = self.request("PATCH", "/_mock/accounts/GLOBEX",
                             body={"account_number": "1234"})
        self.assertEqual(clash.status, 400)


class FromVersionSeven(FileDatabaseCase):
    """A file from 0.3, before money could arrive from somebody else (#91): it
    gains the credit table, keeps its NACHA account as it was, and takes a
    credit that books on the clock."""

    start_on_setup = False
    # A Wednesday. The last test books a credit for today and expects it on the
    # balance a day later, which on the real clock failed every Saturday: the
    # credit waits for Monday and a day's advance only reaches Sunday (#148).
    config_kwargs = {"clock": "2026-09-30T09:00"}

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        with open(SCHEMA_V7, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance, behaviour,"
            " format, account_number) VALUES ('ACME', 'ACME', 'NL41MOCK0000000001',"
            " 'MOCKNL2A', 'EUR', 1000, 'accept', 'nacha', '0000000001')")
        conn.execute("INSERT INTO file (id, msg_id, message, received_at, status)"
                     " VALUES (1, 'MSG-7', 'nacha', '2026-10-01T09:00:00Z', 'ACCP')")
        conn.execute("INSERT INTO payment (file_id, status, end_to_end_id, amount,"
                     " transaction_code) VALUES (1, 'accepted', 'INV-7', 250, '22')")
        conn.commit()
        conn.close()

    def test_it_gains_the_credit_table_and_keeps_its_account(self):
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertNotIn("credit", {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")})
        finally:
            conn.close()
        self.start()
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)
        account = self.get("/_mock/accounts/ACME").json()
        self.assertEqual((account["format"], account["account_number"], account["balance"]),
                         ("nacha", "0000000001", 1000))
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute(
                "SELECT file.msg_id, payment.end_to_end_id, payment.amount,"
                " payment.transaction_code FROM payment JOIN file"
                " ON file.id = payment.file_id").fetchall(),
                [("MSG-7", "INV-7", 250, "22")])
        finally:
            conn.close()
        today = self.get("/_mock/state").json()["clock"]["date"]
        resp = self.request("POST", "/_mock/credits", body={
            "account": "ACME", "amount": 500, "value_date": today,
            "debtor": {"name": "Customer Ltd", "iban": "NL14MOCK0000000002"}})
        self.assertEqual(resp.status, 201, resp.body)
        self.request("POST", "/_mock/advance?days=1")
        self.assertEqual(self.get("/_mock/accounts/ACME").json()["balance"], 1500)


class FromVersionEight(FileDatabaseCase):
    """A file from 0.4 or 0.5, before a direct debit could be collected (#131):
    it gains the collection table, keeps the credit it held, and takes a
    `pain.008`."""

    start_on_setup = False
    config_kwargs = {"clock": "2026-01-02T09:00"}

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        with open(SCHEMA_V8, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance, behaviour)"
            " VALUES ('ACME', 'ACME', 'NL41MOCK0000000001', 'MOCKNL2A', 'EUR', 1000,"
            " 'accept')")
        conn.execute(
            "INSERT INTO credit (account_id, amount, currency, value_date, booking_date,"
            " received_at, booked_at) VALUES ('ACME', 500, 'EUR', '2025-12-30',"
            " '2025-12-30', '2025-12-30T09:00:00Z', '2025-12-30T09:00:00Z')")
        conn.commit()
        conn.close()

    def test_it_gains_the_collection_table_and_keeps_its_credit(self):
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertNotIn("collection", {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")})
        finally:
            conn.close()
        self.start()
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)
        self.assertEqual(self.get("/_mock/accounts/ACME").json()["balance"], 1000)
        self.assertEqual([c["amount"] for c in self.get("/_mock/credits").json()], [500])
        self.assertEqual(self.get("/_mock/collections").json(), [])
        answer = self.post("/payments", body=pain008([("C1", 1000)]))
        self.assertEqual((answer.status, answer.json()["status"]), (202, "ACCP"),
                         answer.body)
        [kept] = self.get("/_mock/collections").json()
        self.assertEqual((kept["end_to_end_id"], kept["status"], kept["mandate_id"]),
                         ("C1", "accepted", "M-C1"))


class FromVersionNine(FileDatabaseCase):
    """A file from before a message kept its key (#155): the message it holds
    answers to `m<id>`, and the next one the bank writes has a key of its own."""

    start_on_setup = False
    config_kwargs = {"clock": "2026-10-01T09:00"}

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        with open(SCHEMA_V9, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance, behaviour)"
            " VALUES ('ACME', 'ACME', 'NL41MOCK0000000001', 'MOCKNL2A', 'EUR', 100000,"
            " 'accept')")
        conn.execute(
            "INSERT INTO message (id, type, account, due_at, released_at, body)"
            " VALUES (7, 'camt.054.001.08', 'ACME', '2026-09-30T09:00:00Z',"
            " '2026-09-30T09:00:00Z', '<Document/>')")
        conn.commit()
        conn.close()

    def test_an_old_message_answers_to_its_id_and_a_new_one_to_its_key(self):
        self.start()
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)
        [old] = self.get("/_mock/mailbox?type=camt.054").json()
        self.assertEqual((old["id"], old["key"]), (7, "m7"))
        self.post("/payments", body=pain001("N-1", "NL41MOCK0000000001",
                                            [("P1", 1000, UMBRELLA)],
                                            when=datetime.date(2026, 10, 6)))
        [coming] = self.get("/_mock/queue").json()
        self.assertEqual(coming["key"],
                         "camt.054.001.08/ACME/2026-10-06/payments-settling")
        self.post("/_mock/advance?to=2026-10-06")
        [new] = self.get("/_mock/mailbox?type=camt.054").json()
        self.assertEqual(new["key"], coming["key"])


class FromANewerMock(FileDatabaseCase):
    """A file from a version that knows more than this one: refused, untouched."""

    start_on_setup = False

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version = %d" % (db.SCHEMA_VERSION + 1))
        conn.commit()
        conn.close()

    def test_it_is_refused_with_the_file_and_both_versions(self):
        with self.assertRaises(db.DatabaseError) as caught:
            make_server(Config(host="127.0.0.1", port=0, db_path=self.db_path,
                               quiet=True))
        message = str(caught.exception)
        self.assertIn(self.db_path, message)
        self.assertIn("version %d" % (db.SCHEMA_VERSION + 1), message)
        self.assertIn("knows %d" % db.SCHEMA_VERSION, message)
        # And nothing was changed on the way to refusing it. A mock that
        # "upgraded" a file it does not understand has destroyed it.
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION + 1)


class TheVersionMovesWithTheSchema(unittest.TestCase):
    """The one test that fails on purpose.

    When it does, the schema has changed: bump `db.SCHEMA_VERSION`, keep the
    old schema as a fixture beside `schema-v0.sql`, and record the new pair
    here. A file written by the new schema must not look, to an older mock,
    like one it already understands.
    """

    FINGERPRINT = (10, "5aed922072cbd3e1")

    def test_a_changed_schema_has_a_new_version(self):
        text = " ".join("".join(db.SCHEMA + db.INDEXES).split())
        found = (db.SCHEMA_VERSION,
                 hashlib.sha256(text.encode()).hexdigest()[:16])
        self.assertEqual(found, self.FINGERPRINT,
                         "the schema changed: bump SCHEMA_VERSION, add a "
                         "fixture for the old one, and update FINGERPRINT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
