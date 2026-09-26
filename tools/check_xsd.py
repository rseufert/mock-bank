#!/usr/bin/env python3
"""Hold the dictionary and the mock's output to the published XSDs.

`GeneratedMessagesAreValid` checks every message the mock writes against
`mockbank/schema.py`, which is what the writers are built from too, so a
declaration that is wrong against the standard passes it. This is the check
that cannot share that blind spot. It fetches the published XSD for each
message and, where it can:

1. **compares the dictionary with the XSD**, element by element: every
   declared element exists at that place in the XSD, in the XSD's order, with
   its occurrence, as a group or a value as the XSD has it, and no longer than
   the XSD allows;
2. **validates files with `xmllint`**: the project's own clean samples and the
   external ones in `tests/samples/external/` must pass, the ones in
   `external/invalid/` must fail, and every message a running mock writes for
   the sample file - a status report, a rejection, notifications, statements -
   must pass.

The XSDs are not in this repository: they are ISO's, and the project would
rather point at them than redistribute them. They are fetched from pinned
commits of two projects that publish them, and checked against a SHA-256
before use, into `tools/.xsd/` (ignored by git). With no network the fetch is
skipped and so is everything that needs it; with no `xmllint` the validation
is skipped and the comparison still runs. `--require` turns a skip into a
failure, which is how CI runs it.

    python3 tools/check_xsd.py
    python3 tools/check_xsd.py --require
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from xml.etree import ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mockbank import schema  # noqa: E402

CACHE = os.path.join(ROOT, "tools", ".xsd")
SAMPLES = os.path.join(ROOT, "tests", "samples")
XS = "{http://www.w3.org/2001/XMLSchema}"

_GENKGO = "https://raw.githubusercontent.com/genkgo/camt/56e047d1599854ca34db0ccabce15230fcdd3f16/assets/"
_PROGNOV = ("https://raw.githubusercontent.com/prog-nov/iso20022-struct-go/"
            "b105620042e86826436edfdc45fdfa079b19894e/xsd/")

# message -> (where it is published, the SHA-256 it must have)
XSDS = {
    "pain.001.001.09": (_PROGNOV + "pain.001.001.09.xsd",
                        "de038b373e47b0077b1832ddd81f4b2f1eb25d35721f62da1e38b7f5a09fda24"),
    "pain.001.001.03": (_PROGNOV + "pain.001.001.03.xsd",
                        "6bb5c6f24250ab807f31f6164142bafd6d43bad8d162a926e258ff4c11e128af"),
    "pain.002.001.10": (_PROGNOV + "pain.002.001.10.xsd",
                        "2f9f8d0e9891fa9f31ccf0576397afe501614384d688ae6e43ba694b3d24b0cf"),
    "camt.053.001.08": (_GENKGO + "camt.053.001.08.xsd",
                        "c3cfac080dc31476bde7444b05d00e1b23558d5e44529e58d0ad562e6013873d"),
    "camt.054.001.08": (_GENKGO + "camt.054.001.08.xsd",
                        "2b392a1f7e70e70902fd0d803ff85989613bd1cae351663240b0bb9243be2c28"),
}


def fetch(message):
    """The local path of a message's XSD, fetching it once; None if it cannot."""
    url, digest = XSDS[message]
    path = os.path.join(CACHE, message + ".xsd")
    if os.path.exists(path) and _sha256(path) == digest:
        return path
    os.makedirs(CACHE, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read()
    except (urllib.error.URLError, OSError):
        return None
    if hashlib.sha256(data).hexdigest() != digest:
        raise SystemExit("%s is not the file this check was pinned to; refusing to "
                         "use it" % url)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def _sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# -- 1. the dictionary against the XSD ----------------------------------------

def compare(message, xsd_path):
    """Every way the declaration of `message` departs from its XSD."""
    root = ET.parse(xsd_path).getroot()
    types = {t.get("name"): t for t in root
             if t.tag in (XS + "complexType", XS + "simpleType")}
    document = types["Document"]
    body_type = document.find(XS + "sequence/" + XS + "element").get("type")
    problems = []
    _compare(message.body, body_type, types, "/Document/" + message.body.name, problems)
    return problems


def _children(complex_type):
    """[(name, type, min, max)] of a complex type, and whether it is a choice."""
    sequence = complex_type.find(XS + "sequence")
    if sequence is not None and len(sequence) == 1 and sequence[0].tag == XS + "choice":
        complex_type = sequence        # the 2009 schemas wrap a choice in a sequence
    for kind in ("sequence", "choice"):
        group = complex_type.find(XS + kind)
        if group is not None:
            out = []
            for element in group.findall(XS + "element"):
                most = element.get("maxOccurs", "1")
                out.append((element.get("name"), element.get("type"),
                            int(element.get("minOccurs", "1")),
                            None if most == "unbounded" else int(most)))
            return out, kind == "choice"
    return None, False


def _compare(decl, type_name, types, path, problems):
    xsd_type = types.get(type_name)
    if xsd_type is None:
        problems.append("%s: the XSD has no type %s" % (path, type_name))
        return
    kids, choice = (_children(xsd_type) if xsd_type.tag == XS + "complexType"
                    else (None, False))
    if decl.type != "group":
        if kids is not None:
            problems.append("%s: declared a %s value; the XSD's %s holds %s"
                            % (path, decl.type, type_name, ", ".join(k[0] for k in kids)))
        elif decl.type in ("text", "identifier"):
            limit = _length(xsd_type)
            if limit is not None and decl.length != limit:
                problems.append("%s: declared up to %s characters; the XSD's %s allows %s"
                                % (path, decl.length, type_name, limit))
        return
    if kids is None:
        problems.append("%s: declared a group; the XSD's %s holds a value" % (path, type_name))
        return
    if decl.choice != choice:
        problems.append("%s: declared a %s; the XSD's %s is a %s"
                        % (path, "choice" if decl.choice else "sequence", type_name,
                           "choice" if choice else "sequence"))
    names = [k[0] for k in kids]
    last = -1
    for child in decl.children:
        child_path = "%s/%s" % (path, child.name)
        if child.name not in names:
            problems.append("%s: the XSD's %s has no such element" % (child_path, type_name))
            continue
        index = names.index(child.name)
        _, child_type, least, most = kids[index]
        if index < last:
            problems.append("%s: declared out of the XSD's order" % child_path)
        last = index
        if not choice and (child.min, child.max) != (least, most):
            problems.append("%s: declared %s..%s; the XSD says %s..%s"
                            % (child_path, child.min, child.max or "n", least, most or "n"))
        _compare(child, child_type, types, child_path, problems)


def _length(simple_type):
    """The longest a value of this type may be, from maxLength or a UUID pattern."""
    restriction = simple_type.find(XS + "restriction")
    if restriction is None:
        return None
    limit = restriction.find(XS + "maxLength")
    if limit is not None:
        return int(limit.get("value"))
    pattern = restriction.find(XS + "pattern")
    if pattern is not None and pattern.get("value").startswith("[a-f0-9]{8}-"):
        return 36
    return None


# -- 2. files against the XSD ---------------------------------------------------

def message_of(path_or_text, is_text=False):
    text = path_or_text if is_text else open(path_or_text, encoding="utf-8",
                                              errors="replace").read()
    for name in XSDS:
        if schema.NAMESPACE_PREFIX + name in text:
            return name
    return None


def xmllint(xsd_path, xml_path):
    result = subprocess.run(["xmllint", "--noout", "--schema", xsd_path, xml_path],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    return result.returncode == 0, result.stderr.strip()


def files_to_check():
    """(path, must it validate) for every file whose verdict is known."""
    own = [os.path.join(SAMPLES, name) for name in
           ("pain001_four_payments.xml", "pain001_four_payments_001_03.xml")]
    valid = sorted(glob.glob(os.path.join(SAMPLES, "external", "*.xml")))
    invalid = sorted(glob.glob(os.path.join(SAMPLES, "external", "invalid", "*.xml")))
    return [(p, True) for p in own + valid] + [(p, False) for p in invalid]


def written_by_the_mock():
    """(label, XML) for every message a running mock writes for the sample:
    its status report and a DUPL rejection, the debit notification, and the
    statements for the days an advance crosses."""
    from mockbank.server import Config, make_server
    httpd = make_server(Config(port=0, quiet=True, clock="2026-10-01T09:00"))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]

    def call(method, path, body=None):
        request = urllib.request.Request(base + path, data=body, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            return error.read()
    try:
        with open(os.path.join(SAMPLES, "pain001_four_payments.xml"), "rb") as handle:
            sample = handle.read()
        call("POST", "/payments", sample)
        call("POST", "/payments", sample)                 # DUPL: a rejection
        call("POST", "/_mock/advance?to=2026-10-06")
        messages = json.loads(call("GET", "/_mock/mailbox").decode("utf-8"))
    finally:
        httpd.shutdown()
        httpd.server_close()
    return [("%s #%d for %s" % (m["type"], m["id"], m["account"]), m["body"])
            for m in messages]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--require", action="store_true",
                        help="fail, rather than skip, without the network or xmllint")
    args = parser.parse_args()
    problems, skipped, checked = [], [], 0

    paths = {name: fetch(name) for name in XSDS}
    missing = sorted(name for name, path in paths.items() if path is None)
    if missing:
        # URL and hash both, so a re-pin is copy and paste.
        skipped.append("could not fetch the XSD for %s:\n%s" % (", ".join(missing), "\n".join(
            "      %s\n        from %s\n        sha256 %s" % (name, XSDS[name][0], XSDS[name][1])
            for name in missing)))

    for name, message in schema.MESSAGES.items():
        if paths.get(name):
            found = compare(message, paths[name])
            checked += 1
            problems += ["%s %s" % (name, p) for p in found]

    if shutil.which("xmllint") is None:
        skipped.append("xmllint is not on the path, so no file was validated")
    else:
        for path, should_pass in files_to_check():
            name = message_of(path)
            if not paths.get(name):
                continue
            passed, detail = xmllint(paths[name], path)
            checked += 1
            if passed != should_pass:
                problems.append("%s %s the XSD, and it should %s:\n      %s"
                                % (os.path.relpath(path, ROOT),
                                   "passes" if passed else "fails",
                                   "not" if passed else "pass", detail[-400:]))
        written = written_by_the_mock()
        if not written:
            problems.append("the mock wrote nothing to validate")
        for label, body in written:
            name = message_of(body, is_text=True)
            if not paths.get(name):
                continue
            with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False,
                                             encoding="utf-8") as handle:
                handle.write(body)
            try:
                passed, detail = xmllint(paths[name], handle.name)
            finally:
                os.unlink(handle.name)
            checked += 1
            if not passed:
                problems.append("the mock wrote a %s that fails the XSD:\n      %s"
                                % (label, detail[-400:]))

    for note in skipped:
        print("skipped: %s" % note)
    if problems:
        print("the dictionary or the mock's output departs from the published XSDs:\n")
        for problem in problems:
            print("  - %s" % problem)
        print("\n%d problem(s)." % len(problems))
        return 1
    if skipped and args.require:
        print("--require was given and something was skipped.")
        return 1
    print("%d check(s) against the published XSDs, and all agree." % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
