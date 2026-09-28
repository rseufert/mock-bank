#!/usr/bin/env python3
"""Guard CHANGELOG.md against drift, and keep entries out of each other's way.

The second one is why this exists. Two pull requests that each added a bullet
under `## [Unreleased]` conflicted on the same lines, and resolving that
conflict by hand is one keystroke away from keeping one side and dropping the
other. This check came to mock-bank from its sibling project mock-sap, where
exactly that happened: an entry went missing in a merge and came back two
commits later by luck rather than by a check.

Guarding the resolution was the first answer. Not needing one is the better
answer, so an entry is now **its own file** under `changelog.d/`, named
`<issue>.<kind>.md`, holding the bullet's text exactly as it used to be
written. Two pull requests add two different files and never touch the same
line, so the conflict does not happen and nothing has to be resolved
correctly. `--assemble` writes them into the file at release time.

What is checked:

1. **Structure.** Every released heading has a link reference and every
   reference a heading; versions descend; `[Unreleased]` is present and its
   compare link names the newest release; `pyproject.toml` agrees with the
   newest released heading.
2. **Released sections are history.** Once a version is released its section
   is frozen: a change to it is either a mistake or a rewrite of the past.
3. **`[Unreleased]` holds the pointer and nothing else.** This one exists
   because of a *clean* merge: a branch replaced that section with a pointer
   while `main` added an entry to it, git merged the two without a marker, and
   the file then said "Nothing is added here by hand" above an entry added
   there by hand. No conflict marker shows that and no diff review does either,
   because both sides were right on their own. So it is asked directly, and the
   answer names the fragment file the entry belongs in.
4. **Fragments are well formed.** The name carries an issue number and one of
   the kinds Keep a Changelog defines, and optionally a step - `42.added.md` or
   `42.added.part-b.md` - so an issue that ships in more than one pull request
   has somewhere to write its second entry. The body is not empty. A fragment
   named wrongly is refused by name rather than silently left out of the
   release, which is the failure that matters: it looks like an entry, it sits
   in the right directory, and it would vanish at assembly.
5. **A change to the package brings a fragment.** A pull request that touches
   `mockbank/` adds at least one entry under `changelog.d/`, or cuts a release.
   An entry counts as added when its file is new **or its body changed**, so
   appending to an existing fragment satisfies this too.
6. **An entry already waiting for a release does not lose text.** A fragment
   `--base` already had, whose body no longer carries lines it used to, is
   reported with what went. Appending to one, reordering it or re-indenting it
   is not a loss and is not reported.

   (5) and (6) are two halves of one mistake, and #116 exists because (5)
   without (6) let it through. #57 shipped in two parts; part a had taken
   `57.added.md` and `57.fixed.md`; part b needed both kinds, found both names
   in use, and overwrote them. That deleted twenty lines describing the feature
   and **passed this check**, because (5) asked whether a filename was new and
   the filenames had not changed. The step in (4) is what part b should have
   used, and (6) is what would have said so.

Both (5) and (6) are lifted by the `no changelog` label, which only the
maintainer applies: for (5) when a change genuinely needs no entry - a comment,
a rename, a pure refactor - and for (6) when rewriting somebody else's waiting
entry is the intention rather than the accident.

(2) and (5) need something to compare against, so they run only when `--base`
names a revision this checkout has; CI passes the pull request's base. Run it
with no arguments and you get (1), (3) and (4).

    python3 tools/check_changelog.py
    python3 tools/check_changelog.py --base origin/main
    python3 tools/check_changelog.py --assemble 0.2.0 --date 2026-10-01
"""
from __future__ import annotations

import argparse
import collections
import datetime
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG = "CHANGELOG.md"
PYPROJECT = "pyproject.toml"
PACKAGE = "mockbank/"
ESCAPE_HATCH = "no changelog"
FRAGMENTS = "changelog.d"

# What `## [Unreleased]` holds, exactly. Kept here rather than inferred, so the
# check can decide by comparison instead of by hunting for things that look
# wrong: prose, a numbered list or a `####` heading are all as wrong as a bullet
# and none of them look it. Rewording the paragraph in CHANGELOG.md means
# rewording it here; the check says so when they disagree.
POINTER = """Entries for the next release are one file each in
[`changelog.d/`](changelog.d/), so that two pull requests adding an entry do not
conflict on the same lines of this file. `tools/check_changelog.py --assemble`
writes them into this section at release time. Nothing is added here by hand."""

