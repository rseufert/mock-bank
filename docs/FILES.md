# Every file in this project

A guided index of the repository. If you are looking for *how the pieces fit
together* rather than *what each file is*, read [ARCHITECTURE.md](ARCHITECTURE.md)
first.

## Top level

| File | What it is |
| --- | --- |
| `docs/ARCHITECTURE.md` | How the pieces are meant to fit together: the dictionary at the bottom, one pipeline fed by two doors, a clock instead of sleeps, and what is deliberately absent. |
| `docs/FILES.md` | This file. |
| `README.md` | The user-facing documentation: quick start, the message choreography, account behaviours, the endpoint table, configuration, the roadmap. |
| `LICENSE` | MIT, verbatim, so GitHub and `licensee` detect it. The standards-body disclaimer lives in the README instead. |
| `pyproject.toml` | Packaging metadata and the **single source of truth for the version**. Declares the `mock-bank` console script and, notably, zero dependencies. |
| `CHANGELOG.md` | Every release, what it added and what it fixed, in Keep a Changelog form. |
| `CONTRIBUTING.md` | What the project values, how the two-person team works the issue queue, where to add each kind of thing, what a good pull request carries, and the release process. |
| `MANIFEST.in` | Adds the Dockerfile, examples and tests to the sdist; without it an sdist carries only the package itself. |
| `Dockerfile` | `python:3.12-slim`, `pip install .`, entrypoint bound to `0.0.0.0:8080`. Built and exercised by CI on every push. |
| `.gitignore` | Build output, virtualenvs, `*.db` files left behind by `--db`. |

## `mockbank/` - the package

