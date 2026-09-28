# Changelog

Every release of [mock-bank](https://pypi.org/project/mock-bank/). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html) -
while the major version is 0, a minor bump may change behaviour, and each entry
says so where it does.

## [Unreleased]

Entries for the next release are one file each in
[`changelog.d/`](changelog.d/), so that two pull requests adding an entry do not
conflict on the same lines of this file. `tools/check_changelog.py --assemble`
writes them into this section at release time. Nothing is added here by hand.

## [0.3.0] - 2026-09-28

### Added

- **NACHA files at both doors** (#53). `POST /payments` and the drop directory
  take a NACHA file as well as a `pain.001`, decide it with the same engine, and
  say which they saw in the answer's `format`. An account has a `format`,
  `iso20022` by default or `nacha` by `PATCH`, which decides what the bank writes
  for it: a NACHA account is sent a plain-text acknowledgement (`nacha.ack`) where
  another gets a `pain.002`, one fact to a line, which is the mock's own shape
  because NACHA specifies none. In the mailbox it is `text/plain`, in the pickup
  directory a `.txt` file, and `?raw` refuses with `409` to mix it into a
  sequence of XML documents. The bank's routing number is the fictional
  `999999992`, and an account has a domestic `account_number`, unique, which the
  seed gives the four accounts (`0000000001` to `0000000004`). Schema version 7
  adds both columns; a version 6 file's accounts become `iso20022` accounts with
  no number. A payment now records its
  creditor's bank by clearing member id, `creditor_clearing_id`, beside the
  account it named.

- **NACHA returns** (#54). A NACHA account is sent a NACHA return file where an
  ISO 20022 account gets a `pacs.004`: one return entry per payment, with its
  addenda 99 naming the reason, the original trace number and the original
  receiving bank, and every count, hash, total and block computed so that it
  reads back through the reader with no finding. What the three behaviours reject
  is `R01` (`insufficient-funds`), `R02` (`closed-account`) or `R03`
  (`bad-bank-id`) in the acknowledgement, and comes back as a return entry the
  next business day as well, with nothing credited because nothing was debited.
  `return-later` on a NACHA account comes back as a return entry, and its reason
  is an `R` code, `R02` by default, refused at `PATCH` otherwise. The
  `camt.054` and `camt.053` still say it in ISO 20022, where `AM04` is now in the
  dictionary's return reasons for `R01`. A return file sent to `POST /payments` is
  refused by name.

- **The payment run pays by ACH too** (#55). `examples/payment_run.py` takes
  `file_format="nacha"`: it writes the run as a NACHA file instead of a
  `pain.001`, reads the bank's acknowledgement instead of the `pain.002`, and pays
  each supplier to the routing and account number SAP holds for it. Every
  `payment_run` test passes in that mode, against mock-sap from PyPI, and CI runs
  them both ways: the 0.3 milestone's definition of done. In that mode:
  - The run's identification is carried exactly in the file creation time and
    modifier, so two runs on one day are never taken for one file. It can be one
    to three letters or digits.
  - An item a NACHA entry cannot carry is skipped with the reason, never cut
    short.

- **BAI2 statements, the records and the writer** (#56), the first step of the US
  statement format. `mockbank/bai2.py` declares the 01, 02, 03, 16, 49, 98 and 99
  records by field position and derives both a writer and a reader from the
  declaration, so nothing builds a line by hand. `write_statement` takes exactly
  the arguments the `camt.053` writer takes, because the two are two renderings of
  one statement: the balances are `010` and `015` on the account record, each
  movement is a `16` with the `EndToEndId` in the bank reference number that a
  treasury system reconciles on, a debit and a payment that came back get different type codes — both
  **placeholders** until #57 settles them against a file from outside the
  project — and the `49`, `98` and `99` trailers are computed from the records they
  cover. BAI2 has no escape character, so every alphanumeric field is made safe
  where the record is built: a comma in an account name would shift every field
  after it, and a line break would split one record into two that the trailers
  still counted once. The reader counts each record's fields against the
  declaration, so neither can pass unnoticed. Nothing is wired to an endpoint yet and no schema changed. Two readings
  of the format the published specification would settle - whether a control total
  sums signed amounts, and whether a record count includes its own trailer - are
  named as constants and asserted in the tests rather than left implicit, so #57
  can hold them against a file from outside the project.

- **A NACHA account's statement is BAI2** (#57). An account whose `format` is
  `nacha` is sent its end-of-day statement as a BAI2 file rather than a
  `camt.053`: the same clock hook, the same balances and entries, numbered from the
  same counter, reaching `GET /_mock/mailbox` as `bai2.statement` and the pickup
  directory as `.bai2`. The choice is per account, so one payment file produces a
  BAI2 statement for a NACHA debtor and `camt.053` for everyone else in it.
  `statement-gap` leaves one detail record out and keeps the balances true, exactly
  as it does for `camt.053`, so the file is a bank that left an entry off rather
  than a bank that wrote a broken file. The statement counter is deliberately
  shared between the formats: an account that changes format keeps counting where
  it left off instead of restarting at 1 and colliding with statements it has
  already been sent.

- **The example copies are checked against their originals** (#100).
  `examples/invoice_check.py` is mock-sap's file, byte-for-byte, because example
  code is not importable across repositories - no wheel carries `examples` - and
  the alternative is forking logic that is tested over there.
  `tools/check_examples.py` fails if a copy stops matching, with a unified diff,
  and `--update` takes the other repository's version. It compares against the
  tip of the source repository's default branch rather than a pinned commit,
  which is the opposite of `check_xsd.py` and deliberate: the drift comes from
  another repository, so a run that passes today and fails tomorrow with nothing
  changed here is the point. `--require` turns a fetch it could not make into a
  failure, as the other checks do, so a contributor with no network skips while
  CI stays strict; a `404` is refused at once rather than retried, because an
  original that moved needs a new path and not another attempt. It runs in its
  own workflow on push and pull request and daily, because per-push alone would
  never notice mock-sap moving for a fortnight.

### Changed

- **A held account can be named by number, not only by IBAN** (#53): a creditor
  at the bank's routing number whose account number is a held account's is that
  account, and so is a debtor whose identifier is one. This reaches the
  `pain.001` path too, where `Othr/Id` and `ClrSysMmbId` name an account that
  way; one that names no held account is decided as before.

- **A BAI2 statement names its two parties the way a real BAI2 file does, and
  signs its balances** (#57). Two changes to the bytes the mock writes.

  The `02` group header gave `ultimate receiver` as the account's *name* and
  `originator` as its id - both the customer, with the bank named nowhere - so
  every statement claimed the account had originated its own statement. In BAI2
  **the originator of a group is the bank that produced the file**, which reads
  oddly beside ISO 20022, where an originator is whoever started a payment. The
  two parties are derived once now and handed to both headers, so they cannot
  drift apart again.

  **Balances and control totals carry an explicit sign**, where they wrote `-`
  when negative and nothing when positive: `49,+25125000,5/` for what used to be
  `49,25125000,5/`. Movement amounts stay unsigned, because a `16`'s direction is
  in its type code.

  Both are settled against `tests/samples/external/bai2-sample1.txt`, a statement
  from [moov-io/bai2](https://github.com/moov-io/bai2) vendored at a pinned commit
  - the first BAI2 file this project has been shown that it did not write. It also
  confirmed two readings the module had flagged as guesses, that record counts
  include the trailer stating them and that control totals are signed; corrected
  the reason recorded for keeping movement totals off the `03`; and showed that
  `mockbank.bai2.read` cannot parse a real BAI2 file, which is #114. It could not
  settle the two placeholder transaction codes, which stay marked. The pull request
  has the arithmetic for each.

### Fixed

- **A NACHA file's controls are read by each entry's side** (#55). The reader
  counted every return as a credit and never checked the batch or file debit
  totals, and the mock's own return writer agreed with it, so nothing noticed.
  Held to a return file from outside the project (moov-io/ach's `return-WEB.ach`),
  a return of a debit (code `26`) is a debit: each entry now counts in its
  control total by its transaction code, and both the credit and the debit total
  are checked. Returns of debits (`26`, `36`, `46`) are read too.

- **A control character in an account name no longer reaches a BAI2 file** (#57).
  `_safe` replaced the line breaks it had been given a list of, which left a tab
  and other C0 controls to pass through - harmless to the field boundaries and
  still junk in a file a bank parses. It replaces every C0 control and DEL now,
  because a list of the dangerous ones is a list somebody has to keep complete.
  The writer also raised `Unreadable`, which reads as though something had been
  parsed; its refusals are `Unwritable`, with both under one base so a caller that
  does not care can catch either.

- **`payment_run` names a bank or SAP that does not answer, instead of stopping**
  (#88). An error answer from either side was already a line in `run.problems`,
  but a host that did not answer at all - down, refusing the connection - raised
  `URLError` and ended the run with a traceback. Now it is a problem too, naming
  which side did not answer and what that left undone, and a payment file the
  bank never received leaves every item `selected`, to send again.

- **A NACHA entry counts on the side its transaction code says** (#102). `side`
  asked whether a code's second digit was `in "1234"`, which is a substring test,
  so `"" in "1234"` was true and a blank or one-character code counted as a
  credit - silently, and on the side the mock's own writer computes the same way,
  which is the side where a wrong answer cannot be caught by the two disagreeing.
  It compares against the four digits themselves now. The visible change is
  exactly that: a code with no second digit no longer lands in the credit total.
  Every two-character code keeps the side it had, so nothing a well-formed file
  can carry moves - such a code reaches `side` only from a malformed line, which
  is a finding of its own. `RETURNS` also gains `56`, the automated return of a
  loan debit: `26`, `36` and `46` were already read, and the fourth was an
  asymmetry rather than a decision.

- **The copy of mock-sap's `invoice_check` is current again** (#108). mock-sap
  0.13.2 taught it to declare its purchase order's currency in the `850` and to
  block an invoice billed in a different currency from the order
  (rseufert/mock-sap#74), so this repository's checked copy stopped matching and
  `tools/check_examples.py` failed on every open pull request. Taken with
  `--update`, which is what that flag is for.

## [0.2.0] - 2026-09-28

### Added

- **Returns** (#14). Under `return-later` a payment settles as usual and then
  comes back `days` business days later (3 by default) with `reason` (`AC04`
  by default), every payment or only the `end_to_end_id` named in the
  account's `parameters`. The bank credits the debtor account back and sends
  a `pacs.004.001.09` - declared in the dictionary and checked against its
  published XSD like the rest - with a `camt.054` credit beside it; the day's
  `camt.053` carries a `CRDT` entry under `PMNT`/`ICDT`/`RRTN` whose
  `RtrInf` names the reason, and still reconciles.
  `GET /_mock/payments/<EndToEndId>` shows `returned` with the reason and
  when it was due. The database gains three return columns on `payment`
  (schema version 5); a 0.1.0 `--db` file is upgraded in place.

- **Folder transport** (#15). Most bank connections are two directories on an
  SFTP host rather than an HTTP endpoint, so the bank has a second door.
  `--drop-dir` is watched (every `--drop-interval-ms`, and `POST
  /_mock/drop/scan` looks now); each settled file goes through **the same
  pipeline** as `POST /payments` and then moves to `processed/`, or to `failed/`
  when the bank could not put it through, with the answer written beside it as
  `<name>.findings.txt` in the same prose `POST /_mock/validate` prints.
  `--pickup-dir` receives every released message as
  `<type>-<account>-<id>.xml`, written to a temporary name and renamed so a
  poller never reads half a file - including the `camt.054` and `camt.053` the
  clock releases days later, and messages from files posted over HTTP. A file
  still being written is left alone for `--drop-settle-ms`; a file is claimed by
  renaming before it is read, so a scan and the poller cannot both take it; and
  one that could not be moved out of the way is remembered and left until it
  changes rather than read again on every pass. `GET /_mock/drop` reports all of
  it, and `/_mock/state` names both directories. The database gains a
  `written_at` column on `message` (schema version 6); a 0.1.0 or 0.2 `--db`
  file is upgraded in place. One directory for both, or either inside the other,
  is refused at startup - the bank would read every message it wrote back in as
  a payment file - and a file the mock cannot write is reported on stderr and in
  `GET /_mock/drop` rather than failing silently. The folder door is outside
  `--auth`, as a real SFTP drop is, and the README says so.

- **A payment run against SAP** (#16). `examples/payment_run.py` is what an
  `F110` run does, end to end and against two mocks rather than one: it asks
  mock-sap for the supplier items that are due, pays them in one `pain.001`,
  and posts each `camt.053` back as a `FINSTA01` so SAP clears what was paid
  and reopens what came back. The `EndToEndId` is the supplier's own invoice
  number and the `MsgId` is the run date and identification, as `F110`'s are,
  so the bank's answers and SAP's clearing meet on the same reference and the
  same run sent twice is `DUPL`. Thirteen tests cover the six from the plan -
  a clean run cleared in SAP, a closed account rejected `AC04` with the rest
  accepted, a return reopening an invoice so that it is distinguishable from
  one never paid, the same run twice paying nothing twice, a statement gap
  leaving exactly one payment unreconciled, and a run after the cutoff waiting
  for Monday's statement - plus what the suite adds around them: a statement
  posted twice clearing nothing twice, an item not yet due left alone, a
  blocked invoice never selected, and what goes into `run.problems` when the
  bank or SAP answers badly. They need mock-sap 0.13.1 or newer, and CI runs
  them against it from PyPI. Standard library only, and it imports neither mock.

- **Retention for a long-running mock** (#17). A mock left up as a shared
  staging bank had a request log and a message table that grew without end, and
  the only remedy was `POST /_mock/reset`, which throws the accounts away with
  them. `--keep-requests N` (default 5000) keeps the newest N request-log rows
  and `--retention-days D` (default off) removes request rows and
  already-collected messages older than D days, trimmed at startup, after each
  advance and every few hundred requests - so the default in-memory bank is
  untouched. `GET /_mock/state` reports what has been removed, so a tester who
  wonders where their rows went can look rather than guess. Payments, files,
  *uncollected* messages, accounts, holidays and counters are never pruned: the
  first two are the evidence a failing test is read against, an uncollected
  message is the one thing a mailbox exists to hold, and the rest are what the
  mock is rather than a record of what it did. The indexes the issue also asked
  for turned out to be there already, so the tests now hold them in place by
  name against the query plans. A message's age is measured on the *bank* clock,
  because that is what wrote its `taken_at`; the request log's age is real
  elapsed time, because that is when the request arrived. Pruning a `camt.053`
  clears the statement's reference to it rather than leaving one that answers
  `404`, and a `--retention-days` the mock cannot act on - `nan`, `inf`, a
  negative, or a century and a half - is refused at startup instead of crashing
  or being silently off. With a `--pickup-dir` configured, retention only
  removes a message the folder has actually been given: aging out one the
  directory never received would lose it silently, because a client that polls a
  directory has no other way to see it.

- **A worked example with mock-edi** (#35). `examples/pay_invoices.py` pays
  the supplier invoices mock-edi sends as EDIFACT `INVOIC`s: one `pain.001`,
  each payment on its invoice's due date, then the `pain.002`, `camt.054` and
  `camt.053` matched back by `EndToEndId` and `MsgId`. Seven tests in
  `examples/test_pay_invoices.py` cover a clean run, `AC04`, a duplicate
  invoice, a run retried after a crash (`DUPL`), `AM04`, a return and a
  statement gap. CI's smoke job runs them against mock-edi from PyPI.

- **NACHA files are read and checked** (#52), the first step of the US formats.
  `POST /_mock/validate` recognises a NACHA file by its `101` file header and
  reads it into the same payments a `pain.001` becomes, from a declaration of
  record types 1, 5, 6, 7, 8 and 9 by field position and width
  (`mockbank/nacha.py`). The `EndToEndId` is the individual identification
  number, the originator's own reference, and the debtor account is the company
  identification, since a NACHA file carries none. Findings name the line, the
  record and the field with its columns: a line that is not 94 characters, a
  routing number that fails its check digit (`RC01`), an entry hash or block
  count that is wrong (`FF01`), counts (`AM18`) and credit totals (`AM10`) that
  disagree with the entries, and any entry that is not a credit.
  `POST /payments` refuses a NACHA file by name until #53.

### Changed

- **The pipeline moved from the request handler onto `State.receive`**,
  so that the folder transport and `POST /payments` are two doors onto one
  pipeline rather than two copies of it. No behaviour change: the answer, the
  statuses and the mailbox are what they were.

- **The list of endpoints is read from the route table** (#45), in the 404
  body, the index page and `/_mock/state`'s `supported`, so it cannot drift
  from what the mock serves. The same 25 endpoints and notes as before; the
  order is now the order they are registered in, so `GET /_mock/behaviours`
  comes before the dictionary and `POST /_mock/validate` after the request
  log. The README gains the row for `GET /` it never had, which the new check
  in `tools/check_docs.py` found on its first run: it now fails the build when
  the README's endpoint table and the route table disagree either way.

- **A NACHA file is checked as a `pain.001` is** (#53, carried from #71). A past
  effective entry date is a `DT01` warning and an individual identification
  number used twice is `AM05`, the two checks a `pain.001` gets that NACHA has no
  rule of its own for. A blank file creation time is no longer a finding, since
  NACHA allows it. The reading of either format now keeps the creditor's bank by
  clearing member id, `creditor_clearing_id`: the receiving DFI's routing number
  in a NACHA entry, `CdtrAgt/FinInstnId/ClrSysMmbId/MmbId` in a `pain.001`.

### Fixed

- **Three answers from the old router, which the route table (#44) no longer
  gives.** `server.py` is split into `handler.py`, `state.py` and one module
  per surface under `routes/`, with every endpoint in one table. Moving the
  endpoints into the table changed three answers a client could see, and each
  old answer was a bug:
  - `GET /_mockxyz/health` answered as health, because any path starting with
    `/_mock` had its first segment dropped. It is now a 404.
  - `POST /_mock/dictionary/<message>/<more>` was a 405, as if the path
    existed. It is now a 404.
  - The same for `POST /_mock/payments/<EndToEndId>/<more>`: now a 404.

  Nothing else changed: 99 requests sent to both routers, with and without
  `--auth`, got the same answers.

- **A file the bank cannot read now gets a `pain.002` through either door**
  (#64). A body with no `MsgId` used to be answered only in the HTTP response
  (#26), so a client banking through `--drop-dir` got prose in `failed/` and
  nothing in `--pickup-dir`. Now the refusal is queued as a message like any
  other `pain.002`: `RJCT` with its reason, `OrgnlMsgId` (and `OrgnlMsgNmId`,
  when the message type could not be read either) `NOTPROVIDED` rather than an
  identifier the bank made up, and a dropped file's name in
  `StsRsnInf/AddtlInf`. It reaches the mailbox and the pickup directory, and
  `POST /payments` lists it in `queued`. It is filed under the debtor's account
  when one could be read, and as `bank` when none could. A `silent` debtor still
  hears nothing, and a `DUPL` refusal, which has a `MsgId`, is unchanged.

## [0.1.0] - 2026-09-26

### Fixed

- **Binding `0.0.0.0` no longer waits for DNS before the mock finishes
  starting.** `http.server` sets its `server_name` from a reverse lookup on the
  bind address; on a host with no reverse record for what it is binding - a CI
  runner, or a container on a network with no resolver for `0.0.0.0`, which is
  what the image binds - that lookup waited for DNS to time out first. It cost a
  minute on a macOS runner. Nothing in the mock needs the name, so the address
  as given is used instead.

- **A field sent as the wrong JSON type is a `400` naming that field**, not a
  `500` with a traceback. `POST /_mock/accounts` with `{"id": 5}`, or a `name`
  sent as an object or an `iban` as a list, used to reach SQLite and fail
  there; a `500` from a mock is indistinguishable from the mock being broken.
- **`PUT /_mock/accounts/<id>` is refused rather than treated as a partial
  `PATCH`.** It used to do a partial update while the `405` other methods got
  advertised only `GET` and `PATCH`, so a client doing a full replace got a
  partial one and no word about it.

### Added

0.1 is ISO 20022 credit transfers end to end: a `pain.001` arrives, the bank
decides it, books what it accepts on the settlement date, and sends back the
`pain.002`, `camt.054` and `camt.053` a real bank sends - on a clock a test
moves, so none of it involves waiting. Read in order, the entries below are that
story.

- **The repository, and a mock that answers.** `python -m mockbank` starts
  a server with `/_mock/health`, `/_mock/state` and `POST /_mock/reset`; every
  surface the plan promises and this skeleton lacks answers `404` with a body
  naming what is supported and what is planned. The account behaviours are
  declared in `mockbank/accounts.py` so the command line, the README and the
  docs check agree on their names from the first commit. The release
  machinery, the documentation and changelog checks and the test harness are
  carried over from mock-edi.
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
- **The bank clock** (#4). `mockbank/clock.py` keeps bank time, and nothing in
  the mock waits for it: `POST /_mock/advance?days=N` moves it by calendar days
  and `?to=YYYY-MM-DD` to midnight on a date, and either way the answer lists
  the business days the move passed through, because three days from a Thursday
  is Sunday to a calendar and Tuesday to a bank. `settlement_date()` is the
  later of the requested execution date and the day the bank can start on - the
  day of receipt before the cutoff, the next business day at or after it -
  rolled forward past weekends and holidays. `GET/PUT /_mock/holidays` is the
  holiday calendar, `GET /_mock/state` reports the clock, and the clock never
  goes backwards, though advancing to a date it has already reached today does
  nothing rather than failing. New flags: `--timezone` (IANA name, `UTC` by default, and on
  Python 3.8 a named zone is refused rather than quietly treated as `UTC`),
  `--cutoff HH:MM` and `--clock YYYY-MM-DDTHH:MM`; `POST /_mock/reset` returns
  bank time to where `--clock` put it rather than to real time.
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
- **Deciding and booking payments** (#6). `POST /payments` reads, validates
  and decides a `pain.001`, books what is due, and answers `202` with each
  payment's outcome, reason code and settlement date (`422` when the file is
  rejected outright). A behaviour describes the account it is set on:
  `closed-account` (`AC04`) and `bad-bank-id` (`RC01`) are read from a held
  creditor account, the rest from the debtor account. A `MsgId` seen before is
  `DUPL` unless `--allow-duplicates`; a debtor account the bank does not hold
  is `AC02`; a payment in another currency than its account is `AM03`; only
  `insufficient-funds` (`AM04`) looks at the balance. Accepted payments debit
  their account on the settlement date, debit side only.
  `GET /_mock/payments` and `/_mock/payments/<EndToEndId>` show what was
  decided, and `/_mock/state` counts it. The database gains `file` and
  `payment` tables (schema version 2); an older `--db` file is upgraded in
  place.
- **The bank answers** (#7). A decided file gets a `pain.002.001.10`: the
  group status with the original `MsgId`, a status per batch, and a
  `TxInfAndSts` per payment with its `EndToEndId` and, for a rejection, its
  reason code; a file rejected outright gets its group status and reason
  only, and a `silent` account none. Each time payments book, the debtor
  account gets a `camt.054.001.08` with an entry per payment, carrying its
  `EndToEndId` and `PMNT`/`ICDT`/`ESCT`. Both are built from the dictionary.
  They wait in a new `message` table (schema version 3) until due -
  `--status-delay-ms` holds the `pain.002` back - and `GET /_mock/mailbox`
  collects what is released. `POST /payments` says what is queued and for
  when, and `GET /_mock/payments/<EndToEndId>` now always answers with the
  newest payment, `?all` with every one. The README's example now shows what
  the seed does: two accepted, one `AC04`, one `RC01`. Payments now settle
  on the clock's settlement date - the cutoff, weekends and holidays count -
  and advancing the clock books what comes due and releases its messages.
- **The end-of-day statement** (#9). As the clock passes the end of a
  business day, every open account gets a `camt.053.001.08` for it, in
  order, empty days included: `OPBD` and `CLBD`, an entry per booked debit
  with its `EndToEndId`, and `TxsSummry`. The closing balance is the opening
  less the entries to the cent, and each statement opens where the last
  closed. `statement-gap` leaves one entry off and keeps the balances true.
  `GET /_mock/accounts/<id>/statements` lists what was issued; a new
  `statement` table (schema version 4) means a restart on `--db` neither
  renumbers nor re-issues. Statement numbers and `camt.054` `MsgId`s now come
  from a persistent counter rather than a count of messages, so pruning can
  never make one repeat.
- **The rest of the mailbox, and the request log** (#8). `GET /_mock/mailbox`
  takes `?leave` to peek without taking, `?raw` for the XML bodies alone with
  `Content-Type: application/xml`, and `?type=pain.002` to filter on a type
  prefix so a caller need not know the version; they combine.
  `GET /_mock/mailbox/<id>` returns one message's XML whether or not it has
  been collected, and `POST /_mock/mailbox/<id>/unread` puts one back for a
  test that wants to collect twice. `GET /_mock/requests` serves the newest
  hundred rows of the request log with a `?path=` prefix filter, so a tester
  can see what their client actually sent. `?raw` returns a *sequence* of
  documents rather than one: wrapping several messages in an invented root
  element would put an element on the wire that no ISO 20022 schema has, and
  the README says so rather than leaving it to be discovered. With these, every
  endpoint the 0.1 plan promised answers, so the `planned` list in a `404` is
  now empty and the index page stops showing the heading.
- **The mock checks its own output against the standard** (#10).
  `GeneratedMessagesAreValid` sends the sample under every account behaviour
  and walks every message the mock writes against its dictionary, counting
  them, so a path that stops writing fails. Because the writers and that
  dictionary could be wrong together, it is also held to eleven files from
  outside the project (`tests/samples/external/`, each with its source and
  licence) and, by `tools/check_xsd.py` in CI, to the published XSDs. That
  found three mistakes, now fixed: `UETR` and `OrgnlUETR` are 36 characters,
  not 35; a postal address's proprietary `AdrTp` is an identifier with its
  issuer, not text; and `ACCC` is a status code the mock now knows.
- **`--auth USER:PASSWORD`, and a warning when the control plane is open**
  (#12). HTTP basic credentials on every request, including `/_mock/health` and
  the index, compared with `hmac.compare_digest`; without them, `401` and
  `WWW-Authenticate: Basic realm="mock-bank"`. The check runs before the body
  is read, so an unauthenticated `POST /payments` never has its file parsed,
  and the refused request is still recorded in the request log. Binding an
  address other machines can reach with no `--auth` prints a warning to stderr
  naming the flag and what is at stake, and `-q` does not silence it: `-q` is
  about the access log, not about whether the bank is open to the network. The
  README's `docker run` example passes `--auth`, since the image binds
  `0.0.0.0`. A value with no colon, or with an empty user or password, is
  refused at startup rather than accepted as a credential nothing could ever
  match. The warning and the startup banner are flushed: before Python 3.9 a
  piped stderr is block-buffered, so in a container - where the output is
  collected rather than shown on a terminal - the warning sat in a buffer until
  something else filled it, and a warning that reaches `docker logs` minutes
  after the port opened is not a warning.
- **The curl tour and the example client for 0.1** (#11). `examples/demo.sh`
  now tells the whole story: send `tests/samples/pain001_four_payments.xml`,
  read the `pain.002` and its reason codes, move bank time to the settlement
  date and one day past it, watch the `camt.054` and then a `camt.053` whose
  balances reconcile, and break it on purpose to see `DUPL` and `AM04`. It asks
  `/_mock/state` what the mock supports and skips what is missing, naming the
  endpoint, so it runs against a mock started any way; `BANK_AUTH` covers one
  started with `--auth`. `examples/client.py` is the same choreography as a
  client an integrator would copy - standard library only - and it matches every
  answer back to its payment by `EndToEndId` rather than by position, which is
  the part a curl tour cannot demonstrate and the part integrations get wrong.
  `examples/statement.py` does the arithmetic the statement step is about. CI's
  smoke job runs the tour and the client, each against a plain mock and again
  against one wanting credentials, and the package job now sends a real payment
  file through the installed console script instead of only asking for its
  health.

[Unreleased]: https://github.com/rseufert/mock-bank/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/rseufert/mock-bank/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rseufert/mock-bank/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rseufert/mock-bank/releases/tag/v0.1.0
