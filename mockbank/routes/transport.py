"""The folder transport: what it is doing, and a scan on demand."""
from __future__ import annotations

from typing import Any, Dict

from . import route


@route("GET", "/_mock/drop")
def drop(h) -> None:
    h.json(200, transport(h))


@route("POST", "/_mock/drop/scan")
def scan(h) -> None:
    if h.state.dropbox is None:
        return h.json(409, {
            "error": "this mock has no drop directory; start it "
                     "with --drop-dir PATH",
            "transport": transport(h)})
    found = h.state.dropbox.scan()
    h.json(200, {"scanned": len(found), "files": [f.to_json() for f in found]})


def transport(h) -> Dict[str, Any]:
    """What the folder transport is doing, or that there is none."""
    if h.state.dropbox is None:
        return {"dropDir": "", "pickupDir": "",
                "note": "no folder transport; start the mock with "
                        "--drop-dir and/or --pickup-dir"}
    return h.state.dropbox.state_json()
