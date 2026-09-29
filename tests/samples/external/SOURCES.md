# External samples: where each one came from

These files were written outside this project. They are the ground truth
`GeneratedMessagesAreValid` checks the dictionary against. The mock's writers
and its validator are both built from `mockbank/schema.py`, so they can agree
with each other and both still be wrong about the standard. A file somebody
else wrote, which the published XSD accepts, cannot share that blind spot.

Every file is **unmodified**, fetched from the commit named below. Each one
in this directory validates against the published XSD for its message, and
each one in `invalid/` fails it. `tools/check_xsd.py` checks both, and the
tests check the mock agrees: the valid ones walk with no error and the
invalid ones do not.

## Valid: the XSD accepts them, and so must the mock

| File | Message | Source | Path at that commit | What it exercises |
| --- | --- | --- | --- | --- |
| `pain.001.001.09-every-element.xml` | `pain.001.001.09` | [sebastienrousseau/pain001](https://github.com/sebastienrousseau/pain001) @ `b86d0de5c7704a1fa6159f009c8e0ab3a3801bc4` | `pain001/corpus/data/coverage/pain.001.001.09/01-transfer-every-element.xml` | Every element the XSD allows, once: most of it is read past with a warning, none of it may be an error |
| `pain.001.001.09-remittance-parties.xml` | `pain.001.001.09` | same | `…/02-transfer-RmtInf-InitgPty-Cdtr.xml` | Structured remittance, postal addresses with a proprietary address type, debtor, creditor and intermediary agents |
| `pain.001.001.09-ultimate-debtor.xml` | `pain.001.001.09` | same | `…/06-transfer-UltmtDbtr-Id-PstlAdr.xml` | An ultimate debtor with an identifier and a postal address |
| `pain.001.001.09-de-sct-inst.xml` | `pain.001.001.09` | same | `pain001/corpus/data/market/de/sepa-instant/de.sepa.sct-inst.pain.001.001.09.xml` | A German SEPA instant credit transfer as a bank would receive it |
| `pain.002.001.10-every-element.xml` | `pain.002.001.10` | [INX-EXCHANGE/iso20022-rs](https://github.com/INX-EXCHANGE/iso20022-rs) @ `637c1460b65714990ad32b0d682b9f365dcaa859` | `samples/BaseSample.xml` | A status report carrying an `OrgnlUETR` and the `ACCC` status |
| `pain.002.001.10-cbpr-originator.xml` | `pain.002.001.10` | same | `samples/CBPR_Originator_Option1Rule_01.xml` | A status report named for a CBPR+ originator rule |
| `pain.002.001.10-sepa-status.xml` | `pain.002.001.10` | [apiome/apiome](https://github.com/apiome/apiome) @ `80f60429eb4c80b8c36a6fe1d6a33f9e3474b706` | `apiome-ui/examples/sepa/07-status-set/pain002.xml` | A SEPA status report with transaction-level statuses |
| `camt.053.001.08-sepa-statement.xml` | `camt.053.001.08` | same | `apiome-ui/examples/sepa/06-typical-camt053-statement.xml` | A day's statement: balances, entries, related parties |
| `camt.054.001.08-notification.xml` | `camt.054.001.08` | same | `apiome-ui/examples/iso20022/05-camt.054-notification.xml` | A notification with charges and supplementary data |

The three ISO 20022 projects publish under the **Apache License 2.0**, a copy of which
is `LICENSE-Apache-2.0.txt`. None of them has a `NOTICE` file. The
`pain.001` samples are copyright (C) 2023-2026 Pain001; the others carry the
copyright of their projects' authors.

## NACHA: written by another reader's authors

NACHA has no XSD, so these hold `mockbank/nacha.py`'s field positions to a
reader that is not this one. They are `.ach` files, so `tools/check_xsd.py`
does not see them; `tests/test_nacha.py` reads them.

| File | Source | Path at that commit | What it exercises |
| --- | --- | --- | --- |
| `nacha-loan-credit.ach` | [moov-io/ach](https://github.com/moov-io/ach) @ `7ee7ad03d7342e1f651c32db22fc8168c2b97cce` | `test/testdata/loan-credit.ach` | A one-entry credit batch that must read with no finding: its entry hash, counts, totals and block count all agree |
| `nacha-ppd-mixedDebitCredit.ach` | same | `test/testdata/ppd-mixedDebitCredit.ach` | A batch with a debit and two credits: the debit is the one finding, the credits are read |
| `nacha-return-WEB.ach` | same | `test/testdata/return-WEB.ach` | A return file: a returned debit (`26`, `R01`) and a returned credit (`21`, `R03`), each with its addenda 99. It pins the positions the mock's return writer (#54) has to match, and it found that a return of a debit totals as a debit, which the mock's own reader and writer had agreed wrongly on (#55) |

moov-io/ach is **Apache License 2.0** (`LICENSE-Apache-2.0.txt`), copyright
2018-2020 The Moov Authors, and it has a `NOTICE` file, kept beside the samples
as `NOTICE-moov-ach.txt`.

## BAI2: the first real file this format's reader was shown

BAI2 has no XSD either, so this holds `mockbank/bai2.py` to a file its authors
did not write. `tests/test_bai2_external.py` reads it.

| File | Source | Path at that commit | What it exercises |
| --- | --- | --- | --- |
| `bai2-sample1.txt` | [moov-io/bai2](https://github.com/moov-io/bai2) @ `d3e11b628d3d59fd6911836b9ca328cb8b7621f2` | `test/testdata/sample1.txt` | Two account sections in one group, each with an `88` continuation carrying credit and debit summaries, eleven and six `16` details, and every amount on a `V` (value-dated) funds type. It settles what the writer's record counts and control totals mean, and it is the file that showed the reader cannot read real BAI2 at all |
| `bai2-sample2.txt` | same | `test/testdata/sample2.txt` | Four groups, five accounts, and the file that settled how continuations work (#114). `88` follows an `03`, a `16` and another `88`; a summary group **splits across the boundary**, its type code ending one record and its amount beginning the next; funds types `S`, `V`, `1` and blank appear, and `D` with its (days, amount) pairs; balances are written `+4350000`, `2830000` and `-500000`, so signed and bare amounts sit in one file |
| `bai2-sample3.txt` | same | `test/testdata/sample3.txt` | A bank's export whose `16` texts name the movement. It settled `142` as the code a bank writes for an ACH credit received (`PPD`) (#127). It also packs several records onto one line - eleven of them share a line with another - and wraps one `16`'s text onto a second line that carries the terminator. Its trailers reconcile, so the packing is checked arithmetically and not only structurally (#128) |
| `bai2-sample4.txt` | same | `test/testdata/sample4-continuations-newline-delimited.txt` | A bank's export with an `SEC` code and `GS ID` on every `16`. It settled `447` as the code for an ACH credit payment the account holder sent (`CCD`, `CTX`), and showed that `495`, `165` and `195`, the codes it replaced, are written for an outgoing wire, a debit collection's proceeds and an incoming wire (#127). 102 of its 116 lines carry no terminator at all - the newline ends them - and its `16`s carry free text with commas inside one field. 85 continuations, 68 of them following another (#128) |
| `bai2-sample5.txt` | same | `test/testdata/sample5-issue113.txt` | Slashes **inside** fields: customer references like `AB/GS/RPFILERP0001/RPBA0001` and remittance text like `08/18/23 Invoice`. Twenty-two of them, and a reader that split on `/` would shatter every one. It also writes one continuation as `88:EREF: ...`, with a colon where the separator should be (#128) |
| `bai2-type-codes.go` | [moov-io/bai2](https://github.com/moov-io/bai2) @ `aee8612bc3e12439f201fc305b551e27a74c4e36` - **a later commit** than the rows above, because the table does not exist at `d3e11b6` | `pkg/bai2/type_codes_data.go` | moov's transcription of the type code table ("Appendix A of Cash Management Balance Reporting Specifications Version 2"): each code's direction, level and description. The evidence for `257`, *Individual ACH Return Item*: the samples' only `257` is a returned debit, and ours is a returned credit, so the table is what covers our case (#127) |

moov-io/bai2 is **Apache License 2.0**. Its `LICENSE` at that commit is
byte-for-byte the copy already here as `LICENSE-Apache-2.0.txt`, so no second
copy is kept. The same holds at `aee8612`, the later commit `bai2-type-codes.go`
is pinned to. It has **no `NOTICE` file** - `NOTICE-moov-ach.txt` is
moov-io/ach's and does not cover it. Copyright The Moov Authors.

SHA-256 of each file as fetched, so a re-pin is a visible change:

| File | SHA-256 |
| --- | --- |
| `bai2-sample1.txt` | `0150331e6118e9fc6a1a10871f739b2d317c5cca5159c007622cffbbb64fe00c` |
| `bai2-sample2.txt` | `34ccf04a37e44353e5aac16981201239ae90102c12806aaa739a2e13ae3aee6b` |
| `bai2-sample3.txt` | `8a13ec611352000fbab9a880858e8349b50380fab9754bc237391738cfd9ada4` |
| `bai2-sample4.txt` | `5a11cde54c9c8266b34d9980ee66237c1311f56b87d9eb1d28e5f02bafebaa9f` |
| `bai2-sample5.txt` | `0391a0999e718ee84048f1b9642be5b08f963c41f3cd628f3f8ac677fb9a2e5c` |
| `bai2-type-codes.go` | `2efbcb0cd05620f4e2c2bdb10c528f70571ad9cba6285cd657ebb0b7eebe66ee` |

**`sample4` and `sample5` do not reconcile, and that is the file rather than the
reader.** Each states one account total of `-1260161341762` and two of `000`,
and its group trailer is the sum of the positive ones alone - which no
consistent file would be. Every coded line in both is read and accounted for.
They are moov-io's parser fixtures, one named for a bug report, so they exercise
reading and not arithmetic. `TheTwoFixturesWhoseOwnArithmeticIsWrong` pins that,
because the tempting conclusion when a total disagrees is that the reader is
wrong.

`.gitattributes` declares this directory `-text` so a Windows checkout does not
rewrite these line endings; without it `bai2-sample1.txt` hashes to
`0258766c...` there and the digest above is wrong on one platform only.

### What it found

Every number below was computed from the file, not read off it.

**Settled, and the writer was already right:**

- **A record count includes the trailer carrying it.** The first `49` states 14
  and covers 14 records counting itself; `98` states 25 for `02` through `98`;
  `99` states 27 for a 27-line file. `COUNTS_INCLUDE_THE_TRAILER` was right.
  Counts include `88` continuations as records in their own right.
- **Control totals are signed**, and a `98` sums its `49`s while a `99` sums its
  `98`s: 834000 + 446000 = 1280000, twice over.
- **The `03` names an account number**, `10200123456`, not an IBAN.

**Settled, and the writer was wrong:**

- **The `02`'s originator is the bank and its ultimate receiver the customer.**
  The `01` is `sender=0004, receiver=12345`; the `02` is
  `ultimate receiver=12345, originator=0004`. `0004` is the bank in both. The
  writer had the customer originating its own statement.
- **A positive control total carries an explicit `+`**: `49,+00000000000834000,14/`.
  The writer wrote a bare number.
- **A control total sums the summary amounts as well as the details.** Each
  `49` here is exactly twice the sum of its `16`s, because the `88`'s credit and
  debit totals are counted too: 417000 + 417000 = 834000, and 223000 + 223000 =
  446000. The writer's own arithmetic does this correctly. What was wrong was
  the reason recorded in `_account` for keeping movement totals out of the `03`:
  it said a reader reconciling the total against the entries would have to know
  to halve it. A BAI2 reader does know that, because this is what BAI2 does.

**Found here, not asked about:** the reader could not read this file at all, in
three ways - an undeclared `88`, a `V` funds type two fields wider than declared,
and a summary group whose sixth field made `_amounts_in` raise a bare
`ValueError` rather than refuse. #114 fixed all three, and `sample2.txt` is what
made the fix evidence rather than a guess:

- a **continuation continues the field stream** of the record before it, so it is
  folded in before anything is parsed. It follows any record, it chains, and a
  group may split across it.
- a **funds type carries its own width**: one field for blank, `Z` or a digit,
  three for `V` (date and time), four for `S` (three availability amounts), and
  2 + 2n for `D` (a count, then that many pairs).
- a **record count counts physical records** including continuations, while a
  **control total sums the logical record**. Both are right in every file this
  mock writes and only a file with an `88` in it can tell them apart.

The check worth having is not that either file reads. It is that
`trailers_agree` finds nothing in either: every control total and every record
count recomputed from the records it covers. A wrong fold or a wrong funds-type
width changes one of those numbers.

**Still not read**, each needing a sample this directory does not hold yet: a
`16` whose text field contains commas and runs to the end of the record
(`sample4`), several records packed onto one line separated by `/` (`sample3`),
and records with no `/` at all, where the newline terminates (`sample4`,
`sample5`). `WhatIsStillNotRead` in `tests/test_bai2_external.py` pins those
three.

## Invalid: the XSD rejects them, and so must the mock

| File | Message | Source | Path at that commit | Why the XSD rejects it |
| --- | --- | --- | --- | --- |
| `invalid/camt.053.001.08-query-farm.xml` | `camt.053.001.08` | [Query-farm/vgi-iso20022](https://github.com/Query-farm/vgi-iso20022) @ `4e5dd8678f704f2225f888a1e20eb4af84725550` | `data/camt053/statement1.xml` | `FrToDt` after `Acct`, `Bal` without its `Dt`, a party name where `.08` has the `Pty`/`Agt` choice |
| `invalid/camt.054.001.08-query-farm.xml` | `camt.054.001.08` | same | `data/camt054/notification1.xml` | An entry without its mandatory `BkTxCd`, and the same party shape |

They are here to show the agreement runs both ways: a dictionary that
accepted them would be too loose. Copyright 2026 Query Farm LLC, under the
**MIT License** in `invalid/LICENSE-MIT-Query-Farm.txt`.

## The XSDs these are checked against

`tools/check_xsd.py` holds the dictionary, these samples and everything the
mock writes to the published XSDs. They are ISO's, and the project points at
copies other projects publish instead of redistributing them. They're fetched
from these pinned commits and must match these SHA-256s before use:

| XSD | Source | Path at that commit | SHA-256 |
| --- | --- | --- | --- |
| `pain.001.001.09.xsd` | [prog-nov/iso20022-struct-go](https://github.com/prog-nov/iso20022-struct-go) (Apache-2.0) @ `b105620042e86826436edfdc45fdfa079b19894e` | `xsd/pain.001.001.09.xsd` | `de038b373e47b0077b1832ddd81f4b2f1eb25d35721f62da1e38b7f5a09fda24` |
| `pain.001.001.03.xsd` | same | `xsd/pain.001.001.03.xsd` | `6bb5c6f24250ab807f31f6164142bafd6d43bad8d162a926e258ff4c11e128af` |
| `pain.002.001.10.xsd` | same | `xsd/pain.002.001.10.xsd` | `2f9f8d0e9891fa9f31ccf0576397afe501614384d688ae6e43ba694b3d24b0cf` |
| `pacs.004.001.09.xsd` | same | `xsd/pacs.004.001.09.xsd` | `e2b13023bed19429bd8347ed9e13d31e6dec33fa4a8ef07dd03138d5442826b9` |
| `camt.053.001.08.xsd` | [genkgo/camt](https://github.com/genkgo/camt) (MIT) @ `56e047d1599854ca34db0ccabce15230fcdd3f16` | `assets/camt.053.001.08.xsd` | `c3cfac080dc31476bde7444b05d00e1b23558d5e44529e58d0ad562e6013873d` |
| `camt.054.001.08.xsd` | same | `assets/camt.054.001.08.xsd` | `2b392a1f7e70e70902fd0d803ff85989613bd1cae351663240b0bb9243be2c28` |
| `camt.052.001.08.xsd` | same | `assets/camt.052.001.08.xsd` | `113d29938c45ba1c993f2d3e31610a214f3fb3e9ea0c6f2945750c0586567d15` |

The same values are in `XSDS` in `tools/check_xsd.py`; `tests/test_dictionary.py`
checks that the two agree. To re-pin, change both.

## What they found

Before these files, the dictionary had three mistakes that no test inside
the project could see, because the writer and the validator shared them. All
three were fixed in the same change that added the files:

- `UETR` and `OrgnlUETR` are 36-character UUIDs; they were declared with a
  35-character limit.
- `PstlAdr/AdrTp/Prtry` is an identifier with its issuer
  (`GenericIdentification30`), not a line of text.
- `ACCC` (AcceptedCreditSettlementCompleted) is a transaction and group
  status the code lists lacked.

## Adding one

Take it from a public source whose licence allows redistribution, fetch it
from a pinned commit, leave it byte-for-byte as published, run
`python3 tools/check_xsd.py` to confirm the XSD agrees with where you put it,
and add a row above.
