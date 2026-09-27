# Contributing

Thanks for looking. mock-bank is a mock bank: it receives ISO 20022 payment
files and answers with the status reports, statements and returns a real bank
sends, so that payment and reconciliation integrations can be built and tested
without a bank sandbox. Everything below is about keeping it useful for that.

## What the project values

These are not style preferences; they decide what gets merged. They are the
same five rules that make mock-sap and mock-edi useful, and there are no new
ones.

**Fidelity over convenience.** If a real bank behaves a certain way, the mock
behaves that way, even when the real behaviour is inconvenient. A payment
received after the cutoff settles tomorrow. A statement's closing balance is
the opening balance plus its entries, to the cent. A reason code is one from
the ISO 20022 external code list, not a string the mock made up. A mock that
accepts what a real bank rejects teaches a client a lie it will discover in
production.

**Say when you are guessing.** Where banks genuinely differ, the mock picks one
profile and says so out loud rather than implying authority. Which `camt.054`
a bank sends per payment versus per batch is the clearest case. Inventing an
element or a code that the standard does not have costs more than leaving a
gap.

**No dependencies.** The Python standard library and SQLite, nothing else. ISO
20022 is read and written through `xml.etree`. A mock you cannot install in a
locked-down CI image is a mock nobody runs. This is not negotiable, and it is
why EBICS and SWIFT transport are refused rather than half-built.

**Declare, do not hand-write.** Message shapes, element trees and code lists
are declarations in `mockbank/schema.py`; parsing, validation, generation and
the published dictionary are derived from them. If you find yourself writing
the same shape in two places, the declaration is missing.

**Refuse rather than half-implement.** An encrypted file, an unknown message
type and a payment from an unregistered debtor account all produce an answer
that names what *is* supported. Silently ignoring something is the one thing
worse than not having it.

**The wire is the product.** Behaviour a client cannot observe does not need to
exist; behaviour it can observe needs to be right. `EndToEndId` survives every
message because a client matches on it. No ledger beyond balances is
simulated, because no client can tell.

## Getting set up

Nothing to install:

```bash
git clone https://github.com/rseufert/mock-bank
cd mock-bank
python3 -m mockbank --port 8080        # it is already runnable
python3 -m unittest discover -s tests -v
python3 tools/check_docs.py
python3 tools/check_changelog.py
python3 tools/check_xsd.py              # fetches the published XSDs; skips without a network
```

Python 3.8 or newer. There is no build step, no virtualenv to create and
nothing to compile.

## How the team works the queue

Two people work the issues, and the maintainer merges. The rules that keep
that from tangling:

- **Your queue is your label.** Issues labelled `senior` are the senior
  developer's; issues labelled `junior` are the junior developer's. Work them
  by priority (`P1` before `P2` before `P3`) and then by number, inside the
  earliest open milestone. If your queue is empty, ask before pulling from
  the other one; an issue is scoped for the person it is labelled for.
- **One branch per issue**, named `<number>-<short-slug>` from `main`, and one
  pull request per issue. The pull request title is the issue title and the
  description starts with `Closes #<number>`. Do not bundle two issues into one
  pull request, even if they touch the same file.
- **Comment on the issue before you start** with a sentence on the approach,
  and again if the approach changes. An issue with an open pull request is
  yours; an issue with no comment is up for grabs by whoever it is labelled for.
- **Say when you are blocked.** If an issue depends on another that is not
  merged yet, say so on the issue and take the next one; do not build on an
  unmerged branch.
- **Keeping up with `main`: rebase before the pull request exists, merge after.**
  While a branch is only yours, rebase it onto `main` as often as you like. Once
  a pull request is open and a review has started, bring `main` in with a merge
  commit instead: rebasing a pushed branch means a force-push, which rewrites
  the commits a reviewer has already read and can lose their comments' place.
  The maintainer squashes on merge, so the merge commits do not survive into
  `main` either way. This came up on every branch in 0.1, because `main` moved
  under each of them more than once.
- **Do not merge.** Pull requests are merged by the maintainer, after CI is
  green and a review. Never force-push over a review; push follow-up commits
  instead. Do not tag releases or edit `pyproject.toml`'s version; that is part
  of the release.
