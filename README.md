# mock-bank

[![CI](https://github.com/rseufert/mock-bank/actions/workflows/ci.yml/badge.svg)](https://github.com/rseufert/mock-bank/actions/workflows/ci.yml)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/mock-bank)](https://pypi.org/project/mock-bank/)

**A mock bank.** Not a payments library and not a bank API client — the thing
on the *other end*. Send it a `pain.001` and it sends back a `pain.002` that
accepts or rejects each payment, then a `camt.054` debit notification on the
settlement date, then a `camt.053` statement at the end of the business day,
and, if you ask it to, a `pacs.004` that returns a payment three days after it
settled.

```
   you ──pain.001──▶  mock-bank
       ◀──pain.002──  PART: 2 accepted, 1 AC04 (account closed), 1 RC01 (bank not found)
       ◀──camt.054──  2 debits on the settlement date
       ◀──camt.053──  end of day: opening, 2 entries, closing
       ◀──pacs.004──  (with a return behaviour) one payment comes back, 3 days later
```

Banks are harder to test against than trading partners. A bank sandbox takes
weeks of onboarding, returns only the happy path, and cannot be asked to
bounce a payment three days after accepting it. The failures that cost money —
a rejected file nobody noticed, a return after the invoice was closed, a
statement line that never reconciles — are exactly the ones nobody can
rehearse. This is the counterparty that does them on demand.

- **Zero dependencies.** Python 3.8+ standard library and SQLite, nothing else.
  ISO 20022 XML through `xml.etree`, NACHA as fixed-width text. It installs in
  a locked-down CI image.
- **Real wire shapes.** Namespaced ISO 20022 messages that validate against
  the published schemas, real reason codes, balances that add up.
- **Failure on demand.** A closed account, insufficient funds, a rejected file,
  a duplicate, a late return, a silent bank, a statement with a hole in it —
  each one `PATCH` away, never a code change.
- **It validates its own output.** Every message the mock writes is checked
  against the same rules it checks yours against, for every account behaviour:
  that is `GeneratedMessagesAreValid` in `tests/test_dictionary.py`. And
  because those rules could be wrong in the same way the writers are, the
  dictionary is held to files from outside the project and, by
  `tools/check_xsd.py`, to the published XSDs.
- **No sleeping.** Settlement dates, cutoffs and returns move on a clock that
  `POST /_mock/advance` moves, so a three-day return is a test line, not a wait.

It is not a bank. It holds no real money, runs no fraud or sanctions
screening, and imitates publicly documented formats for testing only.

MIT licensed. ISO 20022 is a standard of ISO maintained by SWIFT as
Registration Authority; NACHA and BAI2 formats are published by Nacha and BAI
respectively. This project implements publicly documented wire formats for
testing purposes and is not affiliated with or endorsed by any standards body,
bank or vendor.

By [Rick Seufert](https://rickseufert.com). The [projects page](https://rickseufert.com/#projects)
has this mock, [mock-sap](https://github.com/rseufert/mock-sap),
[mock-edi](https://github.com/rseufert/mock-edi) and the worked examples that
use them together. mock-bank is the third leg: `po_bridge` sends the order,
`invoice_check` approves the invoice, and `payment_run` (coming in 0.2) moves
the money. Until then, [`pay_invoices`](#worked-example-paying-the-suppliers-invoices)
pays mock-edi's invoices through this mock directly.

---

## Quick start

Install it and start a bank:

```bash
pip install mock-bank
mock-bank --port 8080
```

Then, in another terminal:

```bash
curl http://127.0.0.1:8080/_mock/health
curl http://127.0.0.1:8080/_mock/accounts
```

The guided tour lives in the repository rather than in the installed package,
so it needs a checkout — which needs nothing installed either:

```bash
git clone https://github.com/rseufert/mock-bank && cd mock-bank
python3 -m mockbank --port 8080 &
bash examples/demo.sh          # the whole choreography, in curl
python3 examples/client.py     # the same thing, as a client you would copy
python3 -m unittest discover -s tests -v
```

`demo.sh` sends one payment file and shows you every message the bank sends
back, then breaks it on purpose so you see a rejection and a duplicate too.
`client.py` does the same in Python and matches each answer to the payment it
is about by `EndToEndId`, which is what your own integration has to do. Both
take `BANK_AUTH=user:password` for a mock started with `--auth`, and both ask
the mock what it supports rather than assuming.

## Formats and choreography

ISO 20022 comes first; NACHA and BAI2 follow in 0.3. You send one payment file
and the mock answers with the messages a real bank sends, in the order it
sends them.

| Message | Direction | When the mock sends it | What it carries |
| --- | --- | --- | --- |
| `pain.001` | In | You send it | Credit transfers: debtor account, one or more payments, amounts, creditors |
| `pain.002` | Out | Minutes after `pain.001` (`--status-delay-ms`, default at once) | Status per file, batch and payment: `ACCP`, `RJCT` with a reason code, `PART` when some are rejected; a file rejected outright gets its group status only |
| `camt.054` | Out | Each payment's settlement date | A debit notification per account each time payments book, an entry per payment, each carrying its `EndToEndId` |
| `camt.053` | Out | End of each business day | The statement: opening and closing balance, every entry, balances that reconcile; one per open account per business day, empty days included |
| `pacs.004` | Out | N business days after settlement, under `return-later` | A payment that had settled, coming back: its `EndToEndId`, what comes back and when, and the return reason. A `camt.054` credit comes with it, and the day's `camt.053` shows a `CRDT` entry whose `RtrInf` names the reason |
| NACHA in, returns out (`R01`, `R02`, `R03`), BAI2 statements out | Both | As above, in US formats | The same choreography for ACH **(not yet — 0.3)** |

Versions: `pain.001.001.09` is read, and the older `pain.001.001.03` is
accepted as well and read into the same model. The mock writes
`pain.002.001.10`, `camt.054.001.08`, `camt.053.001.08` and `pacs.004.001.09`,
the versions that go with `pain.001.001.09` and that most banks accept today. That is a choice, not
the only right answer; so are the others the standard leaves open, and the
mock says which it made:

- **Returns.** A return reaches the client as a `pacs.004`, whose
  `OrgnlGrpInf` names the client's own `pain.001` - standing in for the
  interbank message a real bank would pass on - with `SttlmMtd` `INDA`, settled
  on the bank's own books. The credit it books carries `PMNT`/`ICDT`/`RRTN`.
  One `pacs.004` per account, day and original file; one `camt.054` credit per
  account and day.
- **Bank transaction code.** Every debit the mock books carries
  `PMNT`/`ICDT`/`ESCT` (payments, issued credit transfer, SEPA credit
  transfer). Euro credit transfers are what this release speaks; other
  rails are listed under what is out of scope.
- **Coverage.** The dictionary declares the elements the mock reads and
  writes and those a real bank's file commonly carries, not every optional
  element of the XSD. An element the standard allows but the dictionary does
  not declare is reported as a warning naming its path, never silently
  dropped.
- **Minor units.** Amounts are integers in the currency's ISO 4217 minor units
  everywhere but the wire; a currency the mock has no exponent for is taken to
  have two.

Every element of every message is declared once in `mockbank/schema.py`, and
`GET /_mock/dictionary` serves those declarations, with the code sets and the
choices above, as JSON.

## Validating a file

`POST /_mock/validate` reads a `pain.001` the way the pipeline will and says
what it finds, one line each, without changing anything. The sample below asks
for execution on 2026-10-01, so the mock is started with bank time before that
date — otherwise the answer gains a `DT01` warning and this example stops
matching what you see:

```
$ python3 -m mockbank --port 8080 --clock 2026-09-30T09:00 &
$ curl -s --data-binary @tests/samples/pain001_broken_iban.xml http://127.0.0.1:8080/_mock/validate
pain.001.001.09 ACME-20261001-0001: 1 batch, 4 payments, 1 finding
error AC01 at /Document/CstmrCdtTrfInitn/PmtInf[1]/CdtTrfTxInf[2]/CdtrAcct/Id/IBAN: NL85MOCK0000000003 fails its check digits
```

Run it without `--clock` after that date and you get the warning as well, which
is the mock being right rather than the example being wrong:

```
warning DT01 at /Document/CstmrCdtTrfInitn/PmtInf[1]/ReqdExctnDt: the requested execution date 2026-10-01 is in the past; the bank executes on the next business day instead
```

Each finding names the element and carries the reason code the `pain.002`
will report it with: `FF01` for anything structural or a file the mock cannot
read (it names what it does read), `AM10` for a wrong `CtrlSum`, `AM18` for a
wrong `NbOfTxs`, `AC01` for an IBAN that fails its check digits, `AM03` for a
currency that is not the debtor account's (or a request to convert one),
`AM05` for an `EndToEndId` used twice in one file. Signed and encrypted files
and DTDs are refused by name. A requested execution date in the past is a
`DT01` *warning*, not a rejection: banks differ, and the mock follows the
common SEPA profile of executing on the next business day.

A `camt.053` closes each business day for every open account the bank holds,
in order, as the clock passes the day's end - a day with no entries still gets
one, because a real bank sends it and a client that only handles days with
activity has a bug. The closing balance (`CLBD`) is the opening (`OPBD`) less
the day's entries, to the cent, and the next statement opens where this one
closed. Statement numbers never repeat, across restarts too. Under
`statement-gap` the last entry of a statement is left off and the balances stay
true, so the difference is exactly the missing payment - which is what a
reconciliation has to notice. A balance changed by `PATCH` is a change no entry
explains, and the next statement's opening shows the jump. A closed account
gets no statements.

The `camt.054` profile is a choice too: the mock sends one debit notification
per account each time payments book - on receipt when the settlement date is
today, or when the clock crosses it - with an entry per payment, rather than one
notification per payment. Both are common; this is the one it follows. A body
the mock cannot read far enough to find a `MsgId` gets no `pain.002`, because a
status report has to name the message it reports on; `POST /payments` says so.

Each payment keeps its `EndToEndId` through every message, so a client can
match a status, a statement line and a return to the invoice it paid.

## Account behaviours

Each seeded account has a behaviour, changed at runtime with
`PATCH /_mock/accounts/<id>`, the same way mock-edi changes a partner. The
reason codes are the ISO 20022 external codes a real bank uses.

| Behaviour | What the bank does | Codes |
| --- | --- | --- |
| `accept` | Accepts every payment and settles it on the requested date | `ACCP`, then booked |
| `closed-account` | Rejects payments to one creditor account in `pain.002` | `AC04` |
| `insufficient-funds` | Rejects payments once the debtor's balance would go negative | `AM04` |
| `bad-bank-id` | Rejects a payment whose creditor bank identifier does not resolve | `RC01` |
| `return-later` | Accepts and settles, then returns the payment N business days later: `parameters` `days` (default 3), `reason` (default `AC04`), and `end_to_end_id` to return only that payment | `pacs.004`, `AC04`, `MD07` or another return reason |
| `reject-file` | Rejects the whole file at group level | `RJCT`, `FF01` |
| `duplicate-file` | Rejects a file whose `MsgId` it has already seen | `DUPL` |
| `silent` | Sends no `pain.002` at all | none |
| `statement-gap` | Leaves one settled entry off the `camt.053` | none, which is the point |

### The accounts it starts with

Four, the same four every time, one per failure you are likely to want. The
IBANs carry real mod-97 check digits and `MOCK` is not an assigned bank code,
so they are valid to parse and belong to nobody. They are the same accounts
`tests/samples/pain001_four_payments.xml` names, so the sample file works
against the mock out of the box.

| Id | IBAN | Balance | Behaviour | Why it is there |
| --- | --- | --- | --- | --- |
| `ACME` | `NL41MOCK0000000001` | 125,000.00 EUR | `accept` | The debtor everything works from |
| `GLOBEX` | `NL14MOCK0000000002` | 12.50 EUR | `insufficient-funds` | One ordinary payment breaches it |
| `INITECH` | `NL84MOCK0000000003` | 0.00 EUR | `closed-account` | Closed; the creditor a payment is rejected for with `AC04` |
| `EURODIS` | `NL57MOCK0000000004` | 0.00 EUR | `bad-bank-id` | A `BIC` of the right shape (`ZZZZNL2AXXX`) that resolves to nothing |

Balances are whole numbers of minor units everywhere the mock talks about
money: 12.50 EUR is `1250`, and `PATCH`ing `12.50` is a `400` rather than a
rounding. `POST /_mock/reset` brings these four back exactly as they are here.

There is no debtor/creditor field: which side an account stands on belongs to a
payment, not to the account. The sample pays `GLOBEX`, and a test that wants
insufficient funds sends *from* `GLOBEX`. The sample's fifth party,
`NL30MOCK0000000005`, is deliberately not seeded - a creditor at another bank
is not an account this bank holds, and a payment to one simply settles.

A behaviour describes the account it is set on. `closed-account` and
`bad-bank-id` describe a *creditor*: they are read from the account a payment
pays into, when the bank holds it. The other seven describe a *debtor* and are
read from the account a batch pays from. When a file arrives, the first rule
that applies wins:

1. A finding about the file as a whole - it cannot be read, it breaks the
   structure (`FF01`), or a header total is wrong (`AM10`, `AM18`) - rejects it
   outright (`422`), whatever the behaviour.
2. A `MsgId` the bank has received before is `DUPL`, as at a real bank;
   `--allow-duplicates` turns that off. (So `duplicate-file` is the default
   already.)
3. A debtor account with `reject-file` rejects the file: `RJCT`, `FF01`.
4. Then each payment, in file order: a debtor account the bank does not hold is
   `AC02` and a closed one `AC04`; a finding about the payment rejects it with
   its code; a currency other than the debtor account's is `AM03`; a held
   creditor account that is closed or `closed-account` is `AC04`, and one that
   is `bad-bank-id` is `RC01`; under `insufficient-funds`, a payment that would
   take the available balance below zero is `AM04`, and later smaller ones that
   fit are still accepted.
5. `silent` decides and books, and reports nothing.

Only `insufficient-funds` looks at the balance: under every other behaviour a
payment books even below zero, as on an account with an overdraft. A creditor
at another bank - a well-formed IBAN the mock does not hold - is not something
a bank can check at acceptance, so a payment to it settles.

An accepted payment debits its account on its settlement date, not on
receipt. The mock books the **debit side only**: a payment into an
account it holds does not credit that account, so every balance change has a
statement entry to explain it. That is a choice, and it is stated here. The one
credit it books is a return, which puts the money back where it came from, with
a `CRDT` entry on that day's statement to explain it.

Two rules hold whatever the behaviour, because real banks apply them:

- A payment's settlement date is **the later of the requested execution date
  and the day the bank can start on** — which is the day of receipt before the
  cutoff and the next business day at or after it — **rolled forward past
  weekends and holidays.** The cutoff is 15:00 bank time by default
  (`--cutoff`), and at 15:00 exactly it is already too late, because a bank
  that stops taking today's work at 15:00 has stopped at 15:00:00.
- Weekends and the holiday list at `GET/PUT /_mock/holidays` are not business
  days. Bank time is one zone, `--timezone`, UTC by default.

Nothing waits for any of this. `POST /_mock/advance?days=N` moves bank time by
N whole **calendar** days (0 to 3650; a fraction is refused rather than
rounded, because `?to=` says what you mean about the cutoff) and
`?to=YYYY-MM-DD` moves it to midnight on that date; either
way the answer lists the business days the move passed through, because three
days from a Thursday is Sunday to a calendar and Tuesday to a bank and you
should not have to find out which one you got by experiment. The clock never
goes backwards — though `?to=` a date it has already reached today is a no-op
rather than an error, so "advance to the settlement date" means what a test
thinks it means when that date is today. `--clock YYYY-MM-DDTHH:MM` pins where it starts, and
`POST /_mock/reset` puts it back there.

## Endpoints

Two ways in, both feeding one pipeline, plus a `/_mock` control plane shaped
like mock-edi's so the two feel the same.

| Surface | Endpoint | Notes |
| --- | --- | --- |
| Health, state, reset | `GET /_mock/health`, `GET /_mock/state`, `POST /_mock/reset` | As in the other two mocks |
| Dictionary | `GET /_mock/dictionary`, `GET /_mock/dictionary/<message>` | Every message the mock reads or writes, its element tree, the code sets and the choices made, as JSON; as mock-edi serves its X12 and EDIFACT sets |
| Payment file in | `POST /payments` | Answers `202` with a JSON summary: the file status, each payment's `EndToEndId` with its outcome, reason and settlement date, and what is queued; `422` when the file is rejected outright |
| Payments | `GET /_mock/payments`, `GET /_mock/payments/<EndToEndId>` | Every payment the bank decided on, newest first; by `EndToEndId`, the newest payment with that id, or `?all` for every one (an `EndToEndId` is unique within a file, not across files) |
| Collect answers | `GET /_mock/mailbox` | Every message released and not yet collected, oldest first, as JSON with its XML body; collecting takes them. `?leave` to peek without taking, `?raw` for the XML bodies alone, `?type=pain.002` to filter on a type prefix, and they combine |
| One message | `GET /_mock/mailbox/<id>` | That message's XML, whether or not it has been collected |
| Collect it again | `POST /_mock/mailbox/<id>/unread` | Puts one back in the mailbox, for a test that collects twice |
| What was asked of it | `GET /_mock/requests` | The newest hundred requests with their status, `?path=` to filter on a prefix: what your client actually sent, rather than what you believe it sent |
| Accounts | `GET/POST /_mock/accounts`, `GET/PATCH /_mock/accounts/<id>` | Balances, behaviour, behaviour parameters |
| Statements | `GET /_mock/accounts/<id>/statements` | The `camt.053` statements issued for an account: number, day, opening and closing balance, entries shown |
| Behaviours | `GET /_mock/behaviours` | Every behaviour with what the bank does, from the table the mock itself dispatches on |
| Holiday list | `GET/PUT /_mock/holidays` | The days the bank does not settle on, as a JSON list of dates, replaced whole |
| Clock | `POST /_mock/advance` | `?days=N` (calendar days) or `?to=YYYY-MM-DD`; answers with the business days crossed, and releases whatever came due |
| Validate only | `POST /_mock/validate` | Findings in prose, one line each; `200` when clean, `422` when not; nothing stored. `Accept: application/json` adds the mock's reading of the file |
| Folder in and out | `--drop-dir`, `--pickup-dir`, `GET /_mock/drop`, `POST /_mock/drop/scan` | Most bank connections are still SFTP folders, so the bank reads one directory and writes another |

`GET /_mock/mailbox?raw` returns the message bodies one after another, each
with its own XML declaration — what a bank's drop directory looks like to a
client that cats the files. It is deliberately *not* one document: wrapping
several ISO 20022 messages in an invented root element would put an element on
the wire that no schema has, and a client that learnt to expect it would have
learnt something no real bank sends. Parse one message at a time, or use
`GET /_mock/mailbox/<id>`.

Anything not built yet answers `404` with a body that names what is supported
and what is planned, rather than pretending. With the mailbox and the request
log, every endpoint this release promised answers, so that list is empty.

## Configuration

Every flag `mock-bank --help` lists:

| Flag | Default | What it does |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. The Dockerfile binds `0.0.0.0`. |
| `--port` | `8080` | Port. |
| `--db` | `:memory:` | SQLite file, or `:memory:` for a throwaway bank that forgets everything on exit. |
| `--timezone` | `UTC` | Bank time's zone, an IANA name such as `Europe/Amsterdam`. It needs `zoneinfo` (Python 3.9+) *and* an IANA database, which Windows does not ship — `pip install tzdata` provides one, and mock-bank will not depend on it because it takes no dependencies. Without both, a named zone is refused at startup rather than silently treated as `UTC`: a settlement date an hour out is the kind of lie this mock exists not to tell. |
| `--cutoff` | `15:00` | The hour the bank stops taking today's payments for today. At the cutoff exactly it is already too late. |
| `--clock` | now | Start bank time at `YYYY-MM-DDTHH:MM` instead of now, for a run whose settlement dates are reproducible. `POST /_mock/reset` returns here. |
| `--allow-duplicates` | off | Accept a file whose `MsgId` the bank has already received. Off, it is rejected with `DUPL`, as a real bank does. |
| `--status-delay-ms` | `0` | How long after a file arrives its `pain.002` is due. `0` so a test sees it at once; a real bank takes a few minutes. |
| `--keep-requests` | `5000` | Keep the newest N rows of the request log, so a mock left running for weeks stays bounded. `0` keeps every row. Trimmed at startup, after each `POST /_mock/advance`, and every few hundred requests — so the table sits *near* N rather than exactly at it, and can exceed it briefly between trims. |
| `--retention-days` | off | Remove request-log rows and already-collected messages older than this many days — a message's age on the bank clock, which `--clock` and `POST /_mock/advance` move, and a request's age in real time, because that is when it arrived. Fractions are allowed; `nan`, a negative and anything past a century are refused at startup rather than silently ignored. Payments, files and *uncollected* messages are never removed — they are the evidence a failing test gets read against, and a mock that eats them is no use at the moment you need it. |
| `--drop-dir` | off | A directory to watch for payment files. Each one is fed to the same pipeline as `POST /payments`, then moved to `processed/`, or to `failed/` when the bank could not put it through. |
| `--pickup-dir` | off | A directory to write every released message into, as `<type>-<account>-<id>.xml`. Written to a temporary name and renamed, so a poller never reads half a file. |
| `--drop-settle-ms` | `250` | Leave a file alone until it has been untouched this long, so one still being written is not read half-finished. `0` reads at once. |
| `--drop-interval-ms` | `1000` | How often to look in `--drop-dir`. A test should use `POST /_mock/drop/scan` rather than waiting. |
| `--auth` | off | Require HTTP basic `USER:PASSWORD` on every request, including `/_mock/health` and `/`. Without it, anyone who can reach the port can reset the bank. A value with no colon, or an empty user or password, is refused at startup rather than accepted as a credential nothing could match. |
| `--quiet`, `-q` | off | Log nothing per request. Does not silence the startup warning about an unguarded non-loopback bind. |
| `--version` | | Print the version and exit. |

## Trading through a folder

Most bank connections are not an HTTP endpoint: they are two directories on an
SFTP host, and a scheduler that polls them. So the bank has a second door.

```bash
mkdir -p bank/in bank/out
python3 -m mockbank --port 8080 --drop-dir bank/in --pickup-dir bank/out \
        --drop-settle-ms 0
```

Then, in another terminal:

```bash
cp tests/samples/pain001_four_payments.xml bank/in/
curl -s -X POST http://127.0.0.1:8080/_mock/drop/scan
ls bank/in/processed bank/out
```

`--drop-settle-ms 0` is there so that the scan reads the file you just copied
instead of leaving it for the next pass — the default waits 250ms for a file to
stop changing, which is right for a real drop and wrong for the line after a
`cp`. Without it the scan reports `"scanned": 0` and the poller picks the file
up a second later, which looks like the scan not working.

A file dropped into `--drop-dir` goes through **the same pipeline** as a file
posted to `/payments` — the same decisions, the same `pain.002`, the same
mailbox — and then moves out of the way: into `processed/` when the bank put it
through, or into `failed/` when it could not (a file it cannot read, or one
rejected at group level for `DUPL` or `FF01`). A `PART` counts as processed: the
file was handled, and the rejections are in the `pain.002`, which is what a real
bank's processed folder holds. Either way, the answer is written beside the file as
`<name>.findings.txt`: the group status and `MsgId`, how many were accepted and
rejected, and a line per payment with its `EndToEndId`, outcome and reason code.
Where there are findings, those lines are the prose `POST /_mock/validate`
prints. So you do not have to ask the mock what became of the file.

Two things every folder integration gets wrong, which this handles rather than
leaves to bite you:

- **A file still being written is not read.** One modified within
  `--drop-settle-ms` is left for the next pass, and `.tmp`, `.part` and
  dotfiles are never read at all. The mock writes its own files to a temporary
  name and renames them, because a mock that will not follow the convention it
  recommends is not much of an example.
- **A file is read once.** It is claimed by renaming before it is read, so the
  poller and a scan cannot both take it; and a file that could not be moved out
  of the way afterwards is remembered and left alone until it changes, rather
  than read again on every pass. `GET /_mock/drop` reports those, and anything
  waiting.

`POST /_mock/drop/scan` reads the directory now and says what it found, for the
same reason `POST /_mock/advance` exists: a test that waits for a poll interval
is slow and flaky, and one that asks is neither.

**The folder door is outside `--auth`.** Credentials guard HTTP requests; a file
in the drop directory is processed on its own, because a directory is guarded by
the filesystem and not by the bank. That is how a real SFTP drop works too — the
credentials are the SSH account, not the payment file — but it means `--auth`
alone does not close the second door. If you run with both, the drop directory's
permissions are the control, and anyone who can write into it can move money in
this mock.

Everything the bank releases lands in `--pickup-dir`, not only what a dropped
file produced — a `camt.054` or a `camt.053` released days later by the clock is
written when it is released, and so is a `pain.002` for a file you posted over
HTTP. The pickup directory is the bank's outbound side, not the drop
directory's reply.

Each file is named `<type>-<account>-<id>.xml`, with the full versioned type:
`pain.002.001.10-ACME-1.xml` is the first message the bank wrote and it is about
`ACME`, and `camt.053.001.08-GLOBEX-5.xml` is a statement for `GLOBEX`. The id
is the message's own and it is what makes the name unique; it is not a sort key,
since `-11` sorts before `-3` and the type comes first anyway. Read the order
off `GET /_mock/mailbox?leave`, which is oldest first, or off the ids as
numbers.
Nothing already there is overwritten — `POST /_mock/reset` empties the message
table and the ids start again, so a name can recur while the earlier file is
still waiting to be collected, and the new one is suffixed.

**A restart delivers nothing twice.** Whether a message has been written is a
column on its row, not a set in memory, so a mock stopped and started on the
same `--db` picks up where it left off instead of writing out every message it
ever released. This is worth stating because the first version did keep a set,
and restarting it filled the pickup directory with months of files the client
had collected long before.

## Worked example: paying the supplier's invoices

[`examples/pay_invoices.py`](examples/pay_invoices.py) is the payment leg of an
order-to-pay flow, joining this mock to
[mock-edi](https://github.com/rseufert/mock-edi). The supplier bills in EDIFACT;
the integration pays each `INVOIC` on its due date and then decides, from what
the bank sends back, which invoices are actually paid:

```
mock-edi  ──INVOIC──▶  pay_invoices  ──pain.001──▶  mock-bank
                                     ◀──pain.002──  accepted: scheduled, not paid
                                     ◀──camt.054──  the money left
                                     ◀──camt.053──  on the statement: paid
```

Standard library only, and it imports neither mock: it reads the `INVOIC` by
hand, builds the `pain.001` with `xml.etree`, and matches every answer to its
invoice by `EndToEndId` and `MsgId`. Seven tests, each one a way a payment run
goes wrong quietly:

| Test | What it proves |
| --- | --- |
| `test_accepted_is_scheduled_and_the_statement_makes_it_paid` | A `pain.002` that accepts a payment schedules it on the due date; only the `camt.053` makes the invoice paid |
| `test_closed_supplier_account_leaves_the_invoice_open` | `AC04` leaves the invoice open with the reason, held rather than retried, and nothing debited |
| `test_an_invoice_sent_twice_is_paid_once` | mock-edi's `duplicate-invoice` sends it twice; it is paid once |
| `test_a_run_retried_after_a_crash_does_not_pay_twice` | The same payments make the same `MsgId`, so a retried run's file is refused with `DUPL`, and that refusal does not reopen what the first file paid |
| `test_insufficient_funds_leaves_invoices_open` | `AM04` on every payment is read payment by payment, even though the group status is `RJCT` |
| `test_a_returned_payment_reopens_the_invoice` | Under `return-later` the invoice is paid, then the `pacs.004`'s credit on a later statement reopens it |
| `test_a_payment_missing_from_the_statement_is_not_paid` | Under `statement-gap` the `camt.054` says the money left and the `camt.053` does not show it; the invoice is not paid, and says why |

```bash
pip install mock-edi
mock-edi --port 8080 &
python3 -m mockbank --port 8090 &
cd examples && python3 -m unittest -v test_pay_invoices
```

`EDI_URL` and `BANK_URL` point the tests at mocks running elsewhere. The tests
switch mock-edi's `ACME` partner to EDIFACT `D:96A:UN`, which bills in euros,
so the buyer is ACME in both mocks. CI runs them against mock-edi from PyPI.

## Docker

```bash
docker build -t mock-bank .
docker run -p 8080:8080 mock-bank --auth tester:s3cret
```

The image binds `0.0.0.0`, because `127.0.0.1` inside a container cannot be
reached from outside it. That means anyone who can reach the port can
`POST /_mock/reset`, rewrite every account's balance and behaviour, and read
every message the bank has written — so pass `--auth`, and the mock says so on
stderr at startup if you do not. `-q` does not silence that warning.

## Layout

```
mockbank/accounts.py   the account behaviours, and what a valid account is
mockbank/clock.py      bank time: the cutoff, business days, holidays, and advancing
mockbank/drop.py       the second door: a directory watched, and one written
mockbank/db.py         the schema, the upgrade, and the seeded accounts
mockbank/messages.py   reading a pain.001 into a PaymentFile, and writing the pain.002, camt.054 and camt.053 the bank sends back
mockbank/outbox.py     what the bank sends and when: the message queue, release as the clock moves, the mailbox
mockbank/schema.py     the ISO 20022 dictionary: every message, element and code set, and the walker and builder derived from it
mockbank/server.py     the HTTP surface: the control plane, the dictionary, the accounts, the pipeline and the mailbox
mockbank/validate.py   findings about a payment file: refusals, structure, and the mock's own checks
```

`python -m mockbank` is the entry point; `tests/` drives a real server over
HTTP; `tools/` holds the checks CI runs; `examples/demo.sh` is the curl tour, and
`examples/pay_invoices.py` the worked integration with mock-edi.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) says how the pieces are meant to
fit, and [docs/FILES.md](docs/FILES.md) describes every file.

## Out of scope

Each of these is left out on purpose, and the mock says so by name rather
than half-supporting it.

| Left out | Why |
| --- | --- |
| EBICS, SWIFT FIN and SWIFTNet transport | Both need certificates and cryptography, which breaks zero dependencies; the same call mock-edi made on S/MIME. HTTP and folders cover testing. |
| Signed or encrypted files | Same reason; an encrypted file is refused with a message saying so. |
| Direct debits (`pain.008`) | Collections are a second choreography; they can follow once credit transfers are solid. |
| Real-time payments, cards, FX | Different rails and rules; each is a project of its own. |
| Fraud, sanctions and AML screening | Real logic, not wire shapes; out of scope permanently, like SAP business logic in mock-sap. |
| Intraday reports (`camt.052`) | Useful, but `camt.054` covers what a reconciliation test needs first. |

## Roadmap

| Release | Scope | Done when |
| --- | --- | --- |
| 0.1 | ISO 20022 credit transfers: `pain.001` in; `pain.002`, `camt.054`, `camt.053` out; accounts, balances, cutoff, holidays, clock; every behaviour above except `return-later`; HTTP only | **Done.** Every message the mock writes validates against its own dictionary and against the published XSDs; the tour and the example client run in CI |
| 0.2 | Returns (`pacs.004`, `return-later`), folder transport, the `payment_run` example with its six tests | `payment_run` passes in CI against mock-sap from PyPI |
| 0.3 | US formats: NACHA files in, NACHA returns (`R01`, `R02`, `R03`), BAI2 statements out | The same `payment_run` tests pass in NACHA mode |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): what the project values, where things
live, what a good pull request looks like, and how the team works the issue
queue.
