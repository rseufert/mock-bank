# How mock-bank fits together

If you want to know *what each file is*, read [FILES.md](FILES.md). This
document is about why the pieces are shaped the way they are. It describes
what 0.2 actually does; the two or three things it deliberately does not do yet
are named as such, and the issue that builds each one is the place to argue
with it.

## The one idea

Everything is derived from a dictionary.

`schema.py` describes ISO 20022 the way the standard does: a message is a
namespaced tree of elements, an element is typed, bounded and may carry a code
list from the external code sets. Nothing else in the package hard-codes an
element path. `validate.py` reads a file by naming elements from it,
`messages.py` writes one in the order it gives, and `/_mock/dictionary` serves
it. The test that matters most, `GeneratedMessagesAreValid` in
`tests/test_dictionary.py`, checks every message the mock writes against the same
dictionary it checks yours against, and against the published XSDs, so a
declaration that is wrong about the standard is caught rather than
self-consistent.

## One pipeline, two doors

A `pain.001` arrives by `POST /payments`, or it is dropped into `--drop-dir`.
Both doors call `State.receive`, which is the pipeline: read, validate, decide,
book, queue. The folder door was written second and the pipeline was pulled out
of the HTTP handler to make room for it, rather than the handler being called
with a fabricated request - two doors that agree because there is only one
thing behind them, not because two code paths were kept in step.
`accounts.decide` reads the behaviour of whichever account the rule is about -
debtor-side ones from the debtor, `closed-account` and `bad-bank-id` from a
creditor account the bank holds - and produces an outcome and a reason code per
payment. `accounts.book` moves balances, debit side only. `outbox.queue_status`
puts the `pain.002` on the `message` table due `--status-delay-ms` after
receipt, and the `camt.054` and `camt.053` are written as the clock reaches
them rather than predicted in advance, because a later file can add payments to
the same account and day.

The way out is the same shape. Every message the bank releases is a row, and
`--pickup-dir` is a view of the released rows that have not been written yet:
`drop.write_released` asks the database rather than being handed the rows by
whoever released them, so a release path added later cannot forget to deliver,
and `message.written_at` is a column rather than a set in memory, so a restart
on `--db` does not write the whole history out again. The mailbox and the pickup
directory are two readers of one table, not two queues.

## A clock, not a sleep

Nothing in the mock waits. Settlement dates, the cutoff (15:00 by default),
weekends and holidays are all computed against a bank-time clock that
`POST /_mock/advance` moves; `clock.py` knows nothing about payments, and what
happens when a date arrives belongs to the hooks the pipeline registers in
`on_advance`. Advancing books what came due, writes the notifications for it,
closes a statement for every business day whose end it passed, and delivers
whatever that released into `--pickup-dir`. The order is load-bearing:
statements read the entries the advance has just booked, and the delivery reads
what both of them released. The clock
holds an offset from real time rather than a stored instant, so it keeps ticking
between advances; and it does not go backwards, because whatever was queued for
a date it had passed would come due a second time. A return is the same idea
with a longer fuse: when a payment from a `return-later` account books, its
return is set for `business_days_after(settlement, days)`, and the advance that
reaches that day credits it back and writes the `pacs.004`. A three-day return
is a test line.

## How a request finds its function

Each surface of the mock is one module under `mockbank/routes/`, and each
endpoint in it is a function registered with `@route(method, pattern)`. The
pattern is written the way the index lists it, `/_mock/accounts/<id>`, and the
function is called with the handler and each placeholder's segment. `handler.py`
authenticates, reads the body, takes the lock and looks the request up in that
one table. It knows nothing about any endpoint. A path the table does not have
is a `404` that names what it does have. A path it has only with other methods
is a `405` naming those methods. The index page, the `404` body and `/_mock/state` list
what the table holds, so adding an endpoint touches one file under `routes/`,
and `tools/check_docs.py` fails until the README's endpoint table has its row.
What lasts between requests, the connection, the clock and the pipeline, is
`State` in `state.py`, which the routes reach as `h.state`.

## Refusing, and saying who is asking

The control plane can reset the bank, rewrite every balance and behaviour and
read every message it wrote, so `--auth` puts HTTP basic on every request
including `/_mock/health` - a mock that answers an unauthenticated probe has
told whoever is probing that it is there. The folder door is not covered by it:
a file in the drop directory carries no credentials and is processed on its
own, because what guards a directory is the filesystem. That is true of a real
SFTP drop as well, and it means `--auth` alone does not close both doors. The check runs before the body is
read and before routing, so an unauthenticated file is never parsed and a `404`
cannot be used to map what exists. Binding an address other machines can reach
without `--auth` says so on stderr at startup, and `-q` does not silence it.

## Findings, not exceptions

Validation produces findings: an element path, a code, a sentence. The same
finding renders as a `pain.002` reason and as prose from `/_mock/validate`.

A file the mock cannot read is answered rather than dropped, but the two doors
answer differently, and the difference is worth knowing before you build
against one. Over HTTP the refusal comes back as a `pain.002` in the response
itself. Through the drop directory there is nobody to answer, so the file is
filed under `failed/` with a `<name>.findings.txt` beside it; no message row is
written, so nothing about it reaches the mailbox or `--pickup-dir`. A real SFTP
drop returns a status file, so this is a difference to close rather than a
design, and it needs a decision about the pipeline rather than about the
directory.

## What is deliberately absent

No transport that needs cryptography (EBICS, SWIFT), no signed or encrypted
files, no screening, no ledger beyond balances, no direct debits, and no credit
to a creditor the bank holds - the only credit it books is a return, which puts
money back on the account it left. Each is refused by name rather than ignored:
an encrypted body, an unknown message type and a payment from an account the
bank does not hold all produce an answer that says what *is* supported. The
README's out-of-scope table is the authority.

There is also no debtor/creditor field on an account, which is a smaller
decision of the same kind: which side an account stands on belongs to a
payment, not to the account, and the sample file pays the same `GLOBEX` that an
insufficient-funds test sends from.
