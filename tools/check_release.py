#!/usr/bin/env python3
"""Notice a release that stopped half way.

0.1.0 was merged by one session and stopped there: `main` said 0.1.0, with no
tag and no GitHub Release, until the maintainer tagged it by hand hours later.
mock-edi had the same thing happen with 0.4.0, where PyPI served the previous
version the whole time. Nothing was broken, no test failed, and nobody found
out from the repository - which is the part this fixes.

A release is three things that have to agree:

1. `pyproject.toml` and a dated section in `CHANGELOG.md` say the version,
2. a tag `vVERSION` points at a commit,
3. a published GitHub Release exists for that tag, which is what triggers the
   PyPI upload.

This passes when all three agree, and when the version `main` claims has **no**
dated section yet - which is the ordinary state between releases, where
`[Unreleased]` is a pointer at `changelog.d/` and there is nothing to finish.
It fails, naming what is missing, when `main` carries a dated release with no
tag, or a tag with no published Release.

It is deliberately not run on push: on the release commit itself the tag cannot
exist yet, so a push trigger would fail every release by design. A daily run and
a button are what suit a question whose answer changes by someone forgetting.

Asking (3) needs `gh` and a network. Without them this skips, says so, and
exits 0 - except under `--require`, which CI passes, where a skip is a failure.
A watchdog that passes because it could not ask is worse than no watchdog.

    python3 tools/check_release.py
    python3 tools/check_release.py --require
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG = "CHANGELOG.md"
PYPROJECT = "pyproject.toml"

HEADING = re.compile(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?\s*$", re.M)


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def declared_version() -> str:
    found = re.search(r'^version = "([^"]+)"', read(PYPROJECT), re.M)
    return found.group(1) if found else ""


def dated_section(version: str) -> str:
    """The date `CHANGELOG.md` gives this version, or "" when it has no section."""
    for found in HEADING.finditer(read(CHANGELOG)):
        if found.group(1) == version:
            return found.group(2) or ""
    return ""


def tag_exists(tag: str) -> bool:
    """Whether the tag is here, fetching tags once first.

    A CI checkout is usually made without them, and "no tag" is exactly what
    this script is looking for, so the difference matters more here than
    anywhere else: reporting a missing release because the clone was shallow
    would be a false alarm every single run.
    """
    for attempt in (0, 1):
        if subprocess.call(["git", "rev-parse", "-q", "--verify",
                            "refs/tags/%s" % tag], cwd=ROOT,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0:
            return True
        if attempt == 0:
            subprocess.call(["git", "fetch", "--tags", "--quiet", "origin"],
                            cwd=ROOT, stderr=subprocess.DEVNULL)
    return False


def gh_available() -> bool:
    try:
        return subprocess.call(["gh", "auth", "status"], cwd=ROOT,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def repo_resolves() -> bool:
    """Whether `gh` can tell which GitHub repository this checkout is.

    Asked separately, because `gh release view` fails the same way whether the
    Release is missing or the repository could not be worked out at all - no
    remote, a remote that is not GitHub, a detached checkout. Reading the second
    as the first would report a half-finished release on a tree that simply had
    nothing to ask, which is a false alarm in the one place a false alarm does
    the most damage: a watchdog people learn to ignore.
    """
    try:
        return subprocess.call(["gh", "repo", "view", "--json", "name"],
                               cwd=ROOT, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def published_release(tag: str):
    """(exists, is_draft) for the GitHub Release on this tag; None when unasked."""
    if not repo_resolves():
        return None
    try:
        out = subprocess.run(
            ["gh", "release", "view", tag, "--json", "isDraft,publishedAt"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    if out.returncode != 0:
        return (False, False)
    try:
        answer = json.loads(out.stdout.decode("utf-8"))
    except ValueError:
        return None
    return (True, bool(answer.get("isDraft")) or not answer.get("publishedAt"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--require", action="store_true",
                        help="a skip is a failure; CI passes this")
    args = parser.parse_args()

    version = declared_version()
    if not version:
        print("%s has no `version`, so there is nothing to check." % PYPROJECT)
        return 1
    tag = "v%s" % version

    date = dated_section(version)
    if not date:
        print("%s says %s and %s has no dated section for it yet, so %s is "
              "unreleased and there is nothing half-finished. This is the "
              "ordinary state between releases."
              % (PYPROJECT, version, CHANGELOG, version))
        return 0

    problems = []
    if not tag_exists(tag):
        problems.append(
            "there is no tag %s. %s says %s and %s dates it %s, so the release "
            "was merged and stopped there - `main` claims a version that has no "
            "tag and no published artefact. Finish it with "
            "`python3 tools/release.py %s`."
            % (tag, PYPROJECT, version, CHANGELOG, date, version))
    else:
        found = published_release(tag)
        if found is None:
            message = ("%s is tagged, but whether its GitHub Release is "
                       "published could not be asked: `gh` is unavailable, or "
                       "this checkout has no GitHub repository it can resolve."
                       % tag)
            if args.require:
                print("%s\n\n--require was given, so this is a failure: a check "
                      "that passes because it could not ask is worse than none."
                      % message)
                return 1
            print("%s\nSkipping that half; pass --require to make this a failure."
                  % message)
            return 0
        exists, draft = found
        if not exists:
            problems.append(
                "the tag %s exists but there is no GitHub Release for it. "
                "Publishing the Release is what uploads to PyPI, so until it "
                "exists the index still serves the previous version. Run "
                "`gh release create %s --generate-notes`." % (tag, tag))
        elif draft:
            problems.append(
                "the GitHub Release for %s is a draft. A draft does not trigger "
                "the publish workflow, so PyPI still serves the previous "
                "version. Publish it." % tag)

    if problems:
        print("this release stopped half way:\n")
        for problem in problems:
            print("  - %s" % problem)
        print("\n%d problem(s)." % len(problems))
        return 1

    print("%s is released and finished: %s dates it %s, the tag %s is here, and "
          "its GitHub Release is published." % (version, CHANGELOG, date, tag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
