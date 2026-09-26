"""mock-bank - an open source mock bank.

The mock is the counterparty that receives payment instructions and answers
the way a bank does: a ``pain.001`` arrives, a ``pain.002`` status report goes
back, debit notifications follow on the settlement date, a ``camt.053``
statement closes each business day, and a ``pacs.004`` can bring a payment
back days later.  It holds no real money, runs no screening, and keeps its
state in SQLite.
"""
import os
import re

__all__ = ["Config", "make_server", "__version__"]


def _discover_version():
    """pyproject.toml is the single source of truth for the version.

    Running from a checkout, the pyproject.toml sitting next to the package is
    authoritative and the version is marked `+source`; stale build metadata in
    the working tree (a leftover *.egg-info directory, say) would otherwise
    shadow it and report a version that has already moved on. Installed - in
    site-packages, a wheel, a container - there is no pyproject.toml alongside,
    and the version recorded at build time is read instead.
    """
    pyproject = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "pyproject.toml")
    try:
        with open(pyproject, encoding="utf-8") as handle:
            match = re.search(r'^version\s*=\s*"([^"]+)"', handle.read(), re.M)
        if match:
            return match.group(1) + "+source"
    except OSError:
        pass
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8
        return "0+unknown"
    try:
        return version("mock-bank")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _discover_version()

from .server import Config, make_server  # noqa: E402,F401
