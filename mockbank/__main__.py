"""Command line entry point: ``python -m mockbank`` / ``mock-bank``."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .accounts import BEHAVIOURS
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
    p.add_argument("--allow-duplicates", action="store_true",
                   help="accept a file whose MsgId the bank has seen before "
                        "(by default it is rejected with DUPL, as a real bank does)")
    p.add_argument("-q", "--quiet", action="store_true", help="log nothing per request")
    p.add_argument("--version", action="version", version="mock-bank " + __version__)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = Config(host=args.host, port=args.port, db_path=args.db_path,
                    quiet=args.quiet, allow_duplicates=args.allow_duplicates)
    httpd = make_server(config)
    if not args.quiet:
        print("mock-bank %s listening on http://%s:%d/  (db: %s)"
              % (__version__, config.host, httpd.server_address[1], config.db_path),
              file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
