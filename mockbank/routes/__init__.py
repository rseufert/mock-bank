"""The route table, and the modules that fill it.

Each module here is one surface of the mock - the control plane, the accounts,
the clock, the payments, the mailbox, validation, the folder transport - and
registers its endpoints with :func:`route`: a method, a path pattern and the
function that answers it. The handler looks a request up with :func:`find` and
knows nothing about any endpoint, so adding one touches one file here.

A pattern is written the way the index page and the 404 body list it, with
``<name>`` for a segment that can be anything: ``/_mock/accounts/<id>``. A
route function is called with the handler and each placeholder's segment,
percent-decoded, in order. A registration can carry a note, the line the index
page shows beside it. `SUPPORTED`, the list the index, the 404 body and
`/_mock/state` give, is computed from the table, so nothing lists endpoints by
hand. The control plane is matched segment by segment, so
``/_mock/health/`` is health; the index and ``POST /payments`` are matched on
the path exactly as sent, so ``/payments/`` is not the door a file goes
through.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple


class Route(NamedTuple):
    method: str
    pattern: str
    parts: Tuple[str, ...]
    function: Callable[..., None]
    note: str
    listed: bool


# Every endpoint, in the order the modules registered them. The order is the
# order a 405 lists the allowed methods in; no two patterns match one path
# with the same method, so it never decides which function answers.
TABLE: List[Route] = []


def route(method: str, pattern: str, note: str = "", listed: bool = True):
    """Register the decorated function as the answer to ``method pattern``.

    ``note`` is a line of explanation for an endpoint that is not self-evident
    from its path. ``listed=False`` is for an alias, answered but not
    advertised: ``/index.html`` is the index page under another name.
    """
    def register(function):
        TABLE.append(Route(method, pattern, tuple(segments(pattern)), function,
                           note, listed))
        return function
    return register


def find(method: str, path: str) -> Tuple[Optional[Callable[..., None]],
                                          List[str], List[str]]:
    """The function for a request, its arguments, and the methods allowed.

    Returns ``(function, arguments, [])`` when the path and the method match,
    ``(None, [], allowed)`` when the path matches only with other methods -
    a 405 naming them - and ``(None, [], [])`` when nothing matches, a 404.
    """
    given = segments(path)
    allowed: List[str] = []
    for entry in TABLE:
        if entry.pattern.startswith("/_mock"):
            arguments = _match(entry.parts, given)
        else:
            arguments = [] if path == entry.pattern else None
        if arguments is None:
            continue
        if entry.method == method:
            return entry.function, arguments, []
        if entry.method not in allowed:
            allowed.append(entry.method)
    return None, [], allowed


def _match(parts: Tuple[str, ...], given: List[str]) -> Optional[List[str]]:
    if len(parts) != len(given):
        return None
    arguments = []
    for part, value in zip(parts, given):
        if part.startswith("<") and part.endswith(">"):
            arguments.append(value)
        elif part != value:
            return None
    return arguments


# ---------------------------------------------------------------------------
# What a route reads from the request
# ---------------------------------------------------------------------------

def segments(path: str) -> List[str]:
    """The segments of a still-encoded path, each percent-decoded once."""
    return [urllib.parse.unquote(part) for part in path.split("/") if part]


def first(query: Dict[str, List[str]], name: str, default: str = "") -> str:
    values = query.get(name) or []
    return values[0] if values else default


def flag(query: Dict[str, List[str]], name: str) -> bool:
    """A flag written `?leave` or `?raw`, with or without a value.

    `keep_blank_values` in the handler's query parse is what makes the
    valueless form visible at all; this is what makes `?leave=0` mean what it
    says rather than being true because the parameter was present.
    """
    if name not in query:
        return False
    return first(query, name, "").lower() in ("", "1", "true", "yes", "on")


def json_body(body: bytes) -> Any:
    """The body as a JSON object, `{}` when there is none, `None` when it is not one."""
    if not body.strip():
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# Imported last, so that `route` and the helpers above exist when each module
# asks for them. The order here is the order of the table.
from . import (control, accounts, clock, payments, credits, mailbox,  # noqa: E402,F401
               validate, transport)

# What this build answers, in the order the modules registered it, so a 404
# can say so and the index can list it; and the notes the index shows.
SUPPORTED = ["%s %s" % (entry.method, entry.pattern) for entry in TABLE if entry.listed]
NOTES = {"%s %s" % (entry.method, entry.pattern): entry.note
         for entry in TABLE if entry.note}
