"""Command line entry point: ``python -m mockbank`` / ``mock-bank``."""
from __future__ import annotations

import argparse
import ipaddress
import sys

from . import __version__
from .accounts import BEHAVIOURS
from .clock import DEFAULT_CUTOFF, Invalid
from .db import Unusable
from .server import Config, make_server


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mock-bank",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run a mock bank (ISO 20022 credit transfers in, status "
                    "reports, statements and returns out).",
        epilog="account behaviours:\n" + "\n".join(
            "  %-20s %s" % (name, text) for name, text in BEHAVIOURS.items()))
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080, help="port (default: 8080)")
    p.add_argument("--db", dest="db_path", default=":memory:",
                   help="SQLite file, or :memory: (default) for a throwaway bank")
    p.add_argument("--timezone", default="UTC",
                   help="bank time's zone, an IANA name such as "
                        "Europe/Amsterdam (default: UTC). Needs zoneinfo, so "
                        "on Python 3.8 only UTC is available and anything else "
                        "is refused rather than silently treated as UTC")
    p.add_argument("--cutoff", default=DEFAULT_CUTOFF, metavar="HH:MM",
                   help="the hour the bank stops taking today's payments for "
                        "today; one received at or after it settles on the "
                        "next business day (default: %s)" % DEFAULT_CUTOFF)
    p.add_argument("--clock", default="", metavar="YYYY-MM-DDTHH:MM",
                   help="start bank time at this moment instead of now, for a "
                        "run whose settlement dates are reproducible")
    p.add_argument("--auth", default="", metavar="USER:PASSWORD",
                   help="require HTTP basic credentials on every request. "
                        "Without it, anyone who can reach the port can POST "
                        "/_mock/reset, rewrite every account and read every "
                        "message")
    p.add_argument("--keep-requests", type=int, default=5000, metavar="N",
                   help="keep only the newest N rows of the request log, so a "
                        "mock left running for weeks stays bounded (default: "
                        "5000; 0 keeps every row)")
    p.add_argument("--retention-days", type=float, default=0.0, metavar="D",
                   help="remove request-log rows and already-collected messages "
                        "older than D days (default: off). Payments, files and "
                        "uncollected messages are never removed: they are the "
                        "evidence a failing test is read against")
    p.add_argument("--allow-duplicates", action="store_true",
                   help="accept a file whose MsgId the bank has seen before "
                        "(by default it is rejected with DUPL, as a real bank does)")
    p.add_argument("--status-delay-ms", type=int, default=0, metavar="MS",
                   help="how long after a file arrives its pain.002 is due "
                        "(default: 0, so a test sees it at once; a few minutes "
                        "is what a real bank takes)")
    p.add_argument("-q", "--quiet", action="store_true", help="log nothing per request")
    p.add_argument("--version", action="version", version="mock-bank " + __version__)
    return p


def is_loopback(host: str) -> bool:
    """Whether a bind address can only be reached from this machine.

    An empty host, `0.0.0.0`, `::` and `*` all mean every interface, which is
    the right default in a container - `127.0.0.1` inside one is unreachable
    from outside - and exactly the case worth saying something about.
    """
    host = (host or "").strip().strip("[]").lower()
    if not host or host in ("0.0.0.0", "::", "*"):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost" or host.endswith(".localhost")


def exposure_warning(config) -> str:
    """What to say when the control plane is reachable and unguarded.

    `/_mock` can reset the database, rewrite every account's balance and
    behaviour and read every message the bank has written. On a laptop bound to
    loopback that is the whole point. Bound to an address other machines can
    reach, with no `--auth`, it is worth saying out loud once rather than
    leaving someone to find out.

    Returned rather than printed so that a test can read it without capturing
    a stream, and printed by `main` whether or not `-q` was passed: `-q` means
    "no line per request", not "do not mention that the bank is open".
    """
    if is_loopback(config.host) or config.auth:
        return ""
    return ("mock-bank: WARNING - listening on %s with no --auth. Anyone who "
            "can reach this port can POST /_mock/reset, rewrite every account's "
            "balance and behaviour, and read every message. Pass "
            "--auth USER:PASSWORD, or bind 127.0.0.1."
            % (config.host or "every interface"))


def check_auth(value: str) -> str:
    """`--auth` as given, or a message saying why it could not be used.

    `--auth secret` looks like it works and then nothing can ever authenticate
    against it, which is worse than no flag at all: the operator believes the
    port is guarded and every request is a 401.
    """
    if not value:
        return ""
    if ":" not in value:
        return ("--auth takes USER:PASSWORD, and %r has no colon, so nothing "
                "could ever authenticate against it" % value)
    user, password = value.split(":", 1)
    if not user or not password:
        return ("--auth takes USER:PASSWORD, and %r leaves the %s empty"
                % (value, "user" if not user else "password"))
    return ""


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = Config(host=args.host, port=args.port, db_path=args.db_path,
                    quiet=args.quiet, timezone=args.timezone, cutoff=args.cutoff,
                    clock=args.clock, allow_duplicates=args.allow_duplicates,
                    status_delay_ms=args.status_delay_ms, auth=args.auth,
                    keep_requests=args.keep_requests,
                    retention_days=args.retention_days)
    refused = check_auth(config.auth)
    if refused:
        print("mock-bank: %s" % refused, file=sys.stderr, flush=True)
        return 2
    warning = exposure_warning(config)
    if warning:
        # Not behind `not args.quiet`: -q is about the access log.
        #
        # Flushed, and that is not decoration. Before Python 3.9 a piped stderr
        # is block-buffered, so in a container - or anywhere the output is
        # collected rather than shown on a terminal - this sat in a buffer
        # until something else filled it. A warning that reaches `docker logs`
        # some minutes after the port opened is not a warning.
        print(warning, file=sys.stderr, flush=True)
    try:
        httpd = make_server(config)
    except (Invalid, Unusable) as error:
        # A clock the mock cannot keep, or a retention setting it cannot act
        # on, is refused at startup rather than at the first payment or the
        # first prune. A mistyped --retention-days used to be silently "off",
        # which is the worst of the three outcomes: the operator believes the
        # mock is bounded and it is not.
        print("mock-bank: %s" % error, file=sys.stderr, flush=True)
        return 2
    if not args.quiet:
        print("mock-bank %s listening on http://%s:%d/  (db: %s, bank time: "
              "%s, cutoff %s)"
              % (__version__, config.host, httpd.server_address[1],
                 config.db_path, config.timezone, config.cutoff),
              file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
