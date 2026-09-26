"""Account behaviours: what the bank does with a payment, by name.

Each seeded account has one of these, changed at runtime with
``PATCH /_mock/accounts/<id>``.  The table is the single place the names and
their meaning are declared: the command line prints it, the README's behaviour
table is checked against it, and ``decide()`` (once it exists) dispatches on
it.  The codes named are the ISO 20022 external reason codes a real bank
uses, not inventions of the mock.
"""
from __future__ import annotations

# name -> what the bank does. Kept in the order the README lists them.
BEHAVIOURS = {
    "accept": "accepts every payment and settles it on the requested date (ACCP)",
    "closed-account": "rejects payments to one creditor account in the pain.002 (AC04)",
    "insufficient-funds": "rejects payments once the debtor's balance would go negative (AM04)",
    "bad-bank-id": "rejects a payment whose creditor bank identifier does not resolve (RC01)",
    "return-later": "accepts and settles, then returns the payment N business days later in a pacs.004 (AC04 or MD07)",
    "reject-file": "rejects the whole file at group level (RJCT, FF01)",
    "duplicate-file": "rejects a file whose MsgId it has already seen (DUPL)",
    "silent": "sends no pain.002 at all",
    "statement-gap": "leaves one settled entry off the camt.053",
}

DEFAULT_BEHAVIOUR = "accept"
