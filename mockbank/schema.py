"""The ISO 20022 dictionary: every message the mock reads or writes, as data.

A message is a namespaced tree of elements. Each element here carries its
name, how often it may occur, its type (``text``, ``identifier``, ``code``,
``amount``, ``decimal``, ``count``, ``date``, ``datetime``, ``boolean``, or a
``group`` of children) and, for codes, the code set it draws from. Shapes that
recur - a postal address, an account, a financial institution, a party - are
declared once as functions of the message version and referenced.

Everything else is derived:

* ``walk`` traverses a received tree against a declaration and yields
  ``(path, declaration, element)``, reporting what does not fit - an element
  out of order, in the wrong namespace, missing, repeated too often, or with a
  value its type does not allow - as ``Finding`` s. The reader and the
  validator share it.
* ``read`` turns a tree into a plain mapping keyed by element name, the same
  shape ``build`` takes.
* ``build`` / ``serialize`` turn a mapping into a tree in declared order, so no
  writer hard-codes an element order.
* ``/_mock/dictionary`` serves the declarations as JSON (``to_json``).

Where the standard leaves a choice, the mock picks one and says so here, in
``CHOICES`` (which the dictionary endpoint also serves) and in the README:

* **Versions.** ``pain.001.001.09`` is read, and ``pain.001.001.03`` is
  accepted as well and read into the same mapping (its ``BIC`` and
  ``BICOrBEI`` are keyed as the ``.09`` ``BICFI`` and ``AnyBIC``). The mock
  writes ``pain.002.001.10``, ``camt.054.001.08``, ``camt.053.001.08``,
  ``camt.052.001.08`` and ``pacs.004.001.09``, the versions that go with
  ``pain.001.001.09`` in the 2019 message set most banks accept today.
* **Returns.** A return reaches the client as a ``pacs.004`` whose
  ``OrgnlGrpInf`` names the client's own ``pain.001``, standing in for the
  interbank message a real bank would relay, and whose ``SttlmMtd`` is
  ``INDA``: the bank settles it on its own books. The credit it books carries
  ``PMNT`` / ``ICDT`` / ``RRTN`` (reversal due to a payment return).
* **Bank transaction code.** Every debit the mock books carries
  ``PMNT`` / ``ICDT`` / ``ESCT`` (payments, issued credit transfer, SEPA credit
  transfer): 0.1 speaks euro credit transfers. Received files may carry any
  well-formed code; only the one the mock writes is fixed.
* **Coverage.** The declarations cover the elements the mock reads, writes or
  that a real bank's file commonly carries, not every optional element of the
  XSD. An element the standard allows but the dictionary does not declare is
  reported as a *warning* naming its path - read past, not understood -
  rather than silently ignored or rejected.
* **Minor units.** Amounts are integers in the currency's minor units
  everywhere but the wire. ``CURRENCY_EXPONENTS`` lists the ISO 4217
  exponents that are not 2; any other currency is taken to have two.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import namedtuple
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

NAMESPACE_PREFIX = "urn:iso:std:iso:20022:tech:xsd:"
UNBOUNDED = None

Finding = namedtuple("Finding", "level path code text")
Finding.__doc__ = """Something about a file, not an exception.

