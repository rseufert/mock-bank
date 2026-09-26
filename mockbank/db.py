"""SQLite: the schema, the upgrade, and the seeded accounts.

The mock is a *bank*, so its state is the state a bank keeps: the accounts it
holds, the days it does not settle on, a log of what was asked of it, and the
payment files it received with the payments it decided on. The messages it
wrote and the mailbox they wait in arrive with the issues that need them.

Three things here are worth knowing:

**Balances are integers in minor units.** 12.50 EUR is ``1250``, and there is
no ``float`` anywhere in this module. A balance that is a cent out is exactly
the bug a reconciliation test exists to catch, and binary floating point
produces one for free.

**The schema is the upgrade.** ``upgrade()`` is derived from ``SCHEMA`` rather
than written by hand: a table the file lacks is created and a column it lacks
is added with the default the schema declares. ``--db`` exists so that a bank's
accounts and balances survive a restart, which means surviving an upgrade too.

**The seed is fixed.** There is no ``--seed``: four accounts, the same four
every time, so a test may assert on a balance. Their IBANs are built from their
BBANs with real mod-97 check digits by ``iban()`` rather than typed in, because
an account whose IBAN fails its own checksum could never be matched by an
arriving payment, and a mock whose demo data would not survive the check it
applies to yours is not much of a test.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from . import schema

# Every table, one CREATE per entry. The tables not here yet are named so that
# a reader does not go looking for them:
#
#   message   the writers (#7); the mailbox reads it and is built on it
#             (#8). Whoever lands first owns the statement; the other adds
#             to it.
SCHEMA = [
    # An account the mock knows about: an IBAN, a balance and a behaviour.
    # There is deliberately no debtor/creditor column. Which side an account
    # stands on is a property of a payment, not of the account - the sample
    # file pays GLOBEX, and a test that wants insufficient funds sends *from*
    # GLOBEX - so a column naming one of the two would be wrong half the time.
    """
    CREATE TABLE IF NOT EXISTS account (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL DEFAULT '',
        iban        TEXT NOT NULL DEFAULT '',
        bic         TEXT NOT NULL DEFAULT '',
        currency    TEXT NOT NULL DEFAULT 'EUR',
        -- Minor units, always: 1250 is 12.50 EUR. Never a float.
        balance     INTEGER NOT NULL DEFAULT 0,
        behaviour   TEXT NOT NULL DEFAULT 'accept',
        -- What the behaviour needs, as a JSON object: `return-later` reads
        -- {"days": 3} from it. An object rather than columns, because each
        -- behaviour wants different parameters and most want none.
        parameters  TEXT NOT NULL DEFAULT '{}',
        closed      INTEGER NOT NULL DEFAULT 0
    )
    """,
    # A day the bank does not settle on, `YYYY-MM-DD` in bank time. The clock
    # (#4) reads it; `GET/PUT /_mock/holidays` fills it.
    """
    CREATE TABLE IF NOT EXISTS holiday (
        day     TEXT PRIMARY KEY
    )
    """,
    # What was asked of the mock, so a tester can see what their client
    # actually sent rather than what they believe it sent. `GET /_mock/requests`
    # (#8) serves it.
    """
    CREATE TABLE IF NOT EXISTS request_log (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        method  TEXT NOT NULL,
        path    TEXT NOT NULL,
        status  INTEGER NOT NULL,
        at      TEXT NOT NULL
    )
    """,
    # A payment file the bank received and could read far enough to have a
    # MsgId, whatever became of it. The duplicate check reads `msg_id`: a
    # MsgId the bank has seen before is DUPL, even if that first file was
    # rejected, because that is what a bank's duplicate check does.
    """
    CREATE TABLE IF NOT EXISTS file (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id       TEXT NOT NULL,
        message      TEXT NOT NULL,
        received_at  TEXT NOT NULL,
        -- ACCP, PART or RJCT, and for RJCT the group-level reason
        status       TEXT NOT NULL,
        reason       TEXT,
        -- 0 for a `silent` debtor: decided and booked, never reported (#7)
        reported     INTEGER NOT NULL DEFAULT 1
    )
    """,
    # One credit transfer the bank decided on. Amounts in minor units. A
    # rejected payment is kept too, because the pain.002 reports it. An
    # accepted one debits its debtor account on `settlement_date`; `booked_at`
    # is when that happened, NULL until then.
    """
    CREATE TABLE IF NOT EXISTS payment (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id          INTEGER NOT NULL REFERENCES file (id),
        pmt_inf_id       TEXT,
        end_to_end_id    TEXT,
        instruction_id   TEXT,
        -- the held debtor account's id, NULL when the bank does not hold it
        account_id       TEXT,
        debtor_iban      TEXT,
        amount           INTEGER,
        currency         TEXT,
        creditor_name    TEXT,
        creditor_iban    TEXT,
        creditor_bic     TEXT,
        -- accepted or rejected, and for rejected the ISO 20022 reason code
        status           TEXT NOT NULL,
        reason           TEXT,
        reason_text      TEXT,
        settlement_date  TEXT,
        booked_at        TEXT
    )
    """,
]

# Created after the tables have been brought up to date: an index on a column
# an older file does not have yet is what stops that file from opening at all.
INDEXES = [
    # An arriving payment names its accounts by IBAN, never by the mock's own
    # id, so that is the lookup that has to be fast. Unique because two
    # accounts with one IBAN would leave the pipeline picking one arbitrarily.
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_account_iban ON account (iban)",
    # The request log is read newest-first and pruned oldest-first.
    "CREATE INDEX IF NOT EXISTS ix_request_log_at ON request_log (at)",
    # The duplicate check, and a tester looking a payment up by the id their
    # client matches on.
    "CREATE INDEX IF NOT EXISTS ix_file_msg_id ON file (msg_id)",
    "CREATE INDEX IF NOT EXISTS ix_payment_end_to_end_id ON payment (end_to_end_id)",
    # What is waiting to book, per account: the insufficient-funds check and
    # the clock both ask it.
    "CREATE INDEX IF NOT EXISTS ix_payment_due ON payment (account_id, booked_at)",
]

# The schema's version, kept in the file as `PRAGMA user_version`. Bump it
# whenever SCHEMA or INDEXES changes, so that a file written by a newer mock is
# refused rather than misread; `tests/test_upgrade.py` fails until you do.
# 0 is any file written before the version was recorded.
SCHEMA_VERSION = 2


class DatabaseError(Exception):
    """A ``--db`` file this version of the mock cannot use, and why."""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """A connection, created or upgraded, ready for the server to use."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        if path != ":memory:":
            # A bank left running on a file database is read while it is
            # written; WAL is what makes that not block.
            conn.execute("PRAGMA journal_mode = WAL")
        upgrade(conn, path)
    except BaseException:
        conn.close()
        raise
    return conn


def upgrade(conn: sqlite3.Connection, path: str = "") -> List[str]:
    """Bring a database from an earlier version up to this one, in place.

    Derived from ``SCHEMA`` rather than written by hand: missing tables are
    created, and every column the schema declares that a table lacks is added
    with the default it declares. That covers every change the schema has had
    so far, which have all been additions. A change that is not - a column
    renamed or retyped - needs a step of its own here, keyed on the version it
    upgrades from.

    Returns the columns it added, as ``table.column``.
    """
    found = conn.execute("PRAGMA user_version").fetchone()[0]
    if found > SCHEMA_VERSION:
        raise DatabaseError(
            "%s was written by a newer mock-bank (schema version %d; this one "
            "knows %d). Upgrade mock-bank, or start from a new --db file."
            % (path or "the database", found, SCHEMA_VERSION))
    conn.executescript(";\n".join(SCHEMA))
    added = _add_missing_columns(conn)
    conn.executescript(";\n".join(INDEXES))
    conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    conn.commit()
    return added


def _add_missing_columns(conn: sqlite3.Connection) -> List[str]:
    """Every column SCHEMA declares that the file does not have yet."""
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(";\n".join(SCHEMA))
        tables = [row[0] for row in reference.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%'")]
        added = []
        for table in tables:
            have = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
            for _cid, name, kind, notnull, default, key in reference.execute(
                    "PRAGMA table_info(%s)" % table):
                if name in have:
                    continue
                if key:
                    raise DatabaseError(
                        "%s.%s is part of a primary key and cannot be added to "
                        "an existing table" % (table, name))
                declaration = '"%s" %s' % (name, kind)
                if notnull:
                    # SQLite will not add a NOT NULL column without a default;
                    # the rows already there get an empty one.
                    if default is None:
                        default = "0" if "INT" in kind.upper() else "''"
                    declaration += " NOT NULL DEFAULT %s" % default
                elif default is not None:
                    declaration += " DEFAULT %s" % default
                conn.execute("ALTER TABLE %s ADD COLUMN %s" % (table, declaration))
                added.append("%s.%s" % (table, name))
        return added
    finally:
        reference.close()


# ---------------------------------------------------------------------------
# One clock, and one way of writing it down
# ---------------------------------------------------------------------------
#
# The timestamps here are the *control plane's*: when a request arrived, when a
# message was released. They are UTC with second precision and a trailing `Z`,
# so that two of them sort the way the moments they name do - which matters,
# because the mailbox will compare them as strings. Bank time is a different
# matter: it has a time zone of its own, it can be moved by a test, and it
# belongs to the clock (#4). Nothing in this module reads it.

def utcnow() -> datetime.datetime:
    """The current moment, aware and in UTC."""
    return datetime.datetime.now(datetime.timezone.utc)


def stamp(moment: Optional[datetime.datetime] = None) -> str:
    """One timestamp format for everything the control plane reports.

    A naive value is read as local time, because that is what the host meant
    by it.
    """
    moment = moment or utcnow()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return (moment.astimezone(datetime.timezone.utc)
            .replace(microsecond=0, tzinfo=None).isoformat() + "Z")


def now() -> str:
    """The current moment, as the control plane writes it."""
    return stamp()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def rows(conn: sqlite3.Connection, sql: str,
         params: Sequence = ()) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def one(conn: sqlite3.Connection, sql: str,
        params: Sequence = ()) -> Optional[Dict[str, Any]]:
    row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def count(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM %s%s"
        % (table, " WHERE " + where if where else "")).fetchone()["n"])


# ---------------------------------------------------------------------------
# IBANs
# ---------------------------------------------------------------------------
#
# The seed builds its IBANs here; the check-digit rule itself is ISO 13616's
# and lives with the other identifier rules in `schema`, so the seed, the
# accounts endpoint and the payment validator share one copy.

def iban(country: str, bban: str) -> str:
    """An IBAN, from a country code and a BBAN, with its mod-97 check digits.

    ISO 13616: move the country code and two zeros to the end, read every
    letter as its position in base 36, and the check digits are 98 minus that
    number modulo 97.
    """
    country, bban = country.upper(), bban.upper()
    return "%s%02d%s" % (country, 98 - schema.mod97(bban + country + "00"), bban)


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------
#
# Four accounts, named after the same fictional companies mock-edi trades
# with, so that a tester who knows one mock recognises the other. Each one
# exists to make a failure reachable without a code change:
#
#   ACME      the debtor everything works from
#   GLOBEX    the debtor with nearly nothing on it
#   INITECH   the creditor whose account is closed
#   EURODIS   the creditor at a bank that does not exist
#
# These are the accounts `tests/samples/pain001_four_payments.xml` names, and
# they are the same accounts on purpose: the sample is the file the demo tour
# sends and the pipeline is tested on, so a seed that used different IBANs would
# make the project's own sample bounce off its own bank for an unregistered
# debtor. The sample is the wire, so the wire wins.
#
# IBANs are built, not typed: `iban()` puts the check digits on. `MOCK` where a
# Dutch BBAN carries a bank code is not an assigned one, so these belong to
# nobody, and `MOCKNL2A` is the mock's own BIC as the sample writes it.
#
# `NL30MOCK0000000005` - the sample's fifth party, Umbrella Logistics - is left
# out deliberately. A creditor at another bank is not an account this bank
# holds, and a payment to one is ordinary: it settles. Seeding every party the
# sample names would hide that case.
SEED = [
    # id, name, country, bban, bic, currency, balance (minor), behaviour, closed
    ("ACME", "ACME Corporation", "NL", "MOCK0000000001",
     "MOCKNL2A", "EUR", 12500000, "accept", 0),
    ("GLOBEX", "Globex Supplies B.V.", "NL", "MOCK0000000002",
     "MOCKNL2A", "EUR", 1250, "insufficient-funds", 0),
    # Held at the mock itself, which is the only way a bank can reject a
    # payment for AC04 when it arrives: an account at somebody else's bank is
    # not known to be closed until the payment comes back as a return.
    ("INITECH", "Initech Services N.V.", "NL", "MOCK0000000003",
     "MOCKNL2A", "EUR", 0, "closed-account", 1),
    # A BIC of the right shape that resolves to nothing, which is what
    # `bad-bank-id` is about: the file is well formed and the bank is not there.
    ("EURODIS", "Eurodis Handels GmbH", "NL", "MOCK0000000004",
     "ZZZZNL2AXXX", "EUR", 0, "bad-bank-id", 0),
]


def seed(conn: sqlite3.Connection) -> None:
    """The four accounts, unless the database already holds some.

    Fixed rather than random, and unconditional on an empty database: a
    ``--db`` file that has been used keeps what it has, so a restart does not
    overwrite balances a test moved.
    """
    if conn.execute("SELECT 1 FROM account LIMIT 1").fetchone():
        return
    for (identifier, name, country, bban, bic, currency, balance,
         behaviour, closed) in SEED:
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance,"
            " behaviour, parameters, closed) VALUES (?,?,?,?,?,?,?,?,?)",
            (identifier, name, iban(country, bban), bic, currency, balance,
             behaviour, json.dumps({}), closed))
    conn.commit()