# The kinds Keep a Changelog defines, all six, in the order a release section
# lists them. A kind not in here is refused rather than assembled under a
# heading nobody reads. All six from the start, including the two this project
# has not needed yet, because adding one later is free but a contributor who
# reached for `security` and was refused would have written it as `fixed`.
KINDS = ("added", "changed", "deprecated", "removed", "fixed", "security")

# `<issue>.<kind>.md`, and optionally `<issue>.<kind>.<step>.md`.
#
# The step exists because an issue large enough for rule 11 to split is an issue
# whose second pull request needs somewhere to write. #57 shipped in two parts;
# part a had taken `57.added.md` and `57.fixed.md`, part b needed both kinds, and
# the only names available were the ones already in use. Overwriting them deleted
# twenty lines of part a's entry, and nothing objected - rule 5 asks whether a
# *filename* is new, so a replaced body reads to it as no entry at all.
#
# So `57.added.part-b.md` is issue 57, kind `added`, step `part-b`. A step is
# lowercase letters, digits and hyphens: enough to say which step, not enough to
# hide a kind or an issue number in.
FRAGMENT_NAME = re.compile(r"^(\d+)\.([a-z]+)(?:\.([a-z0-9][a-z0-9-]*))?\.md$")

HEADING = re.compile(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?\s*$")
LINK = re.compile(r"^\[([^\]]+)\]:\s*(\S+)\s*$")
BULLET = re.compile(r"^\s*[-*] ")


def _read(path: str, rev: str = "") -> str:
    """The file as it is now, or as it was at `rev`; "" when it was not there."""
    if not rev:
        with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
            return handle.read()
    try:
        return subprocess.check_output(
            ["git", "show", "%s:%s" % (rev, path)], cwd=ROOT,
            stderr=subprocess.DEVNULL).decode("utf-8")
    except subprocess.CalledProcessError:
        return ""


def fragment_dir() -> str:
    return os.path.join(ROOT, FRAGMENTS)


def fragments():
    """Every fragment as (issue, kind, body, name), ordered as a release lists it.

    Grouped by kind in the order `KINDS` gives, then by issue number as a number
    so #9 comes before #10 rather than after it, then by step so an issue's parts
    read in the order they were written. A fragment with no step comes before its
    issue's steps, which is what an issue split after the fact looks like: the
    first part took the bare name.
    """
    out = []
    directory = fragment_dir()
    if not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        if name.startswith(".") or name == "README.md":
            continue
        found = FRAGMENT_NAME.match(name)
        if not found:
            continue
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            body = handle.read().strip()
        out.append((int(found.group(1)), found.group(2), body, name))
    return sorted(out, key=_order)


def _order(row):
    """(kind, issue, step) - the order a release section lists entries in."""
    issue, kind, _, name = row
    found = FRAGMENT_NAME.match(name)
    step = found.group(3) or ""
    return (KINDS.index(kind) if kind in KINDS else len(KINDS), issue, step)


def check_fragments():
    """Refuse a fragment by name rather than let it vanish at assembly."""
    problems = []
    directory = fragment_dir()
    if not os.path.isdir(directory):
        return problems
    for name in sorted(os.listdir(directory)):
        if name.startswith(".") or name == "README.md":
            continue
        path = "%s/%s" % (FRAGMENTS, name)
        found = FRAGMENT_NAME.match(name)
        if not found:
            problems.append(
                "%s is not a fragment name; it is `<issue>.<kind>.md`, or "
                "`<issue>.<kind>.<step>.md` when an issue ships in more than one "
                "pull request, where kind is one of %s and a step is lowercase "
                "letters, digits and hyphens - for example `%s/42.added.md` or "
                "`%s/42.added.part-b.md`"
                % (path, ", ".join(KINDS), FRAGMENTS, FRAGMENTS))
            continue
        if found.group(2) not in KINDS:
            problems.append(
                "%s has the kind `%s`, which is not one of %s"
                % (path, found.group(2), ", ".join(KINDS)))
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            if not handle.read().strip():
                problems.append(
                    "%s is empty; it holds the entry's text, written as a "
                    "changelog bullet is written" % path)
    return problems


def sections(text):
    """The changelog as [(version, date, body)], in the order it is written."""
    out = []
    version = date = None
    body = []
    for line in text.splitlines():
        found = HEADING.match(line)
        if found:
            if version is not None:
                out.append((version, date, "\n".join(body).strip()))
            version, date = found.group(1), found.group(2)
            body = []
        elif version is not None and not LINK.match(line):
            body.append(line)
    if version is not None:
        out.append((version, date, "\n".join(body).strip()))
    return out


def bullets(body: str):
    """The entries of a section, whitespace collapsed so re-wrapping is not a change."""
    out = []
    current = ""
    for line in body.splitlines():
        if BULLET.match(line):
            if current:
                out.append(" ".join(current.split()))
            current = BULLET.sub("", line)
        elif current and line.strip():
            current += " " + line
        elif current:
            out.append(" ".join(current.split()))
            current = ""
    if current:
        out.append(" ".join(current.split()))
    return out


def _version_key(version: str):
    return tuple(int(part) for part in version.split("."))


def _changed_in_package(base: str):
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", "%s...HEAD" % base], cwd=ROOT).decode("utf-8")
    return sorted(name for name in changed.split() if name.startswith(PACKAGE))


def _resolve(base: str) -> str:
    """The base commit, fetching it first if this is a shallow or partial clone."""
    for attempt in (0, 1):
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "--verify", "--quiet", base + "^{commit}"],
                cwd=ROOT, stderr=subprocess.DEVNULL).decode().strip()
        except subprocess.CalledProcessError:
            if attempt:
                return ""
            branch = base.rsplit("/", 1)[-1]
            subprocess.call(
                ["git", "fetch", "--no-tags", "--quiet", "origin",
                 "%s:refs/remotes/origin/%s" % (branch, branch)],
                cwd=ROOT, stderr=subprocess.DEVNULL)
    return ""


