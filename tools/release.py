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
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG = "CHANGELOG.md"
PYPROJECT = "pyproject.toml"
FRAGMENTS = "changelog.d"
BRANCH = "main"
CI_WORKFLOW = ".github/workflows/ci.yml"

# What a CI *workflow run* may conclude and still be a pass: only success.
# `skipped` and `neutral` belong to individual check runs, where a job may
# legitimately skip - a whole workflow run that concluded `skipped` ran nothing
# at all, which is not a commit that has been tested. Anything else - failure,
# cancelled, timed_out, action_required, stale - is not a release either.
CONCLUDED_WELL = ("success",)

HEADING = re.compile(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?\s*$", re.M)
# `[0.1.0]: https://...` at the foot of the file. It follows the oldest section
# directly, so it is an edge of a section body as much as the next heading is.
LINK_REFERENCE = re.compile(r"^\[[^\]]+\]:\s*\S+\s*$")


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


def notes_for(text: str, version: str) -> str:
    """The body of this version's section, to publish as the Release notes.

    Pure, and the awkward part is knowing where the section stops. Two things
    can follow it and both have to end it:

    - the next `## [` heading, when this is not the oldest section;
    - the link references at the foot of the file, which for the oldest section
      are the only thing after it, and which read as a stray `[0.1.0]: https://`
      in the middle of release notes if they are left in.

    Stopping only at the next heading passes every test where the version sits
    in the middle of the file, which is why the tests do both.

    Generated notes are what this replaces. On a squash-merged repository they
    restate the commit titles, which the changelog already says better and which
    somebody has actually reviewed.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        found = HEADING.match(line)
        if found and found.group(1) == version:
            start = index + 1
            break
    if start is None:
        return ""
    body = []
    for line in lines[start:]:
        if line.startswith("## ") or LINK_REFERENCE.match(line):
            break
        body.append(line)
    return "\n".join(body).strip() + "\n"


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

    fetched = git("fetch", "--quiet", "origin", BRANCH)
    if fetched.returncode != 0:
        raise Refused(
            "could not fetch origin/%s: %s\n    Without it, `%s is level with "
            "origin/%s` would be answered from a stale copy."
            % (BRANCH, fetched.stderr.decode("utf-8").strip() or "no reason given",
               BRANCH, BRANCH))
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


def ci_verdict(answer, workflow: str = CI_WORKFLOW):
    """(ok, reason) for one commit, from the Actions workflow-runs answer.

    Pure, and separate from the fetching, because deciding *is* the logic here
    and the first version of it refused every release this repository could
    ever cut. It asked the combined-status API, which reports `pending` with
    zero statuses on a repository whose checks are all Actions - not "" or
    null, which is what the fallback to check runs was waiting for. So the
    fallback never fired and every commit read as pending.
    `tests/fixtures/commit-status-pending-zero.json` is that real answer, kept
    so the shape cannot be misremembered; `actions-runs-green.json` beside it is
    what this function is asked instead, and `actions-runs-skipped.json` is a
    run that completed having done nothing.

    Two further things this has to get right, both of which the earlier version
    did not:

    - **Only the CI workflow counts.** The scheduled `Release check` workflow
      attaches to `main`'s head commit too, and between the release merge and
      this script running it is *correctly* failing - there is no tag yet.
      Reading every check on the commit therefore made that correct failure
      refuse the release it was waiting for.
    - **A run in progress is not a pass.** An unfinished run has a null
      conclusion, and filtering conclusions rather than checking `status`
      first let twelve finished and one running read as twelve passed.
    """
    runs = [run for run in (answer or {}).get("workflow_runs", [])
            if run.get("path") == workflow]
    if not runs:
        return False, (
            "GitHub reports no %s run on this commit. A release is tagged on a "
            "commit CI has passed, and this one has not been tested." % workflow)

    # The newest run is the answer: a re-run keeps its id and updates in place,
    # but a `workflow_dispatch` makes a new one beside the old.
    newest = max(runs, key=lambda run: run.get("id") or 0)
    status = newest.get("status")
    if status != "completed":
        return False, (
            "the %s run on this commit is `%s`, not `completed`. A run still "
            "going is not a pass; wait for it and try again."
            % (workflow, status))
    conclusion = newest.get("conclusion")
    if conclusion not in CONCLUDED_WELL:
        return False, (
            "the %s run on this commit concluded `%s`. Tagging it would publish "
            "something the tests did not pass." % (workflow, conclusion))
    return True, ("the %s run on this commit passed (attempt %s)"
                  % (workflow, newest.get("run_attempt") or 1))


def ci_answer(commit: str):
    """Ask GitHub for the workflow runs on this commit. Raises Refused."""
    result = subprocess.run(
        ["gh", "api", "repos/{owner}/{repo}/actions/runs"
                      "?head_sha=%s&per_page=100" % commit],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise Refused(
            "could not ask GitHub which workflows ran on %s: %s\n    Releasing "
            "without knowing whether CI passed is not better than not releasing."
            % (commit[:8], result.stderr.decode("utf-8").strip()))
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except ValueError:
        raise Refused("GitHub's answer about the workflows on %s was not JSON."
                      % commit[:8])


def check_ci_is_green(commit: str) -> str:
    ok, reason = ci_verdict(ci_answer(commit))
    if not ok:
        raise Refused(reason)
    return reason


def run(step: str, command, dry_run: bool):
    print("  %s %s" % ("would" if dry_run else "doing:", step))
    print("      $ %s" % " ".join(command))
    if dry_run:
        return
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        raise Refused("`%s` failed. Nothing after this step has run."
                      % " ".join(command))


def remote_tag_commit(tag: str) -> str:
    """The commit the *remote's* tag points at, or "" when it has no such tag.

    Asked of the remote, not of this clone, because the two disagree in exactly
    the case that matters. If `git push origin <tag>` fails - a network blip, a
    protected ref - the local tag exists and the remote's does not. Reading the
    local one, the next run skipped the push as already done and went straight
    to `gh release create`, which creates its own tag on the remote from
    whatever the default branch points at. That is how a release ends up tagged
    at the wrong commit, and it is silent.

    `--verify-tag` on the create is the second half of the same guard: it makes
    `gh` refuse to invent a tag rather than doing it helpfully.
    """
    result = git("ls-remote", "--tags", "origin",
                 "refs/tags/%s" % tag, "refs/tags/%s^{}" % tag)
    if result.returncode != 0:
        raise Refused("could not ask origin about the tag %s: %s"
                      % (tag, result.stderr.decode("utf-8").strip()))
    peeled, direct = "", ""
    for line in result.stdout.decode("utf-8").splitlines():
        sha, _, ref = line.partition("\t")
        if ref.endswith("^{}"):
            peeled = sha.strip()
        elif ref.strip():
            direct = sha.strip()
    # An annotated tag lists the tag object and then the commit it peels to; the
    # commit is the one worth comparing.
    return peeled or direct


def release_state(tag: str) -> str:
    """"published", "draft", "absent", or "unknown" when `gh` cannot say.

    A draft is neither of the other two, and calling it published was a bug: a
    draft does not trigger the publish workflow, so PyPI keeps serving the
    previous version while this said "nothing to do". Calling it absent is no
    better - `gh release create` would fail on the name already existing, which
    tells the reader nothing about what to do. `check_release.py` already drew
    this distinction; this asks the same question the same way.
    """
    if subprocess.call(["gh", "repo", "view", "--json", "name"], cwd=ROOT,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL) != 0:
        # Not a refusal from in here, and not `absent` either. `absent` was the
        # conflation this script refuses elsewhere - it would go on to tag and
        # publish without knowing what is already there. But raising here would
        # put a GitHub problem in front of a dirty working tree, which is both
        # cheaper to discover and more likely to be what is actually wrong. So
        # it is reported to the caller, which refuses once the local checks have
        # had their say.
        return "unknown"
    result = subprocess.run(
        ["gh", "release", "view", tag, "--json", "isDraft,publishedAt"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        return "absent"
    try:
        answer = json.loads(result.stdout.decode("utf-8"))
    except ValueError:
        return "absent"
    if answer.get("isDraft") or not answer.get("publishedAt"):
        return "draft"
    return "published"


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
        remote_tag = remote_tag_commit(tag)
        state = release_state(tag)
        if state == "draft":
            raise Refused(
                "the GitHub Release for %s exists but is a draft, so it has not "
                "published anything: PyPI is still serving the previous version. "
                "Publish that draft - `gh release edit %s --draft=false` - rather "
                "than running this again, which cannot create a Release whose "
                "name is already taken." % (tag, tag))
        released = state == "published"
        if remote_tag and released:
            print("%s is already tagged on origin and its GitHub Release is "
                  "published; there is nothing to do." % tag)
            return 0

        # The local checks first: they are cheaper, they need no network, and a
        # dirty tree is both more likely and more useful to hear about than a
        # GitHub problem.
        commit, date = check_tree_is_releasable(version)

        if state == "unknown":
            raise Refused(
                "`gh` cannot work out which GitHub repository this is, so whether "
                "%s already has a Release could not be asked - and this will not "
                "tag and publish without knowing what is already there." % tag)

        # A tag already on the remote must name the commit being released. If it
        # names another, something has gone wrong that this script must not paper
        # over by publishing a Release against it.
        if remote_tag and remote_tag != commit:
            raise Refused(
                "origin already has the tag %s and it points at %s, not the %s "
                "being released. Work out which is right before going further; "
                "moving a published tag breaks every checkout that has it."
                % (tag, remote_tag[:8], commit[:8]))

        local_tag = git("rev-parse", "-q", "--verify", "refs/tags/%s^{commit}" % tag)
        local = local_tag.stdout.decode("utf-8").strip() if local_tag.returncode == 0 else ""
        if local and local != commit:
            raise Refused(
                "this clone has the tag %s pointing at %s, not the %s being "
                "released. Delete it (`git tag -d %s`) if it is a leftover."
                % (tag, local[:8], commit[:8], tag))
        print("releasing %s (%s), dated %s in %s%s"
              % (version, commit[:8], date, CHANGELOG,
                 " - dry run, nothing will change" if args.dry_run else ""))
        print("  checked: tree clean, on %s, level with origin/%s, %s says %s, "
              "[%s] dated, no fragments waiting"
              % (BRANCH, BRANCH, PYPROJECT, version, version))
        print("  checked: %s" % check_ci_is_green(commit))

        if local == commit:
            print("  the tag %s is already here and points at %s." % (tag, commit[:8]))
        else:
            run("create the annotated tag", ["git", "tag", "-a", tag, "-m",
                                             "mock-bank %s" % version],
                args.dry_run)
        if remote_tag:
            print("  origin already has %s at the same commit; not pushing again." % tag)
        else:
            # Pushed even when the tag was already local: the previous run may
            # have created it and failed here, which is the case that used to
            # end with `gh` inventing a tag of its own.
            run("push the tag to origin", ["git", "push", "origin", tag],
                args.dry_run)
        if released:
            print("  the GitHub Release for %s already exists; leaving it." % tag)
        else:
            notes = notes_for(read(CHANGELOG), version)
            if not notes.strip():
                raise Refused(
                    "%s's [%s] section is empty, so the Release would have no "
                    "notes. Assemble the fragments into it first."
                    % (CHANGELOG, version))
            print("  the Release notes are %s's [%s] section, %d line(s):"
                  % (CHANGELOG, version, len(notes.strip().splitlines())))
            for line in notes.strip().splitlines():
                print("      | %s" % line)
            # A real file, because `gh` reads the body from one. Written next to
            # nothing and removed afterwards even when the publish fails, so a
            # refused release leaves no litter in the tree being released.
            handle, notes_path = tempfile.mkstemp(prefix="release-notes-",
                                                  suffix=".md")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as out_file:
                    out_file.write(notes)
                run("publish the GitHub Release, which uploads to PyPI",
                    ["gh", "release", "create", tag, "--verify-tag",
                     "--title", version, "--notes-file", notes_path],
                    args.dry_run)
            finally:
                os.unlink(notes_path)
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
