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
       ◀──pain.002──  PART: 3 accepted, 1 rejected (AC04 closed account)
       ◀──camt.054──  3 debits on the settlement date
       ◀──camt.053──  end of day: opening, 3 entries, closing
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
  against the same rules it checks yours against. There is a test for it.
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
the money.

> **Status: 0.1 is being built.** The control plane runs today; the payment
> pipeline is tracked in the [0.1 milestone](https://github.com/rseufert/mock-bank/milestone/1).
> Everything below that is not yet true is marked with the release it lands in.

---

## Quick start

```bash
pip install mock-bank
mock-bank --port 8080
```

```bash
curl http://127.0.0.1:8080/_mock/health
curl http://127.0.0.1:8080/_mock/state
bash examples/demo.sh
```

Or from a checkout, with nothing to install:

```bash
python3 -m mockbank --port 8080
python3 -m unittest discover -s tests -v
```

## Formats and choreography

ISO 20022 comes first; NACHA and BAI2 follow in 0.3. You send one payment file
and the mock answers with the messages a real bank sends, in the order it
sends them.

| Message | Direction | When the mock sends it | What it carries | Release |
| --- | --- | --- | --- | --- |
| `pain.001` | In | You send it | Credit transfers: debtor account, one or more payments, amounts, creditors | 0.1 |
| `pain.002` | Out | Minutes after `pain.001` | Status per file, batch and payment: `ACCP`, `RJCT` with a reason code, `PART` when some are rejected | 0.1 |
| `camt.054` | Out | Each payment's settlement date | A debit notification per payment that settled | 0.1 |
| `camt.053` | Out | End of each business day | The statement: opening and closing balance, every entry, balances that reconcile | 0.1 |
| `pacs.004` | Out | Days later, on a return behaviour | A payment that had settled, coming back with a return reason | 0.2 |
| NACHA in, returns out (`R01`, `R02`, `R03`), BAI2 statements out | Both | As above, in US formats | The same choreography for ACH | 0.3 |

Versions: `pain.001.001.09` is read, and the older `pain.001.001.03` is
accepted as well and read into the same model. The mock writes
`pain.002.001.10`, `camt.054.001.08` and `camt.053.001.08`, the versions that go
with `pain.001.001.09` and that most banks accept today. That is a choice, not
the only right answer; so are the others the standard leaves open, and the
mock says which it made:

- **Bank transaction code.** Every debit the mock books carries
  `PMNT`/`ICDT`/`ESCT` (payments, issued credit transfer, SEPA credit
  transfer); 0.1 speaks euro credit transfers.
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

Each payment keeps its `EndToEndId` through every message, so a client can
match a status, a statement line and a return to the invoice it paid.

## Account behaviours

Each seeded account has a behaviour, changed at runtime with
`PATCH /_mock/accounts/<id>`, the same way mock-edi changes a partner. The
reason codes are the ISO 20022 external codes a real bank uses.

| Behaviour | What the bank does | Codes | Release |
| --- | --- | --- | --- |
| `accept` | Accepts every payment and settles it on the requested date | `ACCP`, then booked | 0.1 |
| `closed-account` | Rejects payments to one creditor account in `pain.002` | `AC04` | 0.1 |
| `insufficient-funds` | Rejects payments once the debtor's balance would go negative | `AM04` | 0.1 |
| `bad-bank-id` | Rejects a payment whose creditor bank identifier does not resolve | `RC01` | 0.1 |
| `return-later` | Accepts and settles, then returns the payment N business days later | `pacs.004`, `AC04` or `MD07` | 0.2 |
| `reject-file` | Rejects the whole file at group level | `RJCT`, `FF01` | 0.1 |
| `duplicate-file` | Rejects a file whose `MsgId` it has already seen | `DUPL` | 0.1 |
| `silent` | Sends no `pain.002` at all | none | 0.1 |
| `statement-gap` | Leaves one settled entry off the `camt.053` | none, which is the point | 0.1 |

Two rules hold whatever the behaviour, because real banks apply them:

- A payment received after the cutoff (default 15:00 bank time) settles on the
  next business day.
- Weekends and a configurable holiday list are not business days.

## Endpoints

Two ways in, both feeding one pipeline, plus a `/_mock` control plane shaped
like mock-edi's so the two feel the same.

| Surface | Endpoint | Notes | Release |
| --- | --- | --- | --- |
| Health, state, reset | `GET /_mock/health`, `GET /_mock/state`, `POST /_mock/reset` | As in the other two mocks | now |
| Dictionary | `GET /_mock/dictionary`, `GET /_mock/dictionary/<message>` | Every message the mock reads or writes, its element tree, the code sets and the choices made, as JSON; as mock-edi serves its X12 and EDIFACT sets | now |
| Payment file in | `POST /payments` | Answers with a JSON summary: accepted, rejected, what is queued | 0.1 |
| Collect answers | `GET /_mock/mailbox` | `?leave` to peek, `?raw` for the XML | 0.1 |
| Accounts | `GET/POST /_mock/accounts`, `PATCH /_mock/accounts/<id>` | Balances, behaviour, holiday list | 0.1 |
| Clock | `POST /_mock/advance` | `?days=N` or `?to=YYYY-MM-DD`; releases statements and returns that come due | 0.1 |
| Validate only | `POST /_mock/validate` | Findings in prose, nothing changed | 0.1 |
| Folder in and out | `--drop-dir`, `--pickup-dir` | Most bank connections are still SFTP folders | 0.2 |

Anything not built yet answers `404` with a body that names what is supported
and what is planned, rather than pretending.

## Configuration

Every flag `mock-bank --help` lists:

| Flag | Default | What it does |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. The Dockerfile binds `0.0.0.0`. |
| `--port` | `8080` | Port. |
| `--db` | `:memory:` | SQLite file, or `:memory:` for a throwaway bank that forgets everything on exit. |
| `--quiet`, `-q` | off | Log nothing per request. |
| `--version` | | Print the version and exit. |

## Docker

```bash
docker build -t mock-bank .
docker run -p 8080:8080 mock-bank
```

## Layout

```
mockbank/accounts.py   the account behaviours, declared once
mockbank/schema.py     the ISO 20022 dictionary: every message, element and code set, and the walker and builder derived from it
mockbank/server.py     the HTTP surface: control plane today, the pipeline as 0.1 lands
```

`python -m mockbank` is the entry point; `tests/` drives a real server over
HTTP; `tools/` holds the checks CI runs; `examples/demo.sh` is the curl tour.
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
| 0.1 | ISO 20022 credit transfers: `pain.001` in; `pain.002`, `camt.054`, `camt.053` out; accounts, balances, cutoff, holidays, clock; every behaviour above except `return-later`; HTTP only | Every message the mock writes validates against its own dictionary; the demo tour runs in CI |
| 0.2 | Returns (`pacs.004`, `return-later`), folder transport, the `payment_run` example with its six tests | `payment_run` passes in CI against mock-sap from PyPI |
| 0.3 | US formats: NACHA files in, NACHA returns (`R01`, `R02`, `R03`), BAI2 statements out | The same `payment_run` tests pass in NACHA mode |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): what the project values, where things
live, what a good pull request looks like, and how the team works the issue
queue.