def check_unreleased_is_a_pointer(text: str):
    """[Unreleased] holds the pointer and nothing else.

    This is the check that exists because of a *clean* merge. While this change
    was being written, `main` added an entry under `## [Unreleased]` and this
    branch replaced that section with a pointer at `changelog.d/`. Git merged
    the two without a conflict marker, and the result was a file that said
    "Nothing is added here by hand" directly above an entry added there by hand.

    Nothing about that is visible in a diff review, and no conflict marker will
    ever show it: both sides were edited in different places, so git was right.
    So it is asked directly. It also catches the plainer case of somebody
    writing a bullet where they have always written one, and tells them the file
    to write instead rather than only that they are wrong.
    """
    problems = []
    body = dict((version, section) for version, _, section in sections(text))
    unreleased = body.get("Unreleased", "")

    if " ".join(unreleased.split()) == " ".join(POINTER.split()):
        return problems       # whitespace-insensitive, so re-wrapping is not a change

    # It differs. Say how, in the terms the writer will recognise, and fall back
    # to naming the difference itself when nothing recognisable is there.
    kind = ""
    for line in unreleased.splitlines():
        if line.startswith("### "):
            kind = line[4:].strip().lower()
            problems.append(
                "CHANGELOG.md's [Unreleased] has a `### %s` heading. That section "
                "holds the pointer at %s/ and nothing else; %s writes the headings "
                "at release time." % (line[4:].strip(), FRAGMENTS, "--assemble"))
        elif BULLET.match(line):
            entry = BULLET.sub("", line)
            found = re.search(r"\(#(\d+)\)", entry)
            name = "%s.%s.md" % (found.group(1) if found else "<issue>",
                                 kind or "<kind>")
            problems.append(
                "CHANGELOG.md's [Unreleased] holds an entry: %s\n    Move it to "
                "%s/%s. That section is a pointer now, so an entry written there "
                "is never released - and a merge can put one there without a "
                "conflict." % (_short(entry), FRAGMENTS, name))

    if not problems:
        # Prose, a numbered item, a #### heading: wrong, and nothing about the
        # line says so. Point at the first line that is not in the pointer.
        expected = " ".join(POINTER.split())
        stray = next((line for line in unreleased.splitlines()
                      if line.strip() and " ".join(line.split()) not in expected),
                     "")
        problems.append(
            "CHANGELOG.md's [Unreleased] is not the pointer at %s/ and holds "
            "nothing that looks like an entry either%s\n    That section holds "
            "the pointer paragraph and nothing else. If you meant to reword it, "
            "reword `POINTER` in tools/check_changelog.py to match."
            % (FRAGMENTS, ": %s" % _short(stray) if stray else "."))
    return problems


