"""What both payment examples say to mock-bank, and how they read its answers.

`pay_invoices.py` and `payment_run.py` pay through the same bank and read the
same `pain.002`, `camt.054` and `camt.053`, so the HTTP call and the ISO 20022
reading live here once. Copying either example means copying this file with
it, and nothing else. Standard library only.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from typing import Dict, List
from xml.etree import ElementTree as ET

PAIN001 = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"

# A pain.002 status that means the bank has taken the payment on. Anything
# else except RJCT (PDNG, say) leaves it where it was.
ACCEPTED = {"ACCP", "ACSP", "ACSC", "ACTC", "ACWC"}


def call(base: str, method: str, path: str, body=None, content_type: str = ""):
    """One HTTP request. Returns the status and the body, error or not."""
    data = body.encode("utf-8") if isinstance(body, str) else body
    request = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    if content_type:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


DECLARATION = re.compile(r"(?=<\?xml\b)")


def tag(element) -> str:
    return element.tag.split("}", 1)[-1]


def child_text(element, *path) -> str:
    """The text at a path of local names under `element`, or ""."""
    for name in path:
        element = next((c for c in element if tag(c) == name), None)
        if element is None:
            return ""
    return (element.text or "").strip()


def bank_documents(raw: str):
    """`?raw` is a run of XML documents, each with its own declaration."""
    for chunk in DECLARATION.split(raw):
        if chunk.strip():
            yield ET.fromstring(chunk.strip().encode("utf-8"))


def entries(statement) -> List[Dict[str, str]]:
    """The booked entries of a camt.053 `Stmt` or a camt.054 `Ntfctn`."""
    found = []
    for entry in (c for c in statement if tag(c) == "Ntry"):
        refs = next((e for e in entry.iter() if tag(e) == "Refs"), None)
        found.append({
            "amount": child_text(entry, "Amt"),
            "side": child_text(entry, "CdtDbtInd"),
            "booked_on": child_text(entry, "BookgDt", "Dt"),
            "msg_id": child_text(refs, "MsgId") if refs is not None else "",
            "end_to_end_id": child_text(refs, "EndToEndId") if refs is not None else "",
            # Set on a payment coming back: the credit keeps the original's
            # MsgId and EndToEndId, which is how it finds the invoice.
            "returned_for": next((child_text(e, "Rsn", "Cd") for e in entry.iter()
                                  if tag(e) == "RtrInf"), ""),
        })
    return found
