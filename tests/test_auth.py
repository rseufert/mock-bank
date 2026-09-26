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
import socket
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockbank.__main__ import (build_parser, check_auth, exposure_warning,   # noqa: E402
                               is_loopback, main)
from mockbank.server import Config                                          # noqa: E402

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
        # A mock that answers an unauthenticated probe has told whoever is
        # probing that it is there and which version it is.
        for method, path in (("GET", "/"), ("GET", "/_mock/health"),
                             ("GET", "/_mock/state"),
                             ("GET", "/_mock/accounts"),
                             ("GET", "/_mock/accounts/ACME"),
                             ("GET", "/_mock/dictionary"),
                             ("GET", "/_mock/holidays"),
                             ("GET", "/_mock/mailbox"),
                             ("GET", "/_mock/payments"),
                             ("POST", "/_mock/reset"),
                             ("POST", "/_mock/advance?days=1"),
                             ("POST", "/payments"),
                             ("POST", "/_mock/validate"),
                             ("PATCH", "/_mock/accounts/ACME"),
                             ("PUT", "/_mock/holidays"),
                             ("GET", "/no/such/path")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path).status, 401)

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

    Nothing here reads from the pipe while the child is alive. The first
    version did, line by line with a deadline checked between reads, and on
    Python 3.8 - where a piped stderr is block-buffered - the read simply never
    returned and the deadline never came up for inspection. The job hung for
    ten minutes and the watchdog killed it. So: wait for the port to answer,
    which is proof that startup finished, then stop the process and read
    everything at once, where EOF is guaranteed.
    """

    ROOT = os.path.dirname(HERE)

    def free_port(self):
        """A port nothing is listening on, so the poll below has an address.

        `--port 0` would be tidier, but the port it chose could then only be
        learnt by reading the banner, which is the thing that must not be read
        while the process is running.
        """
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def startup_stderr(self, *extra):
        """Everything the mock wrote to stderr up to the moment it was serving."""
        port = self.free_port()
        process = subprocess.Popen(
            [sys.executable, "-m", "mockbank", "--port", str(port)] + list(extra),
            cwd=self.ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                if process.poll() is not None:
                    break                       # it exited; read what it said
                try:
                    with socket.create_connection(("127.0.0.1", port), 0.25):
                        break                   # listening, so startup is done
                except OSError:
                    time.sleep(0.1)
            if process.poll() is None:
                process.terminate()
            return process.communicate(timeout=30)[1]
        finally:
            if process.poll() is None:          # pragma: no cover - stubborn child
                process.kill()
                process.communicate(timeout=30)

    def test_binding_every_interface_without_auth_prints_the_warning(self):
        err = self.startup_stderr("--host", "0.0.0.0")
        self.assertIn("WARNING", err)
        self.assertIn("--auth", err)
        self.assertIn("/_mock/reset", err)
        # And it really did start: the warning is a warning, not a refusal.
        self.assertIn("listening on", err)

    def test_it_is_flushed_rather_than_left_in_a_buffer(self):
        # Before 3.9 a piped stderr is block-buffered, so an unflushed warning
        # reaches `docker logs` some minutes after the port opens, if at all.
        # This is the same assertion as above; what makes it a separate test is
        # that the stderr here was collected through a pipe, which is how a
        # container reads it, and while the process was still running.
        self.assertIn("WARNING", self.startup_stderr("--host", "0.0.0.0"))

    def test_quiet_does_not_silence_it(self):
        # -q means "no line per request", not "do not mention that the bank is
        # open to the network".
        self.assertIn("WARNING", self.startup_stderr("--host", "0.0.0.0", "-q"))

    def test_loopback_prints_no_warning(self):
        err = self.startup_stderr("--host", "127.0.0.1")
        self.assertIn("listening on", err)
        self.assertNotIn("WARNING", err)

    def test_auth_silences_it_on_every_interface(self):
        err = self.startup_stderr("--host", "0.0.0.0", "--auth", CREDENTIAL)
        self.assertIn("listening on", err)
        self.assertNotIn("WARNING", err)


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