def check_structure(text: str, pyproject: str):
    problems = []
    parsed = sections(text)
    versions = [v for v, _, _ in parsed]

    if not versions or versions[0] != "Unreleased":
        problems.append("the first section should be `## [Unreleased]`")
    released = [(v, d) for v, d, _ in parsed if v != "Unreleased"]
    for version, date in released:
        if not date:
            problems.append("[%s] has no date; a released section is `## [x.y.z] - YYYY-MM-DD`" % version)
        if not re.match(r"^\d+\.\d+\.\d+$", version):
            problems.append("[%s] is not a version number" % version)

    ordered = [v for v, _ in released if re.match(r"^\d+\.\d+\.\d+$", v)]
    if ordered != sorted(ordered, key=_version_key, reverse=True):
        problems.append("released sections are not newest first: %s" % ", ".join(ordered))

    links = dict(m.groups() for m in (LINK.match(line) for line in text.splitlines()) if m)
    for version in versions:
        if version not in links:
            problems.append("[%s] has no link reference at the foot of the file" % version)
    for name in links:
        if not name.startswith("#") and name not in versions:
            problems.append("[%s] has a link reference but no section" % name)

    if ordered and "Unreleased" in links and not links["Unreleased"].endswith("v%s...HEAD" % ordered[0]):
        problems.append(
            "the [Unreleased] link compares against %s, not the newest release v%s"
            % (links["Unreleased"].rsplit("/", 1)[-1], ordered[0]))

    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    if declared and ordered and declared.group(1) != ordered[0]:
        problems.append(
            "pyproject.toml says %s but the newest released section is [%s]; a release "
            "bumps both in one commit" % (declared.group(1), ordered[0]))
    return problems


def check_against_base(text: str, before: str, base: str, labels=()):
    problems = []
    now = {v: body for v, _, body in sections(text)}
    then = sections(before)

    for version, _, body in then:
        if version == "Unreleased":
            continue
        if version not in now:
            problems.append("[%s] was released and is now missing from the file" % version)
        elif bullets(now[version]) != bullets(body):
            problems.append(
                "[%s] is already released, so its section is history; this changes it" % version)

    # A fragment that was waiting for a release has not been quietly deleted.
    # Assembling a release deletes all of them and writes them into a section,
    # so a fragment that is gone while no new section appeared was dropped.
    gone = _fragments_at(base) - _fragments_now()
    cut = [v for v in now if v != "Unreleased" and v not in {x for x, _, _ in then}]
    if gone and not cut:
        for name in sorted(gone):
            problems.append(
                "%s/%s was waiting for a release and is gone; only --assemble "
                "removes a fragment" % (FRAGMENTS, name))

    # An entry that main already had and whose text has been replaced. Rule 5
    # asks whether a *filename* is new, so a replaced body reads to it as no
    # entry at all - which is how #57 part b passed this check while deleting
    # twenty lines of part a's entry. Appending is allowed and is not reported,
    # because nothing is lost by it; what is reported is text that went.
    before, after = _bodies_at(base), _bodies_now()
    if ESCAPE_HATCH not in labels:
        for name in sorted(set(before) & set(after)):
            lost = _lines_lost(before[name], after[name])
            if not lost:
                continue
            problems.append(
                "%s/%s is an entry %s already had, and this drops %d line(s) of "
                "it, beginning:\n      %s\n    If this is a second entry for the "
                "same issue - the issue shipped in steps - give it its own file, "
                "`<issue>.<kind>.<step>.md`, and leave that one alone. If you do "
                "mean to rewrite an entry that is already waiting for a release, "
                "that is somebody else's paragraph and the `%s` label is what "
                "allows it."
                % (FRAGMENTS, name, _named(base), len(lost), _short(lost[0]),
                   ESCAPE_HATCH))

    package = _changed_in_package(base)
    if package and ESCAPE_HATCH not in labels:
        # A fragment counts as added when its *name* is new or its *body*
        # changed, so appending a step's entry to an existing file satisfies this
        # even though the directory listing is unchanged. Before #116 it did not,
        # and the advice it printed - "add a file named `<issue>.<kind>.md`" -
        # named a file that already existed.
        added = (_fragments_now() - _fragments_at(base)
                 | {name for name in set(before) & set(after)
                    if before[name] != after[name]})
        if not added and not cut:
            problems.append(
                "%s changed without an entry in %s/:\n%s\n    Add a file named "
                "`<issue>.<kind>.md` holding what changed and, where it is not "
                "obvious, why - it is what a user of the published package reads. "
                "One file per entry, so two pull requests never conflict over it. "
                "If the change genuinely needs none - a comment, a rename, a pure "
                "refactor - label the pull request `%s`."
                % (PACKAGE.rstrip("/"), FRAGMENTS,
                   "\n".join("      %s" % name for name in package), ESCAPE_HATCH))
    return problems


