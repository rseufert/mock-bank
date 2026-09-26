"""--auth, and the warning when the control plane is open to the network.

The Dockerfile binds `0.0.0.0`, which is right — `127.0.0.1` inside a container
is unreachable from outside — and it means anyone who can reach the port can
`POST /_mock/reset`, rewrite every balance and behaviour, and read every message
the bank wrote. `--auth` is the answer and the warning is what says so to
somebody who has not thought about it.

The `401`/`200` behaviour is tested over real sockets like everything else, and
the warning is tested twice: once on the function that composes it, for the
host shapes that are tedious to bind, and once on a real `python -m mockbank`
subprocess, because where it sits in startup and whether `-q` reaches it are
part of what matters.
"""
import base64
import io
import os
import queue
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockbank.__main__ import (build_parser, check_auth, exposure_warning,   # noqa: E402
                               is_loopback, main)
from mockbank.server import SUPPORTED, Config                               # noqa: E402

from support import MockServerCase                                          # noqa: E402

USER, PASSWORD = "tester", "s3cret"
CREDENTIAL = "%s:%s" % (USER, PASSWORD)


def basic(user=USER, password=PASSWORD):
    raw = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
    return {"Authorization": "Basic " + raw}


class WithoutAuth(MockServerCase):
    """The default: no credentials wanted, none refused."""

    def test_an_ordinary_request_is_answered(self):
        self.assertEqual(self.get("/_mock/health").status, 200)

    def test_credentials_nobody_asked_for_are_not_an_error(self):
        self.assertEqual(self.get("/_mock/health", headers=basic()).status, 200)


class WithAuth(MockServerCase):
    config_kwargs = {"auth": CREDENTIAL}

    def test_no_credentials_is_a_401_that_says_how_to_authenticate(self):
        resp = self.get("/_mock/health")
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.headers["WWW-Authenticate"],
                         'Basic realm="mock-bank"')
        self.assertIn("--auth", resp.json()["error"])

    def test_the_right_credentials_are_a_200(self):
        resp = self.get("/_mock/health", headers=basic())
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_the_wrong_password_is_a_401(self):
        self.assertEqual(
            self.get("/_mock/health", headers=basic(password="wrong")).status, 401)

    def test_the_wrong_user_is_a_401(self):
        self.assertEqual(
            self.get("/_mock/health", headers=basic(user="someone")).status, 401)

    def test_a_password_that_is_a_prefix_of_the_right_one_is_a_401(self):
        # The comparison is over the whole `user:password`, so a partly-right
        # credential is as wrong as an empty one.
        self.assertEqual(
            self.get("/_mock/health", headers=basic(password=PASSWORD[:-1])).status,
            401)

    def test_every_endpoint_is_behind_it_including_health_and_the_index(self):
        """Every endpoint the server declares, not a list I kept up by hand.

        Taken from `server.SUPPORTED`, so an endpoint added without the flag
        covering it fails here rather than waiting to be noticed. The hand-kept
        version had already drifted: it named `/_mock/statements`, which is not
        a path this mock has - the statements live under an account - so it was
        asserting a 401 on nothing, and it had missed `/_mock/behaviours`
        entirely.

        Health and the index are in the list on purpose. A mock that answers an
        unauthenticated probe has told whoever is probing that it is there and
        which version it is.
        """
        self.assertGreater(len(SUPPORTED), 8, "SUPPORTED looks empty")
        for endpoint in SUPPORTED:
            method, path = endpoint.split(" ", 1)
            path = path.replace("<id>", "ACME" if "accounts" in path else "1")
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.request(method, path).status, 401)

    def test_a_path_it_does_not_have_is_a_401_rather_than_a_404(self):
        # The check runs before routing, so a 404 cannot be used to map what
        # exists on an unguarded-looking port.
        for path in ("/no/such/path", "/_mock/nonesuch", "/payments/extra"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status, 401)

    def test_a_reset_without_credentials_does_not_reset_anything(self):
        self.patch("/_mock/accounts/ACME", {"behaviour": "silent"},
                   headers=basic())
        self.assertEqual(self.post("/_mock/reset").status, 401)
        self.assertEqual(
            self.get("/_mock/accounts/ACME", headers=basic()).json()["behaviour"],
            "silent")
        self.post("/_mock/reset", headers=basic())

    def test_a_malformed_authorization_header_is_a_401_not_a_500(self):
        for header in ({"Authorization": "Basic not-base64!!"},
                       {"Authorization": "Basic " + base64.b64encode(
                           b"\xff\xfe").decode()},
                       {"Authorization": "Bearer " + base64.b64encode(
                           CREDENTIAL.encode()).decode()},
                       {"Authorization": "Basic"},
                       {"Authorization": ""}):
            with self.subTest(header=header):
                self.assertEqual(self.get("/_mock/health", headers=header).status,
                                 401)

    def test_a_non_ascii_wrong_credential_is_a_clean_401(self):
        # `hmac.compare_digest` raises TypeError on a str with non-ASCII in it
        # rather than returning False, and the check runs outside the handler's
        # try, so this used to drop the connection with no response at all.
        resp = self.get("/_mock/health", headers=basic(user="b\u00e4nk"))
        self.assertEqual(resp.status, 401)

    def test_a_lowercase_scheme_name_authenticates(self):
        # RFC 7235: the scheme is case-insensitive, and some clients send it
        # lowercase.
        raw = base64.b64encode(CREDENTIAL.encode()).decode()
        for scheme in ("Basic", "basic", "BASIC", "BaSiC"):
            with self.subTest(scheme=scheme):
                resp = self.get("/_mock/health",
                                headers={"Authorization": scheme + " " + raw})
                self.assertEqual(resp.status, 200)

    def test_an_unauthenticated_payment_file_is_not_read(self):
        # The check runs before the body, so a file from someone with no
        # credentials is never parsed - and nothing is recorded about it.
        resp = self.post("/payments", body="<Document><nonsense/></Document>")
        self.assertEqual(resp.status, 401)
        self.assertEqual(
            self.get("/_mock/state", headers=basic()).json()["payments"]["files"], 0)

    def test_the_401_is_in_the_request_log(self):
        # "Someone is hitting this port without credentials" is exactly what a
        # request log is for, so the refused request is still recorded.
        before = self.get("/_mock/state", headers=basic()).json()["requests"]
        self.get("/_mock/health")
        after = self.get("/_mock/state", headers=basic()).json()["requests"]
        self.assertEqual(after, before + 2)


