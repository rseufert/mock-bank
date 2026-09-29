"""The second door: a directory the bank reads, and one it writes.

The two failures every folder integration meets are what most of this is about:
a file read while it is still being written, and a file read twice. Both are
tested by doing them - writing half a file and finishing it later, and scanning
the same directory twice - rather than by asserting that the guard exists.

`POST /_mock/drop/scan` is what these use instead of waiting for the poll
interval. The poller is exercised too, once, because "the thread actually runs"
is not something a scan endpoint can tell you.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

from test_payments import sample

from support import MockServerCase


class DropCase(MockServerCase):
    """A mock with both directories, on a temporary path of its own."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp(prefix="mock-bank-drop-")
        cls.drop = os.path.join(cls.directory, "in")
        cls.pickup = os.path.join(cls.directory, "out")
        # Settle time of zero: these tests write a whole file and then scan, so
        # waiting 250ms per file would only make the suite slower. The settle
        # time has a test of its own below, where it is the point.
        cls.config_kwargs = dict(cls.config_kwargs, drop_dir=cls.drop,
                                 pickup_dir=cls.pickup, drop_settle_ms=0)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self):
        self.post("/_mock/reset")
        for folder in (self.drop, self.pickup):
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) \
                    else os.unlink(path)

    def drop_file(self, name, text=None):
        path = os.path.join(self.drop, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text if text is not None
                         else sample("pain001_four_payments.xml"))
        return path

    def scan(self):
        resp = self.post("/_mock/drop/scan")
        self.assertEqual(resp.status, 200, resp.body)
        return resp.json()

    def listing(self, folder):
        return sorted(name for name in os.listdir(folder)
                      if os.path.isfile(os.path.join(folder, name)))


class ADroppedFileGoesThroughTheSamePipeline(DropCase):

    def test_it_produces_the_same_answer_as_posting_it(self):
        self.drop_file("payments.xml")
        found = self.scan()
        self.assertEqual(found["scanned"], 1)
        one = found["files"][0]
        self.assertEqual(one["status"], "PART")
        self.assertEqual((one["accepted"], one["rejected"]), (2, 2))

    def test_it_produces_the_same_mailbox_as_posting_it(self):
        # The claim in ARCHITECTURE.md is one pipeline fed by two doors, so the
        # two doors have to be indistinguishable from the mailbox's side.
        self.drop_file("payments.xml")
        self.scan()
        through_folder = [m["type"] for m in self.get("/_mock/mailbox").json()]
        self.post("/_mock/reset")
        self.post("/payments", body=sample("pain001_four_payments.xml"))
        through_http = [m["type"] for m in self.get("/_mock/mailbox").json()]
        self.assertEqual(through_folder, through_http)
        self.assertTrue(through_folder)

    def test_a_read_file_moves_into_processed(self):
        self.drop_file("payments.xml")
        self.scan()
        self.assertEqual(self.listing(self.drop), [])
        # The file, and the answer beside it: two of the four payments were
        # rejected, so there is something to say about it.
        self.assertEqual(self.listing(os.path.join(self.drop, "processed")),
                         ["payments.xml", "payments.xml.findings.txt"])

    def test_the_payments_are_in_the_bank_afterwards(self):
        self.drop_file("payments.xml")
        self.scan()
        payments = self.get("/_mock/payments").json()
        self.assertEqual(len(payments), 4)


class AFileIsReadOnce(DropCase):

    def test_scanning_twice_does_not_read_it_twice(self):
        self.drop_file("payments.xml")
        self.assertEqual(self.scan()["scanned"], 1)
        self.assertEqual(self.scan()["scanned"], 0)
        # And the bank did not decide the same file twice, which is what being
        # read twice would actually cost: a DUPL rejection out of nowhere.
        self.assertEqual(len(self.get("/_mock/payments").json()), 4)

    def test_a_second_file_with_the_same_msgid_is_the_duplicate_it_is(self):
        # Not read twice, but genuinely sent twice: the bank should say DUPL.
        self.drop_file("first.xml")
        self.scan()
        self.drop_file("second.xml")
        found = self.scan()
        self.assertEqual(found["files"][0]["status"], "RJCT")
        self.assertEqual(found["files"][0]["reason"], "DUPL")


