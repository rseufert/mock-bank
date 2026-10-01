"""The worked examples, shipped as `mockbank.examples` (#169).

These files are documentation that runs. They are in the wheel so that a reader
who has only `pip install mock-bank` can import and run them, rather than having
to clone the repository: mock-films plays this choreography across three mocks
and had no way to get at it (rseufert/mock-films#9).

The import path is `mockbank.examples`, and `examples/` is mapped onto it by
`package-dir` in `pyproject.toml` rather than the files being moved, so a reader
following a README link still finds them where the README says they are.

Nothing here imports mock-sap or mock-edi. The examples talk to them over HTTP,
so importing this package needs `mock-bank` alone - which is the point of
shipping it.

They are examples, not a supported client library: `payment_run` can still pay an
invoice twice in the ways #164 lists. Read them, copy them, do not put them in
front of real money.
"""