- **The junior developer's pull requests get the senior developer's review
  first**, as a comment on the pull request, before the maintainer looks. The
  senior developer's pull requests go straight to the maintainer.
- **Ask on the issue, not in private.** Questions and decisions live on the
  issue so the next person can read them.

## Where things live

| Adding this | Goes here | Notes |
| --- | --- | --- |
| A message type, element or code list | `mockbank/schema.py` | Define it once and reference it; `PstlAdr`, `Amt` and `Id` shapes are shared across messages |
| A reason code | `mockbank/schema.py` | From the ISO 20022 external code sets; a list that accepts everything acknowledges everything |
| An account behaviour | `mockbank/accounts.py` `BEHAVIOURS`, then `decide` | Keep the precedence rules in the docstring true |
| A validation check | `mockbank/validate.py` | Produce a finding, not a sentence: it has to render as a `pain.002` reason and as prose for `/_mock/validate` |
| A message writer | `mockbank/messages.py` | Built from the declaration; a writer that hard-codes an element order is wrong even when it works |
| An endpoint | `mockbank/server.py` | Add it to the index page and the README table too |
| A CLI flag | `mockbank/__main__.py` and `server.Config` | And the README's Configuration table |
| A table, column or index | `mockbank/db.py` `SCHEMA` / `INDEXES` | Bump `SCHEMA_VERSION`; `tests/test_upgrade.py` fails until you do |

If a change touches more than one of these, it is usually two changes.

## What a good pull request looks like

- **A test that goes over HTTP.** Every test in `tests/` drives a real mock on
  a real socket; nothing is stubbed. Put it in the module for the surface you
  touched, or add one and give it a row in `docs/FILES.md`.
- **Assertions that could fail.** Check a closing balance against the entries
  it summarises, not against itself. Validate the mock's own output against
  its own dictionary and, where one exists, against a sample from outside the
  project.
- **Documentation that keeps up.** `tools/check_docs.py` fails the build if a
  tracked file has no row in `docs/FILES.md`, if a row names a file that is
  gone, if a module is missing from the README's layout block, if a
  command-line flag has no mention in the README's Configuration section, or
  if a behaviour has no row in the README's behaviour table. It checks
  coverage, not prose; keeping the prose true is on you.
- **A line in the changelog.** `tools/check_changelog.py` fails a pull request
  that touches `mockbank/` without adding an entry under `## [Unreleased]`. A
  change that genuinely needs no entry can carry the `no changelog` label; say
  in the description why, and the maintainer will apply it. Applying or removing
  the label re-runs the checks on its own — no push and no reopen — because the
  workflow listens for `labeled` and `unlabeled`.
- **No new dependencies.** See above.
- **A commit message that says what changed and why.** The why is the part a
  reader cannot reconstruct. Wrap at 72 characters.

Small, focused pull requests are easier to take than large ones.

## Reporting a missing or wrong shape

The most useful bug report contains the file a real bank sent or accepted,
with anything sensitive removed, beside what the mock produced. Element paths,
namespaces, the message version and the reason code all matter.

`POST /_mock/validate` is often the fastest way to show one: it returns
the mock's reading of a file as prose, without changing anything.

## Releasing (maintainers)

`pyproject.toml` is the only place the version is written;
`mockbank.__version__` reads it back from the installed package metadata.

```bash
# bump `version` in pyproject.toml, commit, then:
git tag v0.1.0 && git push origin v0.1.0
gh release create v0.1.0 --generate-notes     # or write the notes by hand
```

Publishing the GitHub Release runs the tests, builds the distributions, checks
that the tag, `pyproject.toml` and the built wheel agree, and uploads to PyPI
through [Trusted Publishing](https://docs.pypi.org/trusted-publishers/). Running
the `Publish` workflow by hand publishes to TestPyPI instead. Add the release
to [`CHANGELOG.md`](CHANGELOG.md) in the same commit as the version bump.

Verify a release with a pinned version and a fresh index:

```bash
pip install --no-cache-dir "mock-bank==0.1.0"
```

## Licence

By contributing you agree that your work is licensed under the
[MIT Licence](LICENSE), the same terms as the rest of the project.
