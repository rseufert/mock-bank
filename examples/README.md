# Examples

Three things that need nothing but this mock:

- `demo.sh`, the whole choreography in curl.
- `client.py`, the same thing as a client you would copy.
- `statement.py`, which reads `camt.053` documents and checks that they add up.

`client` and `statement` are also importable from an install, as
`mockbank.examples`.

## The worked integrations moved to mock-acme

`pay_invoices.py`, `payment_run.py` and `procure_to_pay.py` lived here, with
their tests, the `bank_messages.py` they shared and a copy of mock-sap's
`invoice_check.py`. They are code that sits *between* this mock and the others,
so they now live in one place, with one copy of each and tests that run against
all three mocks: [mock-acme](https://github.com/rseufert/mock-acme).

| Was here | Is now |
| --- | --- |
| `examples/pay_invoices.py` | [`mockacme/pay_invoices.py`](https://github.com/rseufert/mock-acme/blob/main/mockacme/pay_invoices.py) |
| `examples/payment_run.py` | [`mockacme/payment_run.py`](https://github.com/rseufert/mock-acme/blob/main/mockacme/payment_run.py) |
| `examples/procure_to_pay.py` | [`mockacme/procure_to_pay.py`](https://github.com/rseufert/mock-acme/blob/main/mockacme/procure_to_pay.py) |
| `examples/bank_messages.py` | [`mockacme/bank_messages.py`](https://github.com/rseufert/mock-acme/blob/main/mockacme/bank_messages.py) |
| `examples/invoice_check.py` | [`mockacme/invoice_check.py`](https://github.com/rseufert/mock-acme/blob/main/mockacme/invoice_check.py) |
| `examples/test_pay_invoices.py` | [`tests/test_pay_invoices.py`](https://github.com/rseufert/mock-acme/blob/main/tests/test_pay_invoices.py) |
| `examples/test_payment_run.py` | [`tests/test_payment_run.py`](https://github.com/rseufert/mock-acme/blob/main/tests/test_payment_run.py) |
| `examples/test_procure_to_pay.py` | [`tests/test_procure_to_pay.py`](https://github.com/rseufert/mock-acme/blob/main/tests/test_procure_to_pay.py) |
| `tests/test_payment_run_readers.py` | [`tests/test_payment_run_readers.py`](https://github.com/rseufert/mock-acme/blob/main/tests/test_payment_run_readers.py) |

`from mockbank.examples import payment_run` worked from mock-bank 0.6.0 to
0.7.0. It now raises an `ImportError` that names mock-acme and how to install
it: `pip install mock-acme`. Its wheel holds the package alone, so mock-acme's
own tests run from a clone of its repository.

The last versions kept here are at
[`ded9818`](https://github.com/rseufert/mock-bank/tree/ded9818/examples).