class AFileStillBeingWritten(MockServerCase):
    """The settle time, which is the whole reason the poller does not pounce.

    This one needs a real settle time, so it gets a mock of its own.
    """

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp(prefix="mock-bank-settle-")
        cls.drop = os.path.join(cls.directory, "in")
        cls.config_kwargs = dict(cls.config_kwargs, drop_dir=cls.drop,
                                 drop_settle_ms=400)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls.directory, ignore_errors=True)

    def test_it_is_read_whole_rather_than_half(self):
        text = sample("pain001_four_payments.xml")
        half, rest = text[:len(text) // 2], text[len(text) // 2:]
        path = os.path.join(self.drop, "slow.xml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(half)
            handle.flush()
            # Less than the settle time: the scan must decline to read it.
            found = self.post("/_mock/drop/scan").json()
            self.assertEqual(found["scanned"], 0,
                             "read a file that was still being written")
            handle.write(rest)
        time.sleep(0.5)
        found = self.post("/_mock/drop/scan").json()
        self.assertEqual(found["scanned"], 1)
        # Read whole: a half file would not have parsed into four payments.
        self.assertEqual(found["files"][0]["accepted"], 2)
        self.assertEqual(len(self.get("/_mock/payments").json()), 4)


class AFileTheBankCannotPutThrough(DropCase):

    def test_an_unreadable_file_lands_in_failed_with_its_findings(self):
        self.drop_file("rubbish.xml", "this is not XML at all")
        found = self.scan()
        self.assertIs(found["files"][0]["ok"], False)
        # The file, and the answer beside it.
        self.assertEqual(self.listing(os.path.join(self.drop, "failed")),
                         ["rubbish.xml", "rubbish.xml.findings.txt"])
        beside = os.path.join(self.drop, "failed", "rubbish.xml.findings.txt")
        self.assertTrue(os.path.exists(beside), os.listdir(
            os.path.join(self.drop, "failed")))
        with open(beside, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("RJCT", text)
        self.assertIn("FF01", text)

    def test_an_unreadable_file_is_answered_in_the_pickup_directory(self):
        # A folder client has no HTTP response to read: the pain.002 is its
        # only answer, and the file's name is all it has to match it to (#64).
        self.drop_file("rubbish.xml", "this is not XML at all")
        self.scan()
        written = self.listing(self.pickup)
        self.assertEqual(len(written), 1, written)
        # No account could be read, so the name falls back to the bank's.
        self.assertTrue(written[0].startswith("pain.002.001.10-bank-"), written)
        with open(os.path.join(self.pickup, written[0]), encoding="utf-8") as handle:
            text = handle.read()
        for expected in ("<GrpSts>RJCT</GrpSts>", "<Cd>FF01</Cd>",
                         "<OrgnlMsgId>NOTPROVIDED</OrgnlMsgId>", "file rubbish.xml"):
            self.assertIn(expected, text)

    def test_a_duplicate_lands_in_failed_too(self):
        # Rejected at group level: the bank could not put the file through, so
        # it is somebody's to look at.
        self.drop_file("first.xml")
        self.scan()
        self.drop_file("again.xml")
        self.scan()
        self.assertIn("again.xml", self.listing(os.path.join(self.drop, "failed")))

    def test_a_part_is_processed_rather_than_failed(self):
        # Two of the four payments are rejected, and the file is still a file
        # the bank handled - the rejections are in the pain.002. A real bank's
        # processed folder holds these.
        self.drop_file("payments.xml")
        self.scan()
        self.assertIn("payments.xml",
                      self.listing(os.path.join(self.drop, "processed")))
        # With the answer beside it: two of the four were rejected, and a
        # folder-only client should not have to parse the pain.002 to find out.
        self.assertTrue(os.path.exists(os.path.join(
            self.drop, "processed", "payments.xml.findings.txt")))

    def test_the_findings_file_says_what_the_validate_endpoint_would(self):
        self.drop_file("broken.xml", sample("pain001_broken_iban.xml"))
        self.scan()
        for folder in ("processed", "failed"):
            beside = os.path.join(self.drop, folder, "broken.xml.findings.txt")
            if os.path.exists(beside):
                break
        with open(beside, encoding="utf-8") as handle:
            text = handle.read()
        prose = self.post("/_mock/validate",
                          body=sample("pain001_broken_iban.xml")).body.decode()
        # The same line, from the same renderer, rather than a second format.
        line = [x for x in prose.splitlines() if x.startswith("error AC01")][0]
        self.assertIn(line, text)

    def test_a_findings_file_is_never_itself_read_as_a_payment_file(self):
        self.drop_file("rubbish.xml", "not XML")
        self.scan()
        # It lives under failed/, but even if somebody copied it back:
        self.drop_file("stray.findings.txt", "error FF01 at /: whatever")
        self.assertEqual(self.scan()["scanned"], 0)


class WhatTheBankWritesBack(DropCase):

    def test_every_released_message_lands_in_the_pickup_directory(self):
        self.drop_file("payments.xml")
        self.scan()
        written = self.listing(self.pickup)
        self.assertEqual(len(written), 1, written)
        self.assertTrue(written[0].startswith("pain.002.001.10-ACME-"))
        self.assertTrue(written[0].endswith(".xml"))

    def test_the_file_is_the_message_the_mailbox_has(self):
        self.drop_file("payments.xml")
        self.scan()
        message = self.get("/_mock/mailbox?leave").json()[0]
        name = self.listing(self.pickup)[0]
        with open(os.path.join(self.pickup, name), encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), message["body"].strip())

    def test_no_half_written_file_is_ever_left_behind(self):
        self.drop_file("payments.xml")
        self.scan()
        # Written to a temporary name and renamed, so nothing with the
        # temporary suffix survives for a poller to trip over.
        self.assertEqual([n for n in os.listdir(self.pickup)
                          if n.endswith(".tmp")], [])

    def test_a_message_released_by_the_clock_lands_too(self):
        # Not only what a file produced at once: the camt.054 and camt.053 that
        # the clock releases days later are written when they are released.
        self.drop_file("payments.xml")
        self.scan()
        before = set(self.listing(self.pickup))
        settles = sorted(p["settlement_date"] for p in
                         self.get("/_mock/payments").json()
                         if p["settlement_date"])
        self.post("/_mock/advance?to=%s" % settles[0])
        self.post("/_mock/advance?days=1")
        after = set(self.listing(self.pickup))
        new = sorted(after - before)
        self.assertTrue(any(n.startswith("camt.054") for n in new), new)
        self.assertTrue(any(n.startswith("camt.053") for n in new), new)

    def test_a_return_lands_too_without_the_transport_knowing_about_returns(self):
        # #14 added the pacs.004 after this branch was written, and nothing here
        # was changed for it. That is the point of write_released asking the
        # database what is released rather than being handed rows by whoever
        # released it: a new kind of message needs no work here at all.
        self.patch("/_mock/accounts/ACME",
                   {"behaviour": "return-later", "parameters": {"days": 2}})
        self.drop_file("payments.xml")
        self.scan()
        settles = sorted(p["settlement_date"] for p in
                         self.get("/_mock/payments").json()
                         if p["settlement_date"])
        self.post("/_mock/advance?to=%s" % settles[0])
        self.post("/_mock/advance?days=7")
        written = self.listing(self.pickup)
        self.assertTrue(any(n.startswith("pacs.004") for n in written), written)

    def test_a_message_is_not_written_twice_even_across_a_restart(self):
        # written_at is on the row, not in a set in memory. With the set, a
        # restart on --db wrote every message ever released into the directory
        # again - including ones the client had collected long before. There is
        # a --db version of this in test_upgrade.py; this is the in-process
        # half: releasing repeatedly must not rewrite what is already written.
        self.drop_file("payments.xml")
        self.scan()
        first = self.listing(self.pickup)
        self.assertTrue(first)
        for _ in range(3):
            self.get("/_mock/mailbox?leave")      # each call releases first
        self.assertEqual(self.listing(self.pickup), first)

    def test_an_intraday_report_lands_at_once(self):
        # #132: asked for over HTTP, and in the folder before the answer comes
        # back, without waiting for anything else to release.
        self.assertEqual(self.post("/_mock/accounts/ACME/report").status, 201)
        written = self.listing(self.pickup)
        self.assertEqual(len(written), 1, written)
        self.assertTrue(written[0].startswith("camt.052.001.08-ACME-"), written)

    def test_posting_over_http_also_writes_to_the_pickup_directory(self):
        # The pickup directory is the bank's outbound side, not the drop
        # directory's reply: a message released by an HTTP post goes there too.
        self.post("/payments", body=sample("pain001_four_payments.xml"))
        self.assertTrue(self.listing(self.pickup))


class AFileLeftClaimedByAnEarlierRun(MockServerCase):
    """`<name>.processing` from a mock that was killed mid-read.

    `ready()` skips that suffix, so such a file was invisible for ever after -
    not read, not filed, not reported. The senior found it; it is reclaimed at
    startup now.
    """

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp(prefix="mock-bank-claim-")
        cls.drop = os.path.join(cls.directory, "in")
        os.makedirs(cls.drop, exist_ok=True)
        with open(os.path.join(cls.drop, "orphan.xml" + ".processing"), "w",
                  encoding="utf-8") as handle:
            handle.write(sample("pain001_four_payments.xml"))
        cls.config_kwargs = dict(cls.config_kwargs, drop_dir=cls.drop,
                                 drop_settle_ms=0)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls.directory, ignore_errors=True)

    def test_it_is_renamed_back_and_read(self):
        drop = self.get("/_mock/drop").json()
        self.assertEqual(drop["reclaimed"], ["orphan.xml"])
        # And it is a file again, so the next scan sees it.
        found = self.post("/_mock/drop/scan").json()
        self.assertEqual([f["name"] for f in found["files"]], ["orphan.xml"])
        self.assertEqual(len(self.get("/_mock/payments").json()), 4)


class TheFolderDoorIsOutsideAuth(MockServerCase):
    """A file in the drop directory needs no credentials, and that is on purpose.

    A directory is guarded by the filesystem, not by the bank, which is how a
    real SFTP drop works - the credentials are the SSH account, not the payment
    file. But it means `--auth` alone does not close the second door, and that
    is worth a test rather than only a sentence in the README: somebody will
    reach for `--auth` expecting it to cover everything.
    """

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp(prefix="mock-bank-authdrop-")
        cls.drop = os.path.join(cls.directory, "in")
        cls.config_kwargs = dict(cls.config_kwargs, drop_dir=cls.drop,
                                 drop_settle_ms=0, auth="tester:s3cret")
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls.directory, ignore_errors=True)

    def credentials(self):
        import base64
        raw = base64.b64encode(b"tester:s3cret").decode()
        return {"Authorization": "Basic " + raw}

    def test_http_needs_credentials_and_the_folder_does_not(self):
        # The same file, both doors.
        self.assertEqual(
            self.post("/payments", body=sample("pain001_four_payments.xml")).status,
            401)
        with open(os.path.join(self.drop, "dropped.xml"), "w",
                  encoding="utf-8") as handle:
            handle.write(sample("pain001_four_payments.xml"))
        self.assertEqual(
            self.post("/_mock/drop/scan", headers=self.credentials()).json()["scanned"],
            1)
        payments = self.get("/_mock/payments", headers=self.credentials()).json()
        self.assertEqual(len(payments), 4,
                         "the folder door did not accept the file")