``level`` is ``"error"`` or ``"warning"``; ``path`` names the element
(``/Document/CstmrCdtTrfInitn/PmtInf[1]/CdtTrfTxInf[2]/Amt/InstdAmt``);
``code`` is the ISO 20022 status reason it is reported with, or None for a
warning; ``text`` is one sentence of prose.
"""

Amount = namedtuple("Amount", "minor ccy")
Amount.__doc__ = "An amount as an integer number of minor units and its currency."

# The structural reason a bank gives for a file it cannot process.
STRUCTURAL = "FF01"

CHOICES = {
    "read": "pain.001.001.09, and pain.001.001.03 read into the same mapping",
    "written": "pain.002.001.10, camt.054.001.08, camt.053.001.08, camt.052.001.08, "
               "pacs.004.001.09",
    "bank_transaction_code": "PMNT/ICDT/ESCT on every debit the mock books, "
                             "PMNT/ICDT/RRTN on the credit a return books",
    "returns": "a pacs.004 to the client, OrgnlGrpInf naming its pain.001, "
               "SttlmMtd INDA",
    "coverage": "elements the standard allows but the dictionary does not "
                "declare are reported as warnings, read past and not understood",
    "minor_units": "ISO 4217 exponents from CURRENCY_EXPONENTS; any other "
                   "currency is taken to have two",
}

# -- code sets -----------------------------------------------------------------
#
# The external code sets the mock uses, each code with a one-line meaning. Sets
# the mock only has to recognise in received files, and whose published lists
# are long (service levels, purposes, bank transaction families), are checked
# by pattern instead; see PATTERNS.

CODE_SETS = {
    "ExternalPaymentGroupStatus1Code": {
        "ACCP": "AcceptedCustomerProfile: preceding checks passed, the customer profile too",
        "ACCC": "AcceptedCreditSettlementCompleted: the creditor's account has been credited",
        "ACSC": "AcceptedSettlementCompleted: the debtor's account has been debited",
        "ACSP": "AcceptedSettlementInProcess: all checks passed, settlement is under way",
        "ACTC": "AcceptedTechnicalValidation: the file passed syntactic and semantic validation",
        "ACWC": "AcceptedWithChange: accepted, with a change such as the execution date",
        "PART": "PartiallyAccepted: some transactions accepted, some rejected",
        "PDNG": "Pending: further checks and status updates will follow",
        "RCVD": "Received: the file was received and nothing more is said yet",
        "RJCT": "Rejected: the file or batch was rejected",
    },
    "ExternalPaymentTransactionStatus1Code": {
        "ACCP": "AcceptedCustomerProfile: preceding checks passed, the customer profile too",
        "ACCC": "AcceptedCreditSettlementCompleted: the creditor's account has been credited",
        "ACSC": "AcceptedSettlementCompleted: the debtor's account has been debited",
        "ACSP": "AcceptedSettlementInProcess: all checks passed, settlement is under way",
        "ACTC": "AcceptedTechnicalValidation: the payment passed syntactic and semantic validation",
        "ACWC": "AcceptedWithChange: accepted, with a change such as the execution date",
        "PDNG": "Pending: further checks and status updates will follow",
        "RCVD": "Received: the payment was received and nothing more is said yet",
        "RJCT": "Rejected: the payment was rejected",
    },
    "ExternalStatusReason1Code": {
        "AC01": "IncorrectAccountNumber: the account number is invalid or missing",
        "AC02": "InvalidDebtorAccountNumber: the debtor account is invalid or not held by the bank",
        "AC04": "ClosedAccountNumber: the account has been closed on the bank's books",
        "AC06": "BlockedAccount: the account is blocked",
        "AG01": "TransactionForbidden: the transaction is forbidden on this account type",
        "AM02": "NotAllowedAmount: the amount exceeds the maximum authorised",
        "AM03": "NotAllowedCurrency: the currency is not allowed for this account",
        "AM04": "InsufficientFunds: the balance is insufficient to cover the amount",
        "AM05": "Duplication: the transaction appears to be a duplicate",
        "AM09": "WrongAmount: the amount received is not the amount agreed or expected",
        "AM10": "InvalidControlSum: the control sum does not equal the sum of the amounts",
        "AM18": "InvalidNumberOfTransactions: the number of transactions does not match",
        "BE05": "UnrecognisedInitiatingParty: the initiating party is not known to the bank",
        "DT01": "InvalidDate: the date is invalid, for example in the past",
        "DUPL": "DuplicatePayment: the file or payment has already been received",
        "FF01": "InvalidFileFormat: the file format is incomplete or invalid",
        "MD07": "EndCustomerDeceased: the end customer is deceased",
        "MS02": "NotSpecifiedReasonCustomerGenerated: reason not specified, customer generated",
        "MS03": "NotSpecifiedReasonAgentGenerated: reason not specified, agent generated",
        "NARR": "Narrative: the reason is given in the additional information",
        "RC01": "BankIdentifierIncorrect: the bank identifier is invalid or missing",
        "RR04": "RegulatoryReason: regulatory reason",
        "TM01": "InvalidCutOffTime: received after the cut-off time",
    },
    "ExternalBalanceType1Code": {
        "CLAV": "ClosingAvailable: closing balance available to the account owner",
        "CLBD": "ClosingBooked: closing booked balance at the end of the period",
        "FWAV": "ForwardAvailable: balance available on a future date",
        "INFO": "Information: balance for information only",
        "ITAV": "InterimAvailable: available balance during the period",
        "ITBD": "InterimBooked: booked balance during the period",
        "OPAV": "OpeningAvailable: opening balance available to the account owner",
        "OPBD": "OpeningBooked: opening booked balance, the previous closing booked balance",
        "PRCD": "PreviouslyClosedBooked: closing booked balance of the previous statement",
        "XPCD": "Expected: expected balance",
    },
    "ExternalEntryStatus1Code": {
        "BOOK": "Booked: the entry has been booked to the account",
        "FUTR": "Future: the entry is expected on a future date",
        "INFO": "Information: the entry is for information only",
        "PDNG": "Pending: the entry is not yet booked",
    },
    "CreditDebitCode": {
        "CRDT": "Credit: the amount is a credit to the account",
        "DBIT": "Debit: the amount is a debit to the account",
    },
    "PaymentMethod3Code": {
        "CHK": "Cheque",
        "TRA": "TransferAdvice",
        "TRF": "CreditTransfer",
    },
    "ChargeBearerType1Code": {
        "CRED": "BorneByCreditor",
        "DEBT": "BorneByDebtor",
        "SHAR": "Shared",
        "SLEV": "FollowingServiceLevel",
    },
    "Priority2Code": {
        "HIGH": "High",
        "NORM": "Normal",
    },
    "CopyDuplicate1Code": {
        "CODU": "CopyDuplicate: a copy of a message that was sent before",
        "COPY": "Copy: a copy of a message sent to another party",
        "DUPL": "Duplicate: the same message sent again",
    },
    "AddressType2Code": {
        "ADDR": "Postal", "BIZZ": "Business", "DLVY": "DeliveryTo",
        "HOME": "Residential", "MLTO": "MailTo", "PBOX": "POBox",
    },
    "DocumentType6Code": {
        "AROI": "AccountReceivableOpenItem", "BOLD": "BillOfLading",
        "CINV": "CommercialInvoice", "CMCN": "CommercialContract",
        "CNFA": "CreditNoteRelatedToFinancialAdjustment",
        "CREN": "CreditNote", "DEBN": "DebitNote", "DISP": "DispatchAdvice",
        "DNFA": "DebitNoteRelatedToFinancialAdjustment",
        "HIRI": "HireInvoice", "MSIN": "MeteredServiceInvoice",
        "PUOR": "PurchaseOrder", "SBIN": "SelfBilledInvoice",
        "SOAC": "StatementOfAccount", "TSUT": "TradeServicesUtilityTransaction",
        "VCHR": "Voucher",
    },
    "DocumentType3Code": {
        "DISP": "DispatchAdvice", "FXDR": "ForeignExchangeDealReference",
        "PUOR": "PurchaseOrder", "RADM": "RemittanceAdviceMessage",
        "RPIN": "RelatedPaymentInstruction",
        "SCOR": "StructuredCommunicationReference: a creditor reference such as an RF reference",
    },
    "ExternalReturnReason1Code": {
        "AC01": "IncorrectAccountNumber: the account number is invalid or missing",
        "AC04": "ClosedAccountNumber: the creditor's account has been closed",
        "AC06": "BlockedAccount: the creditor's account is blocked",
        "AG01": "TransactionForbidden: the transaction is forbidden on this type of account",
        "AM04": "InsufficientFunds: the amount is more than the account has available",
        "AM05": "Duplication: the payment was a duplicate",
        "BE04": "MissingCreditorAddress: the creditor's address is missing or incorrect",
        "CUST": "RequestedByCustomer: the creditor asked for the payment to be returned",
        "MD07": "EndCustomerDeceased: the end customer is deceased",
        "MS02": "NotSpecifiedReasonCustomerGenerated: reason not specified, customer generated",
        "MS03": "NotSpecifiedReasonAgentGenerated: reason not specified, agent generated",
        "RC01": "BankIdentifierIncorrect: the bank identifier is invalid or missing",
        "RR04": "RegulatoryReason: regulatory reason",
    },
    "SettlementMethod1Code": {
        "CLRG": "ClearingSystem: settled through a clearing system",
        "COVE": "CoverMethod: settled through a cover payment",
        "INDA": "InstructedAgent: settled on the instructed agent's own books",
        "INGA": "InstructingAgent: settled on the instructing agent's own books",
    },
}

# Bank transaction codes (domain, family, subfamily) the mock writes.
BANK_TRANSACTION_CODES = {
    ("PMNT", "ICDT", "ESCT"): "Payments / Issued Credit Transfers / SEPA Credit "
                              "Transfer: every debit the mock books",
    ("PMNT", "ICDT", "RRTN"): "Payments / Issued Credit Transfers / Reversal due "
                              "to Payment Return: the credit a return books",
    ("PMNT", "RCDT", "ESCT"): "Payments / Received Credit Transfers / SEPA Credit "
                              "Transfer: money arriving from somebody else (#91)",
}
BOOKED_DEBIT = ("PMNT", "ICDT", "ESCT")
RETURNED_CREDIT = ("PMNT", "ICDT", "RRTN")
RECEIVED_CREDIT = ("PMNT", "RCDT", "ESCT")

# Code sets checked by pattern rather than list, and identifier shapes.
PATTERNS = {
    "ActiveOrHistoricCurrencyCode": r"[A-Z]{3}",
    "CountryCode": r"[A-Z]{2}",
    "BICFIDec2014Identifier": r"[A-Z0-9]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?",
    "AnyBICDec2014Identifier": r"[A-Z0-9]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?",
    "IBAN2007Identifier": r"[A-Z]{2}[0-9]{2}[a-zA-Z0-9]{1,30}",
    "LEIIdentifier": r"[A-Z0-9]{18}[0-9]{2}",
    "UUIDv4Identifier": r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}",
    "External4Code": r"[A-Za-z0-9]{1,4}",
    "Exact4AlphaNumeric": r"[a-zA-Z0-9]{4}",
    "External5Code": r"[A-Za-z0-9]{1,5}",
    "External35Code": r".{1,35}",
}



def iban_is_valid(value: str) -> bool:
    """Whether an IBAN has the form and the check digits ISO 13616 gives it.

    Form and checksum only. Whether the BBAN is the right length for its
    country is a national rule per country; this does not claim to know them,
    which is the honest position rather than a half-filled table. Spaces and
    case are forgiven, as a person writes an IBAN.
    """
    value = (value or "").replace(" ", "").upper()
    return bool(re.fullmatch(PATTERNS["IBAN2007Identifier"], value)) and \
        mod97(value[4:] + value[:4]) == 1


def mod97(text: str) -> int:
    """`text` read as digits - letters as 10..35 - modulo 97.

    Reduced as it goes rather than built into one enormous integer, which is
    the form the standard is written in.
    """
    remainder = 0
    for char in text:
        value = int(char, 36)
        # A letter stands for two digits (A is 10, Z is 35), so it shifts the
        # running remainder by two places and a digit by one.
        remainder = (remainder * (10 if value < 10 else 100) + value) % 97
    return remainder


# ISO 4217 currencies whose minor unit is not two digits.
CURRENCY_EXPONENTS = {
    "BHD": 3, "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "IQD": 3, "ISK": 0,
    "JOD": 3, "JPY": 0, "KMF": 0, "KRW": 0, "KWD": 3, "LYD": 3, "OMR": 3,
    "PYG": 0, "RWF": 0, "TND": 3, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0,
    "XAF": 0, "XOF": 0, "XPF": 0,
}


def exponent(ccy: str) -> int:
    return CURRENCY_EXPONENTS.get(ccy, 2)


def format_amount(minor: int, ccy: str) -> str:
    """Minor units as the wire's decimal string: 125000 EUR -> ``1250.00``."""
    if not isinstance(minor, int) or isinstance(minor, bool) or minor < 0:
        raise ValueError("an amount is a non-negative integer of minor units, not %r" % (minor,))
    places = exponent(ccy)
    if not places:
        return str(minor)
    whole, frac = divmod(minor, 10 ** places)
    return "%d.%0*d" % (whole, places, frac)


