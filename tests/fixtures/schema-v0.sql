-- The schema as a mock-bank before SCHEMA_VERSION 1 would have written it:
-- an `account` table without `parameters`, `closed` or `role`, no `holiday`
-- table at all, and no indexes. `PRAGMA user_version` is 0, which is what any
-- file written before the version was recorded reports.
--
-- No release ever shipped this, and that is the point: --db promises that a
-- bank's balances survive an upgrade, and the promise needs testing from the
-- first version rather than from the first time somebody's file breaks. When
-- SCHEMA_VERSION moves again, keep this file and add the next one beside it.

PRAGMA user_version = 0;

CREATE TABLE IF NOT EXISTS account (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    iban        TEXT NOT NULL DEFAULT '',
    bic         TEXT NOT NULL DEFAULT '',
    currency    TEXT NOT NULL DEFAULT 'EUR',
    balance     INTEGER NOT NULL DEFAULT 0,
    behaviour   TEXT NOT NULL DEFAULT 'accept'
);

CREATE TABLE IF NOT EXISTS request_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    method  TEXT NOT NULL,
    path    TEXT NOT NULL,
    status  INTEGER NOT NULL,
    at      TEXT NOT NULL
);
