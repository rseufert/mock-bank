# Changelog

Every release of [mock-bank](https://pypi.org/project/mock-bank/). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html) -
while the major version is 0, a minor bump may change behaviour, and each entry
says so where it does.

## [Unreleased]

### Added

- **The ISO 20022 dictionary** (#2). `mockbank/schema.py` declares
  `pain.001.001.09` (and `.001.03`, read into the same model),
  `pain.002.001.10`, `camt.054.001.08` and `camt.053.001.08` as element trees
  with their types, occurrences and code sets, the recurring shapes declared
  once. One walker reports an element out of order, in the wrong namespace,
  missing, repeated or with a bad value as a finding naming its path; one
  builder writes any of them in declared order. `GET /_mock/dictionary` and
  `GET /_mock/dictionary/<message>` serve it all as JSON, with the choices the
  standard leaves open - the versions written, `PMNT`/`ICDT`/`ESCT` as the bank
  transaction code - stated.
- **The repository, and a mock that answers.** `python -m mockbank` starts
  a server with `/_mock/health`, `/_mock/state` and `POST /_mock/reset`; every
  surface the plan promises and this skeleton lacks answers `404` with a body
  naming what is supported and what is planned. The account behaviours are
  declared in `mockbank/accounts.py` so the command line, the README and the
  docs check agree on their names from the first commit. The release
  machinery, the documentation and changelog checks and the test harness are
  carried over from mock-edi.

[Unreleased]: https://github.com/rseufert/mock-bank/commits/main