class APickupDirectoryItCannotWriteTo(DropCase):

    def break_the_pickup(self):
        """Put a regular file where the pickup directory should be.

        Not `chmod 0o500`: that does not stop file creation on Windows, and does
        not stop root on Linux either, so the first version of these tests was
        red on the Windows runner and would have been a false pass under Docker.
        A file where a directory belongs makes `makedirs` raise on every
        platform, which is the failure being tested - the mock cannot write
        there - and removing the file makes it work again.
        """
        shutil.rmtree(self.pickup)
        with open(self.pickup, "w", encoding="utf-8") as handle:
            handle.write("not a directory")

    def fix_the_pickup(self):
        os.unlink(self.pickup)
        os.makedirs(self.pickup, exist_ok=True)

    def test_the_failure_is_reported_rather_than_silent(self):
        # A pickup directory that fills up or loses its permissions used to fail
        # silently and for ever: the bank looked healthy, the client collected
        # nothing from the folder, and nothing said why.
        self.break_the_pickup()
        self.addCleanup(self.fix_the_pickup)
        self.drop_file("payments.xml")
        self.scan()
        unwritten = self.get("/_mock/drop").json()["unwritten"]
        self.assertTrue(unwritten, "a write that failed was not reported")
        self.assertTrue(any("pain.002" in row["name"] for row in unwritten),
                        unwritten)

    def test_it_is_written_once_the_directory_works_again(self):
        # written_at is only set on success, so a later release retries it.
        self.break_the_pickup()
        self.drop_file("payments.xml")
        self.scan()
        self.fix_the_pickup()
        self.assertEqual(self.listing(self.pickup), [])
        self.get("/_mock/mailbox?leave")            # releases, and so retries
        self.assertTrue(self.listing(self.pickup))


