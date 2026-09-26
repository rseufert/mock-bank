#!/usr/bin/env python3
"""Read camt.053 statements on stdin and show that each one reconciles.

`examples/demo.sh` pipes `GET /_mock/mailbox?type=camt.053&raw` into this. The
point of the step is the one claim about a statement that a reconciliation
integration lives or dies by: the closing balance is the opening balance plus
the entries, to the cent. So this does the arithmetic rather than printing the
two balances and leaving a reader to trust them - and says so when they do not
add up, which is exactly what the `statement-gap` behaviour produces.

Standard library only, like everything else here.
"""
import re
import sys
from decimal import Decimal
from xml.etree import ElementTree as ET

# `?raw` hands back a sequence of documents, not one document, so they are split
# before parsing. See outbox.RAW_SEPARATOR in the package for why.
DECLARATION = re.compile(r"(?=<\?xml\b)")


def documents(text):
    for chunk in DECLARATION.split(text):
        chunk = chunk.strip()
        if chunk:
            yield ET.fromstring(chunk)


def tag(element):
    return element.tag.split("}", 1)[-1]


def find(parent, name):
    for child in parent.iter():
        if tag(child) == name:
            return child
    return None


def text_of(parent, name, default=""):
    found = find(parent, name)
    return found.text if found is not None and found.text else default


def signed(entry):
    """An entry's amount, negative when the statement says it is a debit."""
    amount = Decimal(text_of(entry, "Amt", "0"))
    return -amount if text_of(entry, "CdtDbtInd") == "DBIT" else amount


def balances(statement):
    """The opening and closing balances, by their ISO 20022 codes."""
    found = {}
    for balance in statement.iter():
        if tag(balance) != "Bal":
            continue
        code = text_of(balance, "Cd")
        if code in ("OPBD", "CLBD"):
            # The balance amount as stated. A credit balance and a debit entry
            # both carry a positive Amt with a CdtDbtInd beside it; `signed`
            # applies that to the entries, which is where the sign matters.
            found[code] = Decimal(text_of(balance, "Amt", "0"))
    return found.get("OPBD"), found.get("CLBD")


def main():
    text = sys.stdin.read()
    if not text.strip():
        print("  (no statements)")
        return 0

    read = []
    for document in documents(text):
        statement = find(document, "Stmt")
        if statement is None:
            continue
        account = text_of(statement, "IBAN") or text_of(statement, "Id", "?")
        opening, closing = balances(statement)
        entries = [entry for entry in statement.iter() if tag(entry) == "Ntry"]
        if opening is None or closing is None:
            print("  %s: no OPBD/CLBD pair" % account)
            continue
        read.append((account, opening, closing, entries))

    # The ones with entries first, and then a couple of the empty ones. A jump
    # of several business days closes a statement per account per day, so the
    # day that actually moved money would otherwise be buried under a week of
    # "0 entries" - and it is the only one that demonstrates anything. The
    # empty ones are still worth showing: a bank sends them, and a client that
    # falls over on one has a bug.
    with_entries = [row for row in read if row[3]]
    without = [row for row in read if not row[3]]
    for account, opening, closing, entries in with_entries + without[:2]:
        moved = sum((signed(entry) for entry in entries), Decimal("0"))
        print("  %-22s OPBD %10s  %2d entr%s %10s  CLBD %10s  %s"
              % (account, opening, len(entries),
                 "y " if len(entries) == 1 else "ies", moved, closing,
                 "reconciles" if opening + moved == closing
                 else "DOES NOT RECONCILE"))
    if not read:
        print("  (no statements with balances)")
    elif len(without) > 2:
        print("  ... and %d more with no entries, one per account per business day"
              % (len(without) - 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
