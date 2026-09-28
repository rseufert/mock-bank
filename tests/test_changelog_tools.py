"""`tools/check_changelog.py`, driven as the command line CI runs.

The tool is now the thing that gates every pull request and, through
`--assemble`, the thing a release is cut with. Both deserve a test that could
fail, and neither can be checked by reading: the interesting behaviour is what
happens to a *tree*, not what a function returns.

So each test builds a throwaway repository - a `CHANGELOG.md`, a
`pyproject.toml`, a `changelog.d/` and a real git history - and runs the tool
against it as a subprocess. The tool is copied in rather than imported, because
it resolves everything from its own location and importing it would point it at
this project's real changelog.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, "tools", "check_changelog.py")

CHANGELOG = """# Changelog

Every release of mock-bank.

## [Unreleased]

Entries for the next release are one file each in `changelog.d/`.

## [0.1.0] - 2026-09-26

### Added

- **The first one.** It shipped.

[Unreleased]: https://example.invalid/x/compare/v0.1.0...HEAD
[0.1.0]: https://example.invalid/x/releases/tag/v0.1.0
"""

PYPROJECT = 'version = "0.1.0"\n'


class ToolCase(unittest.TestCase):
    """A throwaway tree with the tool in it, and one commit to compare against."""

    def setUp(self):
        self.tree = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tree, ignore_errors=True)
        os.mkdir(os.path.join(self.tree, "tools"))
        os.mkdir(os.path.join(self.tree, "changelog.d"))
        os.mkdir(os.path.join(self.tree, "mockbank"))
        shutil.copy(TOOL, os.path.join(self.tree, "tools"))
        self.write("CHANGELOG.md", CHANGELOG)
        self.write("pyproject.toml", PYPROJECT)
        self.write("mockbank/thing.py", "# a module\n")

    def write(self, path, text):
        full = os.path.join(self.tree, path)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def read(self, path):
        with open(os.path.join(self.tree, path), encoding="utf-8") as handle:
            return handle.read()

    def fragment(self, name, text="**Something happened.** And here is why.\n"):
        # mkdir, because git does not track an empty directory: a checkout or a
        # reset that leaves no fragments behind removes `changelog.d/` itself.
        directory = os.path.join(self.tree, "changelog.d")
        if not os.path.isdir(directory):
            os.mkdir(directory)
        self.write(os.path.join("changelog.d", name), text)

    def git(self, *args):
        subprocess.run(["git"] + list(args), cwd=self.tree, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def commit_all(self, message="a commit"):
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", message)

    def base_commit(self):
        """A repository whose HEAD is the tree as it stands, returned as a rev."""
        self.git("init", "-q", "-b", "main")
        self.commit_all("base")
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       cwd=self.tree).decode().strip()

    def run_tool(self, *args):
        result = subprocess.run([sys.executable, "tools/check_changelog.py"] + list(args),
                                cwd=self.tree, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        return result.returncode, result.stdout.decode("utf-8")


class AFragmentThatWouldVanish(ToolCase):
    """Refused by name, because the failure is that it looks like an entry.

    A fragment named wrongly sits in the right directory, reads like an entry
    and would simply not be assembled. Nobody would notice until the release
    notes were short.
    """

    def test_an_unknown_kind_is_refused_and_named(self):
        self.fragment("42.tweaked.md")
        code, out = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("42.tweaked.md", out)
        self.assertIn("tweaked", out)

    def test_a_name_with_no_issue_number_is_refused_and_named(self):
        self.fragment("folder-transport.added.md")
        code, out = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("folder-transport.added.md", out)

    def test_an_empty_fragment_is_refused_and_named(self):
        self.fragment("42.added.md", "")
        code, out = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("42.added.md", out)
        self.assertIn("empty", out)

    def test_whitespace_is_empty(self):
        # A file with a newline in it is not an entry, and `strip()` is the only
        # thing standing between that and a bullet reading "- ".
        self.fragment("42.added.md", "   \n\n")
        code, out = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("empty", out)

    def test_a_well_named_fragment_passes(self):
        self.fragment("42.added.md")
        code, out = self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertIn("1 entry is waiting", out)


class AChangeToThePackage(ToolCase):
    """The rule that used to be about a bullet is now about a file."""

    def test_touching_the_package_without_a_fragment_fails(self):
        base = self.base_commit()
        self.write("mockbank/thing.py", "# changed\n")
        self.commit_all("change the package")
        code, out = self.run_tool("--base", base)
        self.assertEqual(code, 1)
        self.assertIn("mockbank", out)
        self.assertIn("changelog.d", out)

    def test_a_fragment_satisfies_it(self):
        base = self.base_commit()
        self.write("mockbank/thing.py", "# changed\n")
        self.fragment("42.added.md")
        self.commit_all("change the package, with an entry")
        code, out = self.run_tool("--base", base)
        self.assertEqual(code, 0, out)

    def test_the_label_lifts_it(self):
        base = self.base_commit()
        self.write("mockbank/thing.py", "# renamed only\n")
        self.commit_all("a pure rename")
        code, out = self.run_tool("--base", base, "--labels", "no changelog")
        self.assertEqual(code, 0, out)

    def test_a_change_outside_the_package_needs_nothing(self):
        base = self.base_commit()
        self.write("README.md", "# docs only\n")
        self.commit_all("docs")
        code, out = self.run_tool("--base", base)
        self.assertEqual(code, 0, out)

    def test_a_fragment_deleted_without_a_release_is_noticed(self):
        # The old check caught an entry dropped by a merge resolution. There is
        # no resolution to get wrong any more, but `git rm` still is.
        self.fragment("42.added.md")
        base = self.base_commit()
        os.remove(os.path.join(self.tree, "changelog.d", "42.added.md"))
        self.commit_all("drop the entry")
        code, out = self.run_tool("--base", base)
        self.assertEqual(code, 1)
        self.assertIn("42.added.md", out)


class AssemblingARelease(ToolCase):

    def three_fragments(self):
        self.fragment("9.added.md", "**Nine.** The ninth thing.\n")
        self.fragment("10.added.md", "**Ten.** The tenth thing.\n")
        self.fragment("10.fixed.md", "**A fix.** It was broken.\n")

    def assemble(self, version="0.2.0", date="2026-10-01"):
        return self.run_tool("--assemble", version, "--date", date)

    def test_it_writes_the_section_and_empties_the_directory(self):
        self.three_fragments()
        code, out = self.assemble()
        self.assertEqual(code, 0, out)
        self.assertIn("## [0.2.0] - 2026-10-01", self.read("CHANGELOG.md"))
        self.assertEqual(os.listdir(os.path.join(self.tree, "changelog.d")), [])

    def test_kinds_become_headings_and_issues_sort_as_numbers(self):
        self.three_fragments()
        self.assemble()
        text = self.read("CHANGELOG.md")
        # Grouped by kind, and 9 before 10 rather than after it.
        self.assertLess(text.index("### Added"), text.index("### Fixed"))
        self.assertLess(text.index("**Nine.**"), text.index("**Ten.**"))
        self.assertLess(text.index("**Ten.**"), text.index("**A fix.**"))

    def test_all_six_kinds_are_accepted_in_keep_a_changelogs_order(self):
        kinds = ["security", "fixed", "removed", "deprecated", "changed", "added"]
        for number, kind in enumerate(kinds, start=1):
            self.fragment("%d.%s.md" % (number, kind), "**%s.** It happened.\n" % kind)
        code, out = self.assemble()
        self.assertEqual(code, 0, out)
        text = self.read("CHANGELOG.md")
        headings = [text.index("### %s" % k.capitalize()) for k in reversed(kinds)]
        self.assertEqual(headings, sorted(headings))

    def test_the_body_is_indented_as_a_bullet(self):
        self.fragment("42.added.md", "**A thing.** First line.\nSecond line.\n")
        self.assemble()
        text = self.read("CHANGELOG.md")
        self.assertIn("- **A thing.** First line.\n  Second line.", text)

    def test_both_link_references_are_fixed(self):
        self.three_fragments()
        self.assemble()
        text = self.read("CHANGELOG.md")
        self.assertIn("[Unreleased]: https://example.invalid/x/compare/v0.2.0...HEAD", text)
        self.assertIn("[0.2.0]: https://example.invalid/x/compare/v0.1.0...v0.2.0", text)

    def test_the_unreleased_heading_survives(self):
        # It is where the next entry's pointer lives; assembling must not eat it.
        self.three_fragments()
        self.assemble()
        self.assertIn("## [Unreleased]", self.read("CHANGELOG.md"))

    def test_the_result_is_a_changelog_the_check_accepts(self):
        self.three_fragments()
        self.assemble()
        self.write("pyproject.toml", 'version = "0.2.0"\n')
        code, out = self.run_tool()
        self.assertEqual(code, 0, out)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.three_fragments()
        self.assemble()
        after_once = self.read("CHANGELOG.md")
        code, out = self.assemble()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("CHANGELOG.md"), after_once)

    def test_it_refuses_rather_than_writing_the_section_twice(self):
        # Fragments and an assembled section together: somebody added an entry
        # after the release was cut, or the release was cut twice.
        self.three_fragments()
        self.assemble()
        self.fragment("43.added.md")
        code, out = self.assemble()
        self.assertEqual(code, 1)
        self.assertIn("already", out)
        self.assertEqual(self.read("CHANGELOG.md").count("## [0.2.0]"), 1)

    def test_with_no_fragments_there_is_nothing_to_release(self):
        code, out = self.assemble()
        self.assertEqual(code, 1)
        self.assertIn("nothing to release", out)

    def test_a_bad_fragment_stops_a_release_before_it_is_written(self):
        self.three_fragments()
        self.fragment("44.tweaked.md")
        code, out = self.assemble()
        self.assertEqual(code, 1)
        self.assertIn("44.tweaked.md", out)
        self.assertNotIn("## [0.2.0]", self.read("CHANGELOG.md"))

    def test_a_version_that_is_not_one_is_refused(self):
        self.three_fragments()
        code, out = self.run_tool("--assemble", "v0.2", "--date", "2026-10-01")
        self.assertEqual(code, 1)
        self.assertIn("0.2.0", out)

    def test_a_date_that_is_not_one_is_refused(self):
        self.three_fragments()
        code, out = self.run_tool("--assemble", "0.2.0", "--date", "next tuesday")
        self.assertEqual(code, 1)
        self.assertIn("YYYY-MM-DD", out)


class TheUnreleasedSectionHoldsOnlyThePointer(ToolCase):
    """The check that exists because of a clean merge.

    A branch replaced `## [Unreleased]`'s body with a pointer at `changelog.d/`
    while `main` added an entry to it. Different lines, so git merged them with
    no conflict and no marker, and the result said "Nothing is added here by
    hand" directly above an entry added there by hand. A diff review does not
    show it either: both sides are correct on their own.
    """

    def unreleased(self, extra_text):
        self.write("CHANGELOG.md", CHANGELOG.replace(
            "## [0.1.0] - 2026-09-26", extra_text + "## [0.1.0] - 2026-09-26"))

    def test_a_bullet_is_refused_and_named_with_the_file_it_belongs_in(self):
        self.unreleased("### Changed\n\n- **A thing** (#99). Written by hand.\n\n")
        code, out = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("99.changed.md", out)
        self.assertIn("A thing", out)

    def test_a_bullet_with_no_issue_number_still_says_what_to_do(self):
        self.unreleased("### Added\n\n- **A thing.** No issue number anywhere.\n\n")
        code, out = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("<issue>.added.md", out)

    def test_a_heading_alone_is_refused(self):
        # --assemble writes the headings; one sitting there means somebody was
        # about to write a bullet, or a merge left the section half converted.
        self.unreleased("### Fixed\n\n")
        code, out = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("Fixed", out)

    def test_the_merge_that_produced_this_check_is_caught(self):
        # Reproduced as git, because the point is that git is happy about it.
        self.base_commit()
        self.git("checkout", "-q", "-b", "the-pointer")
        self.write("CHANGELOG.md", CHANGELOG)          # already the pointer
        self.fragment("42.added.md")
        self.commit_all("entries become fragments")

        self.git("checkout", "-q", "main")
        self.write("CHANGELOG.md", CHANGELOG.replace(
            "## [0.1.0] - 2026-09-26",
            "### Fixed\n\n- **Something** (#44). Added the old way.\n\n"
            "## [0.1.0] - 2026-09-26"))
        self.commit_all("an entry, the old way")

        self.git("-c", "user.name=t", "-c", "user.email=t@t",
                 "merge", "--no-ff", "--no-edit", "the-pointer")
        # git merged it without a conflict. That is the whole problem.
        self.assertNotIn("<<<<<<<", self.read("CHANGELOG.md"))
        self.assertIn("**Something** (#44)", self.read("CHANGELOG.md"))

        code, out = self.run_tool()
        self.assertEqual(code, 1, "a clean merge left a wrong file and nothing said so")
        self.assertIn("44.fixed.md", out)

    def test_the_pointer_on_its_own_passes(self):
        code, out = self.run_tool()
        self.assertEqual(code, 0, out)

    def test_a_released_section_may_of_course_hold_entries(self):
        # [0.1.0] in the fixture has a bullet. Only [Unreleased] is a pointer.
        self.assertIn("- **The first one.**", self.read("CHANGELOG.md"))
        code, out = self.run_tool()
        self.assertEqual(code, 0, out)


class TwoPullRequestsEachAddingAnEntry(ToolCase):
    """The point of the whole change: they do not conflict.

    This is the first line of the issue's "Done when", and it is a git question
    rather than a tool question, so it is asked of git.
    """

    def test_they_merge_in_either_order_without_a_conflict(self):
        self.base_commit()
        for order in (("a", "b"), ("b", "a")):
            with self.subTest(order=order):
                self.git("checkout", "-q", "main")
                for name in order:
                    self.git("checkout", "-q", "-B", "branch-" + name, "main")
                    self.fragment("%d.added.md" % (100 + ord(name)),
                                  "**Entry %s.** Written by one pull request.\n" % name)
                    self.commit_all("entry " + name)
                self.git("checkout", "-q", "main")
                for name in order:
                    # --no-ff, because a fast-forward would prove nothing.
                    self.git("-c", "user.name=t", "-c", "user.email=t@t",
                             "merge", "--no-ff", "--no-edit", "branch-" + name)
                both = os.listdir(os.path.join(self.tree, "changelog.d"))
                self.assertEqual(len(both), 2, both)
                self.git("checkout", "-q", "main")
                self.git("reset", "-q", "--hard", "HEAD~2")
                for name in order:
                    self.git("branch", "-q", "-D", "branch-" + name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