class WithNonAsciiAuth(MockServerCase):
    """A credential with an accent in it has to work, or the port is bricked.

    `--auth b\u00e4nk:...` passed `check_auth`, so the operator believes the mock
    is guarded - and before the comparison moved to bytes, nothing could ever
    authenticate against it and every request dropped its connection.
    """

    config_kwargs = {"auth": "b\u00e4nk:gr\u00fc\u00dfe"}

    def test_it_accepts_itself(self):
        resp = self.get("/_mock/health",
                        headers=basic(user="b\u00e4nk", password="gr\u00fc\u00dfe"))
        self.assertEqual(resp.status, 200)

    def test_and_still_refuses_the_wrong_one(self):
        self.assertEqual(
            self.get("/_mock/health",
                     headers=basic(user="b\u00e4nk", password="wrong")).status, 401)
        self.assertEqual(
            self.get("/_mock/health",
                     headers=basic(user="bank", password="gr\u00fc\u00dfe")).status, 401)


class TheExposureWarning(unittest.TestCase):

    def warning(self, **kwargs):
        return exposure_warning(Config(**kwargs))

    def test_loopback_says_nothing(self):
        for host in ("127.0.0.1", "localhost", "::1", "[::1]", "app.localhost"):
            with self.subTest(host=host):
                self.assertEqual(self.warning(host=host), "")

    def test_every_interface_with_no_auth_warns_and_names_the_flag(self):
        for host in ("0.0.0.0", "", "::", "*"):
            with self.subTest(host=host):
                message = self.warning(host=host)
                self.assertIn("WARNING", message)
                self.assertIn("--auth", message)
                # And what is at stake, not just that something is wrong.
                self.assertIn("/_mock/reset", message)

    def test_a_routable_address_with_no_auth_warns(self):
        self.assertIn("WARNING", self.warning(host="192.168.1.5"))

    def test_auth_silences_it_because_the_port_is_then_guarded(self):
        self.assertEqual(self.warning(host="0.0.0.0", auth=CREDENTIAL), "")

    def test_what_counts_as_loopback(self):
        for host in ("127.0.0.1", "127.0.0.53", "localhost", "::1", "[::1]",
                     "app.localhost"):
            with self.subTest(host=host, expected=True):
                self.assertTrue(is_loopback(host))
        for host in ("", "0.0.0.0", "::", "*", "192.168.1.5", "10.0.0.1",
                     "bank.internal", "localhost.example.com"):
            with self.subTest(host=host, expected=False):
                self.assertFalse(is_loopback(host))

    def test_a_host_that_is_not_an_address_is_treated_as_exposed(self):
        # A name the mock cannot resolve to a loopback address might be one,
        # and might not. Warning about a guarded port is a nuisance; staying
        # quiet about an open one is the failure worth avoiding.
        self.assertIn("WARNING", self.warning(host="bank.internal"))


