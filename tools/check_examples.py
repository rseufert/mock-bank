#!/usr/bin/env python3
"""Hold this repository's copies of other projects' examples to their originals.

The worked example using all three mocks composes two pieces: `payment_run`,
which lives here, and `invoice_check`, which lives in mock-sap.  Example code is not importable across repositories - no
wheel carries `examples`, this project's included - so the choice is between
forking the other project's logic and carrying a copy of it.

A fork is the worse option: `InvoiceCheck.problems()` is tested code in mock-sap,
run there against both mocks on every push, and a paraphrase of it here would be
a second implementation nobody tests.  So the file is copied byte-for-byte, and
this is what stops the copy quietly ceasing to be one.  **The drift originates in
another repository**, which is why nothing already here could notice it: no
commit to this project is involved when mock-sap changes its example.

    python3 tools/check_examples.py             # compare, and diff what differs
    python3 tools/check_examples.py --update     # fetch theirs over ours
    python3 tools/check_examples.py --require    # a skipped fetch is a failure

Each copy is compared against the **tip** of its source repository's default
branch, not a pinned commit.  That is deliberate, and it is the opposite of what
`check_xsd.py` does: ISO's schemas are pinned so they cannot move under us,
whereas here a run that passes today and fails tomorrow with nothing changed in
this repository is the entire point.  `--update` is then the fix, and the diff
says what moved.

It follows `check_xsd.py` in the part that matters for a contributor: with no
network the fetch is skipped rather than failed, so being offline does not block
work that has nothing to do with the copies, and `--require` turns a skip into a
failure, which is how CI runs it.

Standard library only, like the project it checks.
"""
from __future__ import annotations

import argparse
import difflib
import sys
import time
import urllib.error
import urllib.request

RAW = "https://raw.githubusercontent.com/rseufert/%s/%s/%s"
BRANCH = "main"

# A transient failure fetching from GitHub used to be indistinguishable from a
# copy that had drifted, and a check people learn to re-run without reading is
# worse than no check - the same reasoning as ATTEMPTS in check_xsd.py.
ATTEMPTS = 3
PAUSE = 0.5

# local path -> (repository, path within it). The local name matches the
# original's on purpose, so a reader looking for the original knows where to
# look - and `invoice_check.py` has no imports of its own beyond the standard
# library, which is why making the examples a package (#169) left this copy
# byte-identical. `procure_to_pay.py` reaches it as `from . import invoice_check`
# now rather than as a flat import; the copy is held to the source, not the way
# it is imported.
COPIES = {
    "examples/invoice_check.py": ("mock-sap", "examples/invoice_check.py"),
}


def fetch(repo: str, path: str, attempts: int = ATTEMPTS, pause: float = PAUSE):
    """The source file's text, or None if the network would not give it.

    None means "could not look", not "does not match". The caller decides
    whether that is a skip or a failure, because a contributor offline and CI
    want opposite answers.
    """
    url = RAW % (repo, BRANCH, path)
    for attempt in range(attempts):
        if attempt:
            time.sleep(pause * 2 ** (attempt - 1))
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            # A 404 is not a flaky network: the path moved or was deleted in the
            # other repository, and retrying will not find it. Say so at once.
            if error.code == 404:
                raise SystemExit(
                    "%s does not exist. The original moved or was deleted; this "
                    "check needs its new path, not a retry." % url)
            continue
        except (urllib.error.URLError, OSError):
            continue
    return None


def read(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except IOError:
        return None


def compare(local: str, repo: str, remote: str, update: bool):
    """(state, detail) for one copy: matched, updated, drifted, missing or skipped."""
    theirs = fetch(repo, remote)
    if theirs is None:
        return "skipped", "%s: could not read %s/%s" % (local, repo, remote)

    ours = read(local)
    if ours is None:
        if update:
            with open(local, "w", encoding="utf-8") as handle:
                handle.write(theirs)
            return "updated", "%s <- %s/%s (created)" % (local, repo, remote)
        return "missing", ("%s is registered as a copy of %s/%s and is not here"
                           % (local, repo, remote))

    if ours == theirs:
        return "matched", local

    if update:
        with open(local, "w", encoding="utf-8") as handle:
            handle.write(theirs)
        return "updated", "%s <- %s/%s" % (local, repo, remote)

    diff = "".join(difflib.unified_diff(
        theirs.splitlines(keepends=True), ours.splitlines(keepends=True),
        fromfile="%s/%s@%s" % (repo, remote, BRANCH), tofile=local))
    return "drifted", "%s has drifted from %s/%s@%s:\n%s" % (
        local, repo, remote, BRANCH, diff)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check this repository's copies of other projects' examples.")
    parser.add_argument("--update", action="store_true",
                        help="overwrite our copies with theirs")
    parser.add_argument("--require", action="store_true",
                        help="a copy that could not be fetched is a failure")
    args = parser.parse_args(argv)

    results = [(state, detail) + (local,)
               for local, (repo, remote) in sorted(COPIES.items())
               for state, detail in [compare(local, repo, remote, args.update)]]

    for state, detail, _ in results:
        if state == "updated":
            print("updated %s" % detail)

    bad = [d for state, d, _ in results if state in ("drifted", "missing")]
    skipped = [d for state, d, _ in results if state == "skipped"]

    for detail in bad:
        print(detail, file=sys.stderr)
    for detail in skipped:
        print("skipped: %s" % detail, file=sys.stderr)

    if bad:
        print("\n%d of %d copies need attention. `python3 tools/check_examples.py "
              "--update` takes the other repository's version."
              % (len(bad), len(COPIES)), file=sys.stderr)
        return 1
    if skipped and args.require:
        print("\n--require was given and %d of %d copies could not be fetched."
              % (len(skipped), len(COPIES)), file=sys.stderr)
        return 1
    if not any(state == "updated" for state, _, _ in results) and not skipped:
        print("All %d example copies match their source repositories." % len(COPIES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
