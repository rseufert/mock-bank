"""`tools/release.py` and `tools/check_release.py`, against throwaway repositories.

Both tools exist because of one failure: 0.1.0 was merged and stopped there, and
`main` claimed a version that had no tag and no published Release until somebody
noticed by hand. So what is worth testing is not the happy path - that ends in an
irreversible `gh release create` and is not a thing a test may do - but the
refusals, and the three states `check_release.py` has to tell apart.

Each test builds a repository with its own `pyproject.toml`, `CHANGELOG.md`,
`changelog.d/` and history, and runs the tool as a subprocess with the tool
copied in, so it resolves `ROOT` to the throwaway tree rather than to this
project.

Nothing here reaches the network. Every refusal `release.py` raises happens
before it asks GitHub anything, which is deliberate in the tool: the cheap
questions come first. `already_done` does shell out to `gh`, which in a
repository with no remote fails immediately and is read as "not released".
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CHANGELOG = """# Changelog

Every release of mock-bank.

## [Unreleased]

Entries for the next release are one file each in `changelog.d/`.

## [0.2.0] - 2026-10-01

### Added

- **A thing.** It shipped.

## [0.1.0] - 2026-09-26

### Added

- **The first one.** It shipped too.

[Unreleased]: https://example.invalid/x/compare/v0.2.0...HEAD
[0.2.0]: https://example.invalid/x/compare/v0.1.0...v0.2.0
[0.1.0]: https://example.invalid/x/releases/tag/v0.1.0
"""


class ReleaseToolCase(unittest.TestCase):
    """A throwaway repository on `main`, clean, with 0.2.0 dated and no tag."""

    tools = ("release.py", "check_release.py")

    def setUp(self):
        self.tree = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tree, ignore_errors=True)
        os.mkdir(os.path.join(self.tree, "tools"))
        os.mkdir(os.path.join(self.tree, "changelog.d"))
        for name in self.tools:
            shutil.copy(os.path.join(ROOT, "tools", name),
                        os.path.join(self.tree, "tools"))
        self.write("CHANGELOG.md", CHANGELOG)
        self.write("pyproject.toml", 'version = "0.2.0"\n')
        self.git("init", "-q", "-b", "main")
        self.commit_all("the release commit")
        # A remote `main` to be level with, so that check is satisfied without a
        # network: a bare repository on disk is a real remote as far as git cares.
        self.remote = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.remote, ignore_errors=True)
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True,
                       stdout=subprocess.DEVNULL)
        self.git("remote", "add", "origin", self.remote)
        self.git("push", "-q", "origin", "main")
        self.git("branch", "-q", "--set-upstream-to=origin/main", "main")

    def write(self, path, text):
        with open(os.path.join(self.tree, path), "w", encoding="utf-8") as handle:
            handle.write(text)

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t"] + list(args),
            cwd=self.tree, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def commit_all(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def run_tool(self, name, *args):
        result = subprocess.run([sys.executable, os.path.join("tools", name)] + list(args),
                                cwd=self.tree, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        return result.returncode, result.stdout.decode("utf-8")

    def release(self, *args):
        return self.run_tool("release.py", *args)


class ReleaseRefusesBeforeItDoesAnything(ReleaseToolCase):
    """Each of these is a thing that has gone wrong in a real release somewhere."""

    def assertRefused(self, out, *phrases):
        self.assertIn("not releasing", out)
        for phrase in phrases:
            self.assertIn(phrase, out)

    def test_a_version_that_is_not_one(self):
        code, out = self.release("0.2", "--dry-run")
        self.assertEqual(code, 1)
        self.assertRefused(out, "0.2.0")

    def test_a_dirty_working_tree(self):
        self.write("pyproject.toml", 'version = "0.2.0"\n# edited\n')
        code, out = self.release("0.2.0", "--dry-run")
        self.assertEqual(code, 1)
        self.assertRefused(out, "not clean", "pyproject.toml")

    def test_a_branch_that_is_not_main(self):
        self.git("checkout", "-q", "-b", "some-branch")
        code, out = self.release("0.2.0", "--dry-run")
        self.assertEqual(code, 1)
        self.assertRefused(out, "some-branch", "main")

    def test_a_detached_head(self):
        self.git("checkout", "-q", "--detach", "HEAD")
        code, out = self.release("0.2.0", "--dry-run")
        self.assertEqual(code, 1)
        self.assertRefused(out, "detached")

    def test_a_main_that_is_ahead_of_the_remote(self):
        # The tag has to name the commit everybody else has.
        self.write("CHANGELOG.md", CHANGELOG + "\n")
        self.commit_all("a commit nobody else has")
        code, out = self.release("0.2.0", "--dry-run")
        self.assertEqual(code, 1)
        self.assertRefused(out, "disagree")

    def test_a_version_pyproject_does_not_claim(self):
        code, out = self.release("0.3.0", "--dry-run")
        self.assertEqual(code, 1)
        self.assertRefused(out, "pyproject.toml", "0.2.0")

    def test_a_version_the_changelog_has_no_section_for(self):
        self.write("pyproject.toml", 'version = "0.3.0"\n')
        self.commit_all("bump only")
        self.git("push", "-q", "origin", "main")
        code, out = self.release("0.3.0", "--dry-run")
        self.assertEqual(code, 1)
        self.assertRefused(out, "no section", "--assemble")

    def test_a_changelog_section_with_no_date(self):
        self.write("CHANGELOG.md", CHANGELOG.replace(
            "## [0.2.0] - 2026-10-01", "## [0.2.0]"))
        self.commit_all("undate it")
        self.git("push", "-q", "origin", "main")
        code, out = self.release("0.2.0", "--dry-run")
        self.assertEqual(code, 1)
        self.assertRefused(out, "no date")

    def test_fragments_still_waiting(self):
        # The release notes would be missing what somebody wrote.
        self.write("changelog.d/99.added.md", "**Left behind.** Never assembled.\n")
        self.commit_all("an entry that missed the release")
        self.git("push", "-q", "origin", "main")
        code, out = self.release("0.2.0", "--dry-run")
        self.assertEqual(code, 1)
        self.assertRefused(out, "99.added.md", "waiting")

    def test_a_refusal_writes_no_tag(self):
        # The point of refusing before acting: nothing is half done afterwards.
        self.write("changelog.d/99.added.md", "**Left behind.**\n")
        self.commit_all("an entry that missed the release")
        self.git("push", "-q", "origin", "main")
        code, out = self.release("0.2.0")          # not even --dry-run
        self.assertEqual(code, 1)
        # Named the fragment, so this is failing for the reason it claims and
        # not because some later check happened to refuse as well.
        self.assertIn("99.added.md", out)
        tags = subprocess.run(["git", "tag", "-l"], cwd=self.tree,
                              stdout=subprocess.PIPE).stdout.decode().split()
        self.assertEqual(tags, [])


class ReleaseOnAFinishedVersion(ReleaseToolCase):
    """A finished release stays finished however the working copy looks.

    This is why `already_done` is asked before the tree is examined: fragments
    waiting for the *next* version are not a reason to re-examine a released
    one, and refusing there would report the wrong problem entirely.
    """

    def test_a_dirty_tree_does_not_change_the_answer_about_an_old_version(self):
        # Tagged but with no GitHub Release, `gh` unavailable in a bare remote:
        # so this still refuses - but on the tree, which is the honest reason.
        self.git("tag", "-a", "v0.1.0", "-m", "old")
        code, out = self.release("0.1.0", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("not releasing 0.1.0", out)

    def test_waiting_fragments_are_not_the_complaint_about_an_old_version(self):
        self.write("changelog.d/99.added.md", "**For the next one.**\n")
        self.commit_all("an entry for the next release")
        self.git("push", "-q", "origin", "main")
        self.git("tag", "-a", "v0.1.0", "-m", "old")
        code, out = self.release("0.1.0", "--dry-run")
        # It may refuse, but not because of an entry belonging to a later release.
        self.assertNotIn("99.added.md", out)


class CheckReleaseTellsTheThreeStatesApart(ReleaseToolCase):

    def check(self, *args):
        return self.run_tool("check_release.py", *args)

    def test_an_unreleased_version_is_the_ordinary_state(self):
        # Between releases [Unreleased] is a pointer and there is nothing to
        # finish. This is the case that must not cry wolf, since it is true on
        # every ordinary day.
        self.write("pyproject.toml", 'version = "0.3.0"\n')
        self.commit_all("bump for the next one")
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("unreleased", out)

    def test_a_dated_version_with_no_tag_fails_and_says_what_is_missing(self):
        # The 0.1.0 failure, exactly: bumped, dated, merged, never tagged.
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("v0.2.0", out)
        self.assertIn("no tag", out)

    def test_it_names_the_command_that_finishes_the_release(self):
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("tools/release.py 0.2.0", out)

    def test_no_version_at_all_is_a_failure_rather_than_a_pass(self):
        self.write("pyproject.toml", "# nothing here\n")
        self.commit_all("lose the version")
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("no `version`", out)

    def test_a_tag_with_no_release_is_asked_about_and_not_assumed(self):
        # With a tag present, the remaining question needs `gh`. Whatever the
        # answer, it must not silently pass: either it reports the missing
        # Release, or it says it could not ask.
        self.git("tag", "-a", "v0.2.0", "-m", "0.2.0")
        code, out = self.check()
        if code == 0:
            self.assertIn("could not be asked", out)
        else:
            self.assertTrue("GitHub Release" in out or "could not be asked" in out, out)

    def test_require_turns_a_skip_into_a_failure(self):
        self.git("tag", "-a", "v0.2.0", "-m", "0.2.0")
        skipped, out = self.check()
        if skipped == 0:            # it skipped, so --require must not
            code, out = self.check("--require")
            self.assertEqual(code, 1)
            self.assertIn("--require", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
