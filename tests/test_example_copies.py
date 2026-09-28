"""`tools/check_examples.py`: the decisions it makes, without touching GitHub.

The tool's job is a judgement in five parts - matched, drifted, missing, updated
or could-not-look - and the last of those has to behave differently depending on
who is asking: a skip for a contributor with no network, a failure for CI. That
is the interesting behaviour, and none of it needs a real fetch to test.

So `fetch` is replaced with a stub and `COPIES` with a map of throwaway files,
and the tool is called as CI calls it, through `main(argv)` with the same flags.
Unlike `test_changelog_tools.py`, which copies its tool into a throwaway
repository because that tool resolves paths from its own location, this one
resolves them from the working directory - so a temporary directory and a
`chdir` is the whole setup.

The two network behaviours that cannot be reached through `fetch` - a retried
failure and a 404 that must not be retried - drive `urllib.request.urlopen`
instead, because *not* retrying a 404 is a decision worth a test: it is the
difference between "the original moved, fix the path" and twenty seconds of
pointless waiting followed by the same wrong answer.
"""
import io
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import check_examples                                                # noqa: E402

THEIRS = "def problems():\n    return []\n"
OURS_DRIFTED = "def problems():\n    return ['edited here']\n"


class ExampleCopyCase(unittest.TestCase):
    """A temporary directory standing in for the repository."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mockbank-copies-")
        self.addCleanup(self._cleanup)
        self.here = os.getcwd()
        os.chdir(self.dir)
        os.makedirs("examples")

        self.saved = dict(check_examples.COPIES)
        self.saved_fetch = check_examples.fetch
        check_examples.COPIES = {"examples/invoice_check.py": ("mock-sap", "examples/invoice_check.py")}
        self.addCleanup(self._restore)

    def _restore(self):
        check_examples.COPIES = self.saved
        check_examples.fetch = self.saved_fetch

    def _cleanup(self):
        import shutil
        os.chdir(self.here)
        shutil.rmtree(self.dir, ignore_errors=True)

    def answers(self, text):
        """Make the fetch return `text`, or None to mean "could not look"."""
        check_examples.fetch = lambda repo, path, **kw: text

    def write(self, text):
        with open("examples/invoice_check.py", "w", encoding="utf-8") as handle:
            handle.write(text)

    def read(self):
        with open("examples/invoice_check.py", encoding="utf-8") as handle:
            return handle.read()

    def run_tool(self, *argv):
        """The tool as CI runs it, with its output captured."""
        out, err = io.StringIO(), io.StringIO()
        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = check_examples.main(list(argv))
        finally:
            sys.stdout, sys.stderr = stdout, stderr
        return code, out.getvalue(), err.getvalue()


class TestComparing(ExampleCopyCase):
    def test_a_copy_that_matches_passes(self):
        self.answers(THEIRS)
        self.write(THEIRS)

        code, out, _ = self.run_tool()

        self.assertEqual(code, 0)
        self.assertIn("match their source repositories", out)

    def test_a_drifted_copy_fails_and_says_what_moved(self):
        self.answers(THEIRS)
        self.write(OURS_DRIFTED)

        code, _, err = self.run_tool()

        self.assertEqual(code, 1)
        self.assertIn("has drifted", err)
        # The diff is the point: a failure saying only "they differ" leaves the
        # reader to fetch both copies by hand before they can judge it.
        self.assertIn("edited here", err)
        self.assertIn("--update", err)
        self.assertEqual(self.read(), OURS_DRIFTED, "a check must not write")

    def test_a_registered_copy_that_is_missing_fails(self):
        self.answers(THEIRS)

        code, _, err = self.run_tool()

        self.assertEqual(code, 1)
        self.assertIn("is not here", err)


class TestUpdating(ExampleCopyCase):
    def test_update_takes_theirs(self):
        self.answers(THEIRS)
        self.write(OURS_DRIFTED)

        code, out, _ = self.run_tool("--update")

        self.assertEqual(code, 0)
        self.assertIn("updated", out)
        self.assertEqual(self.read(), THEIRS)

    def test_update_creates_a_copy_that_was_never_here(self):
        self.answers(THEIRS)

        code, out, _ = self.run_tool("--update")

        self.assertEqual(code, 0)
        self.assertIn("created", out)
        self.assertEqual(self.read(), THEIRS)

    def test_update_then_check_passes(self):
        """The fix the failure message tells you to run has to actually work."""
        self.answers(THEIRS)
        self.write(OURS_DRIFTED)

        self.assertEqual(self.run_tool("--update")[0], 0)
        self.assertEqual(self.run_tool()[0], 0)


class TestWhenItCannotLook(ExampleCopyCase):
    """The asymmetry the tool exists to get right."""

    def test_no_network_is_a_skip(self):
        self.answers(None)
        self.write(THEIRS)

        code, _, err = self.run_tool()

        self.assertEqual(code, 0, "a contributor offline is not blocked")
        self.assertIn("skipped", err)

    def test_no_network_with_require_is_a_failure(self):
        self.answers(None)
        self.write(THEIRS)

        code, _, err = self.run_tool("--require")

        self.assertEqual(code, 1, "a check that passes because it could not ask "
                                 "is worse than no check")
        self.assertIn("--require", err)

    def test_a_skip_does_not_hide_a_drift_in_another_copy(self):
        """One unreachable copy must not stop the others being judged.

        The unreachable one is named so that it sorts *first*, which is the
        whole test: with the drifted copy first, a version that gave up at the
        first skip still reported the drift it had already found and passed this
        test. That mutation survived until the names were swapped.
        """
        check_examples.COPIES = {
            "examples/aaa_unreachable.py": ("mock-edi", "examples/other.py"),
            "examples/invoice_check.py": ("mock-sap", "examples/invoice_check.py"),
        }
        self.write(OURS_DRIFTED)
        with open("examples/aaa_unreachable.py", "w", encoding="utf-8") as handle:
            handle.write(THEIRS)
        check_examples.fetch = lambda repo, path, **kw: (
            None if repo == "mock-edi" else THEIRS)

        code, _, err = self.run_tool()

        self.assertEqual(code, 1, "the drift is behind the skip and must still count")
        self.assertIn("has drifted", err)
        self.assertIn("skipped", err)


class TestFetching(ExampleCopyCase):
    """The two network decisions, driven at urlopen."""

    def urlopen_raises(self, *errors):
        """Fail with each error in turn, then succeed. Counts the calls.

        The original is captured *before* patching. Capturing it afterwards
        restores the fake instead - which leaves a stub in `urllib.request` for
        every later test in the process, and cost 306 errors elsewhere in this
        suite before it was spotted.
        """
        original = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", original)
        self.calls = []
        queue = list(errors)

        def fake(url, timeout=None):
            self.calls.append(url)
            if queue:
                raise queue.pop(0)

            class Response:
                def read(self):
                    return THEIRS.encode()

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False
            return Response()

        urllib.request.urlopen = fake

    def test_a_flaky_network_is_retried(self):
        self.urlopen_raises(urllib.error.URLError("first"),
                            urllib.error.URLError("second"))

        got = check_examples.fetch("mock-sap", "examples/invoice_check.py", pause=0)

        self.assertEqual(got, THEIRS)
        self.assertEqual(len(self.calls), 3, "two failures then the answer")

    def test_a_404_is_not_retried(self):
        """The original moved. Retrying finds the same absence three times."""
        self.urlopen_raises(urllib.error.HTTPError(
            "u", 404, "Not Found", {}, None))

        with self.assertRaises(SystemExit) as caught:
            check_examples.fetch("mock-sap", "examples/gone.py", pause=0)

        self.assertIn("does not exist", str(caught.exception))
        self.assertEqual(len(self.calls), 1)

    def test_a_network_that_never_answers_gives_up_and_says_nothing_found(self):
        self.urlopen_raises(*[urllib.error.URLError("no") for _ in range(9)])

        self.assertIsNone(
            check_examples.fetch("mock-sap", "examples/invoice_check.py", pause=0))
        self.assertEqual(len(self.calls), check_examples.ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