def parse_amount(text: str, ccy: str):
    """The wire's decimal string as minor units, or None if it is not one
    or carries more decimals than the currency has."""
    if not re.fullmatch(r"[0-9]{1,18}(\.[0-9]{1,5})?", text or ""):
        return None
    try:
        value = Decimal(text) * (10 ** exponent(ccy))
    except InvalidOperation:  # pragma: no cover - the pattern rules it out
        return None
    if value != value.to_integral_value():
        return None
    return int(value)


# -- the element model -------------------------------------------------------

TYPES = ("group", "text", "identifier", "code", "amount", "decimal", "count",
         "date", "datetime", "boolean")


class El:
    """One element of a message: its name, type, occurrence and children.

    ``choice`` marks an ``xs:choice``: exactly one of the children appears.
    ``key`` is the name the element goes by in a mapping; it defaults to the
    element name and differs only where two versions name the same thing
    differently. Declarations are shared between parents, so ``occurs``
    returns a copy rather than changing one in place.
    """

    __slots__ = ("name", "type", "children", "min", "max", "codes", "pattern",
                 "length", "key", "choice", "iso", "_index")

    def __init__(self, name, type="group", children=(), codes=None, pattern=None,
                 length=None, key=None, choice=False, iso=None):
        if type not in TYPES:
            raise ValueError("unknown element type %r" % type)
        if codes is not None and codes not in CODE_SETS:
            raise ValueError("unknown code set %r" % codes)
        if pattern is not None and pattern not in PATTERNS:
            raise ValueError("unknown pattern %r" % pattern)
        self.name = name
        self.type = type
        self.children = tuple(children)
        self.min = 1
        self.max = 1
        self.codes = codes
        self.pattern = pattern
        self.length = length
        self.key = key or name
        self.choice = choice
        self.iso = iso
        self._index = {c.name: i for i, c in enumerate(self.children)}
        if len(self._index) != len(self.children):
            raise ValueError("%s declares a child twice" % name)

    def occurs(self, lo, hi):
        copy = El.__new__(El)
        for slot in El.__slots__:
            setattr(copy, slot, getattr(self, slot))
        copy.min, copy.max = lo, hi
        return copy

    @property
    def opt(self):
        return self.occurs(0, 1)

    def many(self, lo=0, hi=UNBOUNDED):
        return self.occurs(lo, hi)

    def keyed(self, key):
        copy = self.occurs(self.min, self.max)
        copy.key = key
        return copy

    def child(self, name):
        i = self._index.get(name)
        return None if i is None else self.children[i]

    def to_json(self):
        out = {"name": self.name, "type": "choice" if self.choice else self.type,
               "min": self.min, "max": "unbounded" if self.max is None else self.max}
        if self.key != self.name:
            out["key"] = self.key
        if self.iso:
            out["iso_type"] = self.iso
        if self.codes:
            out["codes"] = self.codes
        if self.pattern:
            out["pattern"] = self.pattern
        if self.length:
            out["max_length"] = self.length
        if self.children:
            out["children"] = [c.to_json() for c in self.children]
        return out


def Group(name, *children, iso=None):
    return El(name, "group", children, iso=iso)


def Choice(name, *children, iso=None):
    return El(name, "group", children, choice=True, iso=iso)


def Text(name, length=140):
    return El(name, "text", length=length)


def Ident(name, length=35, pattern=None):
    return El(name, "identifier", length=length, pattern=pattern)


def Code(name, codes=None, pattern=None):
    return El(name, "code", codes=codes, pattern=pattern)


def Amt(name):
    return El(name, "amount", iso="ActiveOrHistoricCurrencyAndAmount")


def Dec(name):
    return El(name, "decimal", iso="DecimalNumber")


def Count(name):
    return El(name, "count", iso="Max15NumericText")


def Date(name):
    return El(name, "date", iso="ISODate")


def DateTime(name):
    return El(name, "datetime", iso="ISODateTime")


def Bool(name):
    return El(name, "boolean", iso="TrueFalseIndicator")


# -- shared shapes -------------------------------------------------------------
#
# Each takes the element name it appears under and, where versions differ, the
# pain.001 version it belongs to: 3 for the 2009 set, 9 for the 2019 set (which
# pain.002.001.10, camt.054.001.08 and camt.053.001.08 also belong to).

