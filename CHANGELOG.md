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

## [0.8.0] - 2026-10-06

### Changed

- **The `ImportError` for a moved integration names `pip install mock-acme`, and
  mock-acme is on PyPI** (#202). The message named the repository alone, which was
  the whole answer while there was nothing to install; mock-acme has been on PyPI
  since 2026-10-05, so it now names the one command that fixes the import as well.
  `README.md` and `examples/README.md` said mock-acme was not on PyPI and no
  longer do. Its wheel holds the package alone, so its own tests still run from a
  clone.

- **The default port is 8090, not 8080** (#203). mock-edi defaults to `8080` too,
  so the two could not both be started without a flag - the second one died on a
  bind error - and mock-sap's `8000`, mock-edi's `8080` and this mock's `8090` are
  now three distinct numbers a reader can start side by side. Anyone who runs
  `mock-bank` with no `--port`, or `docker run -p 8080:8080 mock-bank`, is
  affected: pass `--port 8080` to keep the old one, or publish `-p 8090:8090`.

### Removed

- **The worked integrations have moved to mock-acme, and
  `mockbank.examples` no longer carries them** (#201). `pay_invoices`,
  `payment_run`, `procure_to_pay`, `bank_messages` and the copy of mock-sap's
  `invoice_check` sat between this mock and the other two, so they now live in
  [mock-acme](https://github.com/rseufert/mock-acme), one copy of each, tested
  against all three mocks. `from mockbank.examples import payment_run`, which
  worked from 0.6.0, now raises an `ImportError` that names mock-acme;
  `mockbank.examples.client` and `mockbank.examples.statement` are still in the
  wheel. `examples/README.md` says which file became which. The fix to
  `examples/payment_run.py` recorded under #164 in this release went with the
  file: it is in mock-acme, not in this package.

### Fixed

- **A business day that ends by itself gets its statement** (#157). Statements were
  issued for the days a `POST /_mock/advance` crossed and for no others, so a mock
  left running over midnight never issued that day's, and the day's debits were on
  no statement at all - the balance was right and nothing explained it. The bank
  now remembers how far its statements go, and the next release - a mailbox read,
  a file received, an advance - issues every business day that has ended since,
  whoever moved the clock. They are stamped when they are issued, not at midnight.
  `Clock` takes a `real=` source of real time, so a test of a night passing does
  not sleep through one.

- **A payment whose account closes before it settles is rejected, not booked**
  (#158). A payment accepted for a later day used to debit the account on that day
  even if the account had been closed since, and a closed account gets no
  statement, so the balance moved and no statement showed why. The bank now checks
  again on the settlement day: the payment is rejected `AC04`, nothing books, no
  `camt.054` is sent, and a further `pain.002` for the file (`MsgId` `MB-P002-S…`)
  reports each such payment `RJCT`. A NACHA account is sent a return entry, `R02`,
  instead. `GET /_mock/queue` shows the `pain.002` in place of the debit's
  `camt.054` for as long as the account is closed, and the `camt.054` again if it
  reopens before the day. A `return-later` credit due back to a closed account is
  now put off a business day at a time until the account reopens, as a credit and
  a collection already were. **A client that closed an account and relied on its
  pending payments still booking will see them rejected.**

- **`POST /_mock/reset` is a new bank, its row ids included** (#159). A reset
  emptied every table and restarted the `counter` table, but not SQLite's
  `AUTOINCREMENT` sequences - a `DELETE` leaves those where they are - so the same
  file sent after a reset came back with payment ids 5-8 instead of 1-4, message
  ids 3-4 instead of 1-2, `MB-P002-000002` instead of `MB-P002-000001`, and
  pickup file names ending `-3` and `-4`. A test that reset between cases and
  asserted on an id, a `MsgId` or a file name in the pickup directory passed alone
  and failed in a suite. The sequences are now restarted with the tables, so the
  same input gives the same answer on `:memory:` and on `--db`. A *restart* on
  `--db` still carries on where the old mock left off: that is what a restart is,
  and it is unchanged.

- **No two accounts are sent the same `MsgId`** (#160). Every `MsgId` the bank
  writes for an account is built from the first 18 characters of its id, which is
  what keeps it inside ISO's 35 - so two accounts whose ids were identical over
  that much of them were sent their statements and notifications under one
  `MsgId`, day after day, and a client that de-duplicates on `MsgId` dropped one
  of the two. The second such account is now refused at creation, naming the
  first and saying to differ inside the first 18 characters: the same answer
  `POST /_mock/accounts` already gives for an id it could not write messages for
  at all. The 18 lived at nine places in `outbox.py` and is now one constant and
  one function, so the refusal and the writers cannot come to disagree about it.

- **An unknown key on an account is the 400 that lists the fields, whatever it is
  called** (#161). `POST /_mock/accounts` and `PATCH /_mock/accounts/<id>` passed
  the body to `accounts.create` and `accounts.update` as keyword arguments, so a
  key named `conn` or `identifier` collided with one of their parameters and
  raised a `TypeError` before the field check could name it: the answer was a 500
  carrying "update() got multiple values for argument 'conn'" instead of the 400
  every other unknown key gets. The fields are now one dictionary, which is what
  they always were on the wire.

- **The sdist ships everything its tests read, and CI runs them from it** (#163).
  `MANIFEST.in` named the tests by extension and listed `*.py *.xml *.sql *.md
  *.txt`, so an unpacked sdist held no NACHA `.ach` sample, no `.json` fixture,
  no `tools/`, no `docs/` and no workflow - and `python3 -m unittest discover -s
  tests` from it reported 2 failures and 98 errors, with 92 tests never
  discovered at all. It now carries those too, and the suite passes from an
  unpacked sdist, running the same tests the checkout does. CI's `package` job unpacks
  the archive it just built and runs the suite from the unpacked tree, in a
  directory of its own so nothing of the checkout can stand in for a file the
  archive left out. `CONTRIBUTING.md` says how to do the same by hand, in a
  throwaway virtualenv, since `build` is not a dependency of this project and a
  system Python usually does not have it.

- **`examples/payment_run.py` writes the currency the run actually pays in**
  (#164, item 4). The `FINSTA01` it hands SAP had `CUXWAERZ` and `FIIKWAER`
  hardcoded to EUR whatever account the run paid from, so an ACH run - which pays
  in dollars, and skips any open item not in them - sent SAP a statement of dollar
  payments labelled as euros. SAP reconciled it anyway, but only because it did
  not compare a line's currency against the open item's either; rseufert/mock-sap#88
  is that half. An amount without a currency is not an amount, and this run has
  exactly one, so both fields now come from it.

  **The two halves have to land in this order.** Against mock-sap 0.17.1 this
  change alone is green, because that version ignores the currency. Landing
  mock-sap's check first instead takes the ACH suite from 26 passing to 4 failing,
  which is what the two errors cancelling looks like from the other side.

## [0.7.0] - 2026-10-02

### Added

- **A queue entry says when the bank came to owe it, and so does its message**
  (#185). Every entry in `GET /_mock/queue` has `queuedAt`: the bank-clock moment
  it entered the queue, whatever caused that. For a payment or a collection not
  yet settled it is when the file arrived; for money arriving, when the bank heard
  of it; for a return, when the payment booked and its return was scheduled, not
  when the file arrived; for a refused collection, when the debtor's bank refused
  it. It does not depend on when the queue was read, so two captures of one run
  agree, and it is never later than the bank clock at the first read that shows
  the entry. One case moves, and the README says which: an entry for collections
  not yet settled, when every collection of the earliest file in it is refused
  before settling.

  The message carries the same `queuedAt` into `GET /_mock/mailbox`, so one read at
  the end can say when each message was promised, paired on `key`. A statement and
  an intraday report have `null`: neither is ever in the queue. So does a message
  written by an older version, because nothing recorded it and none is made up.

  A collection also keeps `refused_at`, the moment the debtor's bank refused it,
  shown on `GET /_mock/collections`. That is what a refused collection's return is
  queued from, and no other field said it: a refusal can come days after the
  booking.

  The database schema is version 13 (`message.queued_at`, `collection.refused_at`).
  A `--db` file from 0.6.0 is upgraded in place when the mock opens it.

- **`GET /_mock/messages` lists what the bank has sent, collected or not** (#186).
  The mailbox no longer lists a message once a client has collected it, so somebody
  watching that client could not find it again: `GET /_mock/mailbox/<id>` serves
  only the body, to a caller who already knows the id. The new listing has every
  released message, oldest first, with the mailbox's fields and `takenAt`: the
  bank-clock moment it was collected, or `null`. `?type=` filters on a prefix, as
  the mailbox does.

  It is a path of its own and not a flag on the mailbox, whose default takes what
  it lists. Nothing asked of this one takes a message, and it releases nothing
  either, so reading it between a client's calls changes nothing the client sees.
  With `GET /_mock/queue`, which lists what has not been released, every message
  is listed once.

  It shows what the bank still holds. `--retention-days` removes collected messages
  as they age, and a reset removes them all.

- **A `pain.008` sample the seed accepts**, `tests/samples/pain008_four_collections.xml`
  (#187). One batch of four euro direct debits ACME collects under SEPA Core
  mandates, with the seeded accounts as its debtors, so one post against a fresh
  mock gives a first-time user every outcome worth seeing:

  ```
  DD-2026-0101  1250.00  at another bank          accepted
  DD-2026-0102   340.00  GLOBEX, 12.50 to hand    rejected  AM04
  DD-2026-0103   980.25  INITECH, closed          rejected  AC04
  DD-2026-0104  1500.50  EURODIS, held            accepted
  ```

  The file's status is `PART`, and the two accepted ones credit ACME 2750.50 on
  their settlement date. It stands beside `pain001_four_payments.xml` and
  `nacha_four_payments_to_the_seed.ach`, and is used in the README's direct debit
  walk-through.

  Until now the repository shipped no direct debit file the mock would collect
  anything for: the two external `pain.008` samples are decided `AC03` on every
  collection, because their creditor accounts are not the mock's, so mock-films
  wrote its own file to film the choreography.

  It is written by `schema.serialize`, so the element order is the dictionary's
  rather than a hand-typed guess, and `tools/check_xsd.py` now validates it with
  the other clean samples - ISO's own XSD agreeing with the order the dictionary
  chose. `tests/test_collections_book.py` posts it, holds the four outcomes
  against the behaviours the seed gives those accounts, and reads the file's own
  IBANs with a regex rather than with the mock's reader, so the test cannot agree
  with itself about what the sample says.

### Fixed

- **The README said a same-day return waits on the queue** (#187). Under *The
  debtor's bank can say no*, the "After it settled" bullet ended "Until then both
  are listed in `GET /_mock/queue`", which holds only for a return the bank books
  on a later day. Reported by mock-films, which refused a settled collection
  `MD06` on a business day before the cutoff and found the queue empty.

  The behaviour was right and the sentence was not. Measured, on a Thursday clock
  with the collection settled:

  ```
  refused Monday 09:00 (before the cutoff)   return books Monday   queue []
  refused Saturday                           return books Monday   queue [camt.054, pacs.004]
  refused Monday 16:00 (after the cutoff)    return books Tuesday  queue [camt.054, pacs.004]
  ```

  So the sentence now names both cases: released at once and never queued when
  the bank can book the return today, listed until the day it books when it
  cannot - a weekend or a holiday, or after the cutoff.

  The two neighbouring passages were checked for the same wording and neither
  needed it. A collection's own `camt.054` *is* always queued, because an accepted
  collection never settles on the day its file arrives - asked to collect on the
  day of arrival, it settles the next business day - so "Until then the
  notification is listed in `GET /_mock/queue`" a few paragraphs above is right as
  it stands. The NACHA return passage makes no claim about the queue; its
  `nacha.return` and `camt.054` go out at once on a same-day return exactly as the
  `pacs.004` pair does.

## [0.6.0] - 2026-10-01

### Added

- **A direct debit initiation, `pain.008.001.08`, is read and validated** (#131,
  the first step of direct debits). `POST /_mock/validate` reads one into
  collections: the creditor account and the requested collection date per batch,
  and per collection the debtor, the amount, the mandate id and its date of
  signature, the sequence type and the creditor scheme identifier. The last two are
  taken from the batch where a collection leaves them out. It reports what a
  `pain.001` would get, turned round to the creditor's side: a wrong count or sum,
  a repeated `EndToEndId`, a collection date already past, and an amount in
  another currency than the creditor account. The message is declared in the
  dictionary and checked against the published XSD, and two files from outside the
  project read with no finding. **Collections are not booked yet.**
  `POST /payments` and the drop folder refuse a `pain.008` by name until the next
  step books them.

- **A `pain.008` is decided and answered with a `pain.002`** (#131, the second
  step of direct debits). `POST /payments` and the drop folder take a file of
  collections through the same pipeline as a `pain.001`, with the account holder
  on the creditor side: the creditor account must be one the bank holds (`AC03`
  if not), open (`AC04`) and in the collection's currency (`AM03`), and a
  collection that states no mandate or no date of signature is `MD02`. **A debtor
  account the bank holds decides the collection by its own state and behaviour**:
  closed or `closed-account` is `AC04`, and `insufficient-funds` is `AM04` when
  the collection is more than that account has available. The debtor's balance is
  read and never changed, because the mock books only the account holder's side.
  `reject-file` and `silent` on the creditor account act on the file it sends, as
  they do for a `pain.001`. The README has the table. An accepted collection is
  given its settlement date: the requested collection date, rolled to a business
  day, and never before the business day after the bank can start on the file.
  `GET /_mock/collections` and `GET /_mock/collections/<EndToEndId>` show what was
  decided, and `/_mock/state` counts them. **Nothing is booked yet**: no balance
  moves and no `camt.054` is sent until the next step. A `--db` file from 0.4 or
  0.5 gains the `collection` table when it is opened (schema version 9).

- **An accepted collection credits the creditor account on its settlement date**
  (#131, the third step of direct debits). The bank sends a `camt.054` with a
  `CRDT` entry for each collection, one notification per account and settlement
  date, and the day's `camt.053`, the BAI2 statement of a NACHA-format account and
  a `camt.052` carry the same entries. An entry names the debtor, the
  `EndToEndId`, the file's `MsgId` and the mandate, and its bank transaction code
  is `PMNT`/`IDDT`/`ESDD`, as banks' own example statements write for a collected
  direct debit; the README names two. On a BAI2 statement it is type code `165`,
  which a bank's export writes for the proceeds of a debit collection. A collection
  that would take the account past the 18 digits a statement can write is rejected
  `AM02`; a day that becomes a holiday moves the collections still to settle on
  it; and an account closed while a collection waited is not credited until it is
  reopened. The notification is listed in `GET /_mock/queue` until it settles, and
  `GET /_mock/collections` shows the file's `received_at` and each collection's
  `booked_at`, both on the bank clock. Only the account holder's side books: a
  debtor the bank holds is not debited. The debtor's bank refusing or returning a
  collection is the next step.

- **The debtor's bank can refuse a collection or send it back** (#131, the last
  step of direct debits). `POST /_mock/collections/<EndToEndId>/refuse` with
  `{"reason": "MD01"}` is that bank's answer for a debtor at another bank. Before
  the collection settles it is rejected with the reason: the bank sends a further
  `pain.002` naming the original file and batch with that one transaction `RJCT`,
  and nothing books. After it settled the money goes back on the first day the
  bank can book it: the creditor account is debited, and the bank sends a
  `pacs.004` naming the `pain.008` and a `camt.054` debit, with the entry on that
  day's `camt.053`, BAI2 statement and `camt.052`. The debit's bank transaction
  code is `PMNT`/`IDDT`/`UPDD`, from the same two example statements as the
  credit's, and on a BAI2 statement it is type code `557`. Until a return books,
  both of its messages are listed in `GET /_mock/queue`. **A debtor account this
  bank holds with `return-later` sends a settled collection back by itself**,
  after its `days` and with its `reason`, and refusing one by hand is `409`:
  its own state and behaviour decide. A collection already rejected, on its way
  back or returned is `409` too, as is a return that would overdraw the account
  past what a statement can write. What is left of direct debits in the README's
  out-of-scope table is `pain.008.001.02` and a mandate register; a NACHA file's
  debit entries as collections follow in #176.

- **`GET /_mock/queue` lists what the bank is going to send and has not released**
  (#155), soonest first, each with `dueAt`. Until now that was visible only in the
  answer to `POST /payments` and as a bare count in `/_mock/state`, so a caller who
  did not post the file could not ask what was coming. Two kinds of entry, told
  apart by `written`: a message already written and held back, which is a status
  report under `--status-delay-ms` and has an `id`; and a message the bank will
  write when something books, which has none yet - the `camt.054` for payments
  accepted and not yet settled, the one for money arriving, and the `pacs.004` or
  NACHA return file and the credit a return brings. Those are due at the start of
  their day in bank time, and `reports` says which of them an entry is. Every
  entry has a `key`, and the message arrives in `GET /_mock/mailbox` under the same
  `key`, so a reader can pair what was waiting with what arrived: type, account,
  the day it books under and what it reports for a message the bank will write,
  `m<id>` for one already written. A database from an earlier version gains the
  `message.key` column (schema version 10), and its old messages answer to
  `m<id>`. `?type=`
  filters on a prefix, as the mailbox does. Reading it releases nothing and takes
  nothing. A statement is not listed: one is written for every open account when
  the clock is advanced past the end of a business day, not for anything that has
  happened.

- **The worked examples ship in the wheel, as `mockbank.examples`** (#169). After
  `pip install mock-bank`, `procure_to_pay` and everything it composes can be
  imported and run without cloning the repository:

  ```python
  from mockbank.examples import procure_to_pay, payment_run, invoice_check
  ```

  Asked for by mock-films, which plays this three-mock choreography and had no way
  to reach the code: `examples/` was in the sdist and in no importable place.

  `pyproject.toml` maps `examples/` onto that import path rather than moving or
  copying the files, so every README link still points at the file a reader is
  reading about. The modules import each other relatively, which means one import
  mechanism rather than a flat one inside the repository and a packaged one in the
  wheel - so the repository's own invocation changes from
  `cd examples && python3 -m unittest -v test_payment_run` to
  `python3 -m unittest -v examples.test_payment_run`.

  Importing needs `mock-bank` alone. The examples reach mock-sap and mock-edi over
  HTTP and import neither, and CI checks that from a clean install with nothing else
  in it.

  The import path is the only promise made about them. They are examples, not a
  supported client library: `payment_run` can still pay an invoice twice in the ways
  #164 lists.

- **A NACHA file of debit entries is a file of collections** (#176). An entry
  with transaction code `27`, `37` or `47` is read as a collection, with the
  company as the creditor and the receiver as the debtor, and is decided, recorded
  and booked by the code a `pain.008` goes through: the creditor account has to be
  one the bank holds and in dollars, a debtor the bank holds decides by its own
  state and behaviour, and on the settlement date the creditor account is
  credited. The answer follows the account's format, not the file's: a NACHA
  account gets its acknowledgement and a BAI2 statement line `165`, an ISO 20022
  account a `pain.002` and a `camt.054`. **A NACHA file carries no mandate**, so
  none is asked for and `MD02` does not apply; a `WEB` or `TEL` debit's payment
  type code is recorded as the sequence type. The entry's own code decides which
  kind a file is, whatever its service class. A file with both credits and debits
  is refused with a finding that says why, where it used to be refused for the
  debit alone; a prenote, a zero-dollar entry and a loan debit are each named for
  what they are. A `--db` file gains three columns on `collection` (schema version
  11). A NACHA collection coming back as a return file is the next step.

- **A NACHA account's collection comes back as a return file** (#176). A NACHA
  return file now carries collections as well as payments: one entry per
  collection with transaction code `26`, `36` or `46`, an addenda `99` with the
  `R` code, the original trace number and the receiver's bank, counted in the
  debit totals. `POST /_mock/collections/<EndToEndId>/refuse` takes an `R` code on
  a NACHA-format account - `R01`, `R02`, `R03`, and for the authorization `R05`,
  `R07`, `R08`, `R10` and `R29` - where it takes an ISO 20022 reason on any other.
  After the collection settled the money goes back with the return file and a
  `camt.054` debit, which says the reason in ISO 20022. Before it settled the
  collection is rejected and nothing books, and because NACHA has no message that
  rejects one entry of an accepted file, the return entry goes out on the day it
  would have settled. A collection rejected when the file arrived, for a reason
  with an `R` code, comes back in a return file the next business day as a
  rejected payment does, and `return-later` on a debtor account the bank holds
  sends a settled collection back with the `R` code for its reason. An ISO 20022
  account that sends a NACHA file of debits is answered as before, with a
  `pain.002` and a `pacs.004`. The README lists what a NACHA account is sent.

### Changed

- **A wrapped line joins the record above with nothing, not with a line break**
  (#142). This changes released behaviour, in 0.5.0 only: 0.3.0 and 0.4.0 refused
  such a file outright (`line 4 does not end with '/'`), and 0.5.0 read it with a
  `\n` left in the middle of the wrapped field, which is what #128 settled.

  ```
  16,495,125000,Z,INV-2026-
  0101,MSG-1,Globex Supplies B.V./

  v0.3.0, v0.4.0:  Unreadable: line 4 does not end with '/'
  v0.5.0:          reference = 'INV-2026-\n0101'
  now:             reference = 'INV-2026-0101'
  ```

  That matters beyond tidiness because `examples/payment_run.py` reconciles on the
  bank reference, so a wrapped reference was a payment that could not be matched to
  its invoice. Both readers change together, and
  `tests/test_payment_run_readers.py` now compares the two **field list by field
  list** rather than only the four values one caller uses — reverting one reader's
  join alone used to pass the whole suite, because both wraps in the corpus fall in
  a text field and the comparison stopped short of the text.

  Joining with nothing inserts nothing and **drops** nothing: a line that is
  continued keeps its trailing spaces, so `PAYMENT FOR ` followed by `INVOICE 12`
  reads `PAYMENT FOR INVOICE 12` and a fixed-width `ACME      ` keeps its column.
  Both readers used to strip every line before joining, and until this change the
  line break was what held those words apart. Trailing whitespace anywhere else is
  padding *around* a record and still comes off, exactly as before: a
  `49,+125000,2   ` that ends its record states 2. moov draws the line in the same
  place, trimming where the next line starts a record and leaving its buffer alone
  on the continuation path.

  #128 kept the break on an argument that does not hold: it said a space is a
  character the field could have contained and a newline is not, which argues
  against joining with a *space* and says nothing against joining with nothing. A
  newline left in a value is a character no producer meant either.

  **One implementation and no file.** moov-io/bai2's scanner joins with nothing — a
  newline never reaches its buffer and the continuation path appends to the same
  one. No BAI2 file in that repository wraps outside a `16`'s text, so nothing
  attests the case this rule is for. Of the two wraps that exist, `sample3`'s is
  filler that reads as meaninglessly either way; `sample5`'s is not — line 62 ends
  `GS ID: SC213480000120999` and line 63 is `88:EREF: 07370568132`, so the two
  references now run together. moov reads that the same way, and both readings are
  recorded on #142. One source rather than proof, and the rule is the PM's decision
  on that basis.

- **A booking is stamped with the bank's clock, not the host's** (#147). A payment's
  `booked_at` and a credit's recorded real time while the bank clock decided the
  booking, so a payment the bank booked on its own Sunday carried the real date of
  the run, and a reader ordering control-plane events by timestamp got two clocks
  mixed.

  ```
  --clock 2026-10-01T09:00, then advance

  was:  booked_at = 2026-10-01T05:19:13Z   (the real moment of the run)
  now:  booked_at = 2026-10-01T09:00:02Z   (the bank's)
  ```

  The rule, now stated in the README with a table: a stamp that records a moment
  **the bank clock decided** is on the bank clock, and a stamp that records when
  this process did something is on the real one. On the bank clock: a message's
  `releasedAt` and `dueAt` as before, and now a payment's `booked_at`, a credit's
  `booked_at`, a payment's `returned_at` for both kinds of return, and a file's
  `received_at`. Still real time, deliberately: `/_mock/requests`' `at`, `started`
  in `/_mock/state`, and the pickup folder's record of having written a file - each
  of those is about this process rather than about the bank's day.

  A credit's `received_at` was already bank time, because `create` is handed the
  bank's now; a file's was not, although `accounts.decide` judges the cutoff on
  exactly that moment. So one credit used to carry both clocks and could report a
  `received_at` later than the `booked_at` that followed it.

  **A file's `received_at` is now reported**, beside its `msg_id` on each of its
  payments at `GET /_mock/payments` and `GET /_mock/payments/<EndToEndId>`. It was
  written to the row and served nowhere, so a client had no bank-clock moment of
  receipt at all: the nearest was the `pain.002`'s `releasedAt`, which is the same
  moment only when `--status-delay-ms` is zero. A file of collections is stamped
  the same way - `accounts.record_file` is shared with `pain.008` since #131 step
  b1 - and serving it on `GET /_mock/collections` is that feature's own step.

  A database carried over from 0.5.0 with `--db` keeps the real-time stamps on the
  rows it already had, so one listing can show both clocks after an upgrade. Only
  new bookings are on the bank clock; nothing rewrites old rows.

  Nothing in the bank reads these values - every query tests them for `NULL` - so
  balances, statements, reports and returns are unchanged. Reported by the
  mock-films team, who read the control plane over HTTP.

- **A `/` at the end of a wrapped line is the field's content, not a terminator**
  (#150). One rule now covers the terminator: a `/` ends a record only when a
  record code and a separator follow it - on that line or on the next non-blank one
  - or when nothing follows at all. Before this it always ended a record at a line
  break, even where the next line continued it, and the character was dropped.

  ```
  16,495,125000,Z,AB/
  GS/RP0001,MSG-1,Globex/

  v0.5.0:  reference = 'AB\nGS/RP0001'     the slash lost, a newline left in
  0.6/#142: reference = 'ABGS/RP0001'      the slash lost, the lines joined
  now:      reference = 'AB/GS/RP0001'     what the file says
  ```

  So this changes released behaviour in 0.5.0 **and** in 0.6's own #142: both lose
  the slash, by different routes. v0.4.0 refused a file of this shape outright
  (`line 2 has record code 'GS/RP0001', which is not declared`), because a wrapped
  line was not something it read at all.

  It matters for the same reason #142 did: `examples/payment_run.py` reconciles on
  the bank reference, so a reference missing a character is an invoice that cannot
  be matched. Both readers change together and
  `tests/test_payment_run_readers.py` holds them to each other field list by field
  list.

  No vendored file has this shape - five BAI2 samples, two wrapped lines between
  them, and neither ends in `/` - so the case is pinned by a constructed file, and
  all five samples read exactly as before, record for record. moov's scanner at
  `aee8612` appears to end the record at the `/`, which is option 2 of the three on
  the issue; that reading comes from the code and not from running it, no Go
  toolchain being available, and the decision does not rest on it. If somebody runs
  moov and it differs, that is a new issue.

### Fixed

- **A rejected payment moves no balance on a statement or a report** (#144). A
  NACHA account's rejections are answered as return entries the next business day
  and nothing is credited back, because nothing was debited (#54, option (a)).
  `outbox._position` did not know that: it read every payment with a `returned_at`
  as money that came back. So the statement for the settlement day understated both
  balances by the rejected total, and the day the returns went out booked that
  total as credits that never happened. Written that way by 0.3.0, 0.4.0 and
  0.5.0 - as `165` in the first two and as `257` from 0.5.0, where #127 settled the
  real type codes; the credit is wrong in all three. The two errors cancel, so the
  account's own balance and every later statement were right, and the file
  reconciles against itself - which is why no reader and no control total could
  find it. The `camt.052` for a NACHA account
  read the same numbers, and `still_to_arrive` reserved balance headroom for
  returns that were never going to credit. A return now moves a balance only if the
  payment was booked. Also new: the check that catches this on its own, one file
  run into one account as both `nacha` and `iso20022` so the BAI2 statement and the
  `camt.053` for the same day are compared with each other instead of each with
  itself.

- **Input the bank cannot answer for is refused at the door, not booked and then
  left unanswerable** (#166, the first of two parts). Six values were accepted that
  a later message could not hold. Four of them then made **every**
  `POST /_mock/advance` and every mailbox read answer 500 until the mock was reset,
  because the same unwritable message was retried on each one.

  Settled at the door now, before anything is written:

  - An account **name** no message can carry - empty, or only spaces - is `400`,
    naming the element the writer refused. A name longer than the standard allows is
    written, shortened to fit, rather than refused.
  - A `pain.001` whose own identifiers cannot be echoed back - a `MsgId` over 35
    characters, a control sum of more than 18 digits - is `422`, `RJCT`/`FF01`,
    saying the status report could not be written. Nothing is booked and nothing is
    queued: the `pain.002` would have to carry the same value back, so there is no
    answer to send.
  - On a NACHA account, an **`InstrId`** longer than a return addenda's original
    entry trace number rejects that payment with `FF01`. A NACHA account's
    rejections come back as returns (#54), and at receipt the bank cannot know
    whether this payment will.
  - Changing an account's **`format`** while a return is already scheduled on one of
    its payments is `400`, saying how many, with which reason, and the last day one
    is due. A return's reason is stored when the payment books, so the switch would
    leave behind a reason the new format cannot write. It is allowed again once the
    returns have gone back.

  **A creditor or debtor account that is not an IBAN is written back the way it
  came.** This one was not bad input: an account given as `Othr/Id` is valid, and
  the bank accepted it. The writers put it into an `Id/IBAN` element, which holds no
  spaces, and refused it on the way out. They now choose by the XSD's own pattern -
  `Id/IBAN` when it strictly is an IBAN, `Id/Othr/Id` when it is not - so
  `NL30 MOCK 0000 0000 05` is accepted, matched to the account it names, and
  reported as `Othr/Id`. `schema.iban_is_valid` forgives spaces on the way in,
  deliberately, which is what made it the wrong test for the way out.

  The pattern throughout is `credits.create`'s, which has refused an unreportable
  credit at the door since #106: write the message once, now, the way the release
  will write it, and refuse the input the writer refuses.

  Still to come, in the second part: one message that cannot be written must not
  stop the others.

  Found by DJ's review of the three mocks.

- **One message the bank cannot write no longer stops the others** (#166, the second
  and last part). Part 1 shut the doors on values a later message could not hold;
  this is what happens when one gets through anyway - and one always can, because a
  `--db` file carries rows an older mock wrote, before a guard existed.

  Before, the writer's `ValueError` came out of the release itself: the day's other
  bookings were abandoned with it, and **every** later `POST /_mock/advance` and
  mailbox read tried the same message again and answered 500, until the mock was
  reset. One account with an unwritable name stopped the whole bank.

  Now the booking stands - a payment that booked has booked, a return that came back
  has come back - and only the message is given up on, with the writer's own
  complaint kept:

  ```
  GET /_mock/unsent
  [{"type": "camt.053.001.08", "account": "OLD", "day": "2026-10-01",
    "problem": ".../Acct/Ownr/Nm: Nm is empty", "at": "2026-10-01T09:00:00Z"}]
  ```

  `GET /_mock/state` counts them under `messages.unsent`, so a tester sees something
  is missing without knowing to look. Three things follow, each on purpose:

  - **Every other message of that release is still written.** One account's
    unwritable statement stops neither another account's nor the day's bookings.
  - **Nothing retries it.** The value will not fix itself, so retrying would add a
    row per advance for ever. A statement given up on is recorded as issued with no
    message, so `GET /_mock/accounts/<id>/statements` shows `"message_id": null`.
    That null means "no message to fetch" and nothing more: `--retention-days` nulls
    it as well, when a `camt.053` that was sent and collected ages out.
    `GET /_mock/unsent` is what says the bank could not write one.
  - **A reset forgets it**, because a reset is a new bank; on `--db` it survives a
    restart, because the row that caused it does.

  `POST /_mock/accounts/<id>/report` is outside this: it answers one request, so a
  report it cannot write fails that call and blocks nothing.

  Schema version 12 adds the `unsent` table. A `--db` file from 11 gains it empty -
  nothing is invented for a message an older mock failed to write, because nothing
  recorded it.

  **One door part 1 missed is closed too.** `POST /_mock/accounts` did not try the
  writers the way `PATCH` does, and `create` falls back to the id only when the name
  is *empty* - a name of only spaces is truthy, so it survived. That made every
  later advance a 500 with no payment involved at all, since the end-of-day
  `camt.053` names the account. It is refused now, and creating an account with no
  name still takes the id.

  Found by DJ's review of the three mocks.

- **A payment run no longer pays an invoice twice in two of the ways it could**
  (#171, the two of the five listed on issue 164 that need nothing from mock-sap).

  **A reference a BAI2 statement cannot carry back unchanged is skipped, with the
  character named.** BAI2 has no escape character, so a `,` or a `/` in the
  reference is replaced with a space on the way back, and SAP matches the structured
  reference exactly. The payment was made, never matched to its invoice, and the
  invoice stayed open - so the next run selected it and paid it again, with nothing
  in either log saying anything had gone wrong. The run cannot change what the
  statement carries, so skipping it beforehand is the only honest answer it has:

  ```
  reference 'GLX,4711' holds ',', which a BAI2 statement cannot carry back
  unchanged, so the payment could not be matched to the invoice
  ```

  The characters are `bai2.UNSAFE` and `bai2.CONTROLS` - a comma, a slash, the three
  Unicode line separators, and the C0 controls with `DEL`. The example imports no
  mock and so writes the set out, and `tests/test_payment_run_readers.py` holds the
  copy equal to the writer's own, because a copy that nothing checks drifts.

  **A clearing is attributed by accounting document, not by invoice number.** An
  invoice number is a supplier's own sequence, so two suppliers can both bill
  `INV-1`. The run built `{item.reference: item}`, which collapsed the two into one
  entry before any matching happened and dropped the earlier one - so a clearing
  could be recorded against an item SAP never cleared while the item it did clear
  was left looking unpaid, and paid again. SAP returns `ACCOUNTINGDOCUMENT` on every
  `CLEARED` and `REOPENED` row and it is unique; the run reads it now. A row that
  names no accounting document goes into `run.problems` rather than passing
  silently. rseufert/mock-sap#87 is the other half: it makes SAP pick the right item,
  and this makes the run agree with whichever item SAP picked.

  The three other faults on issue 164 need mock-sap and stay there.

## [0.5.0] - 2026-09-29

### Added

- **An intraday report, `camt.052`, on request** (#132).
  `POST /_mock/accounts/<id>/report` writes a `camt.052.001.08` for the account
  as it stands now, and releases it to the mailbox and the pickup directory at
  once. It carries the day's opening balance (`OPBD`), the balance now (`ITBD`)
  and every entry booked today, worked out exactly as the day's `camt.053` will
  be. Anything already due is booked first. `statement-gap` doesn't apply to a
  report, so a reconciler can compare the two and find the entry the statement
  leaves out. Reports are numbered on their own sequence and carry no
  `LglSeqNb`. Every account can have one, whatever its `format`; a closed account
  is refused with `409`. The message is declared in the dictionary and validated
  against the published XSD in CI, like the others. No schema change.

### Changed

- **The BAI2 statement writes real transaction type codes** (#127). **This changes
  the bytes the mock writes**, so a client that matched on the old codes will
  stop matching:

  | Movement | Was | Now | Its description in the BAI2 code table |
  | --- | --- | --- | --- |
  | A payment sent | `495` | `447` | ACH Disbursement Funding Debit |
  | A payment returned | `165` | `257` | Individual ACH Return Item |
  | Money arriving (#91) | `195` | `142` | ACH Credit Received |

  The old codes were placeholders, and they were wrong. A bank writes `495` for
  an outgoing wire, `165` for the proceeds of a debit collection and `195` for an
  incoming wire. Each new code has a named source, vendored under
  `tests/samples/external/`: moov-io/bai2's transcription of the type code table,
  and two of its sample files that are a bank's own exports. `447` and `142` are
  what those exports write for an ACH credit payment sent and an ACH credit
  received. For `257`, the evidence is the table's description, since the
  samples' only `257` is a returned debit. A reader that takes the direction from
  the code's range (100–399 a credit, 400–699 a debit), as `payment_run` does,
  reads the new codes as it read the old ones. `bai2.PLACEHOLDER_CODES` is now
  empty.

### Fixed

- **`mockbank.bai2.read` knows where a BAI2 record ends** (#128). #114 taught it
  continuations and funds-type widths; it still assumed one record to a line, a
  terminator on every record, and a field count it could get by counting commas.
  Three real files say otherwise, and all three are now vendored.

  **A `16`'s text runs to the terminator, commas and all.** BAI2 has no escape
  character, so the only field that may contain a separator is the last one.
  `sample4` writes `ACH Credit Payment,Entry Description: EXP; -, SEC: CCD, Client
  Ref ID: 1111` as a single field, which counting commas made four fields too wide.
  The declaration says so now, with a `REST` kind, rather than the reader knowing
  it by position.

  **A `/` is not a delimiter to split on.** `sample5` carries customer references
  like `AB/GS/RPFILERP0001/RPBA0001` and remittance text like `08/18/23 Invoice` -
  twenty-two slashes inside fields, every one of which a naive split would have
  shattered. A `/` ends a record only when a declared record code and a separator
  follow it. Measured across all five samples that is exact both ways: all eleven
  of `sample3`'s packed records are found, and none of `sample5`'s content slashes
  is mistaken for one.

  **A newline is not a delimiter either.** `sample4` leaves 102 of its 116 lines
  unterminated, so a newline usually does end a record - but `sample3` wraps one
  `16`'s text onto a second line that carries the terminator, and `sample5` writes
  a continuation as `88:EREF: ...` with a colon where the separator should be.
  Taking every newline as a terminator makes those two records whose codes are
  `111111111111111` and `88:EREF: 07370568132`. So the rule runs the other way: a
  line starts a record when it begins with a declared code and a separator, and
  otherwise continues the record above. Across the five samples exactly two lines
  continue one, and they are those two.

  Field padding is content and is kept: `RETURNED CHEQUE     ` is twenty
  characters in a fixed-width field, and an earlier draft of this change stripped
  five of them off the end of the record.

  Two of the new samples read and do **not** reconcile. That is those files rather
  than the reader - each states one account total of `-1260161341762` and two of
  `000`, and its group trailer is the sum of the positive ones alone. Every coded
  line in both is accounted for. A test pins the disagreement with its arithmetic,
  because the tempting conclusion when a total does not match is that the reader
  is wrong.

- **A comma in a creditor's name reaches the BAI2 statement** (#129). The
  `camt.053` of a statement said `Umbrella, Logistics, S.A.` and the BAI2
  rendering of the same statement said `Umbrella  Logistics  S.A.` — two
  renderings disagreeing about who was paid, since 0.3.

  `_safe` replaced the comma to stop it ending a delimited field early. The `16`'s
  text is not delimited: it is the last field of the record and runs to the
  terminator, which is why a real file can carry `ACH Credit Payment,Entry
  Description: EXP; -, SEC: CCD, Client Ref ID: 1111` as one field. So the
  sanitising was protecting a field that needed none, and the cost was a statement
  that misnamed the payee. A slash is kept for the same reason — `sample5` carries
  twenty-two of them inside fields.

  One sequence a run-to-end field still cannot carry is `/` before a record code
  and a separator, which the reader takes for the end of a record. The writer
  replaces **the separator** that completes it, keeping the slash: `Umbrella/16,Inc`
  is written `Umbrella/16 Inc`. Neutralising the slash instead — the obvious
  version — is wrong, because the reader's rule allows whitespace after the slash,
  so blanking or removing one exposes the one before it: `A//16,B` becomes
  `A/ 16,B` or `A/16,B` and still splits. Replacing the separator needs one pass
  and provably so, since a match requires a comma.

  **Money arriving had the same defect**, one branch over: the payer's name was
  sanitised where the payee's was, so a `camt.054` and a `camt.053` said
  `Customer, Ltd` while the BAI2 statement said `Customer  Ltd`. Both branches
  pass the name through now.

  The `16`'s text is made safe from its declaration rather than at the call site,
  which is where every other field has been made safe since #57. That matters here
  beyond tidiness: a line break in a name would otherwise put a **forged record**
  into the file, because since #128 a line beginning with a declared code and a
  separator starts one. A payee called `Foo\n49,+0,2` wrote a second account
  trailer. The writer has always replaced line breaks and control characters, so
  this was never reachable - but nothing held that guard, and removing it passed
  every test in the suite.

  **And the assertion that was missing.** `tests/test_bai2.py` exists to hold the
  two renderings of one statement to each other, and it compared each entry's
  reference and amount and stopped there — so the disagreement about the payee had
  nowhere to surface. It compares the name now, which is worth more than the fix:
  the fix was one line, and nothing would have caught the next one.

- **`payment_run` reads a real bank's BAI2 statement** (#130). Its reader, which
  the example writes by hand because it imports neither mock, could only read the
  files mock-bank writes. It now reads what `mockbank.bai2` reads since #128:
  funds types `V`, `S` and `D` (the bank reference moves by the fields each one
  takes, where before they were refused), several records packed onto one line, a
  record wrapped onto the next, and texts full of commas and slashes. A slash
  followed by a record code is only a record boundary when a comma follows the
  code. An account the bank reports without both ledger balances (`010`, `015`),
  such as an intraday position, no longer stops the rest of the file. If it is the
  paying account, `reconcile` names it in `run.problems` instead of posting it.
  Over moov-io/bai2's five files, every movement reads as `mockbank.bai2` reads
  it.

## [0.4.0] - 2026-09-28

### Added

- **Money can arrive** (#91). `POST /_mock/credits` makes a credit arrive in an
  account the bank holds, from a payer the test describes: the amount, the value
  date, the payer's name, account and bank, a structured reference, and the note
  to payee. It books on its value date, or the next business day for a weekend,
  a holiday or a missed cutoff, and it is reported like everything else: a
  `camt.054` as it books, and an entry on the day's `camt.053`, which still
  reconciles. That entry is a received transfer, `CRDT` under
  `PMNT`/`RCDT`/`ESCT`, with the payer as `Dbtr`, the note in `RmtInf/Ustrd`, and
  the structured reference apart from it in `RmtInf/Strd/CdtrRefInf`. `wrap`
  re-cuts the note at 35 or 70 characters wherever the cut falls, invoice numbers
  included, as a bank reformatting remittance does. So a short payment, a payment
  that names no invoice, one that settles several, and a reference split across
  lines are each one request away, and cash application can be tested against the
  mock at last. `GET /_mock/credits` lists them. Schema version 8 adds the table.

  What the bank could not report is refused with a reason when it is sent, never
  stored. In a BAI2 statement, money arriving is type code `195`, a received
  transfer, which is a placeholder like the other two until #57 settles them. An
  account closed while a credit waits does not book it. If the
  account is reopened, the credit books on the next business day.

  **Changed for `statement-gap`:** credits come after a day's debits, and the gap
  leaves off the day's last entry. On a day money arrives, the entry left off is
  now that credit, where it used to be the last debit. Exactly one entry is still
  missing.

- **A worked example using all three mocks** (#93). `examples/procure_to_pay.py`
  carries one purchase from a purchase order in SAP to a cleared payment at the
  bank: an `850` out, the supplier's `855`/`856`/`810` back, a three-way match, an
  `INVOIC` posted as an open payable, a payment run, and the statement that clears
  it. The other examples each use two mocks, and this exists for what only appears
  between them - each pair can be correct while the chain is broken, because each
  pair's tests assert what the *next* system received rather than what it could do
  with it. Three bugs in mock-sap's `invoice_check` were found this way and none
  was visible to its own green tests: an `INVOIC` naming no supplier that created
  no payable, a mock reporting status 53 for having posted nothing, and an order
  placed in one currency that came back invoiced in another
  (rseufert/mock-sap#68, #67, #74). The match is a checked copy of mock-sap's
  example and the payment run is this repository's, composed rather than
  reimplemented; the only new logic is `DurableInvoiceCheck`, which asks SAP
  whether a supplier invoice is already there instead of remembering in a `set`.
  It does not tell the supplier what was paid - that needs a remittance advice,
  which mock-edi does not speak yet (rseufert/mock-edi#149).

### Fixed

- **A balance a statement cannot write is refused where it would arise** (#106).
  A camt amount has 18 digits, so a balance past that made every statement for its
  account a 500, and `POST /_mock/advance` answered 500 until a reset. A `PATCH` or
  a new account with such a balance is now refused with a 400. So is a `PATCH` or a
  credit that would leave no room for the returns due back and the credits waiting
  to book. A payment that would overdraw past the limit is rejected with `AM02`,
  under every behaviour.

- **A holiday declared after something was due on it no longer loses it** (#107).
  A settlement, a return and a credit have their date fixed when the bank accepts
  them. Declaring that day a holiday afterwards left them booking on it, but no
  statement is issued for a holiday, so the entries appeared on none. The next
  statement then opened at a balance the one before had not closed at. Whatever is
  still due on a newly declared holiday now moves to the next business day,
  payments and credits by the same rule. Declaring today a holiday is refused with
  a 409 once something has booked on it, because that cannot move.

- **`mockbank.bai2.read` reads a real bank's BAI2 file** (#114). It refused any
  file containing an `88`, and would have misread two more things had it got past
  that. All three were one wrong assumption - that a record's width is a constant.

  **A continuation continues a field stream, not a record.** `88` is folded into
  the record above before anything is parsed. It may follow any record and it
  chains, and a summary group can split straight across the boundary, its type code
  ending one record and its amount beginning the next - which is why declaring it
  with fields of its own would also have been wrong.

  **A funds type carries its own width**: one field for blank, `Z` or a digit;
  three for `V`, followed by an availability date and time; four for `S`, followed
  by three availability amounts; and 2 + 2n for `D`, followed by a count and that
  many (days, amount) pairs. The old reader assumed one, which made a value-dated
  `16` too wide to accept and made a summary group pass the "repeats in fours"
  check whenever a `V` group appeared in pairs - then read every amount out of the
  wrong slot and raised a bare `ValueError` rather than refusing. `statements` was
  reading a value-dated entry's availability date as its bank reference for the
  same reason.

  **An amount may be signed or bare**, which corrects what #57 recorded here: that
  sample settled *a* notation, not the notation. Most real files write control
  totals bare. The mock still signs what it writes, as a choice rather than the
  format's requirement.

  `statements` is narrower than `read` now, and says so: it needs the two ledger
  balances, and a file that reports available balances instead reads fine and
  cannot be reduced to what a `camt.053` asserts.

  Settled against `tests/samples/external/bai2-sample2.txt`, vendored from
  [moov-io/bai2](https://github.com/moov-io/bai2) at the same pinned commit as
  `sample1.txt`. Three things it still does not read, each pinned by a passing
  test: a `16` whose text field contains commas, several records packed onto one
  line, and a record with no `/` where the newline terminates.

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

[Unreleased]: https://github.com/rseufert/mock-bank/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/rseufert/mock-bank/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/rseufert/mock-bank/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/rseufert/mock-bank/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/rseufert/mock-bank/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/rseufert/mock-bank/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/rseufert/mock-bank/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rseufert/mock-bank/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rseufert/mock-bank/releases/tag/v0.1.0
