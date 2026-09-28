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
`invoice_check` approves the invoice, and
[`payment_run`](#worked-example-a-payment-run-against-sap) moves the money and
posts the statement back to SAP.
[`pay_invoices`](#worked-example-paying-the-suppliers-invoices) pays mock-edi's
invoices through this mock directly.

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
| NACHA in, returns out (`R01`, `R02`, `R03`), BAI2 statements out | Both | As above, in US formats | The same choreography for ACH **(0.3; so far NACHA files are read at both doors and acknowledged, and returns and BAI2 follow)** |

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

**A NACHA file** is read by the same endpoint. The mock recognises one by its
first line, a file header starting `101`, and reads it into the same payments
a `pain.001` becomes:

```
$ curl -s --data-binary @tests/samples/nacha_broken_entry_hash.ach http://127.0.0.1:8080/_mock/validate
NACHA 1234567890-2610010900A: 1 batch, 4 payments, 1 finding
error FF01 at /line 8 (batch control)/entry hash (columns 11-20): the entry hash says 265100431, but the batch's receiving DFI identifications sum to 26400041, to ten digits
```

A finding names the line, the record and the field with its columns. The file
checks are a line that is not 94 characters, a routing number that fails its
check digit (`RC01`), and an entry hash, block count, count (`AM18`) or credit
total (`AM10`) that disagrees with the entries. Two checks come from the
`pain.001` side, where NACHA has no rule of its own: a past effective entry date
is a `DT01` warning, and an individual identification number used twice is
`AM05`. A blank file creation time is allowed, as NACHA allows it. Where NACHA
has no ISO 20022 equivalent, the mock makes these choices:

- **`EndToEndId` is the individual identification number**, the originator's
  own reference for the payment (an invoice number, say), which the receiver
  also sees. `NOTPROVIDED` when it is blank. The trace number is assigned by
  the originating bank, so it becomes the `InstrId` instead.
- **The debtor account is the company identification.** A NACHA file carries
  no originator account: the originating bank knows its customer by that id.
- **The file's identity**, where a `pain.001` has a `MsgId`, is the immediate
  origin with the creation date, time and file ID modifier. Those are the
  fields a bank tells two files apart by.
- **Only credit entries are payments.** A debit, a prenote or a return entry
  is an `FF01` finding, because this bank sends money and does not collect it.

**Both doors take a NACHA file**, `POST /payments` and the drop directory, and
it is decided by the same engine as a `pain.001`; the JSON answer's `format`
says which it saw. A NACHA file names accounts by number, never by IBAN, so the
bank finds the ones it holds before deciding:

- **The bank's routing number is `999999992`.** It is fictional on purpose:
  it passes the check digit, and no Federal Reserve district starts with 99.
- **Each account has an `account_number`**, its domestic account number, up to
  17 digits and unique. The seed gives the four accounts the account part of
  their IBANs, `0000000001` to `0000000004`; an account you create or `PATCH`
  has one only if you give it one. **A `--db` file from before 0.3 has no
  account numbers at all** after the upgrade, seeded accounts included: set them
  by `PATCH` before sending a NACHA file that names them.
- A creditor at `999999992` whose account number is a held account's **is that
  account**; so is a debtor whose company identification is one. Anything else
  is an account at another bank, or a debtor the bank does not hold (`AC02`).
  A `pain.001` naming accounts by `Othr/Id` and `ClrSysMmbId` is found the same
  way. `GET /_mock/payments/<EndToEndId>` shows a held creditor by its IBAN
  whatever the file named it by, one at another bank by what the file gave (a
  NACHA entry's account number), and the creditor's bank as
  `creditor_clearing_id`.

A NACHA file is in dollars, so an account that sends one is set up for it:

```
curl -s -X PATCH http://127.0.0.1:8080/_mock/accounts/ACME \
     -H 'Content-Type: application/json' -d '{"format": "nacha", "currency": "USD"}'
curl -s --data-binary @tests/samples/nacha_four_payments_to_the_seed.ach http://127.0.0.1:8080/payments
```

A NACHA account held in euros is not refused: each payment is rejected `AM03`,
the currency mismatch rule that already exists, and says so.

**A NACHA account is sent an acknowledgement, not a `pain.002`.** Real banks
acknowledge an ACH file in shapes that differ from bank to bank, and NACHA
specifies none, so this one is the mock's choice: plain text, one fact to a
line, each line starting with what it is. For the file above it begins:

```
ACKNOWLEDGEMENT MB-ACK-000001
CREATED 2026-10-01T09:00:00+00:00
FILE 0000000001-2609300930A
STATUS PART
ENTRY 999999990000001 INV-2026-0101 1250.00 ACCEPTED 2026-10-01
ENTRY 999999990000002 INV-2026-0102 3400.50 REJECTED R02 the creditor account NL84MOCK0000000003 is closed
```

**What comes back is a NACHA return file.** A payment the bank rejects for one
of three behaviours is rejected with its NACHA return code - `R01` for
`insufficient-funds`, `R02` for `closed-account`, `R03` for `bad-bank-id` - and
the acknowledgement says so. It also comes back the next business day as a
return entry, the way ACH answers it: nothing was debited, so nothing is
credited, and the return file is the whole answer. A payment that settles and
then comes back under `return-later` is a return entry too, where an ISO 20022
account gets a `pacs.004`; its `reason` is an `R` code on a NACHA account, `R02`
by default, and anything else is refused at `PATCH`. A return file is a real
NACHA file, `nacha.return`, with one return entry per payment - the original's
return transaction code (`22` becomes `21`), amount, account number and
identification - and its addenda 99: the reason, the original trace number and
the original receiving bank. Its counts, entry hash, totals and padding are
computed, and it reads back through the same reader with no finding. Sent to
`POST /payments` it is refused by name: the bank sends return files, it does
not take them.

Statements and notifications stay `camt.053` and `camt.054` until BAI2, so a
NACHA return shows there with the same reason in ISO 20022's words (`R01` is
`AM04`, `R02` is `AC04`, `R03` is `AC01`). In the mailbox an acknowledgement's
type is `nacha.ack` and a return file's `nacha.return`; `GET
/_mock/mailbox/<id>` serves either as `text/plain`, and in `--pickup-dir` they
are `.txt` and `.ach` files. `?raw` is a sequence of XML documents, so it
refuses with `409` to mix text into one and says how to ask for each:
`?raw&type=nacha.ack`, `?raw&type=nacha.return` and `?raw&type=camt.`, say.

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
the mock cannot read far enough to find a `MsgId` still gets a `pain.002`,
`RJCT` with the reason, and names the original as `NOTPROVIDED`: the ISO 20022
convention for a mandatory identifier the sender did not give. The bank does not
invent one. A dropped file's name goes in `StsRsnInf/AddtlInf`, since that is all
a folder client has to match the refusal to.

Each payment keeps its `EndToEndId` through every message, so a client can
match a status, a statement line and a return to the invoice it paid.

## Money arriving

Everything above is money leaving, or coming back. `POST /_mock/credits` makes
money *arrive*: a customer paying your invoice, which is the whole receivable
side of a business and where cash application quietly goes wrong, because **the
payer controls the reference**. You describe what the payer sent; the bank books
it and reports it the way it reports anything else.

```
curl -s -X POST http://127.0.0.1:8080/_mock/credits -H 'Content-Type: application/json' \
     -d '{"account": "ACME", "amount": 118000, "note": "INV-1001 less 70.00 damaged goods",
          "debtor": {"name": "Customer Ltd", "iban": "NL14MOCK0000000002"}}'
```

- **It books on its value date** (`value_date`, bank today by default), or on
  the next business day if that is a weekend, a holiday or today after the
  cutoff - the rule a payment's settlement follows - so `POST /_mock/advance`
  drives it. Booking sends a `camt.054` credit notification, and the day's
  `camt.053` shows it and still reconciles, to the cent.
- **On the statement it is a received transfer**: `CRDT` under
  `PMNT`/`RCDT`/`ESCT`, not a return, with the payer in `Dbtr`, `DbtrAcct` and
  `DbtrAgt`, the payer's value date in `ValDt`, the note to payee in
  `RmtInf/Ustrd` and a structured `reference` apart from it in
  `RmtInf/Strd/CdtrRefInf/Ref`. A receiving system that reads the structured
  field can be tested against one that has to parse the prose.
- **Amounts are minor units** and the currency is the account's; a credit in
  another is refused, as the mock does no FX, and so is one to a closed or
  unknown account, or for a value date already past.
- **What the bank could not report is refused when it is sent, never stored.**
  That covers a blank name, reference or line of note, a control character or
  line break, a BIC that is not one, and an amount that would take the balance
  past a statement's 18 digits. The credit's `camt.054` is written once before
  the credit is kept, so nothing waiting can stop a later release.
- **An account closed while a credit waits does not book it.** A closed account
  gets no statement, so the money would arrive unreported. The credit stays
  unbooked in `GET /_mock/credits`. The mock does not send it back to the payer.
- **On a `statement-gap` account the entry left off is the day's last**, and
  credits come after the day's debits. So on a day money arrives, it is a credit
  that goes missing rather than a debit.

The failures worth testing are four requests:

| Scenario | The request |
| --- | --- |
| A short payment | `"amount"` below the invoice, with or without a reason in `"note"` |
| A payment that names no invoice | leave out `"note"` and `"reference"`, or send `"note": "PAYMENT"` |
| One credit for several invoices | several invoice numbers in `"note"` - or fewer than it settles |
| A reference the bank splits | `"wrap": 35` or `70`: the bank re-cuts the note at that width wherever the cut falls, invoice numbers included, as banks that reformat remittance do. The default, `140`, keeps the payer's lines |

The first three are what the payer sent, which is why they are fields and not
account behaviours; the fourth is the bank's doing, and the only one the mock
adds. Whether a deduction is justified is the receiving system's to judge.

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
| Index | `GET /` | An HTML page listing everything this build answers, from the same table the router dispatches on |
| Health, state, reset | `GET /_mock/health`, `GET /_mock/state`, `POST /_mock/reset` | As in the other two mocks |
| Dictionary | `GET /_mock/dictionary`, `GET /_mock/dictionary/<message>` | Every message the mock reads or writes, its element tree, the code sets and the choices made, as JSON; as mock-edi serves its X12 and EDIFACT sets |
| Payment file in | `POST /payments` | Answers `202` with a JSON summary: the file status, each payment's `EndToEndId` with its outcome, reason and settlement date, and what is queued; `422` when the file is rejected outright |
| Payments | `GET /_mock/payments`, `GET /_mock/payments/<EndToEndId>` | Every payment the bank decided on, newest first; by `EndToEndId`, the newest payment with that id, or `?all` for every one (an `EndToEndId` is unique within a file, not across files) |
| Collect answers | `GET /_mock/mailbox` | Every message released and not yet collected, oldest first, as JSON with its XML body; collecting takes them. `?leave` to peek without taking, `?raw` for the XML bodies alone, `?type=pain.002` to filter on a type prefix, and they combine |
| One message | `GET /_mock/mailbox/<id>` | That message's XML, whether or not it has been collected |
| Collect it again | `POST /_mock/mailbox/<id>/unread` | Puts one back in the mailbox, for a test that collects twice |
| Money arriving | `POST /_mock/credits`, `GET /_mock/credits` | Make a credit arrive in an account from a payer you describe: it books on its value date and shows on the `camt.054` and `camt.053` as a received transfer. The listing is every credit, newest first |
| What was asked of it | `GET /_mock/requests` | The newest hundred requests with their status, `?path=` to filter on a prefix: what your client actually sent, rather than what you believe it sent |
| Accounts | `GET/POST /_mock/accounts`, `GET/PATCH /_mock/accounts/<id>` | Balances, behaviour, behaviour parameters, `format` (`iso20022` or `nacha`) and the domestic `account_number` a NACHA file names it by |
| Statements | `GET /_mock/accounts/<id>/statements` | The `camt.053` statements issued for an account: number, day, opening and closing balance, entries shown |
| Behaviours | `GET /_mock/behaviours` | Every behaviour with what the bank does, from the table the mock itself dispatches on |
| Holiday list | `GET/PUT /_mock/holidays` | The days the bank does not settle on, as a JSON list of dates, replaced whole |
| Clock | `POST /_mock/advance` | `?days=N` (calendar days) or `?to=YYYY-MM-DD`; answers with the business days crossed, and releases whatever came due |
| Validate only | `POST /_mock/validate` | A `pain.001` or a NACHA file. Findings in prose, one line each; `200` when clean, `422` when not; nothing stored. `Accept: application/json` adds the mock's reading of the file |
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
prints. So you do not have to ask the mock what became of the file. The
`pain.002` lands in `--pickup-dir` as well, even for a file the bank could not
read at all, named `pain.002.001.10-bank-<n>.xml` when no debtor account could
be read from it.

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

## Worked example: a payment run against SAP

[`examples/payment_run.py`](examples/payment_run.py) is the other end of the
same flow, joining this mock to [mock-sap](https://github.com/rseufert/mock-sap).
The invoices are already posted in SAP. The run does what SAP's `F110` does:
it selects the open supplier items that are due, pays each one, and posts every
statement back so SAP clears what was paid and reopens what came back.

```
mock-sap  ──open items──▶  payment_run  ──pain.001──▶  mock-bank
                                        ◀──pain.002──  accepted, or why not
                                        ◀──camt.053──  what actually left, and came back
mock-sap  ◀──FINSTA01─────  payment_run                clear what it paid, reopen returns
```

Standard library only, and it imports neither mock. The `EndToEndId` is the
supplier's own invoice number (`SupplierInvoiceIDByInvcgParty`), so the bank's
answers and SAP's clearing meet on the same reference. The `MsgId` is the run
date and identification, as `F110`'s are, so the same run sent twice is `DUPL`.
Each `camt.053` is checked (opening plus entries is closing) before it is
converted, and the `FINSTA01` writes a debit the way SAP writes a negative
number, with the minus after it, because nothing in the IDoc says which way a
line goes. What it shares with `pay_invoices.py` is in
[`examples/bank_messages.py`](examples/bank_messages.py), so a copy of either
takes that one file with it.

**Look at `run.problems` first.** An answer the run could not use goes there,
in words: the bank answering anything but `202` or `422` to the payment file, a
mailbox answering with an error, SAP refusing a statement - and a bank or SAP
that does not answer at all, down or refusing the connection, which is named as
such. None of it is raised and none of it is dropped. A payment file the bank
never received leaves every item `selected`, to send again. An empty list is what a clean run looks like. Each item's
`status` and `reason` say the rest: `rejected` with the bank's code, `cleared`
with SAP's clearing document, `unreconciled` when a statement does not add up,
`returned` with the bank's reason.

The six tests from the plan on [#16](https://github.com/rseufert/mock-bank/issues/16),
numbered as there, and what the rest of the suite adds to them:

| Test | What it proves |
| --- | --- |
| `test_1_a_clean_run_is_paid_matched_and_cleared` | Monday's statement adds up, every item is cleared in SAP with a clearing document, and the next run finds nothing to pay |
| `test_a_closed_account_is_rejected_ac04_and_the_rest_accepted` (2) | The item paid to INITECH's closed account is rejected `AC04` from the `pain.002` and the rest are accepted; each payment went to the account its invoice names |
| `test_3_a_return_reopens_the_invoice_distinguishable_from_one_never_paid` | Under `return-later` a paid invoice comes back three business days later and is reopened in SAP, open like one never paid but with `ClearingIsReversed` set, and the next run selects it |
| `test_the_same_run_twice_is_dupl_and_pays_nothing_twice` (4) | The same run has the same `MsgId`; the bank refuses the copy with `DUPL`, nothing is paid twice, and the refusal leaves the first file's outcome alone |
| `test_5_a_statement_gap_leaves_the_missing_payment_unreconciled_and_open` | Under `statement-gap` the statement does not add up, by exactly one payment: that payment is `unreconciled` and its item stays open, and SAP's own arithmetic check says so too |
| `test_6_after_the_cutoff_it_waits_for_mondays_statement` | A run at 16:00 on a Friday settles on Monday; Friday's statement clears nothing and Monday's clears everything |
| `test_posting_the_same_statement_twice_clears_nothing_twice` | A client that retries a statement post does no harm |
| `test_an_item_not_yet_due_is_not_selected` | An invoice on `NT30` terms is left for a later run |
| `test_the_selection_asks_sap_to_leave_blocked_and_cleared_items_out` | The query SAP receives asks for supplier lines that are neither blocked nor cleared |
| `test_a_blocked_invoice_is_never_selected` | An invoice blocked for payment on the supplier invoice, as an SAP user blocks one, never reaches the bank; the unblocked one beside it is paid |
| `test_a_bank_that_answers_with_an_error_is_a_problem_not_silence`, `test_sap_refusing_a_statement_is_recorded_against_it` | An error answer from either side goes into `run.problems` in words, and nothing stops half way |
| `test_a_bank_that_does_not_answer_is_a_problem_not_silence`, `test_sap_not_answering_the_selection_selects_nothing_and_says_so`, `test_sap_not_answering_a_statement_is_recorded_against_it` | A side that does not answer at all - a port nothing listens on - is a named problem too, not a traceback, and a file that never reached the bank leaves its items as they were |
| `test_two_payments_of_the_missing_amount_are_both_named` | A shortfall two payments could explain names both rather than guessing one |

```bash
pip install mock-sap
mock-sap --port 8000 &
python3 -m mockbank --port 8090 --clock 2026-10-02T16:00 &
cd examples && python3 -m unittest -v test_payment_run
```

**NACHA mode.** `PaymentRun(..., file_format="nacha")` pays the same
invoices by ACH: a NACHA file of credits instead of the `pain.001`, the bank's
acknowledgement read instead of the `pain.002`, and each supplier paid to the
ABA routing and account number SAP holds for it (`BankNumber`, `BankAccount`)
instead of an IBAN. The file's header is built from the run, so the same run
sent twice is still the same file and still `DUPL`. The bank tells NACHA files
apart by origin, date, creation time and file ID modifier, and the run's
identification goes into the last two exactly, which leaves room for one to
three capital letters or digits. A longer identification is refused before
anything is selected. SAP's `F110` identifications are five characters, so a
caller with those keeps a mapping of its own to three. If it were hashed instead, two runs that hashed alike
would be one file to the bank, and the second would be refused as a repeat and
never paid. An item a NACHA entry cannot carry is skipped with the reason, as
a foreign-currency item is:
- a reference over 15 characters, or holding a space or anything that is not
  ASCII;
- an amount of 100,000,000.00 or more;
- an account number over 17 characters;
- a name that is not ASCII.

Statements are still `camt.053`, so reconciling does not change. The same
tests run in this mode too, which is 0.3's definition of done, with one more
for what an entry cannot carry:

```bash
PAYMENT_RUN_FORMAT=nacha python3 -m unittest -v test_payment_run
```

In that mode the tests make ACME a dollar account in NACHA format and give the
three suppliers US bank details through `A_BusinessPartnerBank`; a closed
account is answered `R02` where the ISO run sees `AC04`.

**The example does not advance bank time; the tests do.** A client cannot move
a real bank's clock. It sends its file and reads statements as they arrive, so
`reconcile` posts whatever the bank has sent so far, and a payment not on a
statement yet simply stays `accepted`. The tests move mock-bank's clock with
`POST /_mock/advance` to make those statements arrive. That is also why
mock-bank starts at `--clock 2026-10-02T16:00`, a Friday after the 15:00 cutoff:
test 6 needs that moment, a reset returns to it, and the other tests advance to
Monday morning first.

mock-sap's seed has suppliers that bank where mock-bank's seed says they do
(GLOBEX, INITECH, EURODIS and Umbrella Logistics), but no supplier invoices, so
each test posts its own `INVOIC` IDocs. Nothing in the example or the tests
writes to the open-item cube, which is read-only in SAP and, from mock-sap
0.13.1, in the mock. `SAP_URL` and `BANK_URL` point the tests at mocks running
elsewhere. CI runs them against mock-sap from PyPI.

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
mockbank/accounts.py           the account behaviours, and what a valid account is
mockbank/clock.py              bank time: the cutoff, business days, holidays, and advancing
mockbank/credits.py            money arriving: a credit the test describes, booked on its value date (#91)
mockbank/drop.py               the second door: a directory watched, and one written
mockbank/db.py                 the schema, the upgrade, and the seeded accounts
mockbank/handler.py            the request handler: authentication, the body, the request log, and the lookup in the route table
mockbank/messages.py           reading a pain.001 into a PaymentFile, and writing the pain.002, camt.054 and camt.053 the bank sends back
mockbank/nacha.py              NACHA: the record declarations, and a reader into the same payments as a pain.001
mockbank/outbox.py             what the bank sends and when: the message queue, release as the clock moves, the mailbox
mockbank/routes/__init__.py    the route table: each surface registers method, path pattern and function
mockbank/routes/control.py     health, state, reset, behaviours, the dictionary and the index page
mockbank/routes/accounts.py    the accounts and their statements
mockbank/routes/clock.py       advancing bank time, and the holidays
mockbank/routes/credits.py     POST and GET /_mock/credits
mockbank/routes/payments.py    POST /payments, and /_mock/payments
mockbank/routes/mailbox.py     the mailbox, and the request log
mockbank/routes/validate.py    POST /_mock/validate
mockbank/routes/transport.py   the folder transport's state, and a scan on demand
mockbank/schema.py             the ISO 20022 dictionary: every message, element and code set, and the walker and builder derived from it
mockbank/server.py             Config, and make_server, which puts the state, the handler and the routes together
mockbank/state.py              what survives between requests: the database, the clock, the lock, and the pipeline both doors feed
mockbank/validate.py           findings about a payment file: refusals, structure, and the mock's own checks
```

`python -m mockbank` is the entry point; `tests/` drives a real server over
HTTP; `tools/` holds the checks CI runs; `examples/demo.sh` is the curl tour, and
`examples/pay_invoices.py` and `examples/payment_run.py` the worked integrations
with mock-edi and mock-sap.
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
| 0.2 | Returns (`pacs.004`, `return-later`), folder transport, retention, the `payment_run` example | **Done.** `payment_run`'s thirteen tests pass in CI against mock-sap from PyPI |
| 0.3 | US formats: NACHA files in, NACHA returns (`R01`, `R02`, `R03`), BAI2 statements out | The same `payment_run` tests pass in NACHA mode |
| 0.4 | Money arriving: an incoming credit on `POST /_mock/credits`, so cash application is testable; a worked example using all three mocks, procure to pay | The procure to pay example runs in CI against mock-sap and mock-edi from PyPI |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): what the project values, where things
live, what a good pull request looks like, and how the team works the issue
queue.
