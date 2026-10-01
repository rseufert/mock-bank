-- The schema at SCHEMA_VERSION 8, as v0.4.0 and v0.5.0 shipped it: before a
-- direct debit could be collected, the collection table (#131).
--
-- Kept so the upgrade path is tested from every version that existed,
-- generated from db.SCHEMA and db.INDEXES as they stood.
PRAGMA user_version = 8;
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
    );
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
        -- when it was written into --pickup-dir, if there is one. On the row
        -- rather than in memory: the first version of the folder transport kept
        -- the written ids in a set, so a restart on --db wrote every message
        -- ever released into the directory again - including ones the client
        -- had collected long before. It also survives a crash between
        -- releasing and writing, which a set seeded at startup would not.
        written_at   TEXT,
        -- the XML, UTF-8
        body         TEXT NOT NULL
    );
CREATE TABLE IF NOT EXISTS statement (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        account     TEXT NOT NULL,
        day         TEXT NOT NULL,
        number      INTEGER NOT NULL,
        opening     INTEGER NOT NULL,
        closing     INTEGER NOT NULL,
        entries     INTEGER NOT NULL,
        message_id  INTEGER REFERENCES message (id)
    );
CREATE TABLE IF NOT EXISTS counter (
        name   TEXT PRIMARY KEY,
        value  INTEGER NOT NULL
    );
CREATE UNIQUE INDEX IF NOT EXISTS ix_account_iban ON account (iban);
CREATE UNIQUE INDEX IF NOT EXISTS ix_account_number ON account (account_number) WHERE account_number <> '';
CREATE INDEX IF NOT EXISTS ix_request_log_at ON request_log (at);
CREATE INDEX IF NOT EXISTS ix_file_msg_id ON file (msg_id);
CREATE INDEX IF NOT EXISTS ix_payment_end_to_end_id ON payment (end_to_end_id);
CREATE INDEX IF NOT EXISTS ix_payment_due ON payment (account_id, booked_at);
CREATE INDEX IF NOT EXISTS ix_payment_return ON payment (return_due, returned_at);
CREATE INDEX IF NOT EXISTS ix_message_due ON message (released_at, due_at);
CREATE INDEX IF NOT EXISTS ix_message_mailbox ON message (released_at, taken_at);
CREATE UNIQUE INDEX IF NOT EXISTS ix_statement_day ON statement (account, day);
CREATE INDEX IF NOT EXISTS ix_credit_due ON credit (booked_at, booking_date);
