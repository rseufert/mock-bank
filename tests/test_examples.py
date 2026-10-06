"""The examples, run against a real mock, on every platform CI covers.

CI's smoke job runs `demo.sh` and `client.py` on Linux. That leaves the examples
unverified on macOS and Windows, and `client.py` is the file an integrator is
most likely to copy - so it is driven here as well, as a subprocess against the
harness's own server. `demo.sh` is bash and cannot run on the Windows runner, so
it stays in the smoke job; what *can* be checked everywhere is that the tour
only reaches for endpoints the mock actually has, which is a static question.
"""
import os
import re
import subprocess
import sys
import unittest

from test_payments import sample

from support import MockServerCase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXAMPLES = os.path.join(ROOT, "examples")

from mockbank.server import SUPPORTED                                # noqa: E402


class TheExampleClient(MockServerCase):
    """`python3 examples/client.py`, against a mock on an ephemeral port."""

    def run_client(self, *extra):
        result = subprocess.run(
            [sys.executable, os.path.join(EXAMPLES, "client.py"),
             "--base", self.base] + list(extra),
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=120)
        return result

    def setUp(self):
        self.addCleanup(self.post, "/_mock/reset")

    def rows(self, output):
        """The table's data rows: an EndToEndId, then its columns."""
        return [line.split() for line in output.splitlines()
                if line.startswith("INV-")]

    def test_it_prints_a_row_per_payment_with_a_status(self):
        result = self.run_client()
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.rows(result.stdout)
        # The sample is four payments, so four rows - one each, no more.
        self.assertEqual(len(rows), 4, result.stdout)
        for row in rows:
            with self.subTest(payment=row[0]):
                self.assertIn(row[1], ("accepted", "rejected"))

    def test_the_accepted_ones_carry_a_settlement_date(self):
        result = self.run_client()
        accepted = [row for row in self.rows(result.stdout) if row[1] == "accepted"]
        self.assertTrue(accepted, result.stdout)
        for row in accepted:
            with self.subTest(payment=row[0]):
                # The date column, which for an accepted payment is a real date.
                self.assertRegex(" ".join(row), r"\d{4}-\d{2}-\d{2}")

    def test_it_matches_the_wire_statuses_to_the_json_summary(self):
        # The client fails with a non-zero status if the pain.002 and the JSON
        # summary disagree, which is the cross-check worth having in it: the
        # summary is this mock's convenience, the pain.002 is what a bank sends.
        result = self.run_client()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("matched to its payment by EndToEndId", result.stdout)

    def test_a_rejected_payment_shows_its_reason_code(self):
        result = self.run_client()
        rejected = [row for row in self.rows(result.stdout) if row[1] == "rejected"]
        self.assertTrue(rejected, result.stdout)
        for row in rejected:
            with self.subTest(payment=row[0]):
                # AC04, RC01, AM04: four characters from the external code list,
                # never a sentence the mock made up.
                self.assertRegex(row[2], r"^[A-Z]{2}\d{2}$")

    def test_it_says_what_to_do_when_there_is_no_mock(self):
        result = subprocess.run(
            [sys.executable, os.path.join(EXAMPLES, "client.py"),
             "--base", "http://127.0.0.1:1"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=120)
        self.assertNotEqual(result.returncode, 0)
        # A stack trace would be the wrong answer here: the likely cause is
        # that nobody started the mock, and the fix is one command.
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("python3 -m mockbank", result.stderr)


class WhenTheMockWantsCredentials(MockServerCase):
    config_kwargs = {"auth": "tester:s3cret"}

    def run_client(self, *extra):
        return subprocess.run(
            [sys.executable, os.path.join(EXAMPLES, "client.py"),
             "--base", self.base] + list(extra),
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=120)

    def test_without_them_it_says_which_flag_to_pass(self):
        result = self.run_client()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("--auth", result.stderr)

    def test_with_them_it_runs(self):
        result = self.run_client("--auth", "tester:s3cret")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INV-2026-0101", result.stdout)


class TheStatementArithmetic(MockServerCase):
    """`examples/statement.py`, over statements the mock really wrote.

    The case that matters is an account that went below zero. ISO 20022 carries
    every amount as a positive number with a `CdtDbtInd` beside it, so an
    overdrawn closing balance is `16150.00` with `DBIT` - and reading the amount
    without the indicator turns it into a credit of sixteen thousand, which made
    a statement that reconciled to the cent print DOES NOT RECONCILE. Going
    below zero is not an edge case here: it is what `accept` on a small balance
    does, by design.
    """

    def setUp(self):
        self.addCleanup(self.post, "/_mock/reset")

    def overdraw(self):
        """Leave ACME below zero, and return the statements the bank wrote."""
        # accept, not insufficient-funds: the point is a booking that goes
        # through and takes the balance negative, not one that is refused.
        self.patch("/_mock/accounts/ACME", {"behaviour": "accept",
                                            "balance": 10000})
        answer = self.post("/payments", body=sample("pain001_four_payments.xml")).json()
        settles = sorted(p["settlement_date"] for p in answer["payments"]
                         if p["settlement_date"])
        self.post("/_mock/advance?to=%s" % settles[0])
        self.post("/_mock/advance?days=1")       # so that day's statement closes
        self.assertLess(self.get("/_mock/accounts/ACME").json()["balance"], 0)
        return self.get("/_mock/mailbox?type=camt.053&raw&leave").body

    def run_statement(self, xml):
        return subprocess.run(
            [sys.executable, os.path.join(EXAMPLES, "statement.py")],
            input=xml, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120)

    def test_an_overdrawn_statement_reconciles(self):
        statements = self.overdraw()
        # The case is only being tested if a DBIT balance is actually in there.
        self.assertIn(b"DBIT", statements)
        result = self.run_statement(statements)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout.decode("utf-8")
        self.assertNotIn("DOES NOT RECONCILE", output, output)
        # And it reports the closing balance as the negative number it is,
        # rather than as a large credit.
        self.assertRegex(output, r"CLBD\s+-\d")

    def test_every_statement_it_reads_reconciles(self):
        statements = self.overdraw()
        output = self.run_statement(statements).stdout.decode("utf-8")
        shown = [line for line in output.splitlines() if "CLBD" in line]
        self.assertTrue(shown, output)
        for line in shown:
            self.assertIn("reconciles", line)

    def test_it_says_so_when_they_do_not_add_up(self):
        # The check has to be able to fail, or it is decoration. statement-gap
        # is the behaviour that produces a real one; this is the same shape,
        # built by hand so the test does not depend on that behaviour's details.
        broken = self.overdraw().replace(b"<Amt Ccy=\"EUR\">100.00</Amt>",
                                         b"<Amt Ccy=\"EUR\">999.00</Amt>", 1)
        output = self.run_statement(broken).stdout.decode("utf-8")
        self.assertIn("DOES NOT RECONCILE", output, output)

    def test_nothing_in_is_not_a_crash(self):
        result = self.run_statement(b"")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no statements", result.stdout.decode("utf-8"))


class TheWorkflowRerunsOnALabelChange(unittest.TestCase):
    """The `no changelog` label has to be able to turn a red run green.

    `tools/check_changelog.py` reads the labels from the event, so without
    `labeled` in the trigger, applying the label ran nothing and re-running the
    failed job reused the original event. The only way to get a run that saw the
    label was to close and reopen the pull request, which happened on #37.

    A test cannot prove what GitHub does with a workflow; what it can do is stop
    the types being dropped again by somebody tidying the file. The behaviour
    itself is demonstrated on the pull request, by toggling the label.
    """

    ROOT = os.path.dirname(HERE)

    def workflow(self):
        with open(os.path.join(self.ROOT, ".github", "workflows", "ci.yml"),
                  encoding="utf-8") as handle:
            return handle.read()

    def test_the_pull_request_trigger_listens_for_label_changes(self):
        text = self.workflow()
        types = re.search(r"pull_request:\s*\n\s*types: \[([^\]]*)\]", text)
        self.assertIsNotNone(types, "the pull_request trigger has no types list")
        listed = {name.strip() for name in types.group(1).split(",")}
        # The three defaults have to stay, or ordinary pushes stop running.
        for needed in ("opened", "synchronize", "reopened", "labeled", "unlabeled"):
            with self.subTest(type=needed):
                self.assertIn(needed, listed)

    def test_the_concurrency_group_is_keyed_on_the_ref(self):
        # Which is what keeps a label change from queueing a second full run
        # beside the one already going.
        text = self.workflow()
        self.assertIn("group: ci-${{ github.ref }}", text)
        self.assertIn("cancel-in-progress: true", text)

    def test_the_changelog_check_still_reads_the_labels(self):
        # If this ever stops being passed, the label does nothing however many
        # event types the trigger lists.
        self.assertIn("--labels", self.workflow())


class TheTourOnlyUsesEndpointsThisMockHas(unittest.TestCase):
    """A static check, so it holds on the runners that cannot run bash.

    The tour is the first thing a newcomer runs. A step that reaches for a path
    the mock does not serve would give them a 404 as their introduction, and the
    smoke job only covers Linux.
    """

    PATHS = re.compile(r'\$BASE(/[A-Za-z0-9_/.-]*)')

    def script(self):
        with open(os.path.join(EXAMPLES, "demo.sh"), encoding="utf-8") as handle:
            return handle.read()

    def served(self):
        """Every path in SUPPORTED, with the <id> placeholders as patterns."""
        patterns = []
        for endpoint in SUPPORTED:
            path = endpoint.split(" ", 1)[1]
            # Split first, escape the pieces: `re.escape` leaves `<` and `>`
            # alone on 3.7 and later, so escaping and then replacing the escaped
            # form matched nothing and every `<id>` path failed.
            pattern = "[^/]+".join(re.escape(part) for part in path.split("<id>"))
            patterns.append(re.compile("^" + pattern + "$"))
        return patterns

    def test_every_path_the_tour_calls_is_one_the_mock_serves(self):
        served = self.served()
        found = set(self.PATHS.findall(self.script()))
        self.assertTrue(found, "no paths found in demo.sh; has it moved?")
        for path in sorted(found):
            with self.subTest(path=path):
                self.assertTrue(any(pattern.match(path) for pattern in served),
                                "demo.sh calls %s, which this mock does not "
                                "serve" % path)

    def test_it_asks_before_it_acts(self):
        # The tour reads /_mock/state's `supported` and skips what is missing,
        # naming it, rather than requiring a particular command line.
        script = self.script()
        self.assertIn('"supported"', script)
        self.assertIn("skipped: this mock does not answer", script)

    def test_it_honours_credentials_without_a_second_copy_of_itself(self):
        script = self.script()
        self.assertIn("BANK_AUTH", script)
        # One curl invocation that knows about --auth, not a fork of the tour.
        self.assertEqual(script.count("CURL+=(-u"), 1)



class TheIntegrationsThatMoved(unittest.TestCase):
    """`mockbank.examples` says where a moved example went.

    `from mockbank.examples import payment_run` worked from 0.6.0 to 0.7.0 and
    mock-films used it. Python's own message for a name that is gone is that it
    cannot be imported, which is true and sends the reader nowhere.
    """

    def test_asking_for_one_names_mock_acme(self):
        sys.path.insert(0, ROOT)
        try:
            import examples
        finally:
            sys.path.remove(ROOT)
        self.assertEqual(set(examples.MOVED),
                         {"bank_messages", "invoice_check", "pay_invoices",
                          "payment_run", "procure_to_pay"})
        for name in examples.MOVED:
            with self.subTest(name=name):
                self.assertFalse(
                    os.path.exists(os.path.join(EXAMPLES, name + ".py")),
                    "a file by that name is back, and this message would hide it")
                with self.assertRaises(ImportError) as moved:
                    getattr(examples, name)
                self.assertIn("mockacme.%s" % name, str(moved.exception))
                self.assertIn("https://github.com/rseufert/mock-acme",
                              str(moved.exception))
                # mock-acme has been on PyPI since 0.1.0 (#202), so the message
                # carries the one command that fixes the import rather than
                # sending the reader to a repository to work it out.
                self.assertIn("pip install mock-acme", str(moved.exception))

    def test_the_from_import_a_caller_actually_wrote_gets_that_message(self):
        sys.path.insert(0, ROOT)
        try:
            with self.assertRaises(ImportError) as moved:
                from examples import payment_run                # noqa: F401
        finally:
            sys.path.remove(ROOT)
        self.assertIn("mock-acme", str(moved.exception))

    def test_a_name_that_was_never_there_is_an_ordinary_attribute_error(self):
        sys.path.insert(0, ROOT)
        try:
            import examples
        finally:
            sys.path.remove(ROOT)
        with self.assertRaises(AttributeError):
            examples.nothing_by_this_name

    def test_what_stayed_still_imports(self):
        sys.path.insert(0, ROOT)
        try:
            from examples import client, statement             # noqa: F401
        finally:
            sys.path.remove(ROOT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