def _fragments_now():
    return {name for _, _, _, name in fragments()}


def _bodies_now():
    return {name: body for _, _, body, name in fragments()}


def _bodies_at(rev: str):
    """{fragment name: body} at `rev`, for the two checks that compare content.

    Names alone cannot see a fragment whose text was replaced, which is the whole
    of what #57 part b did wrong and what rule 5 could not notice.
    """
    out = {}
    for name in _fragments_at(rev):
        body = _read("%s/%s" % (FRAGMENTS, name), rev).strip()
        if body:
            out[name] = body
    return out


def _fragments_at(rev: str):
    """The fragment file names present at `rev`."""
    try:
        listed = subprocess.check_output(
            ["git", "ls-tree", "--name-only", "%s:%s" % (rev, FRAGMENTS)],
            cwd=ROOT, stderr=subprocess.DEVNULL).decode("utf-8")
    except subprocess.CalledProcessError:
        return set()          # the directory did not exist yet at that revision
    return {name for name in listed.split() if FRAGMENT_NAME.match(name)}


def _short(entry: str, width: int = 70) -> str:
    return entry if len(entry) <= width else entry[:width - 1] + "…"


def _lines_lost(before: str, after: str):
    """The non-blank lines of `before` that `after` no longer has.

    A multiset of stripped lines rather than a positional diff, so reordering an
    entry or re-indenting it is not a loss and appending to it is not either.
    Reflowing a paragraph *is* a loss by this measure, and that is the right
    answer for text somebody else wrote: from the outside it cannot be told apart
    from replacing it, and the label is there for when it is deliberate.
    """
    gone = collections.Counter(_lines(before))
    gone.subtract(collections.Counter(_lines(after)))
    lost = []
    for line in _lines(before):
        if gone[line] > 0:
            gone[line] -= 1
            lost.append(line)
    return lost


def _lines(text: str):
    return [line.strip() for line in text.splitlines() if line.strip()]


def _named(rev: str) -> str:
    """`rev` as something to read in a sentence.

    A branch name stays as it is; a 40-character SHA becomes seven characters,
    because the message it lands in is prose and the full hash reads as noise.
    """
    return "`%s`" % (rev[:7] if re.match(r"^[0-9a-f]{40}$", rev) else rev)