def code_or_proprietary(name, codes=None, pattern="External4Code", length=35):
    return Choice(name, Code("Cd", codes=codes, pattern=None if codes else pattern),
                  Text("Prtry", length))


def date_or_datetime(name):
    return Choice(name, Date("Dt"), DateTime("DtTm"), iso="DateAndDateTime2Choice")


def currency(name="Ccy"):
    return Code(name, pattern="ActiveOrHistoricCurrencyCode")


def country(name="Ctry"):
    return Code(name, pattern="CountryCode")


def postal_address(v, name="PstlAdr"):
    if v >= 9:
        # AddressType3Choice: the proprietary alternative is an identifier
        # with its issuer, not a line of text (GenericIdentification30).
        adr_tp = Choice("AdrTp", Code("Cd", codes="AddressType2Code"),
                        Group("Prtry", Ident("Id", 4, pattern="Exact4AlphaNumeric"),
                              Text("Issr", 35), Text("SchmeNm", 35).opt)).opt
    else:
        adr_tp = Code("AdrTp", codes="AddressType2Code").opt
    lines = [adr_tp, Text("Dept", 70).opt, Text("SubDept", 70).opt,
             Text("StrtNm", 70).opt, Text("BldgNb", 16).opt]
    if v >= 9:
        lines += [Text("BldgNm", 35).opt, Text("Flr", 70).opt,
                  Text("PstBx", 16).opt, Text("Room", 70).opt]
    lines += [Text("PstCd", 16).opt, Text("TwnNm", 35).opt]
    if v >= 9:
        lines += [Text("TwnLctnNm", 35).opt, Text("DstrctNm", 35).opt]
    lines += [Text("CtrySubDvsn", 35).opt, country().opt, Text("AdrLine", 70).many(0, 7)]
    return Group(name, *lines, iso="PostalAddress24" if v >= 9 else "PostalAddress6")


def generic_id(name="Othr", length=35):
    return Group(name, Ident("Id", length),
                 code_or_proprietary("SchmeNm").opt, Text("Issr", 35).opt)


def contact(v):
    return Group("CtctDtls", Text("NmPrfx", 4).opt, Text("Nm", 140).opt,
                 Text("PhneNb", 35).opt, Text("MobNb", 35).opt, Text("FaxNb", 35).opt,
                 Text("EmailAdr", 2048).opt,
                 iso="Contact4" if v >= 9 else "ContactDetails2")


def party(v, name):
    if v >= 9:
        org = Group("OrgId", Ident("AnyBIC", pattern="AnyBICDec2014Identifier").opt,
                    Ident("LEI", pattern="LEIIdentifier").opt, generic_id().many(),
                    iso="OrganisationIdentification29")
    else:
        org = Group("OrgId", Ident("BICOrBEI", pattern="AnyBICDec2014Identifier").keyed("AnyBIC").opt,
                    generic_id().many(), iso="OrganisationIdentification4")
    birth = Group("DtAndPlcOfBirth", Date("BirthDt"), Text("PrvcOfBirth", 35).opt,
                  Text("CityOfBirth", 35), country("CtryOfBirth"))
    prvt = Group("PrvtId", birth.opt, generic_id().many())
    return Group(name, Text("Nm", 140).opt, postal_address(v).opt,
                 Choice("Id", org, prvt).opt, country("CtryOfRes").opt, contact(v).opt,
                 iso="PartyIdentification135" if v >= 9 else "PartyIdentification32")


def account(v, name, owner=False):
    ident = Choice("Id", Ident("IBAN", 34, pattern="IBAN2007Identifier"),
                   generic_id("Othr", 34), iso="AccountIdentification4Choice")
    children = [ident, code_or_proprietary("Tp").opt, currency().opt, Text("Nm", 70).opt]
    if v >= 9:
        children.append(Group("Prxy", code_or_proprietary("Tp").opt, Text("Id", 2048)).opt)
    if owner:
        children += [party(v, "Ownr").opt, agent(v, "Svcr").opt]
    iso = "CashAccount39" if owner else ("CashAccount38" if v >= 9 else "CashAccount16")
    return Group(name, *children, iso=iso)


def agent(v, name):
    clearing = Group("ClrSysMmbId",
                     code_or_proprietary("ClrSysId", pattern="External5Code").opt,
                     Ident("MmbId"))
    if v >= 9:
        fin = Group("FinInstnId",
                    Ident("BICFI", pattern="BICFIDec2014Identifier").opt,
                    clearing.opt, Ident("LEI", pattern="LEIIdentifier").opt,
                    Text("Nm", 140).opt, postal_address(v).opt, generic_id().opt,
                    iso="FinancialInstitutionIdentification18")
        branch = Group("BrnchId", Ident("Id").opt, Ident("LEI", pattern="LEIIdentifier").opt,
                       Text("Nm", 140).opt, postal_address(v).opt)
    else:
        fin = Group("FinInstnId",
                    Ident("BIC", pattern="BICFIDec2014Identifier").keyed("BICFI").opt,
                    clearing.opt, Text("Nm", 140).opt, postal_address(v).opt,
                    generic_id().opt, iso="FinancialInstitutionIdentification7")
        branch = Group("BrnchId", Ident("Id").opt, Text("Nm", 140).opt,
                       postal_address(v).opt)
    return Group(name, fin, branch.opt,
                 iso="BranchAndFinancialInstitutionIdentification6" if v >= 9
                 else "BranchAndFinancialInstitutionIdentification4")


def party_or_agent(v, name):
    return Choice(name, party(v, "Pty"), agent(v, "Agt"), iso="Party40Choice")


def payment_type(v):
    service = code_or_proprietary("SvcLvl")
    return Group("PmtTpInf", Code("InstrPrty", codes="Priority2Code").opt,
                 service.many() if v >= 9 else service.opt,
                 code_or_proprietary("LclInstrm", pattern="External35Code").opt,
                 code_or_proprietary("CtgyPurp").opt)


def remittance(v):
    doc_type = Group("Tp", code_or_proprietary("CdOrPrtry", codes="DocumentType6Code"),
                     Text("Issr", 35).opt)
    doc = Group("RfrdDocInf", doc_type.opt, Ident("Nb").opt, Date("RltdDt").opt)
    doc_amt = Group("RfrdDocAmt", Amt("DuePyblAmt").opt, Amt("CdtNoteAmt").opt,
                    Amt("RmtdAmt").opt)
    ref_type = Group("Tp", code_or_proprietary("CdOrPrtry", codes="DocumentType3Code"),
                     Text("Issr", 35).opt)
    creditor_ref = Group("CdtrRefInf", ref_type.opt, Ident("Ref").opt)
    strd = Group("Strd", doc.many(), doc_amt.opt, creditor_ref.opt,
                 party(v, "Invcr").opt, party(v, "Invcee").opt,
                 Text("AddtlRmtInf", 140).many(0, 3))
    return Group("RmtInf", Text("Ustrd", 140).many(), strd.many(),
                 iso="RemittanceInformation16" if v >= 9 else "RemittanceInformation5")


