# Changelog

Every release of [mock-bank](https://pypi.org/project/mock-bank/). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html) -
while the major version is 0, a minor bump may change behaviour, and each entry
says so where it does.

## [Unreleased]

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

[Unreleased]: https://github.com/rseufert/mock-bank/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rseufert/mock-bank/releases/tag/v0.1.0
