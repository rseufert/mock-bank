"""`POST /_mock/validate`: a file's findings, with nothing stored."""
from __future__ import annotations

from .. import validate
from . import route


@route("POST", "/_mock/validate",
       note=("send a pain.001 or a NACHA file, get its findings as prose, "
            "one line each; nothing is stored"))
def validate_file(h) -> None:
    """The findings as prose, one line each, or as JSON when asked for.

    A pain.001, or a NACHA file: this door reads both, and says which in the
    first line (the JSON's `file.message`).
    """
    payment_file, findings = validate.inspect(
        h.body, h.headers.get("Content-Type"), h.state.clock.today())
    status = 422 if validate.errors(findings) else 200
    if "application/json" in (h.headers.get("Accept") or ""):
        return h.json(status, {
            "file": payment_file.to_json() if payment_file else None,
            "findings": [f._asdict() for f in findings],
        })
    lines = [validate.render(f) for f in findings]
    if payment_file is not None:
        batches, payments = len(payment_file.batches), len(payment_file.payments)
        lines.insert(0, "%s %s: %d batch%s, %d payment%s, %d finding%s" % (
            payment_file.message, payment_file.msg_id or "(no MsgId)",
            batches, "" if batches == 1 else "es",
            payments, "" if payments == 1 else "s",
            len(findings), "" if len(findings) == 1 else "s"))
    h.text(status, "\n".join(lines) + "\n")