def status_reason(v):
    return Group("StsRsnInf", party(v, "Orgtr").opt,
                 Choice("Rsn", Code("Cd", codes="ExternalStatusReason1Code"),
                        Text("Prtry", 35)).opt,
                 Text("AddtlInf", 105).many(), iso="StatusReasonInformation12")


def group_header_statement():
    pagination = Group("MsgPgntn", Count("PgNb"), Bool("LastPgInd"))
    return Group("GrpHdr", Ident("MsgId"), DateTime("CreDtTm"),
                 party(9, "MsgRcpt").opt, pagination.opt,
                 Text("AddtlInf", 500).opt, iso="GroupHeader81")


def bank_transaction_code(name="BkTxCd"):
    family = Group("Fmly", Code("Cd", pattern="External4Code"),
                   Code("SubFmlyCd", pattern="External4Code"))
    domain = Group("Domn", Code("Cd", pattern="External4Code"), family)
    proprietary = Group("Prtry", Text("Cd", 35), Text("Issr", 35).opt)
    return Group(name, domain.opt, proprietary.opt, iso="BankTransactionCodeStructure4")


def return_reason(v, version):
    """Why a payment came back: ``PaymentReturnReason5`` in a statement's
    ``RtrInf``, ``PaymentReturnReason6`` in a ``pacs.004``'s ``RtrRsnInf``.
    The ``.5`` form can also name the original bank transaction code."""
    reason = Choice("Rsn", Code("Cd", codes="ExternalReturnReason1Code"), Text("Prtry", 35),
                    iso="ReturnReason5Choice")
    children = [bank_transaction_code("OrgnlBkTxCd").opt] if version == 5 else []
    children += [party(v, "Orgtr").opt, reason.opt, Text("AddtlInf", 105).many()]
    name = "RtrInf" if version == 5 else "RtrRsnInf"
    return Group(name, *children, iso="PaymentReturnReason%d" % version)


def original_transaction(v):
    """``OrgnlTxRef``: what the original payment said, echoed back in a status
    report or a return. A subset of ``OriginalTransactionReference28``."""
    return Group(
        "OrgnlTxRef", Choice("Amt", Amt("InstdAmt"), iso="AmountType4Choice").opt,
        date_or_datetime("ReqdExctnDt").opt,
        Code("PmtMtd", codes="PaymentMethod3Code").opt, remittance(v).opt,
        party_or_agent(v, "Dbtr").opt, account(v, "DbtrAcct").opt, agent(v, "DbtrAgt").opt,
        agent(v, "CdtrAgt").opt, party_or_agent(v, "Cdtr").opt, account(v, "CdtrAcct").opt,
        iso="OriginalTransactionReference28")


def entry():
    v = 9
    refs = Group("Refs", Ident("MsgId").opt, Ident("AcctSvcrRef").opt,
                 Ident("PmtInfId").opt, Ident("InstrId").opt, Ident("EndToEndId").opt,
                 Ident("UETR", 36, pattern="UUIDv4Identifier").opt, Ident("TxId").opt,
                 iso="TransactionReferences6")
    instructed = Group("InstdAmt", Amt("Amt"))
    amount_details = Group("AmtDtls", instructed.opt, Group("TxAmt", Amt("Amt")).opt)
    parties = Group("RltdPties", party_or_agent(v, "InitgPty").opt,
                    party_or_agent(v, "Dbtr").opt, account(v, "DbtrAcct").opt,
                    party_or_agent(v, "UltmtDbtr").opt, party_or_agent(v, "Cdtr").opt,
                    account(v, "CdtrAcct").opt, party_or_agent(v, "UltmtCdtr").opt,
                    iso="TransactionParties6")
    agents = Group("RltdAgts", agent(v, "InstgAgt").opt, agent(v, "InstdAgt").opt,
                   agent(v, "DbtrAgt").opt, agent(v, "CdtrAgt").opt,
                   iso="TransactionAgents5")
    tx = Group("TxDtls", refs.opt, Amt("Amt").opt,
               Code("CdtDbtInd", codes="CreditDebitCode").opt, amount_details.opt,
               bank_transaction_code().opt, parties.opt, agents.opt,
               code_or_proprietary("Purp").opt, remittance(v).opt, return_reason(v, 5).opt,
               Text("AddtlTxInf", 500).opt, iso="EntryTransaction10")
    batch = Group("Btch", Ident("MsgId").opt, Ident("PmtInfId").opt, Count("NbOfTxs").opt,
                  Amt("TtlAmt").opt, Code("CdtDbtInd", codes="CreditDebitCode").opt)
    details = Group("NtryDtls", batch.opt, tx.many(), iso="EntryDetails9")
    return Group("Ntry", Ident("NtryRef").opt, Amt("Amt"),
                 Code("CdtDbtInd", codes="CreditDebitCode"), Bool("RvslInd").opt,
                 code_or_proprietary("Sts", codes="ExternalEntryStatus1Code"),
                 date_or_datetime("BookgDt").opt, date_or_datetime("ValDt").opt,
                 Ident("AcctSvcrRef").opt, bank_transaction_code(),
                 details.many(), Text("AddtlNtryInf", 500).opt, iso="ReportEntry10")


def transactions_summary():
    net = Group("TtlNetNtry", Dec("Amt"), Code("CdtDbtInd", codes="CreditDebitCode"))
    total = Group("TtlNtries", Count("NbOfNtries").opt, Dec("Sum").opt, net.opt)
    credits = Group("TtlCdtNtries", Count("NbOfNtries").opt, Dec("Sum").opt)
    debits = Group("TtlDbtNtries", Count("NbOfNtries").opt, Dec("Sum").opt)
    return Group("TxsSummry", total.opt, credits.opt, debits.opt, iso="TotalTransactions6")


def report_header(pagination_name):
    """What an account report (``Stmt`` or ``Ntfctn``) carries before its body."""
    pagination = Group(pagination_name, Count("PgNb"), Bool("LastPgInd"))
    period = Group("FrToDt", DateTime("FrDtTm"), DateTime("ToDtTm"))
    return [Ident("Id"), pagination.opt, Dec("ElctrncSeqNb").opt, Dec("LglSeqNb").opt,
            DateTime("CreDtTm").opt, period.opt,
            Code("CpyDplctInd", codes="CopyDuplicate1Code").opt,
            account(9, "Acct", owner=True)]


# -- the messages ------------------------------------------------------------

