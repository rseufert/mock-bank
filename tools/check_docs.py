#!/usr/bin/env python3
"""Guard docs/FILES.md against drift.

This checks *coverage*, not prose: it cannot tell whether a description is
still true, only whether a file exists that nobody documented, or a file is
documented that no longer exists.  That catches the common failure - a module
added without a line in the index - and leaves the judgement calls to review.

Seven checks:

1. every tracked file is named in docs/FILES.md
2. every file named in docs/FILES.md exists
3. every module of the package appears in the README's layout block
4. every command-line flag is mentioned in the README
5. every partner behaviour has a row in the README's behaviour table
6. every file docs/ARCHITECTURE.md names exists
7. the README's endpoint table and the route table name the same endpoints

The sixth is the same check as the second, one document over: ARCHITECTURE.md
explains the design by naming the modules that implement it, and it had been
pointing at `tests/test_generated.py` for a while - a file that has never
existed, for a test that lives in `tests/test_dictionary.py`. A reader who went
looking would have concluded the document was describing a different project.

The fourth asks the real argument parser for its flags, so a flag added to
`mockbank/__main__.py` without a word in the README fails the build - fourteen
of them once existed only in `--help`.

The seventh reads both directions from the route table the server dispatches
on, as the fourth and fifth read theirs. A hand-kept list of endpoints had
drifted before (#27), naming a path the mock does not have and missing one it
does; now an endpoint registered without a README row fails the build, and so
does a row for an endpoint that is gone.

There was a check holding the README to the number of tests the loader
discovers.  It went when the number did.  An exact count sits in one line of
prose that every branch adding a test has to edit, so it conflicted with
every other such branch - and a pull request in conflict runs no CI at all,
which made branches look stalled when they were only dirty.  "Every test
talks to a real mock over real HTTP" is the claim worth making, and it cannot
go stale.

Run it directly (`python3 tools/check_docs.py`); CI runs it on every push.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join("docs", "FILES.md")
ARCHITECTURE = os.path.join("docs", "ARCHITECTURE.md")
README = "README.md"

# Files that are their own documentation, or carry nothing worth describing.
EXEMPT = {".gitignore"}

# Directories documented as a directory rather than file by file. A changelog
# fragment is one entry waiting for a release; a row each would put every pull
# request back to editing one shared file, which is the conflict `changelog.d/`
# exists to end. The directory itself still needs its row.
EXEMPT_DIRS = ("changelog.d/",)

# Tokens in the index that look like a path and are therefore checked to exist.
PATH_RE = re.compile(r"`([\w./-]+\.(?:py|md|yml|yaml|toml|in|sh|cfg))`")


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True,
                         stdout=subprocess.PIPE).stdout.decode()
    return sorted(line for line in out.splitlines() if line)


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def cli_flags():
    """Every long flag the command line accepts, from the parser itself."""
    sys.path.insert(0, ROOT)
    from mockbank.__main__ import build_parser
    flags = set()
    for action in build_parser()._actions:
        flags.update(o for o in action.option_strings if o.startswith("--"))
    return sorted(flags - {"--help"})


def behaviours():
    """Every account behaviour, from the table the mock itself uses."""
    sys.path.insert(0, ROOT)
    from mockbank.accounts import BEHAVIOURS
    return sorted(BEHAVIOURS)


def endpoints():
    """Every endpoint the mock advertises, from its route table."""
    sys.path.insert(0, ROOT)
    from mockbank.routes import SUPPORTED
    return list(SUPPORTED)


# `GET /_mock/health`, or `GET/POST /_mock/accounts` for two methods on a path.
ENDPOINT_RE = re.compile(r"`((?:GET|POST|PUT|PATCH|DELETE)(?:/(?:GET|POST|PUT|PATCH|DELETE))*)"
                         r" (/[^`\s]*)`")


def documented_endpoints(readme):
    """The endpoints the README's endpoint table names, one per method."""
    section = re.search(r"^## Endpoints\n(.*?)^## ", readme, re.S | re.M)
    if section is None:
        return None
    found = []
    for line in section.group(1).splitlines():
        cells = line.split("|")
        if not line.startswith("|") or len(cells) < 4:
            continue
        for methods, path in ENDPOINT_RE.findall(cells[2]):
            found.extend("%s %s" % (method, path) for method in methods.split("/"))
    return found


