#!/usr/bin/env python3
"""Cut a release in one step: assemble, tag, push, publish.

0.1.0's release commit was merged by one session and stopped there. `main` said
0.1.0 with no tag and no GitHub Release until the maintainer tagged it by hand
hours later; mock-edi had the same with 0.4.0, where PyPI served the previous
version the whole time. The steps were written down in `CONTRIBUTING.md` and
nothing made them happen together. This is the thing that makes them happen
together, and `tools/check_release.py` is the thing that notices when they did
not.

Run by the maintainer on a clean `main` after the release pull request has
merged:

    python3 tools/release.py 0.2.0 --dry-run     # print every step, do nothing
    python3 tools/release.py 0.2.0

**It refuses before it does anything**, and every refusal is a thing that has
gone wrong in a real release somewhere:

- the working tree is dirty, so the tag would not describe what was tested;
- HEAD is not `main`, or is detached;
- `main` and `origin/main` disagree, so the tag would name a commit nobody else
  has, or miss one they do;
- `pyproject.toml` does not say VERSION;
- `CHANGELOG.md` has no dated section for VERSION;
- fragments are still waiting in `changelog.d/`, so the release notes would be
  missing an entry somebody wrote;
- CI on the commit about to be tagged is not green.

Then it creates the annotated tag, pushes it, and publishes the GitHub Release
from it - which is what uploads to PyPI. Publishing is last on purpose: it is
the irreversible step, and everything that can be checked is checked before it.

Standard library, `git` and `gh`. Nothing else.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG = "CHANGELOG.md"
PYPROJECT = "pyproject.toml"
FRAGMENTS = "changelog.d"
BRANCH = "main"

HEADING = re.compile(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?\s*$", re.M)


class Refused(Exception):
    """A reason not to release, phrased for the person holding the terminal."""


def git(*args, **kwargs):
    return subprocess.run(["git"] + list(args), cwd=ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          **kwargs)


def out(*args) -> str:
    result = git(*args)
    if result.returncode != 0:
        raise Refused("`git %s` failed: %s"
                      % (" ".join(args), result.stderr.decode("utf-8").strip()))
    return result.stdout.decode("utf-8").strip()


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def check_tree_is_releasable(version: str):
    """Everything that must be true before a tag is written. Raises Refused."""
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        raise Refused("%r is not a version like 0.2.0." % version)

    dirty = out("status", "--porcelain")
    if dirty:
        raise Refused(
            "the working tree is not clean, so the tag would not describe what "
            "was tested:\n%s"
            % "\n".join("      %s" % line for line in dirty.splitlines()[:10]))

    branch = out("rev-parse", "--abbrev-ref", "HEAD")
    if branch != BRANCH:
        raise Refused(
            "HEAD is %s, not %s. A release is cut from %s, after the release "
            "pull request has merged."
            % ("detached" if branch == "HEAD" else "`%s`" % branch, BRANCH, BRANCH))

    git("fetch", "--quiet", "origin", BRANCH)
    local = out("rev-parse", "HEAD")
    try:
        remote = out("rev-parse", "origin/%s" % BRANCH)
    except Refused:
        raise Refused("there is no `origin/%s` to compare against." % BRANCH)
    if local != remote:
        ahead = out("rev-list", "--count", "origin/%s..HEAD" % BRANCH)
        behind = out("rev-list", "--count", "HEAD..origin/%s" % BRANCH)
        raise Refused(
            "%s and origin/%s disagree (%s commit(s) ahead, %s behind). The tag "
            "has to name the commit everybody else has."
            % (BRANCH, BRANCH, ahead, behind))

    declared = re.search(r'^version = "([^"]+)"', read(PYPROJECT), re.M)
    if not declared or declared.group(1) != version:
        raise Refused(
            "%s says %s, not %s. The version bump is part of the release pull "
            "request, not of this step."
            % (PYPROJECT, declared.group(1) if declared else "nothing", version))

    dated = [f.group(2) for f in HEADING.finditer(read(CHANGELOG))
             if f.group(1) == version]
    if not dated:
        raise Refused(
            "%s has no section for [%s]. Assemble it first:\n"
            "      python3 tools/check_changelog.py --assemble %s"
            % (CHANGELOG, version, version))
    if not dated[0]:
        raise Refused(
            "%s's [%s] section has no date; a released section is "
            "`## [%s] - YYYY-MM-DD`." % (CHANGELOG, version, version))

    waiting = sorted(name for name in os.listdir(os.path.join(ROOT, FRAGMENTS))
                     if re.match(r"^\d+\.[a-z]+\.md$", name)) \
        if os.path.isdir(os.path.join(ROOT, FRAGMENTS)) else []
    if waiting:
        raise Refused(
            "%d entr%s still waiting in %s/, so the release notes would be "
            "missing what somebody wrote:\n%s\n    Assemble them into [%s] or "
            "move them to the next release."
            % (len(waiting), "y is" if len(waiting) == 1 else "ies are",
               FRAGMENTS, "\n".join("      %s/%s" % (FRAGMENTS, n) for n in waiting),
               version))

    return local, dated[0]


def check_ci_is_green(commit: str):
    """Refuse a tag on a commit CI has not passed. Raises Refused."""
    result = subprocess.run(
        ["gh", "api", "repos/{owner}/{repo}/commits/%s/status" % commit,
         "--jq", ".state"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise Refused(
            "could not ask GitHub whether CI is green on %s: %s\n    Releasing "
            "without knowing is not better than not releasing."
            % (commit[:8], result.stderr.decode("utf-8").strip()))
    state = result.stdout.decode("utf-8").strip()

    # A repository whose checks are all Actions reports "" here, because the
    # combined-status API only knows about commit statuses. Ask the check runs.
    if state in ("", "null"):
        runs = subprocess.run(
            ["gh", "api", "repos/{owner}/{repo}/commits/%s/check-runs" % commit,
             "--jq", "[.check_runs[] | .conclusion] | @csv"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if runs.returncode != 0:
            raise Refused("could not ask GitHub for the check runs on %s: %s"
                          % (commit[:8], runs.stderr.decode("utf-8").strip()))
        listed = [c.strip('"') for c in
                  runs.stdout.decode("utf-8").strip().split(",") if c.strip('"')]
        if not listed:
            raise Refused(
                "GitHub reports no checks at all on %s. A release is tagged on a "
                "commit CI has passed, and this one has not been tested."
                % commit[:8])
        bad = sorted({c for c in listed if c != "success"})
        if bad:
            raise Refused(
                "CI on %s is not green: %s. Tagging it would publish something "
                "the tests did not pass." % (commit[:8], ", ".join(bad)))
        return "%d check(s) passed" % len(listed)

    if state != "success":
        raise Refused(
            "CI on %s is `%s`, not `success`. Tagging it would publish "
            "something the tests did not pass." % (commit[:8], state))
    return "commit status is success"


def run(step: str, command, dry_run: bool):
    print("  %s %s" % ("would" if dry_run else "doing:", step))
    print("      $ %s" % " ".join(command))
    if dry_run:
        return
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        raise Refused("`%s` failed. Nothing after this step has run."
                      % " ".join(command))


def already_done(tag: str):
    """(tagged, released) for this tag, so a finished release is a no-op."""
    tagged = git("rev-parse", "-q", "--verify",
                 "refs/tags/%s" % tag).returncode == 0
    released = subprocess.run(["gh", "release", "view", tag], cwd=ROOT,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    return tagged, released


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="the version to release, e.g. 0.2.0")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every step in order and do none of them")
    args = parser.parse_args()
    version, tag = args.version, "v%s" % args.version

    try:
        if not re.match(r"^\d+\.\d+\.\d+$", version):
            raise Refused("%r is not a version like 0.2.0." % version)

        # Whether this release is finished is asked first, and answered from the
        # tag and the Release rather than from the tree. A finished release stays
        # finished however the working copy looks - in particular, fragments
        # waiting for the *next* version are not a reason to re-examine a
        # released one, and refusing there would make re-running this on an old
        # version report the wrong problem.
        tagged, released = already_done(tag)
        if tagged and released:
            print("%s is already tagged and its GitHub Release is published; "
                  "there is nothing to do." % tag)
            return 0

        commit, date = check_tree_is_releasable(version)
        print("releasing %s (%s), dated %s in %s%s"
              % (version, commit[:8], date, CHANGELOG,
                 " - dry run, nothing will change" if args.dry_run else ""))
        print("  checked: tree clean, on %s, level with origin/%s, %s says %s, "
              "[%s] dated, no fragments waiting"
              % (BRANCH, BRANCH, PYPROJECT, version, version))
        print("  checked: %s" % check_ci_is_green(commit))

        if tagged:
            print("  %s already exists; not writing it again." % tag)
        else:
            run("create the annotated tag", ["git", "tag", "-a", tag, "-m",
                                             "mock-bank %s" % version],
                args.dry_run)
            run("push the tag", ["git", "push", "origin", tag], args.dry_run)
        if released:
            print("  the GitHub Release for %s already exists; leaving it." % tag)
        else:
            run("publish the GitHub Release, which uploads to PyPI",
                ["gh", "release", "create", tag, "--title", version,
                 "--generate-notes"], args.dry_run)
    except Refused as refusal:
        print("not releasing %s:\n\n  - %s" % (version, refusal))
        return 1

    if args.dry_run:
        print("\nnothing was changed. Run it again without --dry-run to release.")
    else:
        print("\n%s is released. `python3 tools/check_release.py` agrees or says "
              "why not." % version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