def pain001(v):
    header = Group("GrpHdr", Ident("MsgId"), DateTime("CreDtTm"), Count("NbOfTxs"),
                   Dec("CtrlSum").opt, party(v, "InitgPty"),
                   iso="GroupHeader85" if v >= 9 else "GroupHeader32")
    if v >= 9:
        payment_id = Group("PmtId", Ident("InstrId").opt, Ident("EndToEndId"),
                           Ident("UETR", 36, pattern="UUIDv4Identifier").opt)
    else:
        payment_id = Group("PmtId", Ident("InstrId").opt, Ident("EndToEndId"))
    equivalent = Group("EqvtAmt", Amt("Amt"), currency("CcyOfTrf"))
    transaction = Group(
        "CdtTrfTxInf", payment_id, payment_type(v).opt,
        Choice("Amt", Amt("InstdAmt"), equivalent, iso="AmountType4Choice"),
        Code("ChrgBr", codes="ChargeBearerType1Code").opt,
        party(v, "UltmtDbtr").opt, agent(v, "CdtrAgt").opt, party(v, "Cdtr").opt,
        account(v, "CdtrAcct").opt, party(v, "UltmtCdtr").opt,
        code_or_proprietary("Purp").opt, remittance(v).opt,
        iso="CreditTransferTransaction34" if v >= 9 else "CreditTransferTransactionInformation10")
    batch = Group(
        "PmtInf", Ident("PmtInfId"), Code("PmtMtd", codes="PaymentMethod3Code"),
        Bool("BtchBookg").opt, Count("NbOfTxs").opt, Dec("CtrlSum").opt,
        payment_type(v).opt,
        date_or_datetime("ReqdExctnDt") if v >= 9 else Date("ReqdExctnDt"),
        party(v, "Dbtr"), account(v, "DbtrAcct"), agent(v, "DbtrAgt"),
        party(v, "UltmtDbtr").opt, Code("ChrgBr", codes="ChargeBearerType1Code").opt,
        transaction.many(1),
        iso="PaymentInstruction30" if v >= 9 else "PaymentInstructionInformation3")
    return Group("Document", Group("CstmrCdtTrfInitn", header, batch.many(1)))


def pain002():
    v = 9
    per_status = Group("NbOfTxsPerSts", Count("DtldNbOfTxs"),
                       Code("DtldSts", codes="ExternalPaymentTransactionStatus1Code"),
                       Dec("DtldCtrlSum").opt)
    header = Group("GrpHdr", Ident("MsgId"), DateTime("CreDtTm"),
                   party(v, "InitgPty").opt, agent(v, "FwdgAgt").opt,
                   agent(v, "DbtrAgt").opt, agent(v, "CdtrAgt").opt, iso="GroupHeader86")
    original_group = Group(
        "OrgnlGrpInfAndSts", Ident("OrgnlMsgId"),
        Ident("OrgnlMsgNmId"),
        DateTime("OrgnlCreDtTm").opt, Count("OrgnlNbOfTxs").opt, Dec("OrgnlCtrlSum").opt,
        Code("GrpSts", codes="ExternalPaymentGroupStatus1Code").opt,
        status_reason(v).many(), per_status.many(), iso="OriginalGroupHeader17")
    original_tx = original_transaction(v)
    transaction = Group(
        "TxInfAndSts", Ident("StsId").opt, Ident("OrgnlInstrId").opt,
        Ident("OrgnlEndToEndId").opt, Ident("OrgnlUETR", 36, pattern="UUIDv4Identifier").opt,
        Code("TxSts", codes="ExternalPaymentTransactionStatus1Code").opt,
        status_reason(v).many(), DateTime("AccptncDtTm").opt,
        Ident("AcctSvcrRef").opt, Ident("ClrSysRef").opt, original_tx.opt,
        iso="PaymentTransaction105")
    batch = Group(
        "OrgnlPmtInfAndSts", Ident("OrgnlPmtInfId"), Count("OrgnlNbOfTxs").opt,
        Dec("OrgnlCtrlSum").opt,
        Code("PmtInfSts", codes="ExternalPaymentGroupStatus1Code").opt,
        status_reason(v).many(), per_status.many(), transaction.many(),
        iso="OriginalPaymentInstruction32")
    return Group("Document", Group("CstmrPmtStsRpt", header, original_group, batch.many()))


def pacs004():
    v = 9
    settlement = Group("SttlmInf", Code("SttlmMtd", codes="SettlementMethod1Code"),
                       iso="SettlementInstruction7")
    header = Group("GrpHdr", Ident("MsgId"), DateTime("CreDtTm"), Count("NbOfTxs"),
                   Dec("CtrlSum").opt, Amt("TtlRtrdIntrBkSttlmAmt").opt,
                   Date("IntrBkSttlmDt").opt, settlement, iso="GroupHeader90")
    original_group = Group("OrgnlGrpInf", Ident("OrgnlMsgId"), Ident("OrgnlMsgNmId"),
                           DateTime("OrgnlCreDtTm").opt, iso="OriginalGroupHeader18")
    transaction = Group(
        "TxInf", Ident("RtrId").opt, Ident("OrgnlInstrId").opt,
        Ident("OrgnlEndToEndId").opt, Ident("OrgnlUETR", 36, pattern="UUIDv4Identifier").opt,
        Amt("OrgnlIntrBkSttlmAmt").opt, Date("OrgnlIntrBkSttlmDt").opt,
        Amt("RtrdIntrBkSttlmAmt"), Date("IntrBkSttlmDt").opt,
        return_reason(v, 6).many(), original_transaction(v).opt, iso="PaymentTransaction112")
    return Group("Document", Group("PmtRtr", header, original_group.opt, transaction.many(),
                                   iso="PaymentReturnV09"))


def camt054():
    body = report_header("NtfctnPgntn") + [
        transactions_summary().opt, entry().many(), Text("AddtlNtfctnInf", 500).opt]
    notification = Group("Ntfctn", *body, iso="AccountNotification17")
    return Group("Document", Group("BkToCstmrDbtCdtNtfctn", group_header_statement(),
                                   notification.many(1)))


def cash_balance():
    balance_type = Group("Tp", code_or_proprietary("CdOrPrtry", codes="ExternalBalanceType1Code"),
                         code_or_proprietary("SubTp").opt, iso="BalanceType13")
    return Group("Bal", balance_type, Amt("Amt"),
                 Code("CdtDbtInd", codes="CreditDebitCode"), date_or_datetime("Dt"),
                 iso="CashBalance8")


def camt053():
    body = report_header("StmtPgntn") + [
        cash_balance().many(1), transactions_summary().opt, entry().many(),
        Text("AddtlStmtInf", 500).opt]
    statement = Group("Stmt", *body, iso="AccountStatement9")
    return Group("Document", Group("BkToCstmrStmt", group_header_statement(),
                                   statement.many(1)))


def camt052():
    # The statement's shape under other names (#132): a report may carry no
    # balance at all, where a statement needs at least one.
    body = report_header("RptPgntn") + [
        cash_balance().many(), transactions_summary().opt, entry().many(),
        Text("AddtlRptInf", 500).opt]
    report = Group("Rpt", *body, iso="AccountReport25")
    return Group("Document", Group("BkToCstmrAcctRpt", group_header_statement(),
                                   report.many(1)))


