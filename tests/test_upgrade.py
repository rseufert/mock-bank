"""A --db file from an earlier version: opened and upgraded in place, or refused.

`--db` exists so that a bank's accounts and balances survive a restart, which
means they have to survive an upgrade too. The failure this guards against is
the one that costs a morning: a mock that starts, binds its port, and then
answers every request with `no such column: parameters`.
"""
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

OLD_SCHEMA = os.path.join(HERE, "fixtures", "schema-v0.sql")


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
            " 'DE28999000000000000100', 'MOCKDEFFXXX', 'EUR', 9999, 'accept')")
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
        self.assertEqual(account["role"], "debtor")

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
            self.assertIn("account.role", added)
            self.assertEqual(db.upgrade(conn, self.db_path), [])
        finally:
            conn.close()

    def test_the_table_the_old_file_never_had_is_created(self):
        self.start()
        self.assertEqual(self.get("/_mock/state").json()["holidays"], 0)


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

    FINGERPRINT = (1, "6bc327ec18e14652")

    def test_a_changed_schema_has_a_new_version(self):
        text = " ".join("".join(db.SCHEMA + db.INDEXES).split())
        found = (db.SCHEMA_VERSION,
                 hashlib.sha256(text.encode()).hexdigest()[:16])
        self.assertEqual(found, self.FINGERPRINT,
                         "the schema changed: bump SCHEMA_VERSION, add a "
                         "fixture for the old one, and update FINGERPRINT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
