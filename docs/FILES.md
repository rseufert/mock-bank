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
| `mockbank/__main__.py` | The command line: `build_parser()` (which `tools/check_docs.py` reads to find every flag) and `main()`. |
| `mockbank/accounts.py` | `BEHAVIOURS`: every account behaviour with what the bank does, declared once - the command line's epilog, the README's behaviour table check and, later, `decide()` all read it. Then the account itself: `FIELDS` (every field a caller may set) and a `check()` that refuses an unknown field or behaviour by naming what would have been accepted, over `create`, `update`, `listing` and the `by_iban` lookup the pipeline uses. |
| `mockbank/db.py` | SQLite: `SCHEMA` and `INDEXES` as declarations, `SCHEMA_VERSION` recorded in the file, a `connect()` that upgrades an older file and refuses a newer one, the control plane's one timestamp format, the IBAN check digits, and the fixed four-account seed. Balances are integers in minor units; there is no float in it. |
| `mockbank/schema.py` | The ISO 20022 dictionary: `pain.001.001.09` and `.001.03`, `pain.002.001.10`, `camt.054.001.08` and `camt.053.001.08` declared as element trees, with the shared shapes (address, account, agent, party) declared once; the code sets; `walk`/`check` (structural findings), `read` (tree to mapping) and `build`/`serialize` (mapping to tree in declared order); the choices the standard leaves open, in `CHOICES`. |
| `mockbank/server.py` | The HTTP surface: `Config`, the `State` holding the connection and the lock over it, the router, the control plane, `/_mock/dictionary` and the accounts endpoints, the index page built from what this build answers, and `make_server()`. Every answer leaves through one place, so every request is logged before its answer goes out. Unbuilt surfaces answer 404 naming what is supported and what is planned. |

## `tests/` - end-to-end, over HTTP

| File | What it is |
| --- | --- |
| `tests/support.py` | `MockServerCase`: starts a real server on an ephemeral port per test class and offers `get`/`post`/`patch`/`put`/`request` helpers returning a `Response` with `.json()`. `FileDatabaseCase` is the same on a `--db` file that a test can restart, for the questions `:memory:` cannot answer. Arms a faulthandler watchdog when `MOCKBANK_TEST_WATCHDOG` is set. |
| `tests/test_dictionary.py` | The dictionary: `/_mock/dictionary` serves every declaration, the sample walks clean in either namespace style, an element out of order, in the wrong namespace or missing is a finding naming its path, `.001.03` reads into the same mapping, and the builder writes in declared order and round-trips. |
| `tests/test_accounts.py` | The accounts surface: the seed is the same four every time and every seeded IBAN passes a check recomputed the long way, a patched behaviour is what the next `GET` says, an unknown behaviour and a balance that is not minor units are refused by name, two accounts cannot share an IBAN, and `POST /_mock/reset` brings the seed back. |
| `tests/test_server.py` | The control plane: health names the version and the accounts it holds, state counts requests and agrees with what the accounts endpoint lists, reset starts again, the index is HTML, and an unbuilt surface refuses by name. |
| `tests/test_upgrade.py` | A `--db` file from an earlier schema: it opens, the balance it held is still there, the columns it lacked take the defaults the schema declares, and the seed does not overwrite it. A file from a newer mock is refused before the port is taken. The fingerprint test fails on purpose when `SCHEMA` changes without a version bump. |

### `tests/samples/` and `tests/fixtures/`

Samples are wire files a test sends; fixtures are things a test needs that are
not wire files.

| File | What it is |
| --- | --- |
| `tests/samples/pain001_four_payments.xml` | A clean `pain.001.001.09`: one batch of four euro payments from ACME, with structured and unstructured remittance and non-ASCII names. Fake but check-digit-valid IBANs. |
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