class Message:
    """A message the mock speaks: its identifier, direction and declaration."""

    def __init__(self, name, direction, document, description):
        self.name = name
        self.direction = direction
        self.document = document
        self.description = description
        self.namespace = NAMESPACE_PREFIX + name

    @property
    def body(self):
        """The one child of ``Document``: ``CstmrCdtTrfInitn`` and so on."""
        return self.document.children[0]

    def to_json(self):
        return {"message": self.name, "namespace": self.namespace,
                "direction": self.direction, "description": self.description,
                "document": self.document.to_json()}


MESSAGES = {m.name: m for m in (
    Message("pain.001.001.09", "in", pain001(9),
            "Customer credit transfer initiation: the payment file a client sends"),
    Message("pain.001.001.03", "in", pain001(3),
            "The 2009 version of the same, still accepted and read into the same mapping"),
    Message("pain.002.001.10", "out", pain002(),
            "Customer payment status report: accepts or rejects the file, batches and payments"),
    Message("camt.054.001.08", "out", camt054(),
            "Bank-to-customer debit/credit notification: a payment has settled"),
    Message("camt.053.001.08", "out", camt053(),
            "Bank-to-customer statement: the end-of-day account statement"),
    Message("camt.052.001.08", "out", camt052(),
            "Bank-to-customer account report: the intraday report, on request"),
    Message("pacs.004.001.09", "out", pacs004(),
            "Payment return: a payment that had settled comes back, with its reason"),
)}


def identify(root):
    """The Message a parsed tree is, or None if the mock does not speak it."""
    namespace, local = split_tag(root.tag)
    if local != "Document" or not namespace.startswith(NAMESPACE_PREFIX):
        return None
    return MESSAGES.get(namespace[len(NAMESPACE_PREFIX):])


def split_tag(tag):
    if isinstance(tag, str) and tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return "", tag if isinstance(tag, str) else ""


# -- the walker ----------------------------------------------------------------

def walk(message, root, findings=None):
    """Yield ``(path, declaration, element)`` for every declared element of
    ``root``, in document order, appending a ``Finding`` to ``findings`` for
    everything that does not fit the declaration. Never raises on a tree."""
    if findings is None:
        findings = []
    namespace, local = split_tag(root.tag)
    path = "/" + local
    if namespace != message.namespace or local != message.document.name:
        findings.append(Finding("error", path, STRUCTURAL,
                                "the root is %s, not a %s Document" % (root.tag, message.name)))
        return
    yield from _walk(message.document, root, path, message.namespace, findings)


def check(message, root):
    """Every finding ``walk`` makes, as a list."""
    findings = []
    for _ in walk(message, root, findings):
        pass
    return findings


def _walk(decl, elem, path, namespace, findings):
    yield path, decl, elem
    if decl.type == "group":
        for child_decl, child, child_path in _match(decl, elem, path, namespace, findings):
            yield from _walk(child_decl, child, child_path, namespace, findings)
    else:
        _check_value(decl, elem, path, findings)


def _match(decl, elem, path, namespace, findings):
    """Pair the children of ``elem`` with the children ``decl`` declares.

    Yields ``(declaration, element, path)`` for each child the declaration
    knows, and reports order, occurrence, namespace and unknown elements.
    This is the one traversal ``walk`` and ``read`` share.
    """
    counts = [0] * len(decl.children)
    position = 0
    parent_ns = split_tag(elem.tag)[0]  # a subtree in the wrong one is reported once
    if (elem.text or "").strip():
        findings.append(Finding("error", path, STRUCTURAL,
                                "%s holds text; it should hold only elements" % decl.name))
    for child in elem:
        if not isinstance(child.tag, str):
            continue  # a comment or processing instruction
        child_ns, local = split_tag(child.tag)
        index = decl._index.get(local)
        if index is None:
            findings.append(Finding(
                "warning", "%s/%s" % (path, local), None,
                "%s is not in the mock's dictionary under %s; it was read past, "
                "not understood" % (local, decl.name)))
            continue
        child_decl = decl.children[index]
        counts[index] += 1
        child_path = "%s/%s" % (path, local)
        if child_decl.max != 1:
            child_path += "[%d]" % counts[index]
        if child_ns != namespace and parent_ns == namespace:
            findings.append(Finding(
                "error", child_path, STRUCTURAL,
                "%s is in %s; every element of the message is in %s"
                % (local, "namespace " + child_ns if child_ns else "no namespace", namespace)))
        if decl.choice:
            pass  # checked below, once every child has been seen
        elif index < position:
            findings.append(Finding(
                "error", child_path, STRUCTURAL,
                "%s is out of order: the standard puts it before %s"
                % (local, decl.children[position].name)))
        else:
            position = index
        if child_decl.max is not None and counts[index] == child_decl.max + 1:
            findings.append(Finding(
                "error", child_path, STRUCTURAL,
                "%s may occur at most %d time%s under %s"
                % (local, child_decl.max, "" if child_decl.max == 1 else "s", decl.name)))
        yield child_decl, child, child_path
    if decl.choice:
        present = [c.name for c, n in zip(decl.children, counts) if n]
        if len(present) != 1:
            names = ", ".join(c.name for c in decl.children)
            findings.append(Finding(
                "error", path, STRUCTURAL,
                "%s holds exactly one of %s; it holds %s"
                % (decl.name, names, " and ".join(present) if present else "none")))
        return
    for child_decl, count in zip(decl.children, counts):
        if count < child_decl.min:
            findings.append(Finding(
                "error", "%s/%s" % (path, child_decl.name), STRUCTURAL,
                "%s is required under %s and is missing" % (child_decl.name, decl.name)
                if child_decl.min == 1 else
                "%s must occur at least %d times under %s"
                % (child_decl.name, child_decl.min, decl.name)))


_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DATETIME = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?")
_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")


def _problem(decl, elem):
    """Why an element's value does not fit its type, or None if it does."""
    if len(elem):
        return "%s should hold a value, not elements" % decl.name
    text = elem.text or ""
    if not text.strip():
        return "%s is empty" % decl.name
    kind = decl.type
    if kind in ("text", "identifier") and decl.length and len(text) > decl.length:
        return "%s is %d characters; the standard allows %d" % (decl.name, len(text), decl.length)
    if decl.codes and text not in CODE_SETS[decl.codes]:
        return "%s is %r, which is not a code in %s" % (decl.name, text, decl.codes)
    if decl.pattern and not re.fullmatch(PATTERNS[decl.pattern], text):
        return "%s is %r, which is not a valid %s" % (decl.name, text, decl.pattern)
    if kind == "amount":
        ccy = elem.get("Ccy")
        if not ccy or not re.fullmatch(PATTERNS["ActiveOrHistoricCurrencyCode"], ccy):
            return "%s has no valid Ccy attribute" % decl.name
        if not re.fullmatch(r"[0-9]{1,18}(\.[0-9]{1,5})?", text) or \
                len(text.replace(".", "")) > 18:
            return "%s is %r, which is not an amount" % (decl.name, text)
        if parse_amount(text, ccy) is None:
            return "%s is %r, more decimals than %s has (%d)" % (decl.name, text, ccy, exponent(ccy))
    elif kind == "decimal":
        if not _DECIMAL.fullmatch(text) or len(text.strip("-").replace(".", "")) > 18:
            return "%s is %r, which is not a decimal number" % (decl.name, text)
    elif kind == "count":
        if not re.fullmatch(r"[0-9]{1,15}", text):
            return "%s is %r, which is not a number of up to 15 digits" % (decl.name, text)
    elif kind == "date":
        found = _DATE.fullmatch(text)
        if not found or not _valid_date(*found.groups()):
            return "%s is %r, which is not a date (YYYY-MM-DD)" % (decl.name, text)
    elif kind == "datetime":
        found = _DATETIME.fullmatch(text)
        if not found or not _valid_date(*found.groups()[:3]) or \
                int(found.group(4)) > 23 or int(found.group(5)) > 59 or int(found.group(6)) > 59:
            return "%s is %r, which is not a date and time" % (decl.name, text)
    elif kind == "boolean":
        if text not in ("true", "false", "1", "0"):
            return "%s is %r, which is not true or false" % (decl.name, text)
    return None


