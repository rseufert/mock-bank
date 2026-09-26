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

These three projects publish under the **Apache License 2.0**, a copy of which
is `LICENSE-Apache-2.0.txt`. None of them has a `NOTICE` file. The
`pain.001` samples are copyright (C) 2023-2026 Pain001; the others carry the
copyright of their projects' authors.

## Invalid: the XSD rejects them, and so must the mock

| File | Message | Source | Path at that commit | Why the XSD rejects it |
| --- | --- | --- | --- | --- |
| `invalid/camt.053.001.08-query-farm.xml` | `camt.053.001.08` | [Query-farm/vgi-iso20022](https://github.com/Query-farm/vgi-iso20022) @ `4e5dd8678f704f2225f888a1e20eb4af84725550` | `data/camt053/statement1.xml` | `FrToDt` after `Acct`, `Bal` without its `Dt`, a party name where `.08` has the `Pty`/`Agt` choice |
| `invalid/camt.054.001.08-query-farm.xml` | `camt.054.001.08` | same | `data/camt054/notification1.xml` | An entry without its mandatory `BkTxCd`, and the same party shape |

They are here to show the agreement runs both ways: a dictionary that
accepted them would be too loose. Copyright 2026 Query Farm LLC, under the
**MIT License** in `invalid/LICENSE-MIT-Query-Farm.txt`.

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
