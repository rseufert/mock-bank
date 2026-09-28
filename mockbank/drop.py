"""Banking over a directory instead of over HTTP.

HTTP gets the attention, but most bank connections are still a folder pair on an
SFTP host that a scheduler polls: you write a `pain.001` into one directory, the
bank picks it up; the bank writes a `pain.002` into another, you pick it up. An
integration whose outbound path is "drop a file in a folder" cannot be tested
against an HTTP endpoint, so the mock grows the same two halves a real
connection has - an inbox it reads and an outbox it writes.

This is mock-edi's `drop.py`, which has had these problems found in it already.
Two come with every directory integration, and both are handled here rather than
left to bite:

**Partial writes.** A file still being written must not be read. The convention
is write-then-rename, which makes a file appear complete or not at all, but not
every sender follows it - so a file whose modification time is within the last
few hundred milliseconds is left for the next pass, and `.tmp`, `.part` and
dotfiles are never read at all. The mock writes its own files with the rename,
because a mock that does not follow the convention it recommends is not much of
an example.

**Reprocessing.** A file that has been read must not be read again. Read files
move into `processed/`, and files the bank could not put through into `failed/`
beside a `.findings.txt` saying why - moved rather than deleted, because a mock
that eats the evidence is no use when a test fails. Two things can break that:

* *Two scanners.* The poller and `POST /_mock/drop/scan` could read one file at
  once, so a file is *claimed* before it is read - renamed to
  `<name>.processing`, which only one caller can win - and scans take turns.
* *A file that cannot be moved.* If `processed/` is not writable, the file would
  stay put and be read again on every pass. It is remembered by name, size and
  modification time, left alone until it changes, and reported in
  `GET /_mock/drop` and on stderr rather than retried in silence.

The poller is not the only way in. `POST /_mock/drop/scan` scans once and says
what it found, for the same reason `POST /_mock/advance` exists: a test that
waits for a poll interval is slow and flaky, and one that asks for a scan is
neither.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import db, validate

PROCESSED = "processed"
FAILED = "failed"
NOT_MOVED = "could not move: "

# A file this mock has claimed and is reading.
CLAIMED = ".processing"

# What a dropped file's answer is written next to, when there is anything to say.
FINDINGS_SUFFIX = ".findings.txt"

# Never read: a sender's own temporary names, the mock's claim, anything hidden,
# and the findings the mock itself writes.
IGNORED_SUFFIXES = (".tmp", ".part", ".filepart", ".writing", ".swp",
                    FINDINGS_SUFFIX, CLAIMED)


class Invalid(ValueError):
    """A folder transport the mock could not honour, and why. Refused at startup."""


def check_directories(drop_dir: str, pickup_dir: str) -> None:
    """Refuse a drop and pickup pair the mock cannot keep straight.

    **The same directory for both** feeds the bank its own output: every
    `pain.002` it writes lands where it is watching, so it reads its own status
    report back as a payment file, answers it `FF01`, writes that answer into the
    directory too, and goes round again - a mock that looks busy and is only
    talking to itself.

    **Either inside the other** is refused as well, and for a different reason,
    which is worth being accurate about: it would *not* loop, because `ready()`
    lists only the top level of the drop directory. What it would do is mix the
    bank's answers in with the files it manages - `processed/` and `failed/` live
    inside the drop directory, so a pickup directory there leaves a reader unable
    to tell what arrived from what the bank sent, and a pickup directory
    containing the drop directory does the same in reverse. It is also one
    change away from being a real loop, the change being anything that made the
    scan recurse.

    Refusing it costs nobody anything: two directories side by side is what
    every real SFTP drop looks like.
    """
    if not (drop_dir and pickup_dir):
        return
    drop = os.path.realpath(drop_dir)
    pickup = os.path.realpath(pickup_dir)
    if drop == pickup:
        raise Invalid(
            "--drop-dir and --pickup-dir are the same directory (%s). The bank "
            "would read every message it wrote back in as a payment file; give "
            "them separate directories." % drop)
    for inner, outer, which in ((pickup, drop, "--pickup-dir is inside --drop-dir"),
                                (drop, pickup, "--drop-dir is inside --pickup-dir")):
        if inner.startswith(outer + os.sep):
            raise Invalid(
                "%s (%s inside %s). The bank's answers would be mixed in with "
                "the files it manages - processed/ and failed/ are in the drop "
                "directory - and nobody reading either could tell what arrived "
                "from what was sent. Give them directories side by side."
                % (which, inner, outer))


class Scanned:
    """What became of one dropped file."""

    def __init__(self, name, ok, moved_to="", msg_id="", status="", reason="",
                 accepted=0, rejected=0, findings=None, error=""):
        self.name = name
        self.ok = ok
        self.moved_to = moved_to
        self.msg_id = msg_id
        self.status = status
        self.reason = reason
        self.accepted = accepted
        self.rejected = rejected
        self.findings = findings or []
        self.error = error

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "movedTo": self.moved_to,
                "msgId": self.msg_id, "status": self.status,
                "reason": self.reason, "accepted": self.accepted,
                "rejected": self.rejected, "findings": self.findings,
                "error": self.error}


class DropBox:
    """An inbound directory the mock reads, and an outbound one it writes."""

    def __init__(self, state, drop_dir: str = "", pickup_dir: str = "",
                 settle_ms: int = 250, interval_ms: int = 1000):
        self.state = state
        self.drop_dir = os.path.abspath(drop_dir) if drop_dir else ""
        self.pickup_dir = os.path.abspath(pickup_dir) if pickup_dir else ""
        self.settle_ms = settle_ms
        self.interval_ms = interval_ms
        # The mock's own lock, so a scan and a request cannot be inside the
        # pipeline at once. It is reentrant, which matters: a scan takes it and
        # then calls receive(), which takes it again.
        self.lock = state.lock
        self.last_scan: List[Scanned] = []
        self.scans = 0
        self.written: List[str] = []
        # Names that would have landed outside the pickup directory.
        self.refused: List[str] = []
        # Files read once that could not be moved out of the way, keyed by name,
        # with the size and modification time they had and why.
        self.stuck: Dict[str, Tuple[int, float, str]] = {}
        # Files a previous run left mid-read, renamed back at startup.
        self.reclaimed: List[str] = []
        # Files the mock could not write, by name, with why. A pickup directory
        # that fills up is otherwise the quietest possible failure.
        self.unwritten: Dict[str, str] = {}
        # Names already taken in the pickup directory, and what was written
        # instead: a reset empties the message table, so an id can recur while
        # the file from before is still waiting to be collected.
        self.renamed: List[Dict[str, str]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def active(self) -> bool:
        return bool(self.drop_dir or self.pickup_dir)

    # -- lifecycle --------------------------------------------------------

    def prepare(self) -> None:
        """Create the directories, and reclaim anything a previous run left.

        A path that cannot be created is refused here, naming the directory and
        the flag that asked for it. It used to surface through `main`'s catch-all
        as "cannot listen on 127.0.0.1:8080", which sends a reader to look at
        the port.
        """
        check_directories(self.drop_dir, self.pickup_dir)
        for path, flag in ((self.drop_dir, "--drop-dir"),
                           (self.pickup_dir, "--pickup-dir")):
            if path:
                try:
                    os.makedirs(path, exist_ok=True)
                except OSError as error:
                    raise Invalid("%s %s could not be created: %s"
                                  % (flag, path, error)) from None
        if self.drop_dir:
            for name in (PROCESSED, FAILED):
                inner = os.path.join(self.drop_dir, name)
                try:
                    os.makedirs(inner, exist_ok=True)
                except OSError as error:
                    raise Invalid("%s could not be created inside --drop-dir: %s"
                                  % (inner, error)) from None
            self.reclaim()

    def reclaim(self) -> List[str]:
        """Give back the name of any file still claimed from a previous run.

        A mock killed between claiming a file and filing it away leaves
        `<name>.processing` behind. `ready()` skips that suffix, so the file was
        invisible for ever after - not read, not filed, and not reported
        anywhere. It is renamed back on startup instead, and read again: the
        bank may have decided it already, in which case the second read is a
        DUPL and lands in `failed/` where somebody will see it, which is a far
        better outcome than a file that silently never existed.
        """
        reclaimed = []
        for name in sorted(os.listdir(self.drop_dir)):
            if not name.endswith(CLAIMED):
                continue
            path = os.path.join(self.drop_dir, name)
            if not os.path.isfile(path):
                continue
            original = path[:-len(CLAIMED)]
            try:
                os.replace(path, _free_name(original))
            except OSError as error:     # pragma: no cover - permissions
                sys.stderr.write("mock-bank: %s was left claimed by an earlier "
                                 "run and could not be renamed back: %s\n"
                                 % (name, error))
                continue
            reclaimed.append(os.path.basename(original))
        if reclaimed:
            sys.stderr.write("mock-bank: reclaimed %d file(s) an earlier run "
                             "left mid-read: %s\n"
                             % (len(reclaimed), ", ".join(reclaimed)))
        self.reclaimed = reclaimed
        return reclaimed

    def start(self) -> None:
        if self._thread is not None or not self.drop_dir:
            return
        self._thread = threading.Thread(target=self._poll, daemon=True,
                                        name="mock-bank-dropbox")
        self._thread.start()

    def stop(self, wait: float = 2.0) -> bool:
        """Ask the poller to stop, and say whether it did within `wait`."""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=wait)
        if thread.is_alive():
            return False
        self._thread = None
        return True

    def _poll(self) -> None:
        while not self._stop.wait(self.interval_ms / 1000.0):
            try:
                self.scan()
            except Exception as error:   # pragma: no cover - must not die
                # Said, not swallowed: a poller that fails in silence is how a
                # file gets read eleven times with nothing in the log.
                sys.stderr.write("mock-bank: drop scan failed: %s\n" % error)

    # -- inbound ----------------------------------------------------------

    def ready(self) -> List[str]:
        """The files worth reading on this pass, by name.

        A file is skipped when it is hidden, carries a sender's temporary
        suffix, or was modified so recently that it may still be open.
        """
        if not self.drop_dir or not os.path.isdir(self.drop_dir):
            return []
        cutoff = time.time() - (self.settle_ms / 1000.0)
        out = []
        for name in sorted(os.listdir(self.drop_dir)):
            path = os.path.join(self.drop_dir, name)
            if not os.path.isfile(path):
                continue
            if name.startswith(".") or name.lower().endswith(IGNORED_SUFFIXES):
                continue
            try:
                stat = os.stat(path)
            except OSError:              # vanished between listing and stat
                continue
            # A settle time of zero means no waiting at all, and is checked for
            # explicitly rather than falling out of the arithmetic: a file's
            # modification time can read as very slightly *ahead* of the clock,
            # so `mtime > cutoff` is true for a file just written, and zero
            # would otherwise mean "never ready" rather than "always".
            if self.settle_ms and stat.st_mtime > cutoff:
                continue
            if name in self.stuck:
                size, mtime, _reason = self.stuck[name]
                if (stat.st_size, stat.st_mtime) == (size, mtime):
                    continue             # read once already; unchanged since
                del self.stuck[name]     # changed: a new file by the same name
            out.append(name)
        return out

    def scan(self) -> List[Scanned]:
        """Read every settled file in the drop directory, once.

        Scans take turns under the mock's own lock. The scan endpoint already
        holds it - every HTTP request does - so a second lock for scans alone
        would be taken in the opposite order by the poller, and the two would
        deadlock the first time they met.
        """
        with self.lock:
            results: List[Scanned] = []
            for name in self.ready():
                taken = self._take(name)
                if taken is not None:
                    results.append(taken)
            self.scans += 1
            if results:
                self.last_scan = results
            return results

    def _take(self, name: str) -> Optional[Scanned]:
        path = os.path.join(self.drop_dir, name)
        # Claim it first. Whoever wins the rename owns the file; anyone else
        # finds it gone. A directory the mock may not rename in cannot be
        # claimed and is read where it is: scans taking turns and the stuck list
        # still see to it that it is read once.
        working = path + CLAIMED
        try:
            os.replace(path, working)
        except FileNotFoundError:
            return None
        except OSError:
            working = path
        try:
            with open(working, "rb") as handle:
                payload = handle.read()
        except OSError as error:
            self._release(working, path)
            return Scanned(name=name, ok=False, error=str(error))

        answer, decision, findings = self.state.receive(payload, source=name)

        # `failed/` means the bank could not put the file through: one it could
        # not read, or one rejected at group level for DUPL or FF01. A PART is
        # *processed* even though some payments were rejected - the file was
        # handled and the rejections are in the pain.002, which is what a real
        # bank's processed folder holds.
        ok = not decision.rejected_outright
        result = Scanned(
            name=name, ok=ok, msg_id=answer["msg_id"] or "",
            status=answer["status"], reason=answer["reason"] or "",
            accepted=answer["accepted"], rejected=answer["rejected"],
            # validate.render is what /_mock/validate prints, so the file
            # beside a failed drop says exactly what that endpoint would. There
            # was a second copy of this formatting here for a while, and it had
            # the wrong key name in it - `severity` for `level`.
            findings=[validate.render(f) for f in findings])
        result.moved_to = self._file_away(working, name,
                                         PROCESSED if ok else FAILED)
        if result.moved_to.startswith(NOT_MOVED):
            self._remember_stuck(working, path, name, result.moved_to)
        elif not ok or findings or result.rejected:
            # The answer beside the file, so somebody looking in the directory
            # does not have to go and ask the mock what happened to it.
            self._write_findings(result, answer)
        return result

    def _write_findings(self, result: Scanned, answer) -> None:
        """What the bank made of a file, written next to where it was filed."""
        target = os.path.join(self.drop_dir, result.moved_to) + FINDINGS_SUFFIX
        lines = ["%s: %s%s" % (result.name, result.status,
                               " (%s)" % result.reason if result.reason else "")]
        if result.msg_id:
            lines.append("MsgId %s" % result.msg_id)
        lines.append("%d accepted, %d rejected"
                     % (result.accepted, result.rejected))
        for payment in answer["payments"]:
            lines.append("  %s %s %s" % (payment.get("end_to_end_id", "?"),
                                         payment.get("outcome", "?"),
                                         payment.get("reason") or ""))
        lines.extend("  " + line for line in result.findings)
        try:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError as error:
            # Same reasoning as the pickup write: a findings file nobody can
            # write is a file somebody is looking for and will not find.
            self._unwritable(os.path.basename(target), error)

    def _unwritable(self, name: str, error: OSError) -> None:
        """A file the mock could not write: said out loud and kept in the state.

        Once per name, so a directory that has been full for an hour does not
        fill the log with the same line instead.
        """
        if name not in self.unwritten:
            sys.stderr.write("mock-bank: could not write %s into %s: %s\n"
                             % (name, self.pickup_dir or self.drop_dir, error))
        self.unwritten[name] = str(error)

    def _release(self, working: str, path: str) -> None:
        """Give a claimed file its own name back."""
        if working != path:
            try:
                os.replace(working, path)
            except OSError:              # pragma: no cover - left claimed,
                pass                     # which is ignored: still read once

    def _remember_stuck(self, working: str, path: str, name: str,
                        reason: str) -> None:
        """A file read once that could not be put away: never read it again
        until it changes, and say so."""
        self._release(working, path)
        try:
            stat = os.stat(path if os.path.exists(path) else working)
        except OSError:                  # pragma: no cover - gone after all
            return
        self.stuck[name] = (stat.st_size, stat.st_mtime, reason)
        sys.stderr.write("mock-bank: read %s but %s; it will not be read again "
                         "until it changes\n" % (name, reason))

    def _file_away(self, path: str, name: str, folder: str) -> str:
        """Move a read file out of the way, without ever overwriting one."""
        target_dir = os.path.join(self.drop_dir, folder)
        try:
            os.makedirs(target_dir, exist_ok=True)
            candidate = _free_name(os.path.join(target_dir, name))
            os.replace(path, candidate)
            return os.path.join(folder, os.path.basename(candidate))
        except OSError as error:         # pragma: no cover - permissions
            return "%s%s" % (NOT_MOVED, error)

    # -- outbound ---------------------------------------------------------

    def write_released(self) -> List[str]:
        """Write every released message that has not been written yet.

        Called after anything that might have released something. Asking the
        database what is released and not yet written, rather than being handed
        the rows by whoever released them, is what makes a new release path
        impossible to forget: the worst it can do is write the files on the next
        release instead of this one.

        `written_at` is on the row rather than in a set in memory. The first
        version kept a set, and a restart on `--db` then wrote every message
        ever released into the directory again - including ones the client had
        collected long before. Seeding the set at startup would have fixed that
        one case and not a crash between releasing and writing.
        """
        if not self.pickup_dir:
            return []
        waiting = [row["id"] for row in db.rows(
            self.state.conn,
            "SELECT id FROM message WHERE released_at IS NOT NULL"
            " AND written_at IS NULL ORDER BY id")]
        return self.write(waiting)

    def write(self, message_ids: List[int]) -> List[str]:
        """Write released messages into the pickup directory.

        Written to a temporary name and renamed, so whatever is watching the
        directory never sees a half-written file - the convention this module
        asks senders to follow.

        Nothing already there is overwritten. A reset empties the message table
        and its ids start again, so a name can recur while the file from before
        is still waiting to be collected; the new one is suffixed, as a
        filed-away drop is, and the collision is reported in `renamed`.
        """
        if not self.pickup_dir:
            return []
        written: List[str] = []
        for message_id in message_ids:
            row = db.one(self.state.conn,
                         "SELECT * FROM message WHERE id = ?", (message_id,))
            if row is None:
                continue
            name = "%s-%s-%d.xml" % (row["type"], row["account"] or "bank",
                                     row["id"])
            final = self._inside_pickup(name)
            if final is None:
                self.refused.append(name)
                continue
            temporary = final + ".tmp"
            try:
                os.makedirs(self.pickup_dir, exist_ok=True)
                with open(temporary, "w", encoding="utf-8") as handle:
                    handle.write(row["body"])
                target = _free_name(final)
                os.replace(temporary, target)
            except OSError as error:
                # Not swallowed. A pickup directory that fills up or loses its
                # permissions would otherwise fail silently and for ever: the
                # bank would look healthy, the client would collect nothing
                # from the folder, and nothing anywhere would say why. And
                # because `written_at` is only set on success, every later
                # release retries it - so the failure repeats rather than
                # passing, which is exactly why it has to be visible.
                self._unwritable(name, error)
                continue
            written_as = os.path.basename(target)
            if written_as != name:
                self.renamed.append({"name": name, "writtenAs": written_as})
            self.state.conn.execute(
                "UPDATE message SET written_at = ? WHERE id = ?",
                (db.now(), row["id"]))
            self.state.conn.commit()
            written.append(written_as)
        self.written.extend(written)
        return written

    def _inside_pickup(self, name: str) -> Optional[str]:
        """The path to write `name` to, or None if it would leave the directory.

        An account id is checked when the account is created, but a row from an
        older database, or one written some other way, has not been: a name with
        a separator in it is refused rather than followed.
        """
        if os.path.basename(name) != name or name in ("", ".", ".."):
            return None
        root = os.path.realpath(self.pickup_dir)
        final = os.path.realpath(os.path.join(root, name))
        if os.path.dirname(final) != root:
            return None
        return final

    def forget(self) -> None:
        """Drop what the dropbox remembers, for a reset.

        The directories themselves are left alone: what is on disk is the
        user's, and a file written before the reset may not be collected yet.
        """
        self.last_scan = []
        self.scans = 0
        self.written = []
        self.refused = []
        self.renamed = []
        self.stuck = {}
        self.unwritten = {}

    # -- reporting --------------------------------------------------------

    def state_json(self) -> Dict[str, Any]:
        return {
            "dropDir": self.drop_dir,
            "pickupDir": self.pickup_dir,
            "polling": self._thread is not None,
            "intervalMs": self.interval_ms,
            "settleMs": self.settle_ms,
            "scans": self.scans,
            "waiting": self.ready(),
            "written": list(self.written[-20:]),
            "refused": list(self.refused[-20:]),
            "reclaimed": list(self.reclaimed),
            "unwritten": [{"name": name, "reason": reason}
                          for name, reason in sorted(self.unwritten.items())],
            "stuck": [{"name": name, "reason": reason}
                      for name, (_size, _mtime, reason)
                      in sorted(self.stuck.items())],
            "renamed": list(self.renamed[-20:]),
            "lastScan": [item.to_json() for item in self.last_scan],
        }


def _free_name(path: str) -> str:
    """`path`, or the first `stem-N.ext` beside it that does not exist yet."""
    stem, extension = os.path.splitext(path)
    candidate, counter = path, 1
    while os.path.exists(candidate):
        candidate = "%s-%d%s" % (stem, counter, extension)
        counter += 1
    return candidate
