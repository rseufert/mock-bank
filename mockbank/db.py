"""SQLite: the schema, the upgrade, and the seeded accounts.

The mock is a *bank*, so its state is the state a bank keeps: the accounts it
holds, the days it does not settle on, a log of what was asked of it, and the
payment files it received with the payments it decided on, the messages it
sends back, queued until they are due, and the statements it has issued.

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
#   (none)    every table the 0.1 plan names is here
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
        closed      INTEGER NOT NULL DEFAULT 0,
        -- What the bank writes for this account: `iso20022`, or `nacha` for a
        -- plain acknowledgement in place of the pain.002 (#53).
        format      TEXT NOT NULL DEFAULT 'iso20022',
        -- Its domestic account number, which is how a NACHA file names an
        -- account the bank holds: at the bank's routing number, or as the
        -- company identification of the account paying. '' for none.
        account_number TEXT NOT NULL DEFAULT ''
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
        -- What the file named the creditor's account by: its IBAN, or for an
        -- account the bank does not hold, whatever else the file gave - a
        -- NACHA entry's DFI account number, say. A held account is always
        -- its IBAN here, whatever the file named it by (#53).
        creditor_iban    TEXT,
        creditor_bic     TEXT,
        -- The creditor's bank by clearing member id: a NACHA entry's
        -- receiving DFI, or a pain.001's ClrSysMmbId. What a NACHA return's
        -- addenda names as the original receiving bank (#54).
        creditor_clearing_id TEXT,
        -- For a payment that came in a NACHA file: its entry's transaction
        -- code and its batch's standard entry class, which a return of it
        -- echoes (#54). NULL for a pain.001.
        transaction_code TEXT,
        entry_class      TEXT,
        -- accepted or rejected, and for rejected the ISO 20022 reason code
        status           TEXT NOT NULL,
        reason           TEXT,
        reason_text      TEXT,
        settlement_date  TEXT,
        booked_at        TEXT,
        -- A return (0.2): the business day the money comes back, the
        -- ExternalReturnReason1Code it comes back with, and when it did.
        -- Set when a payment from a return-later account books; the payment's
        -- status becomes `returned` when the clock reaches return_due.
        return_due       TEXT,
        return_reason    TEXT,
        returned_at      TEXT
    )
    """,
    # Money arriving from somebody else (#91): a credit to an account the bank
    # holds, from a payer the test describes. Books on `booking_date`, the
    # first day the bank could book it on or after its value date.
    """
    CREATE TABLE IF NOT EXISTS credit (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id      TEXT NOT NULL REFERENCES account (id),
        amount          INTEGER NOT NULL,
        currency        TEXT NOT NULL,
        value_date      TEXT NOT NULL,
        booking_date    TEXT NOT NULL,
        end_to_end_id   TEXT NOT NULL DEFAULT 'NOTPROVIDED',
        debtor_name     TEXT NOT NULL DEFAULT '',
        debtor_iban     TEXT NOT NULL DEFAULT '',
        debtor_bic      TEXT NOT NULL DEFAULT '',
        -- the payer's structured creditor reference, or '' for none
        reference       TEXT NOT NULL DEFAULT '',
        -- the note to payee as the bank will show it: a JSON list of lines
        note            TEXT NOT NULL DEFAULT '[]',
        received_at     TEXT NOT NULL,
        booked_at       TEXT
    )
    """,
    # One direct debit the account holder asked the bank to collect (#131): the
    # mirror of `payment`, with the held account on the creditor side. A
    # rejected one is kept, because the pain.002 reports it. An accepted one
    # credits its creditor account on `settlement_date`. The return columns are
    # for a collection the debtor's bank sends back after it settled.
    """
    CREATE TABLE IF NOT EXISTS collection (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id            INTEGER NOT NULL REFERENCES file (id),
        pmt_inf_id         TEXT,
        end_to_end_id      TEXT,
        instruction_id     TEXT,
        -- the held creditor account's id, NULL when the bank does not hold it
        account_id         TEXT,
        creditor_iban      TEXT,
        amount             INTEGER,
        currency           TEXT,
        debtor_name        TEXT,
        debtor_iban        TEXT,
        debtor_bic         TEXT,
        -- the mandate the collection is made under, as the file states it; the
        -- mock keeps no mandate register and polices none
        mandate_id         TEXT,
        mandate_signed     TEXT,
        sequence_type      TEXT,
        creditor_scheme_id TEXT,
        -- the remittance lines, a JSON list
        remittance         TEXT NOT NULL DEFAULT '[]',
        -- accepted or rejected, and for rejected the ISO 20022 reason code
        status             TEXT NOT NULL,
        reason             TEXT,
        reason_text        TEXT,
        -- the day the file asked for, and the day the bank collects on
        collection_date    TEXT,
        settlement_date    TEXT,
        booked_at          TEXT,
        return_due         TEXT,
        return_reason      TEXT,
        returned_at        TEXT,
        -- a collection read from a NACHA debit entry (#176): its SEC code,
        -- its transaction code and the receiving bank's routing number, which
        -- a return of it has to echo. NULL for one from a pain.008.
        entry_class        TEXT,
        transaction_code   TEXT,
        debtor_clearing_id TEXT,
        -- the bank clock's moment the debtor's bank refused it by hand, before
        -- or after it settled (#185). It is when the return it brings came to
        -- be owed, which no other column says: a refusal can come days after
        -- the booking. NULL for one nobody refused, and for every row from
        -- before version 13.
        refused_at         TEXT
    )
    """,
    # What the bank sends back, queued for when it is due. The writers (#7)
    # put rows here; the mailbox (#8) reads the released ones and marks them
    # taken. Timestamps are `stamp()`s, UTC with a trailing Z, so they compare
    # as strings; `due_at` is a moment in bank time written the same way.
    """
    CREATE TABLE IF NOT EXISTS message (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        -- the message identifier, as in pain.002.001.10 or camt.054.001.08
        type         TEXT NOT NULL,
        -- the account it concerns; NULL for a pain.002 with no held debtor
        account      TEXT,
        file_id      INTEGER REFERENCES file (id),
        due_at       TEXT NOT NULL,
        released_at  TEXT,
        taken_at     TEXT,
        -- when it was written into --pickup-dir, if there is one. On the row
        -- rather than in memory: the first version of the folder transport kept
        -- the written ids in a set, so a restart on --db wrote every message
        -- ever released into the directory again - including ones the client
        -- had collected long before. It also survives a crash between
        -- releasing and writing, which a set seeded at startup would not.
        written_at   TEXT,
        -- the XML, UTF-8
        body         TEXT NOT NULL,
        -- what pairs it with its entry in GET /_mock/queue before it was
        -- written (#155): type, account, the day it books under and what it
        -- reports. NULL for a message that was a row from the start, and for
        -- every row from before version 10; those answer to `m<id>`.
        key          TEXT,
        -- the bank clock's moment it came to be owed (#185): what its entry in
        -- GET /_mock/queue said, kept so the mailbox says the same. NULL for a
        -- statement and a report, which are never in the queue, and for every
        -- row from before version 13 - nothing recorded it, so none is made up.
        queued_at    TEXT
    )
    """,
    # A camt.053 the bank issued: one per account per business day, never
    # twice, so a restart on --db neither renumbers nor re-issues. Balances
    # in minor units, signed.
    """
    CREATE TABLE IF NOT EXISTS statement (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        account     TEXT NOT NULL,
        day         TEXT NOT NULL,
        number      INTEGER NOT NULL,
        opening     INTEGER NOT NULL,
        closing     INTEGER NOT NULL,
        entries     INTEGER NOT NULL,
        message_id  INTEGER REFERENCES message (id)
    )
    """,
    # A message the bank meant to send and could not write (#166, part 2). It is
    # a separate table rather than a `message` row with no body: a `message` is
    # something a client can collect, and this is the opposite - the bank saying
    # what it owed you and could not produce. Keeping them apart means the
    # mailbox, the pickup directory and `GET /_mock/queue` need to know nothing
    # about it, and `body TEXT NOT NULL` stays true.
    """
    CREATE TABLE IF NOT EXISTS unsent (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        -- the message that was going to be written, as in camt.053.001.08
        type     TEXT NOT NULL,
        -- the account it was for; NULL for one that names no single account
        account  TEXT,
        file_id  INTEGER REFERENCES file (id),
        -- the business day it was for, where it had one
        day      TEXT,
        -- the writer's own complaint, with the path of the element it refused
        problem  TEXT NOT NULL,
        -- the bank clock's moment the bank gave up on it
        at       TEXT NOT NULL
    )
    """,
    # Sequences that must never repeat, by name - a camt.054 MsgId or a
    # statement number per account. Kept apart from the rows they number so
    # that pruning messages (#17) cannot make a number come round again.
    """
    CREATE TABLE IF NOT EXISTS counter (
        name   TEXT PRIMARY KEY,
        value  INTEGER NOT NULL
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
    # Unique where there is one: an account number that named two accounts
    # would send a NACHA payment to whichever the lookup found first.
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_account_number ON account (account_number)"
    " WHERE account_number <> ''",
    # The request log is read newest-first and pruned oldest-first.
    "CREATE INDEX IF NOT EXISTS ix_request_log_at ON request_log (at)",
    # The duplicate check, and a tester looking a payment up by the id their
    # client matches on.
    "CREATE INDEX IF NOT EXISTS ix_file_msg_id ON file (msg_id)",
    "CREATE INDEX IF NOT EXISTS ix_payment_end_to_end_id ON payment (end_to_end_id)",
    # What is waiting to book, per account: the insufficient-funds check and
    # the clock both ask it.
    "CREATE INDEX IF NOT EXISTS ix_payment_due ON payment (account_id, booked_at)",
    # What is waiting to come back: the clock asks it on every advance.
    "CREATE INDEX IF NOT EXISTS ix_payment_return ON payment (return_due, returned_at)",
    # The queue releases by due time; the mailbox reads what is released and
    # not yet taken.
    "CREATE INDEX IF NOT EXISTS ix_message_due ON message (released_at, due_at)",
    "CREATE INDEX IF NOT EXISTS ix_message_mailbox ON message (released_at, taken_at)",
    # One statement per account per day; the unique index is what makes
    # issuing twice impossible rather than merely avoided.
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_statement_day ON statement (account, day)",
    # What is waiting to book, as the clock asks on every advance.
    "CREATE INDEX IF NOT EXISTS ix_credit_due ON credit (booked_at, booking_date)",
    # A tester looking a collection up by the id their client matches on.
    "CREATE INDEX IF NOT EXISTS ix_collection_end_to_end_id ON collection (end_to_end_id)",
]

# The schema's version, kept in the file as `PRAGMA user_version`. Bump it
# whenever SCHEMA or INDEXES changes, so that a file written by a newer mock is
# refused rather than misread; `tests/test_upgrade.py` fails until you do.
# 0 is any file written before the version was recorded.
SCHEMA_VERSION = 13


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


def prune(conn: sqlite3.Connection, keep_requests: int = 0,
          retention_days: float = 0,
          bank_now: Optional[datetime.datetime] = None,
          require_written: bool = False) -> Dict[str, int]:
    """Bound what a long-running mock keeps, and say how much went.

    A mock left up as a shared staging bank for a few weeks has a request log
    and a message table that grow without end, and the only remedy was
    ``POST /_mock/reset``, which also throws away the accounts.

    ``keep_requests`` keeps the newest that many request-log rows (0 keeps them
    all). ``retention_days`` removes what is older than that many days (0 keeps
    everything): request-log rows, and messages a client has already taken.

    **Two clocks, and they are not interchangeable.** ``request_log.at`` is when
    an HTTP request arrived, in real UTC, so its age is real elapsed time.
    ``message.taken_at`` is written from the *bank* clock, which a test moves -
    so a mock started with ``--clock 2025-01-01`` writes messages dated last
    year, and measuring their age against real time deleted messages a client
    had collected seconds earlier. ``bank_now`` is the bank's now; without it
    both fall back to real time, which is right for a caller that has no clock.

    What is *not* pruned is deliberate. ``payment`` and ``file`` are what the
    bank did, and they are the evidence somebody reads when a test fails - a
    mock that eats them is no use at the moment you need it. A message still
    waiting to be collected is kept however old it is, because nobody has seen
    it yet, and so are the accounts, the holidays and the counters: they are
    what the mock *is*, not a record of what it did.
    """
    keep_requests = _whole(keep_requests, "keep_requests")
    retention_days = _window(retention_days)
    removed: Dict[str, int] = {}

    def gone(table: str, cursor: sqlite3.Cursor) -> None:
        if cursor.rowcount > 0:
            removed[table] = removed.get(table, 0) + cursor.rowcount

    if keep_requests > 0:
        # By id rather than by timestamp: two rows can share a second, and
        # "the newest N" has to mean exactly N.
        gone("request_log", conn.execute(
            "DELETE FROM request_log WHERE id <= (SELECT id FROM request_log"
            " ORDER BY id DESC LIMIT 1 OFFSET ?)", (keep_requests,)))
    if retention_days > 0:
        window = datetime.timedelta(days=retention_days)
        gone("request_log", conn.execute(
            "DELETE FROM request_log WHERE at < ?",
            (stamp(utcnow() - window),)))
        # Taken, and taken a while ago in *bank* time. A message nobody has
        # collected stays whatever its age: the whole point of the mailbox is
        # that it waits.
        #
        # A statement points at the camt.053 it was sent as, so the reference is
        # cleared for anything about to go. Leaving it would make
        # GET /_mock/accounts/<id>/statements hand out a message id that answers
        # 404, which is worse than saying the message is gone: the statement row
        # is the record, and it still reconciles.
        message_cutoff = stamp((bank_now or utcnow()) - window)
        # `require_written` is set when a pickup directory is configured: a
        # message the folder has not been given yet must not be aged out, or it
        # is a message that simply never arrives for a client that polls a
        # directory rather than the mailbox.
        written_only = " AND written_at IS NOT NULL" if require_written else ""
        conn.execute(
            "UPDATE statement SET message_id = NULL WHERE message_id IN"
            " (SELECT id FROM message WHERE taken_at IS NOT NULL"
            " AND taken_at < ?" + written_only + ")", (message_cutoff,))
        gone("message", conn.execute(
            "DELETE FROM message WHERE taken_at IS NOT NULL AND taken_at < ?"
            + written_only, (message_cutoff,)))
    conn.commit()
    return removed


# The longest retention window the mock will accept, in days: a century.
# `timedelta` refuses much more than this with an OverflowError rather than an
# answer, and a mock asked to keep a thousand years of request log has been
# asked by mistake.
MAX_RETENTION_DAYS = 36500


class Unusable(ValueError):
    """A retention setting the mock cannot act on, and why."""


def _whole(value, name: str) -> int:
    """A count of rows to keep: a whole number, not negative."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise Unusable("%s is a whole number of rows, not %r" % (name, value))
    if value < 0:
        raise Unusable("%s is a whole number of rows and cannot be negative; "
                       "0 keeps every row" % name)
    return value


