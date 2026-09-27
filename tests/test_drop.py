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
import tempfile
import time

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

    def test_posting_over_http_also_writes_to_the_pickup_directory(self):
        # The pickup directory is the bank's outbound side, not the drop
        # directory's reply: a message released by an HTTP post goes there too.
        self.post("/payments", body=sample("pain001_four_payments.xml"))
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


class AStartupThatCannotHappen(MockServerCase):
    """Two failures at startup that used to read as bugs in the mock.

    Both are here rather than in `test_auth.py` because both were found while
    building the transport: the drop directory made the mock start and stop far
    more often than anything else had.
    """

    def test_a_port_already_in_use_is_named_rather_than_tracebacked(self):
        import subprocess
        import sys as _sys
        port = self.base.rsplit(":", 1)[1]
        result = subprocess.run(
            [_sys.executable, "-m", "mockbank", "--port", port],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stderr)
        # The real cause, not `AttributeError: '_Server' object has no
        # attribute 'state'` - which is what came out before, because
        # socketserver calls server_close() on the failed-bind path before
        # make_server has assigned the state.
        self.assertNotIn("AttributeError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("cannot listen on", result.stderr)
        self.assertIn(port, result.stderr)