def main():
    index = read(INDEX)
    readme = read(README)
    problems = []

    # 1. undocumented files
    for path in tracked_files():
        name = os.path.basename(path)
        if name in EXEMPT or path == INDEX:
            continue
        if path.startswith(EXEMPT_DIRS):
            continue
        if ("`%s`" % path) not in index and ("`%s`" % name) not in index:
            problems.append(
                "%s is not documented in %s - add a row describing it" % (path, INDEX))

    for directory in EXEMPT_DIRS:
        if os.path.isdir(os.path.join(ROOT, directory)) and (
                "`%s`" % directory) not in index:
            problems.append(
                "%s is not documented in %s - its files are exempt, so the "
                "directory itself needs the row" % (directory, INDEX))

    # 2. documented files that no longer exist
    existing = set(tracked_files())
    basenames = {os.path.basename(p) for p in existing}
    for token in sorted(set(PATH_RE.findall(index))):
        if token in existing or token in basenames:
            continue
        problems.append(
            "%s mentions `%s`, which no longer exists - update or remove the row"
            % (INDEX, token))

    # 3. the README layout block must list every module of the package
    layout = re.search(r"## Layout\n+```\n(.*?)```", readme, re.S)
    if layout is None:
        problems.append("could not find the layout block in %s" % README)
    else:
        for path in existing:
            if path.startswith("mockbank/") and path.endswith(".py"):
                base = os.path.basename(path)
                if base.startswith("__"):
                    continue  # dunder modules are not part of the map
                if path not in layout.group(1):
                    problems.append(
                        "%s is missing from the layout block in %s" % (path, README))

    # 4. every flag the command line takes is mentioned in the README
    for flag in cli_flags():
        if not re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(flag), readme):
            problems.append(
                "%s is a command-line flag the README never mentions - add it to "
                "the Configuration section" % flag)

    # 5. every behaviour an account can be set to has a row in the README
    for name in behaviours():
        if not re.search(r"^\| `%s` \|" % re.escape(name), readme, re.M):
            problems.append(
                "the behaviour %r has no row in the README's behaviour table"
                % name)

    # 6. every file ARCHITECTURE.md names exists, the way check 2 does for the
    #    index. A design document that names a module is only useful while the
    #    module is there to read.
    architecture = read(ARCHITECTURE)
    for token in sorted(set(PATH_RE.findall(architecture))):
        if token in existing or token in basenames:
            continue
        problems.append(
            "%s mentions `%s`, which does not exist - name the file that does"
            % (ARCHITECTURE, token))

    # 7. the README's endpoint table and the route table agree, both ways
    documented = documented_endpoints(readme)
    if documented is None:
        problems.append("could not find the Endpoints section in %s" % README)
    else:
        for endpoint in endpoints():
            if endpoint not in documented:
                problems.append(
                    "%s is served but has no row in the README's endpoint table"
                    % endpoint)
        for endpoint in documented:
            if endpoint not in endpoints():
                problems.append(
                    "the README's endpoint table names %s, which the mock does "
                    "not serve" % endpoint)

    if problems:
        print("documentation is out of date:\n")
        for problem in problems:
            print("  - %s" % problem)
        print("\n%d problem(s)." % len(problems))
        return 1

    print("docs/FILES.md covers every tracked file, names nothing that is gone, "
          "the README layout block lists every module, it mentions every "
          "command-line flag, it has a row for every behaviour, "
          "docs/ARCHITECTURE.md names nothing that is not there, and the "
          "README's endpoint table matches the route table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