class WhatItReportsAboutItself(DropCase):

    def test_state_names_both_directories(self):
        transport = self.get("/_mock/state").json()["transport"]
        # abspath, not realpath: on macOS /var is a symlink to /private/var,
        # and what the mock reports is the path it was given.
        self.assertEqual(transport["dropDir"], os.path.abspath(self.drop))
        self.assertEqual(transport["pickupDir"], os.path.abspath(self.pickup))

    def test_the_drop_endpoint_says_what_is_waiting(self):
        self.drop_file("waiting.xml")
        self.assertEqual(self.get("/_mock/drop").json()["waiting"],
                         ["waiting.xml"])
        self.scan()
        self.assertEqual(self.get("/_mock/drop").json()["waiting"], [])

    def test_it_remembers_the_last_scan_and_what_it_wrote(self):
        self.drop_file("payments.xml")
        self.scan()
        drop = self.get("/_mock/drop").json()
        self.assertEqual([f["name"] for f in drop["lastScan"]], ["payments.xml"])
        self.assertTrue(drop["written"])
        self.assertGreaterEqual(drop["scans"], 1)

    def test_reset_forgets_the_history_and_leaves_the_files_alone(self):
        self.drop_file("payments.xml")
        self.scan()
        written = self.listing(self.pickup)
        self.post("/_mock/reset")
        drop = self.get("/_mock/drop").json()
        self.assertEqual(drop["lastScan"], [])
        self.assertEqual(drop["written"], [])
        # What is on disk is the user's: a file written before the reset may
        # not have been collected yet.
        self.assertEqual(self.listing(self.pickup), written)

    def test_getting_scan_and_posting_drop_each_say_what_is_allowed(self):
        self.assertEqual(self.get("/_mock/drop/scan").json()["allowed"], ["POST"])
        self.assertEqual(self.post("/_mock/drop").json()["allowed"], ["GET"])


