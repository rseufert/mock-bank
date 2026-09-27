-- The schema at SCHEMA_VERSION 5: before the folder transport gave a
-- message a written_at column, so the mock knew what it had already put
-- in the pickup directory only for as long as it stayed running.
--
-- Kept so the upgrade path is tested from every version that existed,
-- not only the newest.

PRAGMA user_version = 5;

CREATE TABLE account (
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
CREATE TABLE counter (
        name   TEXT PRIMARY KEY,
        value  INTEGER NOT NULL
    );
CREATE TABLE file (
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
CREATE TABLE holiday (
        day     TEXT PRIMARY KEY
    );
CREATE TABLE message (
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
CREATE TABLE payment (
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
        booked_at        TEXT,
        -- A return (0.2): the business day the money comes back, the
        -- ExternalReturnReason1Code it comes back with, and when it did.
        -- Set when a payment from a return-later account books; the payment's
        -- status becomes `returned` when the clock reaches return_due.
        return_due       TEXT,
        return_reason    TEXT,
        returned_at      TEXT
    );
CREATE TABLE request_log (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        method  TEXT NOT NULL,
        path    TEXT NOT NULL,
        status  INTEGER NOT NULL,
        at      TEXT NOT NULL
    );
CREATE TABLE statement (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        account     TEXT NOT NULL,
        day         TEXT NOT NULL,
        number      INTEGER NOT NULL,
        opening     INTEGER NOT NULL,
        closing     INTEGER NOT NULL,
        entries     INTEGER NOT NULL,
        message_id  INTEGER REFERENCES message (id)
    );
CREATE UNIQUE INDEX ix_account_iban ON account (iban);
CREATE INDEX ix_file_msg_id ON file (msg_id);
CREATE INDEX ix_message_due ON message (released_at, due_at);
CREATE INDEX ix_message_mailbox ON message (released_at, taken_at);
CREATE INDEX ix_payment_due ON payment (account_id, booked_at);
CREATE INDEX ix_payment_end_to_end_id ON payment (end_to_end_id);
CREATE INDEX ix_payment_return ON payment (return_due, returned_at);
CREATE INDEX ix_request_log_at ON request_log (at);
CREATE UNIQUE INDEX ix_statement_day ON statement (account, day);
