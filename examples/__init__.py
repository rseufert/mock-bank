"""The examples that need nothing but this mock, shipped as `mockbank.examples` (#169).

`client` and `statement` are documentation that runs. They are in the wheel so
that a reader who has only `pip install mock-bank` can import and run them, and
`examples/` is mapped onto this import path by `package-dir` in
`pyproject.toml` rather than the files being moved, so a reader following a
README link still finds them where the README says they are.

**The worked integrations are not here any more.** `pay_invoices`,
`payment_run`, `procure_to_pay`, `bank_messages` and the copy of mock-sap's
`invoice_check` sat between this mock and the other two, so they moved to
mock-acme (https://github.com/rseufert/mock-acme), which holds one copy of each
and tests it against all three mocks. There they are `mockacme.payment_run` and
so on. Asking this package for one of them says where it went rather than only
that it is missing.
"""

MOVED = ("bank_messages", "invoice_check", "pay_invoices", "payment_run",
         "procure_to_pay")


def __getattr__(name):
    # `from mockbank.examples import payment_run` asks for the attribute before
    # it tries the submodule, so this is reached first and its message is the
    # one the reader sees. `import mockbank.examples.payment_run` goes straight
    # to the import system and gets the ordinary ModuleNotFoundError.
    if name in MOVED:
        raise ImportError(
            "mockbank.examples.%s moved to mock-acme, where it is mockacme.%s: "
            "https://github.com/rseufert/mock-acme" % (name, name))
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