def _valid_date(year, month, day):
    try:
        _dt.date(int(year), int(month), int(day))
    except ValueError:
        return False
    return True


def _check_value(decl, elem, path, findings):
    problem = _problem(decl, elem)
    if problem:
        findings.append(Finding("error", path, STRUCTURAL, problem))


# -- reading -----------------------------------------------------------------

class Node(dict):
    """A group read from a file: a dict of its children, and where it was.

    It compares equal to the plain dict ``build`` takes; ``path`` and
    ``path_of`` are there so a finding about a value can name the element it
    came from without anyone writing the path out.
    """

    __slots__ = ("path", "decl")

    def path_of(self, key):
        """The path of the child ``key``, whether or not the file had it."""
        for child in self.decl.children:
            if child.key == key:
                return "%s/%s" % (self.path, child.name)
        raise KeyError("%s declares no %s" % (self.decl.name, key))


def read(message, root):
    """A received tree as a mapping keyed by element key, the shape ``build``
    takes: a group is a ``Node`` (a dict), a repeatable element a list, an
    amount an ``Amount``, every other value its text. What does not fit is skipped or
    kept as text; ``check`` is what says so. Never raises on a tree."""
    return _read(message.document, root, "/" + message.document.name, message.namespace)


def _read(decl, elem, path, namespace):
    if decl.type != "group":
        text = elem.text or ""
        if decl.type == "amount":
            minor = parse_amount(text, elem.get("Ccy", ""))
            return text if minor is None else Amount(minor, elem.get("Ccy"))
        return text
    out = Node()
    out.path, out.decl = path, decl
    for child_decl, child, child_path in _match(decl, elem, path, namespace, []):
        value = _read(child_decl, child, child_path, namespace)
        if child_decl.max == 1:
            out.setdefault(child_decl.key, value)
        else:
            out.setdefault(child_decl.key, []).append(value)
    return out


# -- building ------------------------------------------------------------------

def build(message, mapping):
    """The mapping as an element tree in declared order. Raises ValueError on
    a mapping that does not fit: a writer's bug, not a client's."""
    return _build(message.document, mapping, message.namespace, "/Document")


def serialize(message, mapping) -> bytes:
    """The mapping as a UTF-8 document with the message's default namespace."""
    # Built with bare names and the namespace declared on the root, because
    # ElementTree's default_namespace refuses the unqualified Ccy attribute.
    root = _build(message.document, mapping, None, "/Document")
    root.set("xmlns", message.namespace)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _build(decl, value, namespace, path):
    elem = ET.Element("{%s}%s" % (namespace, decl.name) if namespace else decl.name)
    if decl.type != "group":
        text, attrs = _format(decl, value, path)
        elem.text = text
        elem.attrib.update(attrs)
        problem = _problem(decl, elem)
        if problem:
            raise ValueError("%s: %s" % (path, problem))
        return elem
    if not isinstance(value, dict):
        raise ValueError("%s: a group is built from a dict, not %r" % (path, type(value).__name__))
    keys = {c.key for c in decl.children}
    unknown = sorted(set(value) - keys)
    if unknown:
        raise ValueError("%s: %s declares no %s" % (path, decl.name, ", ".join(unknown)))
    present = [c for c in decl.children if value.get(c.key) not in (None, [])]
    if decl.choice and len(present) != 1:
        raise ValueError("%s: exactly one of %s, not %d"
                         % (path, ", ".join(c.key for c in decl.children), len(present)))
    for child_decl in decl.children:
        item = value.get(child_decl.key)
        items = [] if item is None else item if child_decl.max != 1 else [item]
        if child_decl.max != 1 and not isinstance(item, (list, tuple, type(None))):
            raise ValueError("%s/%s repeats, so it is built from a list"
                             % (path, child_decl.name))
        if not decl.choice and len(items) < child_decl.min:
            raise ValueError("%s/%s is required" % (path, child_decl.name))
        if child_decl.max is not None and len(items) > child_decl.max:
            raise ValueError("%s/%s occurs at most %d times" % (path, child_decl.name, child_decl.max))
        for n, one in enumerate(items, 1):
            child_path = "%s/%s" % (path, child_decl.name)
            if child_decl.max != 1:
                child_path += "[%d]" % n
            elem.append(_build(child_decl, one, namespace, child_path))
    return elem


def _format(decl, value, path):
    kind = decl.type
    if kind == "amount":
        if not isinstance(value, Amount):
            raise ValueError("%s: an amount is built from an Amount, not %r" % (path, value))
        return format_amount(value.minor, value.ccy), {"Ccy": value.ccy}
    if kind == "datetime" and isinstance(value, _dt.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("%s: a datetime the mock writes carries its zone" % path)
        return value.isoformat(timespec="seconds" if not value.microsecond
                               else "milliseconds"), {}
    if kind == "date" and isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        return value.isoformat(), {}
    if kind == "boolean" and isinstance(value, bool):
        return "true" if value else "false", {}
    if kind in ("count", "decimal") and isinstance(value, int) and not isinstance(value, bool):
        return str(value), {}
    if kind == "decimal" and isinstance(value, Decimal):
        return str(value), {}
    if isinstance(value, str):
        return value, {}
    raise ValueError("%s: cannot write %r as %s" % (path, value, kind))


def dictionary_index():
    """What ``GET /_mock/dictionary`` answers."""
    return {
        "messages": {name: {"namespace": m.namespace, "direction": m.direction,
                            "description": m.description,
                            "href": "/_mock/dictionary/" + name}
                     for name, m in MESSAGES.items()},
        "code_sets": CODE_SETS,
        "patterns": PATTERNS,
        "bank_transaction_codes": {"/".join(k): v for k, v in BANK_TRANSACTION_CODES.items()},
        "currency_exponents": CURRENCY_EXPONENTS,
        "choices": CHOICES,
    }