class WithNoFolderTransport(MockServerCase):
    """The default mock does no file work, and says so rather than pretending."""

    def test_state_says_there_is_none(self):
        transport = self.get("/_mock/state").json()["transport"]
        self.assertEqual(transport["dropDir"], "")
        self.assertEqual(transport["pickupDir"], "")

    def test_scanning_is_refused_by_naming_the_flag(self):
        resp = self.post("/_mock/drop/scan")
        self.assertEqual(resp.status, 409)
        self.assertIn("--drop-dir", resp.json()["error"])


class ThePollerActuallyRuns(MockServerCase):
    """One test that does wait, because a scan endpoint cannot prove a thread."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp(prefix="mock-bank-poll-")
        cls.drop = os.path.join(cls.directory, "in")
        cls.config_kwargs = dict(cls.config_kwargs, drop_dir=cls.drop,
                                 drop_settle_ms=0, drop_interval_ms=100)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(cls.directory, ignore_errors=True)

    def test_a_file_nobody_scans_for_is_read_anyway(self):
        with open(os.path.join(self.drop, "quiet.xml"), "w",
                  encoding="utf-8") as handle:
            handle.write(sample("pain001_four_payments.xml"))
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.get("/_mock/payments").json():
                break
            time.sleep(0.1)
        self.assertEqual(len(self.get("/_mock/payments").json()), 4,
                         "the poller never read the file")
        self.assertIs(self.get("/_mock/drop").json()["polling"], True)


class AFolderPairTheMockWillNotHonour(unittest.TestCase):
    """The bank must not be able to read its own output back as a payment file.

    With one directory for both, every `pain.002` the bank writes lands somewhere
    it is watching: it reads its status report back as a payment file, answers
    that with `FF01`, writes *that* into the directory too, and goes round again.
    A mock that looks busy and is only talking to itself.
    """

    def refusal(self, drop, pickup):
        from mockbank import drop as drop_module
        with self.assertRaises(drop_module.Invalid) as caught:
            drop_module.check_directories(drop, pickup)
        return str(caught.exception)

    def test_the_same_directory_for_both_is_refused(self):
        with tempfile.TemporaryDirectory() as one:
            message = self.refusal(one, one)
            self.assertIn("same directory", message)
            self.assertIn("read every message it wrote back in", message)

    def test_the_pickup_inside_the_drop_is_refused(self):
        # Refused for mixing the bank's answers in with the files it manages,
        # not for looping: `ready()` lists only the top level of the drop
        # directory, so a nested pickup would not actually be read back. The
        # senior pointed that out and the message says the true reason now.
        with tempfile.TemporaryDirectory() as outer:
            inner = os.path.join(outer, "out")
            message = self.refusal(outer, inner)
            self.assertIn("--pickup-dir is inside --drop-dir", message)
            self.assertIn("processed/ and failed/", message)

    def test_the_drop_inside_the_pickup_is_refused(self):
        with tempfile.TemporaryDirectory() as outer:
            inner = os.path.join(outer, "in")
            message = self.refusal(inner, outer)
            self.assertIn("--drop-dir is inside --pickup-dir", message)

    def test_side_by_side_is_fine(self):
        from mockbank import drop as drop_module
        with tempfile.TemporaryDirectory() as parent:
            drop_module.check_directories(os.path.join(parent, "in"),
                                          os.path.join(parent, "out"))

    def test_a_name_that_merely_starts_the_same_is_fine(self):
        # `/tmp/bank-in` is not inside `/tmp/bank`, and a prefix test without a
        # separator would have said it was.
        from mockbank import drop as drop_module
        with tempfile.TemporaryDirectory() as parent:
            drop_module.check_directories(os.path.join(parent, "bank"),
                                          os.path.join(parent, "bank-out"))

    def test_one_directory_alone_is_fine(self):
        from mockbank import drop as drop_module
        with tempfile.TemporaryDirectory() as one:
            drop_module.check_directories(one, "")
            drop_module.check_directories("", one)

    def test_a_directory_that_cannot_be_made_names_itself_and_its_flag(self):
        import subprocess
        import sys as _sys
        result = subprocess.run(
            [_sys.executable, "-m", "mockbank", "--port", "8099",
             "--drop-dir", os.path.join(os.devnull, "nope")],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stderr)
        # Not "cannot listen on 127.0.0.1:8099", which is where this used to
        # surface and sends a reader to look at the port.
        self.assertNotIn("cannot listen", result.stderr)
        self.assertIn("--drop-dir", result.stderr)
        self.assertIn("could not be created", result.stderr)


class AStartupThatCannotHappen(MockServerCase):
    """A bind that fails used to read as a bug in the mock.

    `socketserver.TCPServer.__init__` calls `server_close()` on the failed-bind
    path, before `make_server` has assigned the state - so the real error was
    replaced by `AttributeError: '_Server' object has no attribute 'state'`,
    which says nothing about an address. Found while building the transport,
    because a drop directory had the mock starting and stopping far more often
    than anything else had.
    """

    def run_mock(self, *extra):
        import subprocess
        import sys as _sys
        return subprocess.run(
            [_sys.executable, "-m", "mockbank"] + list(extra),
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=120)

    def test_an_address_this_host_does_not_have_is_named(self):
        # 203.0.113.0/24 is TEST-NET-3, reserved for documentation, so no host
        # has it. Used instead of a port already in use because that is not an
        # error on Windows: `allow_reuse_address` is set, and two sockets may
        # bind one port there, so the second mock starts happily and the test
        # would wait for it forever - which is exactly how this failed on the
        # Windows runner first time round.
        result = self.run_mock("--host", "203.0.113.1", "--port", "8099")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("AttributeError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("cannot listen on 203.0.113.1:8099", result.stderr)

    @unittest.skipIf(sys.platform.startswith("win"),
                     "two sockets may bind one port on Windows, so this is not "
                     "an error there")
    def test_a_port_already_in_use_is_named_rather_than_tracebacked(self):
        port = self.base.rsplit(":", 1)[1]
        result = self.run_mock("--port", port)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("AttributeError", result.stderr)
        self.assertIn("cannot listen on", result.stderr)
        self.assertIn(port, result.stderr)
