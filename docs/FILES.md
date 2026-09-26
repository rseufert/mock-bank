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
| `mockbank/accounts.py` | `BEHAVIOURS`: every account behaviour with what the bank does, declared once. The command line's epilog, the README's behaviour table check and, later, `decide()` all read it. |
| `mockbank/server.py` | The HTTP surface: `Config`, the in-process `State`, the request handler with the control plane, the index page, and `make_server()`. Unbuilt surfaces answer 404 naming what is supported and what is planned. |

## `tests/` - end-to-end, over HTTP

| File | What it is |
| --- | --- |
| `tests/support.py` | `MockServerCase`: starts a real server on an ephemeral port per test class and offers `get`/`post`/`request` helpers returning a `Response` with `.json()`. Arms a faulthandler watchdog when `MOCKBANK_TEST_WATCHDOG` is set. |
| `tests/test_server.py` | The control plane: health names the version, state counts requests and lists behaviours, reset starts again, the index is HTML, and an unbuilt surface refuses by name. |

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
