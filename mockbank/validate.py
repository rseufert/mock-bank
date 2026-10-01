"""Judging a payment file: findings, not exceptions.

``inspect(data)`` reads a body the way a bank's front end does and returns the
``PaymentFile`` (or None when there is nothing to read) with every
``Finding`` about it; ``validate(data)`` is the findings alone. A finding names
the element path and carries the ISO 20022 status reason it is reported with,
so the same finding becomes a ``pain.002`` reason and a line of prose from
``POST /_mock/validate`` (``render``).

Three layers, each only reached when the one before lets the file through:

1. **File-level refusals**, one finding each: an empty body, a signed or
   encrypted file (``application/pkcs7-mime``, PGP armour, an XML
   ``Signature`` or ``EncryptedData``), a DTD, something that is not XML, and
   a message the mock does not read - each naming what *is* supported.
2. **Structure**, from ``schema.walk``: order, namespace, occurrence, types.
3. **Meaning**, the mock's own checks, each with its reason code:

   ======================================  ======  =======================
   check                                   code    level
   ======================================  ======  =======================
   group or batch ``NbOfTxs`` is wrong     AM18    error
   group or batch ``CtrlSum`` is wrong     AM10    error
   an IBAN fails its check digits          AC01    error
   amount currency differs from the        AM03    error
   debtor account's ``Ccy``
   ``EqvtAmt`` (currency conversion)       AM03    error
   ``EndToEndId`` repeats in the file      AM05    error
   requested execution date in the past    DT01    warning
   ======================================  ======  =======================

   A batch with no payments is already a structural finding (``CdtTrfTxInf``
   must occur at least once).

A past execution date is a warning, not a rejection. Banks differ here; the
profile the mock follows is the common SEPA one, where a past date is
executed on the next execution date, and the warning says so. "Today" is the
bank's today, passed in by the server from the clock; alone, it is the host's date.

A body whose first line is a NACHA file header (``101``) is read by
``nacha.inspect`` instead, into the same model, at every door: ``POST
/_mock/validate``, ``POST /payments`` and the drop directory (#53).

``validate`` never raises: whatever the body, the answer is a list of
findings.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from xml.etree import ElementTree as ET

from . import messages, nacha, schema

Finding = schema.Finding

FILE = "/"  # the path of a finding about the body as a whole
SIGNED_TYPES = ("application/pkcs7-mime", "application/pkcs7-signature",
                "application/x-pkcs7-mime", "multipart/signed", "multipart/encrypted",
                "application/pgp-encrypted", "application/pgp-signature")
XMLDSIG = "http://www.w3.org/2000/09/xmldsig#"
XMLENC = "http://www.w3.org/2001/04/xmlenc#"


def validate(data, content_type=None, today=None):
    """Every finding about ``data``; never raises."""
    return inspect(data, content_type, today)[1]


def inspect(data, content_type=None, today=None):
    """``(PaymentFile, CollectionFile or None, [Finding])``; never raises."""
    try:
        return _inspect(data, content_type, today or datetime.date.today())
    except Exception as exc:  # the promise is findings, never a traceback
        return None, [Finding("error", FILE, schema.STRUCTURAL,
                              "the mock could not read this file (%s: %s); that is a "
                              "bug in the mock, please report it" % (type(exc).__name__, exc))]


def errors(findings):
    return [f for f in findings if f.level == "error"]


def render(finding) -> str:
    """One finding as one line of prose."""
    return "%s %s at %s: %s" % (finding.level, finding.code or "-", finding.path, finding.text)


def refusal(text):
    """A finding that refuses the whole file, in words."""
    return Finding("error", FILE, schema.STRUCTURAL, text)


_refusal = refusal


def _inspect(data, content_type, today):
    if isinstance(data, str):
        data = data.encode("utf-8")
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind in SIGNED_TYPES:
        return None, [_refusal(
            "the file is signed or encrypted (%s); the mock accepts plain XML only, "
            "because signing and encryption need cryptography it does not carry" % kind)]
    if not data or not data.strip():
        return None, [_refusal("the body is empty; send a pain.001 (%s)"
                               % ", ".join(messages.READABLE))]
    if nacha.recognise(data):
        payment_file, findings = nacha.inspect(data, today)
        if payment_file is not None and payment_file.returns:
            # Read without a finding - the bank writes these (#54) - but not
            # one to be sent: a return file answers payments, it makes none.
            findings.append(_refusal(
                "this is a NACHA return file, %d return(s) and no payment; the bank "
                "sends return files, it does not take them" % len(payment_file.returns)))
        return payment_file, findings
    if data.lstrip().startswith(b"-----BEGIN PGP"):
        return None, [_refusal("the file is PGP-armoured; the mock accepts plain XML only")]
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return None, [_refusal("the file carries a DTD; ISO 20022 files do not, and "
                               "the mock does not expand one")]
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        return None, [_refusal("the file is not well-formed XML (%s); send a pain.001 (%s)"
                               % (exc, ", ".join(messages.READABLE)))]
    for elem in root.iter():
        namespace, local = schema.split_tag(elem.tag)
        if (local == "Signature" or namespace == XMLDSIG) or \
                (local == "EncryptedData" or namespace == XMLENC):
            return None, [_refusal(
                "the file contains an XML %s; the mock accepts unsigned, unencrypted "
                "files only" % ("signature" if local != "EncryptedData" and namespace != XMLENC
                                else "encryption"))]
    message = schema.identify(root)
    if message is None or message.name not in messages.READABLE:
        namespace, local = schema.split_tag(root.tag)
        what = namespace[len(schema.NAMESPACE_PREFIX):] if namespace.startswith(
            schema.NAMESPACE_PREFIX) else (namespace or local)
        hint = " (that one the mock writes, it does not read it)" if message else ""
        return None, [_refusal(
            "the file is %s%s; the mock reads %s" % (what, hint, ", ".join(messages.READABLE)))]

    findings = []
    ibans = [(path, elem.text or "") for path, decl, elem in schema.walk(message, root, findings)
             if decl.pattern == "IBAN2007Identifier"]
    # check digits (ISO 13616) only for IBANs whose shape already passed
    malformed = {f.path for f in findings}
    findings += [Finding("error", path, "AC01", "%s fails its check digits" % iban.strip())
                 for path, iban in ibans if path not in malformed and not schema.iban_is_valid(iban)]
    payment_file = messages.from_tree(root)
    if isinstance(payment_file, messages.CollectionFile):
        findings += _collection_meaning(payment_file, today)
    else:
        findings += _meaning(payment_file, today)
    return payment_file, findings


def _meaning(payment_file, today):
    out = []
    header = payment_file.header
    everything = payment_file.payments
    if isinstance(header, schema.Node):  # a missing GrpHdr is already structural
        out += _count_and_sum(header, "the file", payment_file.nb_of_txs,
                              payment_file.ctrl_sum, everything)
    seen = {}
    for batch in payment_file.batches:
        node = batch.node
        if batch.payments:
            out += _count_and_sum(node, "batch %s" % batch.pmt_inf_id, batch.nb_of_txs,
                                  batch.ctrl_sum, batch.payments)
        when = batch.requested_execution_date
        if when is not None and when < today:
            out.append(Finding(
                "warning", node.path_of("ReqdExctnDt"), "DT01",
                "the requested execution date %s is in the past; the bank executes on "
                "the next business day instead" % when.isoformat()))
        account_ccy = batch.debtor_account_currency
        for payment in batch.payments:
            amount_path = payment.node.path_of("Amt")
            if payment.equivalent:
                out.append(Finding(
                    "error", amount_path, "AM03",
                    "EqvtAmt asks the bank to convert currencies; the mock does no FX, "
                    "send InstdAmt in the account's currency"))
            elif account_ccy and payment.currency and payment.currency != account_ccy:
                out.append(Finding(
                    "error", payment.node["Amt"].path_of("InstdAmt"), "AM03",
                    "the amount is in %s but the debtor account %s is in %s"
                    % (payment.currency, batch.debtor_account or "", account_ccy)))
            e2e = payment.end_to_end_id
            if e2e is not None:
                if e2e in seen:
                    out.append(Finding(
                        "error", payment.node["PmtId"].path_of("EndToEndId"), "AM05",
                        "EndToEndId %s already appears at %s" % (e2e, seen[e2e])))
                else:
                    seen[e2e] = payment.path
    return out


def _collection_meaning(collection_file, today):
    """What a ``pain.008`` says beyond its structure (#131): the counts and
    sums, a duplicate ``EndToEndId``, a collection date already past and an
    amount in a currency the creditor account is not in - the checks a
    ``pain.001`` gets, turned round to the creditor's side. Whether the
    mandate data is complete enough to collect on is a booking question, and
    waits for booking."""
    out = []
    header = collection_file.header
    everything = collection_file.collections
    if isinstance(header, schema.Node):
        out += _count_and_sum(header, "the file", collection_file.nb_of_txs,
                              collection_file.ctrl_sum, everything, "collection")
    seen = {}
    for batch in collection_file.batches:
        node = batch.node
        if batch.collections:
            out += _count_and_sum(node, "batch %s" % batch.pmt_inf_id, batch.nb_of_txs,
                                  batch.ctrl_sum, batch.collections, "collection")
        when = batch.requested_collection_date
        if when is not None and when < today:
            out.append(Finding(
                "warning", node.path_of("ReqdColltnDt"), "DT01",
                "the requested collection date %s is in the past; the bank collects "
                "on the next business day instead" % when.isoformat()))
        account_ccy = batch.creditor_account_currency
        for collection in batch.collections:
            if account_ccy and collection.currency and collection.currency != account_ccy:
                out.append(Finding(
                    "error", collection.node.path_of("InstdAmt"), "AM03",
                    "the amount is in %s but the creditor account %s is in %s"
                    % (collection.currency, batch.creditor_account or "", account_ccy)))
            e2e = collection.end_to_end_id
            if e2e is not None:
                if e2e in seen:
                    out.append(Finding(
                        "error", collection.node["PmtId"].path_of("EndToEndId"), "AM05",
                        "EndToEndId %s already appears at %s" % (e2e, seen[e2e])))
                else:
                    seen[e2e] = collection.path
    return out


def _count_and_sum(node, what, declared_count, declared_sum, payments, noun="payment"):
    out = []
    if declared_count is not None and declared_count != len(payments):
        out.append(Finding("error", node.path_of("NbOfTxs"), "AM18",
                           "NbOfTxs says %d but %s holds %d %s%s"
                           % (declared_count, what, len(payments), noun,
                              "" if len(payments) == 1 else "s")))
    amounts = [(p.amount, p.currency) for p in payments]
    if declared_sum is not None and all(a is not None for a, _ in amounts):
        actual = sum((Decimal(a).scaleb(-schema.exponent(c)) for a, c in amounts), Decimal(0))
        if actual != declared_sum:
            out.append(Finding("error", node.path_of("CtrlSum"), "AM10",
                               "CtrlSum says %s but the amounts in %s sum to %s"
                               % (declared_sum, what, actual)))
    return out
