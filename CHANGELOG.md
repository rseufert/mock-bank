# Changelog

Every release of [mock-bank](https://pypi.org/project/mock-bank/). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html) -
while the major version is 0, a minor bump may change behaviour, and each entry
says so where it does.

## [Unreleased]

### Added

- **SQLite state and the accounts endpoints** (#3). The mock now keeps what it
  knows: `mockbank/db.py` declares the schema and the indexes, records the
  schema version in the file, upgrades a `--db` file written by an older
  mock-bank in place, and refuses one written by a newer mock-bank before it
  binds the port. It starts with four accounts - `ACME`, `GLOBEX`, `INITECH`
  and `EURODIS` - one per failure the README promises, with IBANs carrying
  real mod-97 check digits and invented bank codes. `GET /_mock/accounts`
  lists them, `GET /_mock/accounts/<id>` shows one, `POST /_mock/accounts`
  creates one, and `PATCH /_mock/accounts/<id>` changes a behaviour, a
  balance, the `closed` flag or a behaviour's parameters at runtime; an
  unknown behaviour or a misspelled field is a `400` naming what would have
  been accepted. `GET /_mock/behaviours` serves the table itself.
  `POST /_mock/reset` puts the seed back and `GET /_mock/state` reports the
  account count and the balance total per currency. Balances are whole
  numbers of minor units everywhere: `12.50` is refused rather than rounded,
  because a statement that is a cent out is the failure this project exists
  to rehearse.
- **Reading and validating a payment file** (#5). `POST /_mock/validate`
  reads a `pain.001` (`.001.09` or `.001.03`, into the same model) and
  answers with its findings as prose, one line each, `200` when clean and
  `422` when not, storing nothing. Each finding names the element path and
  the reason code it will be reported with: structural problems and
  unreadable files are `FF01`, and the mock checks `NbOfTxs` (`AM18`),
  `CtrlSum` (`AM10`), IBAN check digits (`AC01`), currency against the debtor
  account (`AM03`) and repeated `EndToEndId`s (`AM05`). A past execution date
  is a `DT01` warning, since the bank executes on the next business day.
  Signed or encrypted files and DTDs are refused by name. Validation never
  raises, whatever the body. The dictionary's `pain.002` `OrgnlMsgNmId` now
  takes any identifier, as the standard allows, not only a versioned name.

- **The ISO 20022 dictionary** (#2). `mockbank/schema.py` declares
  `pain.001.001.09` (and `.001.03`, read into the same model),
  `pain.002.001.10`, `camt.054.001.08` and `camt.053.001.08` as element trees
  with their types, occurrences and code sets, the recurring shapes declared
  once. One walker reports an element out of order, in the wrong namespace,
  missing, repeated or with a bad value as a finding naming its path; one
  builder writes any of them in declared order. `GET /_mock/dictionary` and
  `GET /_mock/dictionary/<message>` serve it all as JSON, with the choices the
  standard leaves open - the versions written, `PMNT`/`ICDT`/`ESCT` as the bank
  transaction code - stated.
- **The repository, and a mock that answers.** `python -m mockbank` starts
  a server with `/_mock/health`, `/_mock/state` and `POST /_mock/reset`; every
  surface the plan promises and this skeleton lacks answers `404` with a body
  naming what is supported and what is planned. The account behaviours are
  declared in `mockbank/accounts.py` so the command line, the README and the
  docs check agree on their names from the first commit. The release
  machinery, the documentation and changelog checks and the test harness are
  carried over from mock-edi.

[Unreleased]: https://github.com/rseufert/mock-bank/commits/main
