# How mock-bank fits together

If you want to know *what each file is*, read [FILES.md](FILES.md). This
document is about why the pieces are shaped the way they are. It describes the
0.1 design; the parts not yet built are named as such, and the issue that
builds each one is the place to argue with it.

## The one idea

Everything is derived from a dictionary.

`schema.py` describes ISO 20022 the way the standard
does: a message is a namespaced tree of elements, an element is typed,
bounded and may carry a code list from the external code sets. Nothing else in
the package hard-codes an element path. The reader names elements from it, the
validator checks against it, the writers build messages in the order it gives,
and `/_mock/dictionary` serves it. The test that matters most,
`GeneratedMessagesAreValid`, checks every message the mock writes against the
same dictionary it checks yours against, and against sample files from
outside the project so a declaration that is wrong against the standard is
caught too.

## One pipeline, two doors

A `pain.001` arrives by `POST /payments` today and by a drop directory in 0.2.
Both feed the same pipeline: read, validate, decide, book, queue. The decision
(`accounts.decide`) looks at the debtor account's behaviour and the balance and
produces, per payment, an outcome and a reason code. Booking moves balances.
Queueing puts the `pain.002` in the mailbox now and the `camt.054`,
`camt.053` and any `pacs.004` on the clock for the date they are due.

## A clock, not a sleep

Nothing in the mock waits. Settlement dates, the 15:00 cutoff, weekends,
holidays and return windows are all computed against a bank-time clock that
`POST /_mock/advance` moves. Advancing releases whatever came due: the
notifications for that settlement date, the statement for each business day
crossed, the returns whose window elapsed. A three-day return is a test line.

## Findings, not exceptions

Validation produces findings: an element path, a code, a sentence. The same
finding renders as a `pain.002` reason and as prose from `/_mock/validate`. A
file the mock cannot read still gets a `pain.002` saying why, because that is
what a bank does.

## What is deliberately absent

No transport that needs cryptography (EBICS, SWIFT), no signed or encrypted
files, no screening, no ledger beyond balances, no direct debits yet. Each is
refused by name. The README's out-of-scope table is the authority.
