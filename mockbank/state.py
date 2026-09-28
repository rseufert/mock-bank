"""What survives between requests: the database, the clock, and the lock over them.

`State.receive` is the pipeline both doors feed: `POST /payments` wraps it in
JSON, and the drop directory calls it with the bytes of a file.
"""
from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING, Any, Dict

from . import (__version__, accounts, clock as clock_module, db, drop, nacha, outbox,
               validate)
from .accounts import BEHAVIOURS
from .routes import SUPPORTED
from .routes.control import PLANNED

if TYPE_CHECKING:                          # pragma: no cover
    from .server import Config


class State:
    """What survives between requests: the database, and the lock over it."""

    def __init__(self, config: Config):
        self.config = config
        # Reentrant, because a route that takes the lock may call something
        # that takes it again.
        self.lock = threading.RLock()
        self.conn = db.connect(config.db_path)
        db.seed(self.conn)
        # The clock reads its holidays from the table rather than holding them,
        # so that PUT /_mock/holidays and a restart on --db agree about them.
        self.clock = clock_module.Clock(
            zone=config.timezone, cutoff=config.cutoff, start=config.clock,
            holidays=self._holidays)
        # As the clock moves, book what came due and release what is due.
        # release_due is idempotent over "due and not yet done", so a hook
        # that failed half-way is finished by the next advance.
        self.clock.on_advance.append(self._on_advance)
        self.started = db.utcnow()
        self.resets = 0
        # What retention has removed since the mock started, so a tester who
        # wonders where their rows went can see rather than guess.
        self.pruned: Dict[str, int] = {}
        self.requests_since_prune = 0
        # Set by make_server when --drop-dir or --pickup-dir is given. None
        # rather than an inert object, so that the default mock does no file
        # work at all and nothing has to remember to check a flag.
        self.dropbox = None
        if config.drop_dir or config.pickup_dir:
            self.dropbox = drop.DropBox(
                self, config.drop_dir, config.pickup_dir,
                config.drop_settle_ms, config.drop_interval_ms)
            # Created now, so a misspelled path fails at startup rather than at
            # the first file.
            self.dropbox.prepare()
        # After the dropbox, because pruning asks it whether a pickup directory
        # is configured - a message the folder has not received must not age out.
        self.prune()

    def _on_advance(self, before, after):
        # Book first, so a day's statement sees that day's bookings.
        now = after.replace(microsecond=0)
        outbox.release_due(self.conn, now, after.date(), self.clock)
        outbox.issue_statements(
            self.conn, self.clock, outbox.ended_business_days(self.clock, before, after), now)
        # Deliver before pruning, always. Pruning cannot remove a message the
        # folder has not received - see State.prune - but the order also means a
        # message released and aged out in the same advance still reaches the
        # directory, which is the reading a tester would expect.
        self.deliver()
        self.prune()

    def prune(self) -> Dict[str, int]:
        """Apply --keep-requests and --retention-days, keeping a running total.

        With a pickup directory, a message is only prunable once it has actually
        been written there. Retention deleting a message the folder never
        received would lose it silently: the client polls a directory, and a file
        that was aged out before it was written is a message that simply never
        arrived. It matters only now that both features exist, which is why it is
        here rather than in db.prune - the database has no idea whether anyone is
        watching a directory.
        """
        with self.lock:
            removed = db.prune(self.conn, self.config.keep_requests,
                               self.config.retention_days, self.now(),
                               require_written=self.dropbox is not None
                               and bool(self.dropbox.pickup_dir))
            for table, count in removed.items():
                self.pruned[table] = self.pruned.get(table, 0) + count
            self.requests_since_prune = 0
        return removed

    def _holidays(self):
        return [row["day"] for row in db.rows(
            self.conn, "SELECT day FROM holiday ORDER BY day")]

    def reset(self) -> None:
        """Back to the seeded bank, without restarting the process.

        The tables are emptied and reseeded rather than the file replaced, so a
        mock on ``--db`` keeps being the same mock at the same path. That
        includes the ``counter`` table, so statement numbers and ``camt.054``
        ``MsgId`` s start again at 1: a reset is a new bank, not a restart of
        the old one, which is what keeps its numbers (see ``db.next_value``).
        """
        with self.lock:
            for table in ("statement", "counter", "message", "payment", "credit", "file",
                          "request_log", "holiday", "account"):
                self.conn.execute("DELETE FROM %s" % table)
            self.conn.commit()
            db.seed(self.conn)
            self.clock.reset()
            self.pruned = {}
            if self.dropbox is not None:
                self.dropbox.forget()
            self.resets += 1

    def now(self):
        """The bank's now, to the second: the clock's, so a test can move it."""
        return self.clock.now().replace(microsecond=0)

    def today(self):
        """The bank's today, which is the clock's - so a test can move it."""
        return self.clock.today()

    def release(self):
        """Book what is due and release what is due; see outbox.release_due."""
        released = outbox.release_due(self.conn, self.now(), self.today(), self.clock)
        self.deliver()
        return released

    def deliver(self):
        """Put whatever is released into the pickup directory, if there is one.

        Called after every release rather than handed the rows, so that a
        release path nobody remembered still gets its files written on the next
        one. See DropBox.write_released.
        """
        if self.dropbox is not None:
            self.dropbox.write_released()

    def receive(self, body: bytes, content_type: str = "", source: str = ""):
        """The pipeline, once: read, validate, decide, book, queue, release.

        `ARCHITECTURE.md` says one pipeline fed by two doors, and this is the
        pipeline. `POST /payments` wraps it in JSON and the drop directory calls
        it with the bytes of a file; neither knows anything the other does not,
        which is the only way the claim stays true. Returns the same answer the
        endpoint serves, and the findings, which the drop directory writes out
        beside a file it could not put through. ``source`` is a dropped file's
        name, which a refusal's ``pain.002`` carries.
        """
        conn, now, today = self.conn, self.now(), self.today()
        # Either door takes a NACHA file as well as a pain.001 (#53), and then
        # names the accounts it holds by IBAN, which is all `decide` knows.
        payment_file, findings = validate.inspect(body, content_type, today)
        accounts.resolve(conn, payment_file)
        decision = accounts.decide(payment_file, findings, conn, self.clock, now,
                                   self.config.allow_duplicates)
        file_id = accounts.book(conn, decision)
        # A NACHA account's rejections also come back as returns, next business
        # day (#54); scheduled now, released on the clock like any return.
        accounts.schedule_rejected_returns(conn, file_id, self.clock, today)
        queued = outbox.queue_status(conn, decision, file_id, now,
                                     self.config.status_delay_ms, source)
        released = {row["id"] for row in self.release()}
        queued = [dict(q, released=q["id"] in released) for q in queued]
        queued += outbox.upcoming(conn, file_id) if file_id else []
        # the rows went in in decision order, so they line up one for one
        booked = [r["booked_at"] is not None for r in db.rows(
            conn, "SELECT booked_at FROM payment WHERE file_id = ? ORDER BY id",
            (file_id,))]
        answer = {
            "format": ("nacha" if payment_file is not None
                       and payment_file.message == nacha.NAME
                       else "iso20022" if payment_file is not None else None),
            "msg_id": decision.msg_id,
            "status": decision.status,
            "reason": decision.reason,
            "reason_text": decision.reason_text,
            "reported": decision.reported,
            "accepted": len(decision.accepted),
            "rejected": len(decision.rejected),
            "payments": [dict(d.to_json(), booked=done)
                         for d, done in zip(decision.payments, booked)],
            "findings": [f._asdict() for f in findings],
            # What the bank will send, and when: the pain.002 (released at
            # once unless --status-delay-ms says otherwise) and a camt.054
            # for each account and settlement date still to come.
            "queued": queued,
        }
        return answer, decision, findings

    def close(self) -> None:
        """Close the database, under the lock every request takes.

        Closing SQLite while a statement is running is a use-after-free in C:
        it takes the interpreter with it instead of raising.
        """
        if self.dropbox is not None and not self.dropbox.stop():
            # Named rather than ignored: when it next takes the lock it finds
            # the connection closed, which is an ordinary exception.
            sys.stderr.write("mock-bank: the drop poller did not stop in time; "
                             "closing the database once it lets go\n")
        with self.lock:
            self.conn.close()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "version": __version__,
                "db": self.config.db_path,
                "schemaVersion": db.SCHEMA_VERSION,
                "started": db.stamp(self.started),
                "requests": db.count(self.conn, "request_log"),
                "resets": self.resets,
                "accounts": db.count(self.conn, "account"),
                # Per currency, in minor units. See accounts.totals.
                "balances": accounts.totals(self.conn),
                "holidays": db.count(self.conn, "holiday"),
                "clock": self.clock.snapshot(),
                "payments": accounts.payment_counts(self.conn),
                "messages": {
                    "queued": db.count(self.conn, "message", "released_at IS NULL"),
                    "waiting": db.count(self.conn, "message",
                                        "released_at IS NOT NULL AND taken_at IS NULL"),
                    "taken": db.count(self.conn, "message", "taken_at IS NOT NULL"),
                },
                "retention": {
                    "keepRequests": self.config.keep_requests,
                    "retentionDays": self.config.retention_days,
                    "pruned": dict(self.pruned),
                },
                "transport": (self.dropbox.state_json() if self.dropbox
                              else {"dropDir": "", "pickupDir": ""}),
                "behaviours": sorted(BEHAVIOURS),
                "supported": SUPPORTED,
                "planned": PLANNED,
            }