def assemble(version: str, date: str) -> int:
    """Write the fragments into CHANGELOG.md as a release, and delete them.

    Idempotent, because a release is the one moment nobody wants to run a step
    twice and wonder: with no fragments left and the section already written,
    it says so and changes nothing. That is also what makes it safe for
    `tools/release.py` to call before it tags.
    """
    text = _read(CHANGELOG)
    waiting = fragments()
    heading = "## [%s] - %s" % (version, date)
    # Whether the version is already assembled is a question about the version,
    # not about the date. Comparing the full dated heading made a second run
    # with a different date look like a fresh release with no fragments, which
    # reported "nothing to release" and exited 1 - so a release script calling
    # this twice could not tell "already done" from "something is wrong".
    # `seen_date`, not `date`: the parameter is in scope here and naming the
    # generator's variable after it reads as a rebinding even though a genexp
    # has its own scope.
    present = [seen_date for seen, seen_date, _ in sections(text) if seen == version]
    already = bool(present)
    written = present[0] if already else None

    # A heading for this version with no date is not an assembled release and is
    # not a blank slate either. Treating it as absent wrote a second [VERSION]
    # section above the first, which the structure check then failed on with a
    # message about link references - two steps away from the actual problem.
    if already and not written:
        print("%s already has a `## [%s]` heading with no date, so this would "
              "write a second section for the same version. Give that one a date "
              "(`## [%s] - YYYY-MM-DD`) or remove it, then assemble."
              % (CHANGELOG, version, version))
        return 1

    if not waiting:
        if already:
            print("[%s] is already assembled and no fragments are left; nothing to do."
                  % version)
            if written != date:
                print("  note: it is dated %s, not the %s you gave. The date in the "
                      "file is the one that shipped; nothing was changed."
                      % (written, date))
            return 0
        print("no fragments in %s/, so there is nothing to release under [%s]."
              % (FRAGMENTS, version))
        return 1
    if already:
        print("[%s] is already in %s but %d fragment(s) are still in %s/. Assembling "
              "again would write the section twice; move or delete them first."
              % (version, CHANGELOG, len(waiting), FRAGMENTS))
        return 1

    body = [heading, ""]
    for kind in KINDS:
        entries = [(issue, frag) for issue, k, frag, _ in waiting if k == kind]
        if not entries:
            continue
        body.append("### %s" % kind.capitalize())
        body.append("")
        for _, frag in entries:
            lines = frag.splitlines()
            body.append("- %s" % lines[0])
            body.extend("  %s" % line if line else "" for line in lines[1:])
            body.append("")

    marker = "## [Unreleased]"
    start = text.index(marker)
    after = text.index("\n## [", start + len(marker)) + 1
    unreleased = text[start:after]
    text = text[:after] + "\n".join(body) + "\n" + text[after:]

    # The two link references: [Unreleased] now compares against this version,
    # and this version compares against the one before it.
    links = dict(m.groups() for m in
                 (LINK.match(line) for line in text.splitlines()) if m)
    previous = [v for v, _, _ in sections(text)
                if v not in ("Unreleased", version)
                and re.match(r"^\d+\.\d+\.\d+$", v)]
    base_url = links.get("Unreleased", "").rsplit("/compare/", 1)[0]
    text = re.sub(r"^\[Unreleased\]:.*$",
                  "[Unreleased]: %s/compare/v%s...HEAD" % (base_url, version),
                  text, count=1, flags=re.M)
    if previous:
        reference = "[%s]: %s/compare/v%s...v%s" % (
            version, base_url, previous[0], version)
    else:
        reference = "[%s]: %s/releases/tag/v%s" % (version, base_url, version)
    text = re.sub(r"^\[Unreleased\]:.*$", lambda m: m.group(0) + "\n" + reference,
                  text, count=1, flags=re.M)

    with open(os.path.join(ROOT, CHANGELOG), "w", encoding="utf-8") as handle:
        handle.write(text)
    for _, _, _, name in waiting:
        os.remove(os.path.join(fragment_dir(), name))

    print("Assembled [%s] - %s from %d fragment(s):" % (version, date, len(waiting)))
    for issue, kind, _, name in waiting:
        print("  %-20s -> ### %s" % (name, kind.capitalize()))
    print("  link references updated; %s/ is empty." % FRAGMENTS)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="", help="revision to compare against, e.g. origin/main")
    parser.add_argument("--labels", default="", help="comma-separated pull request labels; `%s` lifts the entry rule" % ESCAPE_HATCH)
    parser.add_argument("--assemble", metavar="VERSION", default="",
                        help="write %s/ into %s as this release and delete the "
                             "fragments" % (FRAGMENTS, CHANGELOG))
    parser.add_argument("--date", default="", metavar="YYYY-MM-DD",
                        help="the release date for --assemble (default: today)")
    args = parser.parse_args()
    labels = [label.strip() for label in args.labels.split(",") if label.strip()]

    if args.assemble:
        if not re.match(r"^\d+\.\d+\.\d+$", args.assemble):
            print("--assemble takes a version like 0.2.0, not %r." % args.assemble)
            return 1
        date = args.date or datetime.date.today().isoformat()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            print("--date takes YYYY-MM-DD, not %r." % date)
            return 1
        problems = check_fragments()
        if problems:
            print("the fragments need attention before a release:\n")
            for problem in problems:
                print("  - %s" % problem)
            return 1
        return assemble(args.assemble, date)

    text = _read(CHANGELOG)
    problems = (check_structure(text, _read(PYPROJECT))
                + check_unreleased_is_a_pointer(text) + check_fragments())

    compared = ""
    if args.base:
        compared = _resolve(args.base)
        if not compared:
            print("note: %s is not in this checkout, so only the structure was checked." % args.base)
        else:
            problems += check_against_base(text, _read(CHANGELOG, compared), compared, labels)

    if problems:
        print("CHANGELOG.md needs attention:\n")
        for problem in problems:
            print("  - %s" % problem)
        print("\n%d problem(s)." % len(problems))
        return 1

    waiting = len(fragments())
    print("CHANGELOG.md is well formed, agrees with pyproject.toml, and %s%s."
          % ("%d entr%s waiting in %s/" % (waiting, "y is" if waiting == 1 else "ies are", FRAGMENTS)
             if waiting else "no entries are waiting in %s/" % FRAGMENTS,
             "; nothing lost since %s" % args.base if compared else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