| File | What it is |
| --- | --- |
| `mockbank/__init__.py` | The package docstring, `__version__` read from `pyproject.toml` in a checkout or from the installed metadata otherwise, and the public names `Config` and `make_server`. |
| `mockbank/__main__.py` | The command line: `build_parser()` (which `tools/check_docs.py` reads to find every flag), `is_loopback()` and `exposure_warning()` (what to say when the control plane is reachable and unguarded), `check_auth()` (a credential nothing could match is refused at startup), and `main()`. |
| `mockbank/accounts.py` | `BEHAVIOURS`: every account behaviour with what the bank does, declared once - the command line's epilog, the README's behaviour table check and `decide()` all read it. Then the account itself: `FIELDS` (every field a caller may set) and a `check()` that refuses an unknown field or behaviour by naming what would have been accepted, over `create`, `update`, `listing` and the `by_iban` lookup the pipeline uses. Then the bank's judgement: `decide()` (the rule precedence is its docstring, and which side of a payment each behaviour is read from), `book()` and `book_due()` (a debit on its settlement date), and the payment queries. |
| `mockbank/clock.py` | Bank time: the cutoff, the weekend, the holiday list and the clock a test moves. `settlement_date` is the rule the whole pipeline hangs on; `business_days_after` is what the returns issue counts with; `advance()` moves the offset and calls the `on_advance` hooks the pipeline registers, so releasing what came due is that code and not this. It holds an offset rather than an instant, and knows nothing about payments. |
| `mockbank/db.py` | SQLite: `SCHEMA` and `INDEXES` as declarations, `SCHEMA_VERSION` recorded in the file, a `connect()` that upgrades an older file and refuses a newer one, the `file`, `payment`, `message`, `statement` and `counter` tables, `next_value` (numbers that never repeat), the control plane's one timestamp format, and the fixed four-account seed, its IBANs built with check digits from `schema.mod97`. Balances are integers in minor units; there is no float in it. |
| `mockbank/messages.py` | The writer half: `write_pain002`, `write_camt054` and `write_camt053` (sharing one `Ntry` builder), mappings handed to `schema.serialize` so every element comes out in declared order. The reader half: `read_pain001`/`from_tree` turn a `pain.001.001.09` or `.001.03` into a `PaymentFile` of `Batch`es of `Payment`s (amounts in minor units, the requested date evened out between versions), each keeping its `schema.Node` so a finding can name its path; `to_json` is the reading `/_mock/validate` returns. |
| `mockbank/outbox.py` | What the bank sends and when: `queue_status` puts the `pain.002` on the `message` table due `--status-delay-ms` after receipt, `release_due` books what has come due, writes one `camt.054` per account per booking and releases what is due (the clock's hook), `upcoming` says what a file will still bring, `issue_statements` writes a `camt.053` per open account for each business day the clock passed the end of (balances computed so they chain, `statement-gap` applied), `statements` lists them, and `collect` is the mailbox. |
| `mockbank/schema.py` | The ISO 20022 dictionary: `pain.001.001.09` and `.001.03`, `pain.002.001.10`, `camt.054.001.08` and `camt.053.001.08` declared as element trees, with the shared shapes (address, account, agent, party) declared once; the code sets; `walk`/`check` (structural findings), `read` (tree to mapping) and `build`/`serialize` (mapping to tree in declared order); the ISO 13616 IBAN check (`iban_is_valid`, `mod97`) that the seed, the accounts endpoint and the validator share; the choices the standard leaves open, in `CHOICES`. |
| `mockbank/server.py` | The HTTP surface: `Config`, the `State` holding the connection and the lock over it, the router, the control plane, `/_mock/dictionary`, the accounts endpoints, `POST /_mock/validate`, `POST /payments` and `/_mock/payments`, the index page built from what this build answers, and `make_server()`. Every answer leaves through one place, so every request is logged before its answer goes out. Unbuilt surfaces answer 404 naming what is supported and what is planned. |
| `mockbank/validate.py` | `inspect`/`validate`: file-level refusals (empty, signed or encrypted, DTD, not XML, a message the mock does not read), the walker's structural findings, and the semantic checks (`AM18`, `AM10`, `AC01`, `AM03`, `AM05`, `DT01`), with the table of codes in the docstring; `render` makes a finding one line of prose. Never raises. |

## `tests/` - end-to-end, over HTTP

| File | What it is |
| --- | --- |
| `tests/support.py` | `MockServerCase`: starts a real server on an ephemeral port per test class and offers `get`/`post`/`patch`/`put`/`request` helpers returning a `Response` with `.json()`. `FileDatabaseCase` is the same on a `--db` file that a test can restart, for the questions `:memory:` cannot answer. Arms a faulthandler watchdog when `MOCKBANK_TEST_WATCHDOG` is set. |
| `tests/test_auth.py` | `--auth`: no credentials is a `401` with `WWW-Authenticate` and the right ones a `200`, a wrong or partly-right one is a `401`, every endpoint including `/_mock/health` is behind it, a malformed `Authorization` header is a `401` rather than a `500`, and an unauthenticated payment file is never read. The exposure warning is checked on the function and again on a real subprocess, where `-q` must not silence it. |
| `tests/test_clock.py` | Bank time: a payment at 16:00 on a Friday settles on Monday and on Tuesday when Monday is a holiday, 15:00 exactly is already too late, three business days after a Thursday is the next Tuesday, `?days=3` from a Thursday lands on Sunday and says it counted calendar days, the clock never goes backwards, and a reset returns to where `--clock` put it rather than to real time. Every date asserts the weekday it falls on. |
| `tests/test_dictionary.py` | The dictionary: `/_mock/dictionary` serves every declaration, and the builder writes in declared order, refuses a mapping that does not fit and round-trips. |
| `tests/test_accounts.py` | The accounts surface: the seed is the same four every time and every seeded IBAN passes a check recomputed the long way, a patched behaviour is what the next `GET` says, an unknown behaviour and a balance that is not minor units are refused by name, two accounts cannot share an IBAN, and `POST /_mock/reset` brings the seed back. |
| `tests/test_server.py` | The control plane: health names the version and the accounts it holds, state counts requests and agrees with what the accounts endpoint lists, reset starts again, the index is HTML, and an unbuilt surface refuses by name. |
| `tests/test_messages.py` | What the bank sends back, through the mailbox: the sample's `pain.002` reports every payment in the right order and namespace, a file rejected outright has group status only, `silent` gets none, `--status-delay-ms` holds it, and one `camt.054` per account and booking carries each `EndToEndId` and amounts that match the balance. On a pinned clock: advancing to the settlement date books and releases the debits by itself, twice releases nothing twice, a holiday and the cutoff move the date, and advancing releases a delayed `pain.002`. Every message collected is walked against the dictionary. |
| `tests/test_statements.py` | The `camt.053`: the README file's statement closes at its opening less its entries, both worked out independently; three business days chain through an empty one; every open account gets one and a closed one none; nothing is issued twice or renumbered across a restart; `statement-gap` leaves one entry off by exactly its amount. |
| `tests/test_payments.py` | `POST /payments`: the sample against the seed, the README's example, a duplicate, and one test per line of `decide()`'s precedence, each written to fail if its line is removed; booking on the settlement date, the lookups, and reset. |
| `tests/test_upgrade.py` | A `--db` file from an earlier schema: it opens, the balance it held is still there, the columns it lacked take the defaults the schema declares, and the seed does not overwrite it. A file from a newer mock is refused before the port is taken. The fingerprint test fails on purpose when `SCHEMA` changes without a version bump. A version-1 file gains the payment tables and keeps its accounts; a version-2 file gains the message table and keeps its payments; a version-3 file gains the statement tables and keeps its messages. |
| `tests/test_validate.py` | `POST /_mock/validate`: the clean sample and its `.001.03` twin read into the same four payments, prefixed namespaces and Latin-1 read the same, each broken sample produces exactly the finding its name promises, and random bytes, an empty body and a 10 MB file get findings, never an error. |

### `tests/samples/` and `tests/fixtures/`

Samples are wire files a test sends; fixtures are things a test needs that are
not wire files.

| File | What it is |
| --- | --- |
| `tests/samples/pain001_four_payments.xml` | A clean `pain.001.001.09`: one batch of four euro payments from ACME, with structured and unstructured remittance and non-ASCII names. Fake but check-digit-valid IBANs. |
| `tests/samples/pain001_four_payments_001_03.xml` | The same file as `pain.001.001.03`. |
| `tests/samples/pain001_broken_ctrlsum.xml`, `tests/samples/pain001_broken_nboftxs.xml`, `tests/samples/pain001_broken_iban.xml`, `tests/samples/pain001_broken_currency.xml`, `tests/samples/pain001_broken_past_date.xml`, `tests/samples/pain001_broken_duplicate_end_to_end_id.xml`, `tests/samples/pain001_broken_empty_batch.xml`, `tests/samples/pain001_broken_order.xml`, `tests/samples/pain001_broken_namespace.xml`, `tests/samples/pain001_broken_signed.xml`, `tests/samples/pain001_broken_unknown_message.xml`, `tests/samples/pain001_broken_not_xml.xml` | The clean file broken in exactly one way each, as its name and its opening comment say; `tests/test_validate.py` holds the one finding each must produce. `not_xml` is a CSV, which is the point. |
| `tests/fixtures/schema-v3.sql` | The schema as `SCHEMA_VERSION` 3 wrote it, before the `statement` and `counter` tables. |
| `tests/fixtures/schema-v2.sql` | The schema as `SCHEMA_VERSION` 2 wrote it, before the `message` table. |
| `tests/fixtures/schema-v1.sql` | The schema as `SCHEMA_VERSION` 1 wrote it, before the `file` and `payment` tables. |
| `tests/fixtures/schema-v0.sql` | The schema as a mock-bank before `SCHEMA_VERSION` 1 would have written it, so the upgrade path is tested from the first version rather than from the first time somebody's file breaks. No release shipped it. |

## `examples/`

| File | What it is |
| --- | --- |
| `examples/demo.sh` | The curl tour, run by CI's smoke job. Today it covers the control plane; each release adds its part. |

## `tools/` - what CI checks

| File | What it is |
| --- | --- |
| `tools/check_docs.py` | Fails if a tracked file has no row here, if a row names a missing file, if the README's layout block misses a module, if a CLI flag is unmentioned in the README, or if a behaviour has no row in the README's table. |
| `tools/check_changelog.py` | Fails if the changelog is malformed, if a released section changed, if an unreleased entry vanished, or if a pull request touches `mockbank/` without adding an entry (lifted by the `no changelog` label). |

## `.github/workflows/`

| File | What it is |
| --- | --- |
| `.github/workflows/ci.yml` | Tests on eight Python and OS combinations, the docs and changelog checks, the curl tour as a smoke test, a wheel build and install, and the container image. |
| `.github/workflows/publish.yml` | Trusted Publishing to PyPI on a GitHub Release, or to TestPyPI on a manual run, after checking the tag, `pyproject.toml` and the built wheel agree. |
