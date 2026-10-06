#!/usr/bin/env python3
"""A client for mock-bank, of the shape an integrator would write.

The same choreography as `demo.sh`, in Python, doing the thing a real client has
to do and a curl tour cannot show: **matching every answer back to the payment
it is about, by `EndToEndId`**. That is the whole reason `EndToEndId` survives
every message the bank sends, and it is the part people get wrong - a status
report matched on position, or a statement line matched on amount, works until
two payments have the same amount.

    python3 examples/client.py
    python3 examples/client.py --base http://host:9000
    python3 examples/client.py --auth user:password     # a mock started --auth

Standard library only, so it can be copied into a test suite that has no
dependencies of its own. Written to be read: no framework, no cleverness, and
the XML handled with `xml.etree` the way the mock itself does it.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from decimal import Decimal
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, os.pardir, "tests", "samples",
                      "pain001_four_payments.xml")

# `?raw` returns a sequence of XML documents rather than one document, so they
# are split on the declaration before parsing. The mock says so in its README;
# this is what acting on it looks like.
DECLARATION = re.compile(r"(?=<\?xml\b)")


class Bank:
    """Everything this client does over HTTP, in one place."""

    def __init__(self, base: str, auth: str = ""):
        self.base = base.rstrip("/")
        self.auth = auth
        # Set when the mock could not be reached at all, as opposed to
        # answering something unwelcome.
        self.unreachable = ""

    def request(self, method: str, path: str, body=None, content_type=""):
        data = body.encode("utf-8") if isinstance(body, str) else body
        request = urllib.request.Request(self.base + path, data=data,
                                        method=method)
        if content_type:
            request.add_header("Content-Type", content_type)
        if self.auth:
            token = base64.b64encode(self.auth.encode("utf-8")).decode("ascii")
            request.add_header("Authorization", "Basic " + token)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            # A 422 is an answer, not a failure: it is how the bank says it
            # rejected the file, and the body is the report. A client that
            # treated every non-2xx as an outage would throw it away.
            with error:
                return error.code, error.read()
        except (urllib.error.URLError, OSError) as error:
            # Nothing is listening, or the network is gone. The likely cause is
            # that nobody started the mock, and a traceback is the wrong way to
            # say so to somebody meeting this file for the first time.
            self.unreachable = str(error)
            return 0, b""

    def json(self, method: str, path: str, body=None):
        status, raw = self.request(method, path, body,
                                  "application/json" if body else "")
        return status, json.loads(raw.decode("utf-8")) if raw.strip() else None

    def send(self, payment_file: str):
        return self.json("POST", "/payments", payment_file)

    def messages(self, kind: str = "", leave: bool = False):
        """Collect the mailbox, optionally of one type, optionally peeking."""
        query = []
        if kind:
            query.append("type=" + kind)
        if leave:
            query.append("leave")
        path = "/_mock/mailbox" + ("?" + "&".join(query) if query else "")
        _status, body = self.json("GET", path)
        return body or []

    def advance(self, **where):
        key, value = next(iter(where.items()))
        _status, body = self.json("POST", "/_mock/advance?%s=%s" % (key, value))
        return body


def tag(element) -> str:
    """An element's name without its namespace.

    The namespace matters when writing a message and is noise when reading one,
    so it is dropped here rather than repeated at every lookup. A stricter
    client would match on the full name; the mock's own dictionary does.
    """
    return element.tag.split("}", 1)[-1]


def documents(text: str):
    for chunk in DECLARATION.split(text):
        if chunk.strip():
            yield ET.fromstring(chunk.strip())


def statuses_by_end_to_end_id(xml: str):
    """Every payment status in a `pain.002`, keyed on the id the file gave it.

    This is the match that matters. The bank echoes `OrgnlEndToEndId`, so a
    client never has to rely on the order the statuses come back in - and the
    order is not guaranteed to be the order they were sent.
    """
    found = {}
    for document in documents(xml):
        for info in document.iter():
            if tag(info) != "TxInfAndSts":
                continue
            reference = code = status = ""
            for child in info.iter():
                name = tag(child)
                if name == "OrgnlEndToEndId":
                    reference = (child.text or "").strip()
                elif name == "TxSts":
                    status = (child.text or "").strip()
                elif name == "Cd" and not code:
                    code = (child.text or "").strip()
            if reference:
                found[reference] = (status, code)
    return found


def entries_by_end_to_end_id(xml: str):
    """Every statement or notification entry, keyed on `EndToEndId`.

    The same match again, on the other side: this is how a reconciliation knows
    which invoice a line on the statement paid.
    """
    found = {}
    for document in documents(xml):
        for entry in document.iter():
            if tag(entry) != "Ntry":
                continue
            amount = Decimal("0")
            side = reference = ""
            for child in entry.iter():
                name = tag(child)
                if name == "Amt" and not amount:
                    amount = Decimal((child.text or "0").strip())
                elif name == "CdtDbtInd" and not side:
                    side = (child.text or "").strip()
                elif name == "EndToEndId":
                    reference = (child.text or "").strip()
            if reference:
                found[reference] = (-amount if side == "DBIT" else amount, side)
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Send a payment file to mock-bank and match every answer "
                    "back to the payment it is about.")
    parser.add_argument("--base", default=os.environ.get(
        "BASE", "http://127.0.0.1:8090"), help="where the mock is listening")
    parser.add_argument("--auth", default=os.environ.get("BANK_AUTH", ""),
                        metavar="USER:PASSWORD",
                        help="credentials, for a mock started with --auth")
    parser.add_argument("--file", default=SAMPLE,
                        help="the pain.001 to send (default: the sample)")
    args = parser.parse_args(argv)

    bank = Bank(args.base, args.auth)

    status, health = bank.json("GET", "/_mock/health")
    if status == 401:
        print("The mock wants credentials: pass --auth USER:PASSWORD.",
              file=sys.stderr)
        return 2
    if status == 0:
        print("Nothing is listening at %s (%s).\nStart one:  python3 -m mockbank "
              "--port 8090" % (args.base, bank.unreachable), file=sys.stderr)
        return 2
    if status != 200:
        print("The mock at %s answered %s for /_mock/health.\nStart one:  "
              "python3 -m mockbank --port 8090" % (args.base, status),
              file=sys.stderr)
        return 2
    print("mock-bank %s, holding %d accounts"
          % (health["version"], health["accounts"]))

    with open(args.file, encoding="utf-8") as handle:
        payment_file = handle.read()

    status, answer = bank.send(payment_file)
    print("\nSent %s: %s" % (os.path.basename(args.file), answer["status"]))

    # What the bank said per payment, as JSON. The pain.002 says the same thing
    # on the wire; both are matched below so they can be compared.
    decided = {p["end_to_end_id"]: p for p in answer["payments"]}

    reported = {}
    for message in bank.messages(kind="pain.002", leave=True):
        reported.update(statuses_by_end_to_end_id(message["body"]))

    settlement_dates = sorted({p["settlement_date"] for p in answer["payments"]
                               if p["settlement_date"]})
    notified: dict = {}
    if settlement_dates:
        # Nothing waits: move bank time to the settlement date, then one day
        # further so that day ends and its statement closes.
        bank.advance(to=settlement_dates[0])
        for message in bank.messages(kind="camt.054", leave=True):
            notified.update(entries_by_end_to_end_id(message["body"]))
        bank.advance(days=1)

    statement_lines: dict = {}
    for message in bank.messages(kind="camt.053", leave=True):
        statement_lines.update(entries_by_end_to_end_id(message["body"]))

    print("\n%-16s %-9s %-6s %-8s %-12s %-10s %s"
          % ("EndToEndId", "outcome", "code", "pain.002", "settles",
             "camt.054", "on the statement"))
    for reference in sorted(decided):
        payment = decided[reference]
        wire_status, wire_code = reported.get(reference, ("-", "-"))
        booked, _side = notified.get(reference, ("-", ""))
        line, _side = statement_lines.get(reference, ("-", ""))
        print("%-16s %-9s %-6s %-8s %-12s %-10s %s"
              % (reference, payment["outcome"], payment["reason"] or "-",
                 wire_status or "-", payment["settlement_date"] or "-",
                 booked, line))

    # The two sources have to agree, and this is worth a client's while to
    # check rather than trust: the JSON summary is this mock's convenience,
    # while the `pain.002` is what a real bank actually sends. An integration
    # built against the summary alone would not notice the day they parted.
    disagreements = []
    for reference in sorted(decided):
        payment = decided[reference]
        wire_status, wire_code = reported.get(reference, ("", ""))
        if not wire_status:
            continue
        expected = "RJCT" if payment["outcome"] == "rejected" else "ACCP"
        if wire_status != expected:
            disagreements.append("%s: the summary says %s, the pain.002 says %s"
                                 % (reference, expected, wire_status))
        if (payment["reason"] or "") != (wire_code or ""):
            disagreements.append("%s: the summary says %r, the pain.002 says %r"
                                 % (reference, payment["reason"] or "",
                                    wire_code or ""))
    if disagreements:
        print("\nThe JSON summary and the pain.002 do not agree:", file=sys.stderr)
        for line in disagreements:
            print("  " + line, file=sys.stderr)
        return 1

    print("\nEvery answer above was matched to its payment by EndToEndId, "
          "never by position.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
