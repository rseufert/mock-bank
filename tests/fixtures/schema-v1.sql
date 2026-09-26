-- The schema as SCHEMA_VERSION 1 wrote it (after #3): the account, holiday
-- and request_log tables and their indexes, and no payment or file table.
-- `tests/test_upgrade.py` opens a file made from this and expects the tables
-- #6 added to appear and the accounts to survive.
PRAGMA user_version = 1;
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
);
CREATE TABLE IF NOT EXISTS holiday (
    day     TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS request_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    method  TEXT NOT NULL,
    path    TEXT NOT NULL,
    status  INTEGER NOT NULL,
    at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_account_iban ON account (iban);
CREATE INDEX IF NOT EXISTS ix_request_log_at ON request_log (at);
