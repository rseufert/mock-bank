-- The schema as SCHEMA_VERSION 3 wrote it (after #7): accounts, holidays, the
-- request log, files, payments and the message queue, with no statement or
-- counter table. `tests/test_upgrade.py` opens a file made from this and
-- expects the tables #9 added to appear and the messages to survive.
PRAGMA user_version = 3;
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
);
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
);
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
    -- the XML, UTF-8
    body         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_account_iban ON account (iban);
CREATE INDEX IF NOT EXISTS ix_request_log_at ON request_log (at);
CREATE INDEX IF NOT EXISTS ix_file_msg_id ON file (msg_id);
CREATE INDEX IF NOT EXISTS ix_payment_end_to_end_id ON payment (end_to_end_id);
CREATE INDEX IF NOT EXISTS ix_payment_due ON payment (account_id, booked_at);
CREATE INDEX IF NOT EXISTS ix_message_due ON message (released_at, due_at);
CREATE INDEX IF NOT EXISTS ix_message_mailbox ON message (released_at, taken_at);