class TheWarningAtStartup(unittest.TestCase):
    """It has to reach the stderr of a real process, and -q must not silence it.

    Driven as a subprocess rather than by calling `main()` with something that
    makes it return early: where the warning sits in startup is part of what
    matters, and a lever that exits before the warning would test nothing while
    looking like it tested something.

    Getting the *waiting* right took three attempts, and the two failures are
    worth recording because both looked fine locally:

    1. Reading the pipe line by line on this thread, with a deadline checked
       between reads. On Python 3.8 a piped stderr is block-buffered, so the
       read never returned and the deadline never came up for inspection: the
       job hung for ten minutes until the watchdog killed it. That one was
       hiding a real product bug - an unflushed warning never reaches
       `docker logs` - which `main()` now fixes with `flush=True`.
    2. Waiting for the port to answer, then terminating and reading. The socket
       is bound inside `make_server`, which returns *before* `main` prints the
       banner, so a successful connect proved only that the socket existed - and
       on the slow macOS runner the terminate beat the print and stderr came
       back empty. The shape was wrong: it inferred "startup finished" from a
       different channel to the one the assertions are about.

    So: a thread reads stderr into a queue and this thread waits on the queue,
    with a real timeout, for the line that means startup is over. Nothing
    blocks without a deadline, and the evidence comes from the stream being
    asserted on. See `BANNER` for the third mistake, which this shape made
    easy to make and easy to find.
    """

    ROOT = os.path.dirname(HERE)

    # The banner, and nothing else. "listening on" alone will not do: the
    # warning says "listening on 0.0.0.0 with no --auth", so it matched the
    # warning, the wait ended at the first line, and the assertions that need
    # the banner passed on the warning instead. Only the banner has the URL.
    BANNER = "listening on http://"
    WARNING = "WARNING"

    def startup_stderr(self, *extra, until=BANNER):
        """The mock's stderr up to and including the first line containing `until`.

        `until` is the line that means "there is no more startup output coming":
        the banner normally, since it is the last thing printed before
        `serve_forever`, and the warning under `-q`, which suppresses the
        banner and so leaves nothing later to wait for.
        """
        process = subprocess.Popen(
            [sys.executable, "-m", "mockbank", "--port", "0"] + list(extra),
            cwd=self.ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        self.addCleanup(self.stop, process)
        lines = queue.Queue()

        def drain():
            for line in process.stderr:
                lines.put(line)
            lines.put(None)                     # end of stream

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        collected, deadline = [], time.time() + 60
        while time.time() < deadline:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                continue
            if line is None:
                break
            collected.append(line)
            if until in line:
                break
        err = "".join(collected)
        self.assertIn(until, err,
                      "waited for %r and never saw it; stderr was %r" % (until, err))
        return err

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:    # pragma: no cover
                process.kill()
                process.wait(timeout=30)
        process.stdout.close()
        process.stderr.close()

    def test_binding_every_interface_without_auth_prints_the_warning(self):
        err = self.startup_stderr("--host", "0.0.0.0")
        self.assertIn("WARNING", err)
        self.assertIn("--auth", err)
        self.assertIn("/_mock/reset", err)

    def test_it_is_flushed_rather_than_left_in_a_buffer(self):
        # Before 3.9 a piped stderr is block-buffered, so an unflushed warning
        # reaches `docker logs` some minutes after the port opens, if at all.
        # What makes this a test rather than a repeat of the one above is that
        # it reads the pipe while the process is still running: an unflushed
        # warning would not be there yet.
        self.assertIn("WARNING", self.startup_stderr("--host", "0.0.0.0"))

    def test_quiet_does_not_silence_it(self):
        # -q means "no line per request", not "do not mention that the bank is
        # open to the network". -q does suppress the banner, so there is none
        # to wait for here.
        # -q suppresses the banner, so the warning is the last line of startup
        # and the thing to wait for.
        err = self.startup_stderr("--host", "0.0.0.0", "-q", until=self.WARNING)
        self.assertIn("WARNING", err)

    def test_loopback_prints_no_warning(self):
        self.assertNotIn("WARNING", self.startup_stderr("--host", "127.0.0.1"))

    def test_auth_silences_it_on_every_interface(self):
        err = self.startup_stderr("--host", "0.0.0.0", "--auth", CREDENTIAL)
        self.assertNotIn("WARNING", err)


class StartingUpDoesNotWaitForDns(unittest.TestCase):
    """The bind must not ask DNS what this machine is called.

    `HTTPServer.server_bind` sets `server_name` from `socket.getfqdn(host)`. On
    a host with no reverse record for the address it is binding, that waits for
    DNS to time out before the mock finishes starting - a minute on the macOS
    runner, where it was found. A timing assertion would be flaky, so this pins
    the thing that made it slow: the name is the address as given, not something
    a resolver invented.
    """

    def test_the_server_name_is_the_address_it_was_told_to_bind(self):
        from mockbank.server import Config, make_server
        httpd = make_server(Config(host="127.0.0.1", port=0, quiet=True))
        try:
            self.assertEqual(httpd.server_name, "127.0.0.1")
            # What the lookup would have produced instead.
            self.assertNotIn("arpa", httpd.server_name)
        finally:
            httpd.server_close()


class AuthThatCouldNeverWork(unittest.TestCase):
    """`--auth secret` looks like it works, and then nothing can authenticate."""

    def test_no_colon_is_refused_naming_the_shape(self):
        problem = check_auth("secret")
        self.assertIn("USER:PASSWORD", problem)
        self.assertIn("colon", problem)

    def test_an_empty_user_or_password_is_refused(self):
        self.assertIn("user", check_auth(":password"))
        self.assertIn("password", check_auth("user:"))

    def test_a_usable_credential_is_accepted(self):
        self.assertEqual(check_auth(CREDENTIAL), "")

    def test_no_flag_at_all_is_fine(self):
        self.assertEqual(check_auth(""), "")

    def test_a_password_containing_a_colon_is_kept_whole(self):
        # Split once: a password may contain colons, a user may not.
        self.assertEqual(check_auth("user:pass:word"), "")

    def test_the_mock_refuses_to_start_rather_than_refusing_every_request(self):
        captured = io.StringIO()
        with redirect_stderr(captured):
            code = main(["--auth", "secret", "--port", "0"])
        self.assertEqual(code, 2)
        self.assertIn("USER:PASSWORD", captured.getvalue())


class TheFlagIsDocumented(unittest.TestCase):
    """The README's Docker example has to pass --auth, since it binds 0.0.0.0."""

    ROOT = os.path.dirname(HERE)

    def readme(self):
        with open(os.path.join(self.ROOT, "README.md"), encoding="utf-8") as handle:
            return handle.read()

    def test_auth_is_a_flag_the_parser_takes(self):
        flags = {option for action in build_parser()._actions
                 for option in action.option_strings}
        self.assertIn("--auth", flags)

    def test_the_docker_run_example_passes_auth(self):
        readme = self.readme()
        docker = readme[readme.index("## Docker"):]
        docker = docker[:docker.index("\n## ")]
        self.assertIn("docker run", docker)
        # The image binds 0.0.0.0, so an example without --auth is an example
        # of the thing this issue exists to warn about.
        self.assertIn("--auth", docker)


if __name__ == "__main__":
    unittest.main(verbosity=2)