def _window(days) -> float:
    """A retention window in days: finite, not negative, and not absurd.

    Every one of `inf`, `1e9`, `nan` and `-3` got through before. The first two
    reached `timedelta` and came back as an OverflowError at startup; the last
    two were silently treated as "off", so an operator who asked for retention
    and mistyped it got a mock that kept everything and said nothing.
    """
    if isinstance(days, bool) or not isinstance(days, (int, float)):
        raise Unusable("retention_days is a number of days, not %r" % (days,))
    days = float(days)
    if days != days:                     # nan, which compares false with all
        raise Unusable("retention_days is a number of days, and nan is not one. "
                       "0 turns retention off.")
    if days in (float("inf"), float("-inf")):
        raise Unusable("retention_days is a number of days, and %s is not one. "
                       "0 turns retention off." % days)
    if days < 0:
        raise Unusable("retention_days cannot be negative (%s); 0 turns "
                       "retention off." % days)
    if days > MAX_RETENTION_DAYS:
        raise Unusable("retention_days is at most %d (a century); %s is further "
                       "than a mock is ever asked to remember on purpose."
                       % (MAX_RETENTION_DAYS, days))
    return days


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
        # The domestic account number is the account part of the IBAN, the
        # last ten digits of these NL ones, so the two agree at a glance.
        conn.execute(
            "INSERT INTO account (id, name, iban, bic, currency, balance,"
            " behaviour, parameters, closed, account_number)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (identifier, name, iban(country, bban), bic, currency, balance,
             behaviour, json.dumps({}), closed, bban[-10:]))
    conn.commit()


