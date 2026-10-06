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
`invoice_check` approves the invoice, and `payment_run` moves the money and
posts the statement back to SAP. `pay_invoices` pays mock-edi's invoices
through this mock directly. All of them are in
[mock-acme](https://github.com/rseufert/mock-acme), the integration between the mocks.

---

## Quick start

Install it and start a bank:

```bash
pip install mock-bank
mock-bank --port 8090
```

`8090` is the default, so plain `mock-bank` listens there too. The three mocks
default to different ports on purpose - mock-sap on `8000`, mock-edi on `8080`,
mock-bank on `8090` - so all three run side by side with no flag to pass and no
bind error on the second one.

Then, in another terminal:

```bash
curl http://127.0.0.1:8090/_mock/health
curl http://127.0.0.1:8090/_mock/accounts
```

The guided tour lives in the repository rather than in the installed package,
so it needs a checkout — which needs nothing installed either:

```bash
git clone https://github.com/rseufert/mock-bank && cd mock-bank
python3 -m mockbank --port 8090 &
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

ISO 20022 came first, and NACHA and BAI2 joined it in 0.3. You send one payment file
and the mock answers with the messages a real bank sends, in the order it
sends them.

| Message | Direction | When the mock sends it | What it carries |
| --- | --- | --- | --- |
| `pain.001` | In | You send it | Credit transfers: debtor account, one or more payments, amounts, creditors |
| `pain.008` | In | You send it | Direct debits: the creditor account, and collections from debtors under their mandates. `POST /payments` and the drop folder decide each collection and answer with a `pain.002`; an accepted one credits the creditor account on its settlement date, with a `camt.054` and an entry on the day's statement, and the debtor's bank can refuse it before settlement or send it back after (#131) |
| `pain.002` | Out | Minutes after `pain.001` (`--status-delay-ms`, default at once) | Status per file, batch and payment: `ACCP`, `RJCT` with a reason code, `PART` when some are rejected; a file rejected outright gets its group status only |
| `camt.054` | Out | Each payment's settlement date | A debit notification per account each time payments book, an entry per payment, each carrying its `EndToEndId` |
| `camt.053` | Out | End of each business day | The statement: opening and closing balance, every entry, balances that reconcile; one per open account per business day, empty days included |
| `camt.052` | Out | When you ask: `POST /_mock/accounts/<id>/report` | The intraday report: the day so far for one account, with its opening balance (`OPBD`), the balance now (`ITBD`) and every entry booked today, on the same terms as the statement the day will end with. `statement-gap` does not apply to it, so a reconciler can see the entry the statement then leaves out |
| `pacs.004` | Out | N business days after settlement, under `return-later` | A payment that had settled, coming back: its `EndToEndId`, what comes back and when, and the return reason. A `camt.054` credit comes with it, and the day's `camt.053` shows a `CRDT` entry whose `RtrInf` names the reason |
| NACHA in, returns out (`R01`, `R02`, `R03`, and for a collection `R05`, `R07`, `R08`, `R10`, `R29`), BAI2 statements out | Both | As above, in US formats | The same choreography for ACH, payments and collections, for an account whose `format` is `nacha`: a plain acknowledgement where an ISO 20022 account gets a `pain.002`, a NACHA return file where it gets a `pacs.004`, and a BAI2 statement where it gets a `camt.053` |

Versions: `pain.001.001.09` is read, and the older `pain.001.001.03` is
accepted as well and read into the same model; `pain.008.001.08`, from the
same 2019 set, is read, validated and decided. The mock writes
`pain.002.001.10`, `camt.054.001.08`, `camt.053.001.08`, `camt.052.001.08` and
`pacs.004.001.09`, the versions that go with `pain.001.001.09` and that most banks accept today. That is a choice, not
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
  rails are listed under what is out of scope. The credit a collection books
  carries `PMNT`/`IDDT`/`ESDD` (payments, issued direct debit, SEPA Core direct
  debit), which is what banks' own example statements write for it:
  `test/assets/ing/example_camt.xml` in
  [svapnil/iso20022.js](https://github.com/svapnil/iso20022.js) @ `68ce4fb`
  and `spec/examples/camt053/mixed_examples_08.xml` in
  [MeinGrundeinkommen/konfipay](https://github.com/MeinGrundeinkommen/konfipay)
  @ `c337e64`, each a `CRDT`. The mock writes `ESDD` whatever the file's local
  instrument; a B2B collection would be `BBDD` at a real bank. A collection
  going back is a `DBIT` under `PMNT`/`IDDT`/`UPDD` (reversal due to a returned
  or unpaid direct debit), which the same two files write for it; its `RtrInf`
  carries the reason and the code the credit was booked under.
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
$ python3 -m mockbank --port 8090 --clock 2026-09-30T09:00 &
$ curl -s --data-binary @tests/samples/pain001_broken_iban.xml http://127.0.0.1:8090/_mock/validate
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
$ curl -s --data-binary @tests/samples/nacha_broken_entry_hash.ach http://127.0.0.1:8090/_mock/validate
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
- **A credit entry is a payment and a debit entry is a collection**
  ([#176](https://github.com/rseufert/mock-bank/issues/176)): transaction codes
  `22`, `32`, `42` and `52` on one side, `27`, `37` and `47` on the other. The
  entry's own code decides, whatever service class the batch states.
- **A file is of payments or of collections, not both.** A real originating
  bank takes a batch that mixes them (service class `200`); the mock's pipeline
  takes one kind of file, as a `pain.001` and a `pain.008` are two files. A
  mixed file is an `FF01` finding that counts both sides and says to send the
  debits in a file of their own.
- **What moves no money is not read.** A prenote (`23`, `28` and their savings
  and ledger twins), a zero-dollar entry and a loan debit (`55`, which NACHA
  allows for reversals only) are each an `FF01` finding that says which it is.

**A NACHA file of debit entries is a file of collections**, decided, recorded
and booked as a `pain.008` is (the table under **For a direct debit** below says what the bank decides):

- the **creditor account** is the company identification, as the debtor account
  of a credit file is, and has to be an account the bank holds;
- the **debtor** is the receiver: the DFI account number and individual name,
  at the receiving bank's routing number. One at this bank's routing number
  whose account number the bank holds is a held debtor, and decides the
  collection by its own state and behaviour;
- the **requested collection date** is the effective entry date;
- **there is no mandate.** A NACHA file carries none: the receiver's
  authorization is held by the originator, outside the file. So a collection
  from one has no mandate id and no date of signature and is not held to
  `MD02`. The one thing the file does say is whether a `WEB` or `TEL` debit is
  recurring or single (its payment type code, `R` or `S`), which is recorded as
  the sequence type `RCUR` or `OOFF`. Any SEC code may carry a debit; the mock
  records it and polices none.

Sources, both outside the project: Nicolet National Bank's NACHA file format
specification (<https://www.nicoletbank.com/nacha-file-format-specifications>,
read 2026-10-01) for the service class codes, the debit transaction codes and
the absence of any authorization field; and
[moov-io/ach](https://github.com/moov-io/ach) @ `7ee7ad0` for the ledger debit,
the prenotes and the payment type code. `tests/samples/external/nacha-gl-debit.ach`
is a debit file from the second.

**What a collection is answered with** is the creditor account's format, not
the file's. An ISO 20022 account in dollars that sends a NACHA file of debits
gets a `pain.002`, a `camt.054` and, for one that comes back, a `pacs.004`. A
NACHA account gets what it gets for a payment, whichever way the money moves:

| | A NACHA-format account is sent |
| --- | --- |
| For the file | the plain acknowledgement, `nacha.ack` |
| When something books or comes back | a `camt.054`: notifications stay ISO 20022 on every account |
| For what comes back | a NACHA return file, `nacha.return` |
| At the end of the day | a BAI2 statement |

**A collection comes back as a returned debit.** The return file carries one
entry per collection - transaction code `26`, `36` or `46`, the return of a
`27`, `37` or `47` - with an addenda `99` giving the `R` code, the original
trace number and the receiver's bank, in the debit totals. Three things send
one back:

- **`POST /_mock/collections/<EndToEndId>/refuse`**, the receiver's bank saying
  no, with an `R` code on a NACHA account: `R01`, `R02`, `R03`, and the ones
  about the authorization, `R05`, `R07`, `R08`, `R10` and `R29`. After the
  collection settled the money goes back, as for a `pain.008`. **Before it
  settled** NACHA has no message that rejects one entry of a file the bank
  accepted, so the collection is rejected, nothing ever books, and the return
  entry goes out on the day it would have settled.
- **A rejection when the file arrived**, where the reason has an `R` code
  (`AM04` is `R01`, `AC04` is `R02`): the acknowledgement says rejected, and the
  entry comes back in a return file the next business day too, as a rejected
  payment does. Nothing was credited, so nothing is debited.
- **`return-later` on a debtor account this bank holds**, after its `days`. The
  debtor's reason is said as an `R` code: its own if it has one, the one for
  its ISO 20022 reason if there is one, and `R02` otherwise.

On the `camt.054` the reason is in ISO 20022's words: `R05` is `AG01`, `R07`,
`R10` and `R29` are `MD01`, and `R08` is `MS02`. **That mapping is the mock's
own**: no outside source pairs NACHA's return codes with ISO 20022's, so it is
the nearest meaning, chosen here, and a real bank may say it differently. The
`R` code in the return file is the one to rely on. The `R` codes are moov-io/ach's
table (`addenda99.go` @ `7ee7ad0`); `tests/samples/external/nacha-return-WEB.ach`
is a returned debit from outside, which the mock's reader has taken since #55.

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
curl -s -X PATCH http://127.0.0.1:8090/_mock/accounts/ACME \
     -H 'Content-Type: application/json' -d '{"format": "nacha", "currency": "USD"}'
curl -s --data-binary @tests/samples/nacha_four_payments_to_the_seed.ach http://127.0.0.1:8090/payments
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
credited, and the return file is the whole answer - it is on no statement and
no `camt.052`, and it moves no balance on any day. A payment that settles and
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

A NACHA account's end-of-day statement is a BAI2 file, where every other
account's is a `camt.053`: the same clock, the same balances and entries, and
one counter shared between the two, so an account that changes format keeps
counting. The choice is per account, so one payment file can produce both.
Notifications stay `camt.054`, so a NACHA return shows there with the same
reason in ISO 20022's words (`R01` is `AM04`, `R02` is `AC04`, `R03` is
`AC01`). A BAI2 statement carries no reason at all; the `R` code is in the
return file.

**What the BAI2 statement is held to.** It is held to files from outside the
project, which fixed who the `02` names as originator, what a record count
covers, and that a balance or a control total may state its sign - though most
real files do not, so signing is this mock's choice rather than the format's
requirement. They also settled the transaction type codes on the `16` records
(#127): `447` for a payment sent (ACH Disbursement Funding Debit, which is what
a bank writes for an ACH credit payment), `257` for one coming back (Individual
ACH Return Item), `142` for money arriving (ACH Credit Received), `165` for a
collection that settled (Preauthorized ACH Credit, which the same bank's export
writes for the proceeds of an `ACH Debit Collection`) and `557` for one that
went back (Individual ACH Return Item on the debit side; the code table is the
evidence, since no export here shows a collection returned). Before 0.5
these were `495`, `165` and `195`, which a real bank writes for an outgoing wire,
the proceeds of a debit collection and an incoming wire. A reader that takes a
movement's direction from the code's range, 100 to 399 a credit and 400 to 699 a
debit, as mock-acme's `payment_run` does, reads both the same.

`mockbank.bai2.read` reads a real bank's file as well as the mock's own
([#114](https://github.com/rseufert/mock-bank/issues/114)). A `88` continuation
is folded into the record it continues, because it continues that record's
comma-separated field stream rather than carrying fields of its own; a funds type
occupies one field, or three when it is value-dated, or four when availability is
distributed over three periods, or 2 + 2n when it is distributed over named days;
and an amount may carry an explicit sign or not. It also knows where a record
ends ([#128](https://github.com/rseufert/mock-bank/issues/128)): a `16`'s text
runs to the terminator with its commas, several records may share one line, and a
record with no `/` ends at the newline when the next line starts a new one. One
rule covers the terminator
([#150](https://github.com/rseufert/mock-bank/issues/150)): **a `/` ends a record
only when a record code and a separator follow it - on that line or on the next
non-blank one - or when nothing follows at all.** Anywhere else it is the field's
own content, so a reference written `AB/` and continued by `GS/RP0001` reads
`AB/GS/RP0001` and not `ABGS/RP0001`. A line that does not begin with a record
code continues the one above it, joined with nothing
([#142](https://github.com/rseufert/mock-bank/issues/142)) - so a field a
fixed-width producer wrapped across two lines reads as one value. Nothing is
inserted and nothing is dropped: a space or a column of padding on a line that is
continued is the field's own content and survives the wrap, while trailing
whitespace on a line that ends its record is padding around the record and comes
off. Five files from moov-io/bai2 are held to that. Three of them reconcile, and
the check worth naming is that every
control total and record count in those, recomputed from the records it covers,
comes out as stated. The other two state totals that contradict themselves,
which is those files and not the reader. One limit remains, because BAI2 has no
escape character: a text that itself contains `/16,` cannot be told from the
start of a record.

In the mailbox an acknowledgement's type is `nacha.ack`, a return file's
`nacha.return` and a BAI2 statement's `bai2.statement`; `GET
/_mock/mailbox/<id>` serves each as `text/plain`, and in `--pickup-dir` they
are `.txt`, `.ach` and `.bai2` files. `?raw` is a sequence of XML documents, so
it refuses with `409` to mix text into one and says how to ask for each:
`?raw&type=nacha.ack`, `?raw&type=nacha.return`, `?raw&type=bai2` and
`?raw&type=camt.`, say.

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

**What the bank refuses because it could not answer for it.** A value can be
readable and still be one no later message can hold, and taking it in is worse
than refusing it: the file is booked, and the failure arrives later, on the
release path, where it made every `POST /_mock/advance` and every mailbox read
answer 500 until the mock was reset
([#166](https://github.com/rseufert/mock-bank/issues/166)). So each of these is
settled at the door, before anything is written:

| Given | Answer |
| --- | --- |
| `PATCH /_mock/accounts/<id>` with a name no message can carry — empty, or only spaces | `400`, naming the element the writer refused. A name longer than the standard allows is **written**, shortened to fit, not refused |
| A `pain.001` whose own identifiers cannot be echoed back — a `MsgId` over 35 characters, a control sum of more than 18 digits | `422`, `RJCT`/`FF01`, saying the status report could not be written. Nothing booked and nothing queued, because the `pain.002` would have to carry the same value back |
| On a NACHA account, an `InstrId` longer than a return addenda's original entry trace number | that payment is rejected, `FF01`. A NACHA account's rejections come back as returns ([#54](https://github.com/rseufert/mock-bank/issues/54)), and at receipt the bank cannot know whether this one will |
| Changing an account's `format` while a return is already scheduled on one of its payments | `400`, saying how many, with which reason, and the last day one is due. The reason is stored when the payment books, so the switch would leave a return nothing can write. It is allowed again once they have gone back |

**And what happens when a message cannot be written anyway.** The doors above
cannot be complete: a `--db` file carries rows an older mock wrote, before a guard
existed. So the bank copes rather than stopping. The booking stands — a payment
that booked has booked, a return that came back has come back — and the message it
owed you is given up on, with the writer's own complaint kept:

```bash
curl -s http://127.0.0.1:8090/_mock/unsent
[{"id": 1, "type": "camt.053.001.08", "account": "OLD", "file_id": null,
  "day": "2026-10-01", "at": "2026-10-01T09:00:00Z",
  "problem": "/Document/BkToCstmrStmt/Stmt[1]/Acct/Ownr/Nm: Nm is empty"}]
```

`GET /_mock/state` counts them under `messages.unsent`, so a tester sees that
something is missing without having to know to look. Three things follow, and all
three are deliberate:

- **Every other message of that release is still written.** One account's
  unwritable statement does not stop another account's, and does not stop the
  day's bookings.
- **Nothing retries it.** The value it could not carry will not fix itself, so a
  retry would add a row for every advance for ever. A statement given up on is
  recorded as issued with no message, so `GET /_mock/accounts/<id>/statements`
  shows it with `"message_id": null`. **That null means only "no message to
  fetch", not "the bank could not write it"**: `--retention-days` nulls it too
  when a `camt.053` that *was* sent and collected ages out. `GET /_mock/unsent` is
  what says the bank could not write one, by type, account and day.
- **A reset forgets it**, because a reset is a new bank. On `--db` it survives a
  restart, because the row that caused it does.

`POST /_mock/accounts/<id>/report` is not part of this: it answers one request, so
a report it cannot write fails that call and blocks nothing.

A creditor or debtor account that is **not** an IBAN is none of these: it is
valid input, and the bank writes it back the way it came — in `Id/IBAN` when it
strictly is an IBAN, and in `Id/Othr/Id` when it is not. `NL30 MOCK 0000 0000 05`
is accepted, matched to the account it names, and reported as `Othr/Id`, because
the `IBAN` element holds no spaces.

## Money arriving

Everything above is money leaving, or coming back. `POST /_mock/credits` makes
money *arrive*: a customer paying your invoice, which is the whole receivable
side of a business and where cash application quietly goes wrong, because **the
payer controls the reference**. You describe what the payer sent; the bank books
it and reports it the way it reports anything else.

```
curl -s -X POST http://127.0.0.1:8090/_mock/credits -H 'Content-Type: application/json' \
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
- **For an account that banks in NACHA the statement is BAI2**, as it is for
  that account's debits, and money arriving is a `16` of type code `142`, an
  ACH credit received: the `EndToEndId` as the bank reference, the structured
  reference as the customer reference and the payer's name as the text. The
  `camt.054` goes to every account, whatever its format.
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
  unbooked in `GET /_mock/credits`, and the mock does not send it back to the
  payer. Reopened, the account books it on the next business day, which is on
  a statement, and keeps the payer's value date.
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

**For a direct debit** ([#131](https://github.com/rseufert/mock-bank/issues/131))
the account holder is the creditor: a `pain.008` asks the bank to collect from
debtors under their mandates. A behaviour still describes the account it is set
on, so the table reads from the other side:

| Behaviour | On the account the file collects **for** | On an account it collects **from**, if the bank holds it |
| --- | --- | --- |
| `closed-account`, or a closed account | Every collection is rejected, `AC04` | That collection is rejected, `AC04` |
| `insufficient-funds` | Nothing: a collection adds to this balance | That collection is rejected `AM04` if it is more than the debtor has available: its balance, less its own payments accepted and not yet booked, less what this file has already taken from it |
| `return-later` | Nothing | Accepted and settled, then sent back `days` business days later with `reason`; only the collection `end_to_end_id` names, if it names one |
| `reject-file` | The file is rejected, `RJCT` `FF01` | Nothing: a debtor sends no file |
| `silent` | Decided, and no `pain.002` is sent | Nothing |
| `accept`, `duplicate-file`, `statement-gap`, `bad-bank-id` | Nothing more | Nothing |

A debtor's balance is **read and never changed**: the mock books only the
account holder's side, so two files are each held to the same balance. A debtor
at another bank is accepted, because nothing about it can be known. A creditor
account the bank does not hold is `AC03`, and a collection that states no
mandate or no date of signature is `MD02`. An accepted collection is to settle
on its requested collection date, rolled to a business day, and never before the
business day after the bank can start on the file.

The file the repository ships collects for `ACME` from the four accounts the
seed starts with, so it needs nothing set up first:

```bash
curl -s --data-binary @tests/samples/pain008_four_collections.xml \
     http://127.0.0.1:8090/payments
```

Two of its four collections are accepted - `DD-2026-0101`, whose debtor is at
another bank, and `DD-2026-0104`, a debtor this bank holds - and two are
rejected on receipt: `DD-2026-0102` is `AM04`, because `GLOBEX` has 12.50 to
its name, and `DD-2026-0103` is `AC04`, because `INITECH` is closed. So the
file's status is `PART`, and the two that were accepted credit `ACME` together
on their settlement date.

**On that day the creditor account is credited.** The bank sends a `camt.054`
with a `CRDT` entry for each collection - one notification per account and
settlement date - and the day's `camt.053`, or BAI2 statement for a NACHA-format
account, carries the same entries, as does a `camt.052` asked for that day. Each
entry names the debtor, the `EndToEndId`, the file's `MsgId` and the mandate
(`Refs/MndtId`). Until then the notification is listed in `GET /_mock/queue`.
What already covers money arriving covers this:

- **The balance ceiling.** A collection that would take the creditor account
  past the 18 digits a statement can write, counting everything already on its
  way to that account, is rejected `AM02`.
- **Holidays.** A day that becomes a holiday moves the collections still to
  settle on it to the next business day, and a day one has settled on cannot
  become one.
- **An account closed while a collection waited** is not credited: the
  settlement date moves on a business day each time it comes due, so a reopened
  account books it on a day that has a statement.

`GET /_mock/collections` shows `booked_at` and the file's `received_at`, both on
the bank clock.

**The debtor's bank can say no.** For a debtor at another bank,
`POST /_mock/collections/<EndToEndId>/refuse` with `{"reason": "MD01"}` is that
bank's answer, and what it does depends on when it arrives:

- **Before the collection settles** it is rejected with the reason. The bank
  sends a further `pain.002` naming the original file and batch with that one
  transaction `RJCT`, and nothing books.
- **After it settled** the money goes back: the creditor account is debited on
  the first day the bank can book it - today, on a business day before the
  cutoff - and the bank sends a `pacs.004` naming the `pain.008` and a
  `camt.054` debit, one of each as for a payment's return. The statement for
  that day carries the debit. When the bank can book the return today both are
  released at once and are never queued; when it cannot - a weekend or a
  holiday, or after the cutoff - both are listed in `GET /_mock/queue` until
  the day it books.

The reason is an ISO 20022 return reason the status report can also carry:
`AC04`, `AM04`, `MD01` (no mandate), `MD06` (the debtor asked for it back),
`MS02` and the others the error lists. On a NACHA-format account it is an `R`
code instead, and the answer is a return file; see the NACHA section above. With the same `EndToEndId` in several
files, the newest is the one refused. A collection already rejected, already on
its way back or already returned is `409`, and so is a return that would
overdraw the account past what a statement can write.

**A debtor this bank holds is not refused by hand**: that is `409`, because its
own state and behaviour decide. `return-later` on that account sends a settled
collection back by itself, through the same `pacs.004` and debit. Either way
only the account holder's side books, and a return is not debited from an
account closed meanwhile until it reopens.

### The accounts it starts with

Four, the same four every time, one per failure you are likely to want. The
IBANs carry real mod-97 check digits and `MOCK` is not an assigned bank code,
so they are valid to parse and belong to nobody. They are the same accounts
`tests/samples/pain001_four_payments.xml` names, so the sample file works
against the mock out of the box, as does
`tests/samples/pain008_four_collections.xml`, which collects from them.

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
payment books even below zero, as on an account with an overdraft - down to the
limit every account has, whatever its behaviour. A balance is at most 18
digits of minor units either side of zero, because that is what a statement can
write. A payment that would overdraw past it is `AM02`. A `PATCH` or a new
account past it is refused. So is a `PATCH` or a credit that would leave no room
for the returns due back and the credits waiting to book (#106). A creditor
at another bank - a well-formed IBAN the mock does not hold - is not something
a bank can check at acceptance, so a payment to it settles.

An accepted payment debits its account on its settlement date, not on
receipt. The mock books the **debit side only**: a payment into an
account it holds does not credit that account, and a collection from one does
not debit it, so every balance change has a
statement entry to explain it. That is a choice, and it is stated here. The
credits it books are a return, which puts the money back where it came from,
and money arriving from somebody else through `POST /_mock/credits` (#91). Each
has a `CRDT` entry on that day's statement to explain it.

Two rules hold whatever the behaviour, because real banks apply them:

- A payment's settlement date is **the later of the requested execution date
  and the day the bank can start on** — which is the day of receipt before the
  cutoff and the next business day at or after it — **rolled forward past
  weekends and holidays.** The cutoff is 15:00 bank time by default
  (`--cutoff`), and at 15:00 exactly it is already too late, because a bank
  that stops taking today's work at 15:00 has stopped at 15:00:00.
- Weekends and the holiday list at `GET/PUT /_mock/holidays` are not business
  days. Bank time is one zone, `--timezone`, UTC by default.
- **A day that becomes a holiday takes nothing with it** (#107). Whatever was
  already due on it moves to the next business day when the holiday is
  declared: a payment's settlement, a return, and money arriving, all by the
  same rule. Every statement still opens where the one before it closed.
  Something that has already booked cannot move, so declaring *today* a holiday
  is refused with a 409 naming what booked. A day already past moves nothing,
  and its statement is already out.

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

**Which timestamps are bank time and which are real time.** A stamp that records
a moment the bank clock *decided* is on the bank clock; a stamp that records when
this process did something is on the real one. So a payment due on the bank's
Friday that `POST /_mock/advance?days=3` books on the Sunday says Sunday, and the
request log still says when the call actually arrived. Ordering events from the
control plane therefore works within either clock, and the two are not
comparable.

| Bank clock | Real clock |
| --- | --- |
| a message's `queuedAt`, `releasedAt` and `dueAt` | `/_mock/requests`' `at`: when the call reached this process |
| a payment's `booked_at`, and a credit's | `started` in `/_mock/state`: when the mock was launched |
| a payment's `returned_at`, both kinds | the pickup folder's own record of having written a file |
| a credit's `received_at`, and a file's | |
| a collection's `refused_at` | |

A payment's `booked_at` and a credit's were real time through 0.5.0, which a
reader could see in three places: `booked_at` on a payment and on a credit, and
`returned_at` ([#147](https://github.com/rseufert/mock-bank/issues/147)).

Two things a bank-clock stamp is not. It is **the moment the bank processed the
thing, not the date it settles on**: a payment due on the Friday that the clock
only reaches on the Sunday has `booked_at` `2026-09-27T11:30:00Z` and
`settlement_date` `2026-09-25`, and it is `settlement_date` a statement books it
under. It is also **the bank's moment written in UTC**: under
`--timezone Pacific/Auckland --clock 2026-09-24T00:30` a payment settling on the
bank's 24th has `booked_at` `2026-09-23T12:30:00Z`, so the date part of a stamp is
not the bank's own date outside UTC.

A file's `received_at` is reported beside its `msg_id` on each of its payments, at
`GET /_mock/payments` and `GET /_mock/payments/<EndToEndId>`: the moment the bank
took the file in, which is the moment the cutoff was judged on. It was stored and
served nowhere before, so the nearest thing a client had was the `pain.002`'s
`releasedAt`, which is the same moment only when `--status-delay-ms` is zero. A
file of collections is stamped by the same rule and the same code; reporting it on
`GET /_mock/collections` belongs to that feature's own step.

### When a message was queued

`dueAt` says when the bank will send a message. `queuedAt` says when the bank
came to owe it: the bank-clock moment its entry entered `GET /_mock/queue`,
whatever caused that. It does not depend on when anybody read the queue, so two
captures of one run agree, and the message carries the same `queuedAt` into the
mailbox, where a single read at the end can pair it on `key`
([#185](https://github.com/rseufert/mock-bank/issues/185)).

| The entry | Was queued when |
| --- | --- |
| a `camt.054` for payments or collections not yet settled | the file arrived: its `received_at`. With several files for one account and day, the earliest |
| a `camt.054` for money arriving | the bank heard of it: the credit's `received_at` |
| what a payment's return brings, under `return-later` | the payment booked and its return was scheduled: its `booked_at`, not the file's arrival |
| a NACHA account's rejection coming back as a return file | the file arrived, which is when that return is scheduled |
| what a refused collection brings | the debtor's bank refused it: the collection's `refused_at`, which can be days after it booked |
| a status report held back by `--status-delay-ms` | it was written, which is when its file arrived |

An entry's `queuedAt` is never later than the bank clock at the first read that
shows it. A message that was never in the queue because it was due at once - a
return refused and sent back on the same business day, a status report with no
delay - was queued as it was sent, and says so.

Two things have none, and read `null`:

- **A statement and an intraday report.** Neither is in the queue: a statement is
  written for every open account as a business day ends, owed for nothing that
  happened.
- **A message written before 0.7.** Nothing recorded when it was queued, and none
  is made up. An entry still waiting in a `--db` file from an older version does
  have one, because what queued it was always kept.

One falls back. **A collection refused by hand before 0.7** whose return is still
waiting has no `refused_at`, so that return reads as queued when the collection
booked, or when its file arrived if it never did.

One case moves. A `camt.054` for collections not yet settled reads as queued when
the earliest collection **it still reports** arrived. If two files collect into
one account on one day and every collection of the earlier file is refused before
it settles, the entry stays, for the later file, and its `queuedAt` becomes that
file's arrival.

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
| What it could not send | `GET /_mock/unsent` | The messages the bank owes and could not write, oldest first, each with the writer's own complaint and the day it was for (#166). Counted in `/_mock/state` under `messages.unsent`; nothing retries them, and a reset forgets them |
| Collect answers | `GET /_mock/mailbox` | Every message released and not yet collected, oldest first, as JSON with its XML body, its `key` and its `queuedAt`; collecting takes them. `?leave` to peek without taking, `?raw` for the XML bodies alone, `?type=pain.002` to filter on a type prefix, and they combine |
| What it is going to send | `GET /_mock/queue` | What the bank has not released yet, soonest first, each with `queuedAt` and `dueAt`: a message already written and held back (a status report under `--status-delay-ms`, with its `id`), and the ones it will write when something books, with no `id` yet - the `camt.054` for payments not yet settled, for money arriving and for collections not yet settled or on their way back, and the `pacs.004` or NACHA return file and the credit a return brings. `reports` says which. **Every entry has a `key`, and the message arrives in the mailbox under the same `key`**, so "was waiting" pairs with "arrived" without matching on type and time: `camt.054.001.08/ACME/2026-10-06/payments-settling` for one the bank will write (type, account, the day it books under, what it reports, and the file where there is one message per file), `m<id>` for one already written. Treat it as opaque. A second message of one kind for one day - a file posted on its own settlement day, after that day's notification went out - ends `#2`, so a key names one message. `?type=` filters on a prefix, as the mailbox does. Reading it releases nothing and takes nothing. A statement is not listed: one is written for every open account when the clock is advanced past the end of a business day. **`queuedAt` is when the entry entered the queue**; see [When a message was queued](#when-a-message-was-queued) |
| One message | `GET /_mock/mailbox/<id>` | That message's XML, whether or not it has been collected |
| Collect it again | `POST /_mock/mailbox/<id>/unread` | Puts one back in the mailbox, for a test that collects twice |
| What it has sent | `GET /_mock/messages` | Every message released, **collected or not**, oldest first: the mailbox's fields and `takenAt`, the bank-clock moment it was collected or `null`. For somebody watching a client that collects its own messages, which the mailbox then no longer lists ([#186](https://github.com/rseufert/mock-bank/issues/186)). `?type=` filters on a prefix. Reading it takes nothing and releases nothing, so it and `GET /_mock/queue` between them list every message once. It shows what the bank still holds: `--retention-days` removes collected messages as they age, and a reset removes them all |
| Money arriving | `POST /_mock/credits`, `GET /_mock/credits` | Make a credit arrive in an account from a payer you describe: it books on its value date and shows on the `camt.054` and `camt.053` as a received transfer. The listing is every credit, newest first |
| Collections | `GET /_mock/collections`, `GET /_mock/collections/<EndToEndId>` | Every direct debit the bank decided on from a `pain.008`, newest first, with its mandate, its debtor, the decision, its settlement date, when its file was received, when it booked and when it went back; or the newest with one `EndToEndId`, `?all` for every one |
| The debtor's bank says no | `POST /_mock/collections/<EndToEndId>/refuse` | With `{"reason": "MD01"}`. The collection keeps the moment as `refused_at`. Before settlement the collection is rejected and a further `pain.002` says so; after, the money goes back with a `pacs.004` and a `camt.054` debit. For a debtor at another bank: one this bank holds decides by its own behaviour |
| What was asked of it | `GET /_mock/requests` | The newest hundred requests with their status, `?path=` to filter on a prefix: what your client actually sent, rather than what you believe it sent |
| Accounts | `GET/POST /_mock/accounts`, `GET/PATCH /_mock/accounts/<id>` | Balances, behaviour, behaviour parameters, `format` (`iso20022` or `nacha`) and the domestic `account_number` a NACHA file names it by |
| Statements | `GET /_mock/accounts/<id>/statements` | The `camt.053` statements issued for an account: number, day, opening and closing balance, entries shown |
| Intraday report | `POST /_mock/accounts/<id>/report` | A `camt.052` for the account now, released to the mailbox and the pickup directory at once; answers `201` with its number, day, opening and interim balance and how many entries it carries. Every account gets one, whatever its `format`; a closed account is `409` |
| Behaviours | `GET /_mock/behaviours` | Every behaviour with what the bank does, from the table the mock itself dispatches on |
| Holiday list | `GET/PUT /_mock/holidays` | The days the bank does not settle on, as a JSON list of dates, replaced whole. What is still due on a day that becomes one moves to the next business day; today is refused once something has booked on it |
| Clock | `POST /_mock/advance` | `?days=N` (calendar days) or `?to=YYYY-MM-DD`; answers with the business days crossed, and releases whatever came due |
| Validate only | `POST /_mock/validate` | A `pain.001`, a `pain.008` or a NACHA file. Findings in prose, one line each; `200` when clean, `422` when not; nothing stored. `Accept: application/json` adds the mock's reading of the file |
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
| `--port` | `8090` | Port. |
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
python3 -m mockbank --port 8090 --drop-dir bank/in --pickup-dir bank/out \
        --drop-settle-ms 0
```

Then, in another terminal:

```bash
cp tests/samples/pain001_four_payments.xml bank/in/
curl -s -X POST http://127.0.0.1:8090/_mock/drop/scan
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

## Worked examples: in mock-acme

Three worked integrations lived in `examples/` here. They are code that sits
*between* this mock and the other two, so they have moved to
[mock-acme](https://github.com/rseufert/mock-acme), which holds one copy of each and tests it against all three
mocks:

| Was | Is now | What it does |
| --- | --- | --- |
| `examples/pay_invoices.py` | [`mockacme/pay_invoices.py`](https://github.com/rseufert/mock-acme/blob/main/mockacme/pay_invoices.py) | Collects [mock-edi](https://github.com/rseufert/mock-edi)'s EDIFACT invoices, pays each on its due date in one `pain.001`, and follows it to the statement |
| `examples/payment_run.py` | [`mockacme/payment_run.py`](https://github.com/rseufert/mock-acme/blob/main/mockacme/payment_run.py) | Selects [mock-sap](https://github.com/rseufert/mock-sap)'s open items, pays them as a `pain.001` or a NACHA file, and posts the bank's statement back to SAP as a `FINSTA01` |
| `examples/procure_to_pay.py` | [`mockacme/procure_to_pay.py`](https://github.com/rseufert/mock-acme/blob/main/mockacme/procure_to_pay.py) | One purchase across all three mocks, from the order to the cleared payment |

Their tests moved with them, and so did `tests/test_payment_run_readers.py`,
which holds `payment_run`'s hand-written statement readers to this mock's own
writers. [`examples/README.md`](examples/README.md) says which file became
which.

**`from mockbank.examples import payment_run` no longer works.** It did from
0.6.0 to 0.7.0, and it now raises an `ImportError` naming mock-acme.
`mockbank.examples.client` and `mockbank.examples.statement`, which need nothing
but this mock, are still in the wheel. mock-acme is not on PyPI; install it from
its repository.

## Docker

```bash
docker build -t mock-bank .
docker run -p 8090:8090 mock-bank --auth tester:s3cret
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
mockbank/direct_debit.py       direct debits: what the bank decides about a pain.008, and the collections it records (#131)
mockbank/drop.py               the second door: a directory watched, and one written
mockbank/db.py                 the schema, the upgrade, and the seeded accounts
mockbank/handler.py            the request handler: authentication, the body, the request log, and the lookup in the route table
mockbank/messages.py           reading a pain.001 into a PaymentFile, and writing the pain.002, camt.054 and camt.053 the bank sends back
mockbank/bai2.py               BAI2: the record declarations, and a writer for the statement a camt.053 reports
mockbank/nacha.py              NACHA: the record declarations, and a reader into the same payments as a pain.001
mockbank/outbox.py             what the bank sends and when: the message queue, release as the clock moves, the mailbox
mockbank/routes/__init__.py    the route table: each surface registers method, path pattern and function
mockbank/routes/control.py     health, state, reset, behaviours, the dictionary and the index page
mockbank/routes/accounts.py    the accounts and their statements
mockbank/routes/clock.py       advancing bank time, and the holidays
mockbank/routes/credits.py     POST and GET /_mock/credits
mockbank/routes/collections.py GET /_mock/collections, and the debtor's bank refusing one
mockbank/routes/payments.py    POST /payments, and /_mock/payments
mockbank/routes/mailbox.py     the mailbox, what is queued, and the request log
mockbank/routes/validate.py    POST /_mock/validate
mockbank/routes/transport.py   the folder transport's state, and a scan on demand
mockbank/schema.py             the ISO 20022 dictionary: every message, element and code set, and the walker and builder derived from it
mockbank/server.py             Config, and make_server, which puts the state, the handler and the routes together
mockbank/state.py              what survives between requests: the database, the clock, the lock, and the pipeline both doors feed
mockbank/validate.py           findings about a payment file: refusals, structure, and the mock's own checks
```

`python -m mockbank` is the entry point; `tests/` drives a real server over
HTTP; `tools/` holds the checks CI runs; `examples/demo.sh` is the curl tour. The worked
integrations with mock-edi and mock-sap are in [mock-acme](https://github.com/rseufert/mock-acme).
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) says how the pieces are meant to
fit, and [docs/FILES.md](docs/FILES.md) describes every file.

## Out of scope

Each of these is left out on purpose, and the mock says so by name rather
than half-supporting it.

| Left out | Why |
| --- | --- |
| EBICS, SWIFT FIN and SWIFTNet transport | Both need certificates and cryptography, which breaks zero dependencies; the same call mock-edi made on S/MIME. HTTP and folders cover testing. |
| Signed or encrypted files | Same reason; an encrypted file is refused with a message saying so. |
| Direct debits beyond `pain.008.001.08` and NACHA | 0.6 collects with `pain.008.001.08` (#131) and with a NACHA file's debit entries ([#176](https://github.com/rseufert/mock-bank/issues/176)). Out on purpose: `pain.008.001.02`, the older version many banks still take; and a mandate register - the mock reports the mandate a file states and polices none (no amendments, no `FRST` before `RCUR`). |
| Real-time payments, cards, FX | Different rails and rules; each is a project of its own. |
| Fraud, sanctions and AML screening | Real logic, not wire shapes; out of scope permanently, like SAP business logic in mock-sap. |

## Roadmap

| Release | Scope | Done when |
| --- | --- | --- |
| 0.1 | ISO 20022 credit transfers: `pain.001` in; `pain.002`, `camt.054`, `camt.053` out; accounts, balances, cutoff, holidays, clock; every behaviour above except `return-later`; HTTP only | **Done.** Every message the mock writes validates against its own dictionary and against the published XSDs; the tour and the example client run in CI |
| 0.2 | Returns (`pacs.004`, `return-later`), folder transport, retention, the `payment_run` example | **Done.** `payment_run`'s thirteen tests pass in CI against mock-sap from PyPI |
| 0.3 | US formats: NACHA files in, NACHA returns (`R01`, `R02`, `R03`), BAI2 statements out | **Done.** The same `payment_run` tests pass in NACHA mode, in CI against mock-sap from PyPI |
| 0.4 | Money arriving: an incoming credit on `POST /_mock/credits`, so cash application is testable; a worked example using all three mocks, procure to pay | **Done.** `procure_to_pay`'s ten tests pass in CI against mock-sap and mock-edi from PyPI |
| 0.5 | BAI2 finished: `bai2.read` and `payment_run`'s reader take a real bank's file (a text field with commas, records packed onto a line, a record with no `/`, funds types `V`, `S` and `D`), a payee's name reaches the BAI2 statement as the `camt.053` has it, and the transaction type codes are the ones a bank writes, each from a named source; an intraday `camt.052` on request | **Done.** What the mock reads and writes holds against moov-io/bai2's sample files from outside the project |
| 0.6 | Direct debits: `pain.008.001.08` read, validated and booked as collections, a debtor the bank holds deciding by its own state and behaviour, and the debtor's bank refusing or returning one; a NACHA file's debit entries collected the same way and returned as a return file (#176); `GET /_mock/queue` for what the bank has not sent yet (#155) and `GET /_mock/unsent` for what it could not write (#166); bookings stamped with the bank's clock (#147); a wrapped BAI2 line joined without its line break (#142); the worked examples importable as `mockbank.examples` (#169) | **Done.** A message the bank cannot write no longer stops the others, and input it could not answer for is refused at the door |
| 0.7 | What a client that watches the bank asked for: `queuedAt` on every queue entry and on the message it becomes (#185); `GET /_mock/messages`, a listing that includes collected messages (#186); a `pain.008` sample the seed accepts (#187) | **Done.** Two captures of one run agree on when each message was promised, and one read at the end can say it |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): what the project values, where things
live, what a good pull request looks like, and how the team works the issue
queue.