def restart_sequences(conn: sqlite3.Connection, tables: Sequence[str]) -> None:
    """Make the next row of each emptied table id 1 again; the caller commits.

    `DELETE FROM payment` leaves SQLite's `AUTOINCREMENT` sequence where it was,
    so the next insert carries on from the highest id the table ever held. That
    is right for a mock that keeps running and wrong for `POST /_mock/reset`,
    which is a new bank: the row ids a test reads, the `MB-P002-` `MsgId` built
    from a file's id, and the `<type>-<account>-<id>.xml` names in the pickup
    directory all carried on from the bank before the reset, so a case that
    passed alone failed in a suite that reset between cases (#159).

    `sqlite_sequence` holds one row per `AUTOINCREMENT` table and SQLite creates
    it with the first such table, so the schema's eight guarantee it is there.
    A name with no row - `account`, `counter` and `holiday` declare no
    `AUTOINCREMENT` - matches nothing and is left alone.
    """
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN (%s)"
                 % ", ".join("?" * len(tables)), tuple(tables))


def next_value(conn: sqlite3.Connection, name: str) -> int:
    """The next number in the sequence `name`, starting at 1. Never repeats,
    whatever is deleted elsewhere; the caller commits."""
    # Two statements rather than an upsert, which needs SQLite 3.24.
    conn.execute("INSERT OR IGNORE INTO counter (name, value) VALUES (?, 0)", (name,))
    conn.execute("UPDATE counter SET value = value + 1 WHERE name = ?", (name,))
    return int(conn.execute("SELECT value FROM counter WHERE name = ?",
                            (name,)).fetchone()[0])
