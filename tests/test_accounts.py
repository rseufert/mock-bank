"""The accounts the bank holds, over HTTP: the seed, and changing it at runtime.

The behaviours are the reason the project exists, and this is the surface that
reaches them: one `PATCH` turns the bank into one that rejects, and `POST
/_mock/reset` turns it back. So the assertions here are about what a tester can
see and rely on - the seed being the same seed, a balance being a whole number
of minor units, and a refusal naming what would have been accepted.
"""
import os
import re

from support import MockServerCase

from mockbank import db
from mockbank.accounts import BEHAVIOURS
from mockbank.messages import MSG_ID_ACCOUNT_CHARS


def mod97(text):
    """`text` read as one enormous integer, modulo 97.

    The naive reading of ISO 13616, written out the long way on purpose:
    `schema.mod97` reduces as it goes, and a test that reused it could only prove
    that function agrees with itself.
    """
    return int("".join(str(int(char, 36)) for char in text)) % 97


class TheSeed(MockServerCase):

    def test_it_is_four_accounts_the_same_four_every_time(self):
        listed = self.get("/_mock/accounts").json()
        self.assertEqual([row["id"] for row in listed],
                         ["ACME", "EURODIS", "GLOBEX", "INITECH"])

    def test_every_seeded_iban_passes_its_own_check_digits(self):
        for row in self.get("/_mock/accounts").json():
            with self.subTest(account=row["id"]):
                value = row["iban"]
                # ISO 13616: move the first four characters to the end, and
                # the whole thing read as a number is 1 modulo 97.
                self.assertEqual(mod97(value[4:] + value[:4]), 1,
                                 "%s has an IBAN that fails its own checksum, "
                                 "so no arriving payment could match it" % value)

    def test_every_balance_is_a_whole_number_of_minor_units(self):
        for row in self.get("/_mock/accounts").json():
            with self.subTest(account=row["id"]):
                self.assertIsInstance(row["balance"], int)
                self.assertNotIsInstance(row["balance"], bool)

    def test_each_failure_the_readme_promises_has_an_account_to_reach_it(self):
        by_id = {row["id"]: row for row in self.get("/_mock/accounts").json()}
        self.assertEqual(by_id["ACME"]["behaviour"], "accept")
        self.assertEqual(by_id["GLOBEX"]["behaviour"], "insufficient-funds")
        self.assertEqual(by_id["INITECH"]["behaviour"], "closed-account")
        self.assertEqual(by_id["EURODIS"]["behaviour"], "bad-bank-id")
        self.assertIs(by_id["INITECH"]["closed"], True)
        # The point of GLOBEX: one ordinary payment breaches it, and ACME's
        # balance is nowhere near being breached by one.
        self.assertLess(by_id["GLOBEX"]["balance"], by_id["ACME"]["balance"])

    def test_one_account_is_shown_with_its_balance(self):
        resp = self.get("/_mock/accounts/ACME")
        self.assertEqual(resp.status, 200)
        account = resp.json()
        self.assertEqual(account["currency"], "EUR")
        self.assertEqual(account["balance"], 12500000)
        self.assertEqual(account["parameters"], {})
        # The sample file's debtor, so that the sample works out of the box.
        self.assertEqual(account["iban"], "NL41MOCK0000000001")

    def test_an_id_no_account_has_is_a_404_naming_the_ones_there_are(self):
        resp = self.get("/_mock/accounts/NOPE")
        self.assertEqual(resp.status, 404)
        self.assertIn("ACME", resp.json()["accounts"])


class ChangingAnAccount(MockServerCase):
    """Each test puts the bank back, because the class shares one mock."""

    def setUp(self):
        self.addCleanup(self.post, "/_mock/reset")

    def test_a_patched_behaviour_is_what_the_next_get_says(self):
        resp = self.patch("/_mock/accounts/ACME", {"behaviour": "closed-account"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["behaviour"], "closed-account")
        self.assertEqual(self.get("/_mock/accounts/ACME").json()["behaviour"],
                         "closed-account")

    def test_a_behaviour_the_mock_does_not_have_is_refused_by_name(self):
        resp = self.patch("/_mock/accounts/ACME", {"behaviour": "bounce"})
        self.assertEqual(resp.status, 400)
        body = resp.json()
        self.assertIn("bounce", body["error"])
        # Every real one is named, so the caller does not have to guess.
        for name in BEHAVIOURS:
            self.assertIn(name, body["error"])
        self.assertEqual(body["behaviours"], sorted(BEHAVIOURS))
        # And nothing changed.
        self.assertEqual(self.get("/_mock/accounts/ACME").json()["behaviour"],
                         "accept")

    def test_a_balance_and_a_closed_flag_and_parameters_all_move(self):
        resp = self.patch("/_mock/accounts/GLOBEX",
                          {"balance": 50000, "closed": True,
                           "parameters": {"days": 3}})
        self.assertEqual(resp.status, 200)
        account = self.get("/_mock/accounts/GLOBEX").json()
        self.assertEqual(account["balance"], 50000)
        self.assertIs(account["closed"], True)
        self.assertEqual(account["parameters"], {"days": 3})

    def test_a_balance_that_is_not_minor_units_is_refused_not_rounded(self):
        for value in (12.50, "12.50", "twelve fifty", True):
            with self.subTest(balance=value):
                resp = self.patch("/_mock/accounts/ACME", {"balance": value})
                self.assertEqual(resp.status, 400)
                self.assertIn("minor units", resp.json()["error"])
        self.assertEqual(self.get("/_mock/accounts/ACME").json()["balance"],
                         12500000)

    def test_a_misspelled_field_is_refused_rather_than_dropped(self):
        resp = self.patch("/_mock/accounts/ACME", {"behavior": "accept"})
        self.assertEqual(resp.status, 400)
        error = resp.json()["error"]
        self.assertIn("'behavior'", error)
        self.assertIn("behaviour", error)

    def test_an_iban_whose_check_digits_do_not_agree_is_refused(self):
        # One digit of the real check pair moved: the right shape, wrong number.
        resp = self.patch("/_mock/accounts/ACME",
                          {"iban": "NL42MOCK0000000001"})
        self.assertEqual(resp.status, 400)
        self.assertIn("check digits", resp.json()["error"])

    def test_patching_an_account_that_is_not_there_is_a_404(self):
        resp = self.patch("/_mock/accounts/NOPE", {"behaviour": "accept"})
        self.assertEqual(resp.status, 404)

    def test_a_body_that_is_not_an_object_says_what_the_fields_are(self):
        resp = self.patch("/_mock/accounts/ACME", "closed-account")
        self.assertEqual(resp.status, 400)
        self.assertIn("behaviour", resp.json()["fields"])


class CreatingAnAccount(MockServerCase):

    def setUp(self):
        self.addCleanup(self.post, "/_mock/reset")

    def body(self, **over):
        payload = {"id": "OTHER", "name": "Other Trading Ltd",
                   "iban": db.iban("NL", "MOCK0000000009"),
                   "bic": "MOCKNL2A"}
        payload.update(over)
        return payload

    def test_it_is_created_and_then_listed(self):
        resp = self.post("/_mock/accounts", self.body())
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.json()["balance"], 0)
        self.assertEqual(resp.json()["behaviour"], "accept")
        listed = [row["id"] for row in self.get("/_mock/accounts").json()]
        self.assertIn("OTHER", listed)

    def test_an_account_without_an_iban_is_refused(self):
        payload = self.body()
        del payload["iban"]
        resp = self.post("/_mock/accounts", payload)
        self.assertEqual(resp.status, 400)
        self.assertIn("iban", resp.json()["error"])

    def test_a_second_account_on_one_iban_is_refused_naming_the_first(self):
        acme = self.get("/_mock/accounts/ACME").json()
        resp = self.post("/_mock/accounts", self.body(iban=acme["iban"]))
        self.assertEqual(resp.status, 400)
        self.assertIn("ACME", resp.json()["error"])

    def test_an_id_that_would_not_survive_a_url_is_refused(self):
        resp = self.post("/_mock/accounts", self.body(id="a/b"))
        self.assertEqual(resp.status, 400)
        self.assertIn("URL", resp.json()["error"])

    def test_creating_one_that_exists_points_at_patch(self):
        resp = self.post("/_mock/accounts", self.body(id="ACME"))
        self.assertEqual(resp.status, 400)
        self.assertIn("PATCH", resp.json()["error"])


class NoTwoAccountsAreSentOneMsgId(MockServerCase):
    """An id cut to another account's is refused at creation (#160).

    Every `MsgId` the bank writes for an account is built from the first
    `MSG_ID_ACCOUNT_CHARS` of its id, which is what keeps it inside ISO's 35
    characters. Two accounts identical over that much of their ids were
    therefore sent `MB-C053-ABCDEFGHIJKLMNOPQR-1` each, day after day, and a
    client that de-duplicates on `MsgId` drops one of the two. The refusal is
    at creation, beside the trial write `create` already does: the same failure,
    an input the bank takes and then cannot write a distinguishable answer for.
    """

    CUT = MSG_ID_ACCOUNT_CHARS
    LONG = "ABCDEFGHIJKLMNOPQR-ONE"              # 22 characters, cut to 18

    def setUp(self):
        self.addCleanup(self.post, "/_mock/reset")

    def account(self, identifier, number):
        return self.post("/_mock/accounts", {
            "id": identifier, "name": identifier.title(),
            "iban": db.iban("NL", "MOCK00000090%02d" % number),
            "bic": "MOCKNL2A", "balance": 100000})

    def msgids(self):
        """Every `MsgId` the bank has written, read out of the messages."""
        messages = self.get("/_mock/messages").json()
        return re.findall(r"<MsgId>(MB-[^<]+)</MsgId>",
                          "".join(m.get("body") or "" for m in messages))

    def statements_for(self, *identifiers):
        """Two days of statements for these accounts, with money on each."""
        for identifier in identifiers:
            self.assertEqual(self.post("/_mock/credits", {
                "account": identifier, "amount": 5000,
                "note": "INV-1"}).status, 201)
        self.assertEqual(self.post("/_mock/advance?days=2").status, 200)

    def test_an_id_cut_to_an_existing_one_is_refused_naming_both(self):
        self.assertEqual(self.account(self.LONG, 1).status, 201)
        refused = self.account("ABCDEFGHIJKLMNOPQR-TWO", 2)
        self.assertEqual(refused.status, 400)
        error = refused.json()["error"]
        for expected in (self.LONG, "ABCDEFGHIJKLMNOPQR-TWO", str(self.CUT),
                         "MsgId"):
            self.assertIn(expected, error)
        held = [row["id"] for row in self.get("/_mock/accounts").json()]
        self.assertNotIn("ABCDEFGHIJKLMNOPQR-TWO", held)

    def test_an_id_of_exactly_the_cut_collides_with_a_longer_one(self):
        self.assertEqual(self.account(self.LONG, 1).status, 201)
        # The cut of a 22-character id is itself a legal id, and it would be
        # sent the same MsgIds.
        refused = self.account(self.LONG[:self.CUT], 2)
        self.assertEqual(refused.status, 400, refused.body)
        self.assertIn(self.LONG, refused.json()["error"])

    def test_ids_that_differ_inside_the_cut_are_both_held(self):
        self.assertEqual(self.account("ABCDEFGHIJKLM-ONE", 1).status, 201)
        # Seventeen characters each, differing at the fourteenth: shorter than
        # the cut, so nothing is lost and both are ordinary ids.
        kept = self.account("ABCDEFGHIJKLM-TWO", 2)
        self.assertEqual(kept.status, 201, kept.body)
        self.statements_for("ABCDEFGHIJKLM-ONE", "ABCDEFGHIJKLM-TWO")
        written = self.msgids()
        self.assertIn("MB-C053-ABCDEFGHIJKLM-ONE-1", written)
        self.assertIn("MB-C053-ABCDEFGHIJKLM-TWO-1", written)

    def test_no_two_messages_the_bank_writes_share_a_msgid(self):
        # The rule itself rather than the mechanism that keeps it: ask for both
        # accounts, take whatever the bank holds afterwards, give each of them
        # two days of statements and a notification, and no two messages may
        # share a MsgId. Written this way on purpose - if the refusal goes, the
        # second account is created and this fails, which a test that only ever
        # creates the first one cannot do.
        self.assertEqual(self.account(self.LONG, 1).status, 201)
        self.account("ABCDEFGHIJKLMNOPQR-TWO", 2)
        held = {row["id"] for row in self.get("/_mock/accounts").json()}
        self.statements_for(*[identifier for identifier
                             in (self.LONG, "ABCDEFGHIJKLMNOPQR-TWO", "ACME")
                             if identifier in held])
        written = self.msgids()
        # Not an empty bank agreeing with itself: the statements are there.
        self.assertIn("MB-C053-%s-1" % self.LONG[:self.CUT], written)
        self.assertGreater(len(written), 4, written)
        self.assertEqual(sorted(written), sorted(set(written)))


class Reset(MockServerCase):

    def test_the_seed_comes_back(self):
        before = self.get("/_mock/accounts").json()
        self.patch("/_mock/accounts/ACME", {"behaviour": "silent",
                                            "balance": 1})
        self.post("/_mock/accounts", {"id": "GONE",
                                      "iban": db.iban("NL", "MOCK0000000008")})
        self.assertEqual(self.post("/_mock/reset").status, 200)
        self.assertEqual(self.get("/_mock/accounts").json(), before)

    def test_it_says_how_many_accounts_it_put_back(self):
        self.assertEqual(self.post("/_mock/reset").json(),
                         {"reset": True, "accounts": len(db.SEED)})


class Methods(MockServerCase):

    def test_deleting_an_account_says_what_is_allowed(self):
        resp = self.request("DELETE", "/_mock/accounts/ACME")
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.json()["allowed"], ["GET", "PATCH"])


class TheReadmeSaysWhatTheSeedIs(MockServerCase):
    """The README's seeded-accounts table, held to the running mock.

    `tools/check_docs.py` checks that documentation exists, not that it is
    true, and a table of hand-copied IBANs is the kind of prose that goes
    stale silently - a reader who puts one in a `pain.001` and gets nothing
    back has no way of telling which of the two is wrong. So this reads the
    table out of the README and compares it with what the mock serves.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def documented(self):
        with open(os.path.join(self.ROOT, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        section = re.search(r"### The accounts it starts with\n(.*?)\n\n### |"
                            r"### The accounts it starts with\n(.*?)\nTwo rules",
                            readme, re.S)
        self.assertIsNotNone(section, "the README no longer has the seeded "
                                      "accounts table this test checks")
        rows = {}
        for line in (section.group(1) or section.group(2)).splitlines():
            cells = [cell.strip(" `") for cell in line.strip().strip("|").split("|")]
            if len(cells) == 5 and cells[0] not in ("Id", "---"):
                rows[cells[0]] = cells
        return rows

    def test_every_seeded_account_has_a_row_and_the_row_is_right(self):
        served = {row["id"]: row for row in self.get("/_mock/accounts").json()}
        documented = self.documented()
        self.assertEqual(sorted(documented), sorted(served))
        for identifier, cells in documented.items():
            with self.subTest(account=identifier):
                _id, iban, balance, behaviour, _why = cells
                self.assertEqual(iban, served[identifier]["iban"])
                self.assertEqual(behaviour, served[identifier]["behaviour"])
                # "125,000.00 EUR" is the same number as 12500000 minor units.
                amount, currency = balance.rsplit(" ", 1)
                self.assertEqual(int(amount.replace(",", "").replace(".", "")),
                                 served[identifier]["balance"])
                self.assertEqual(currency, served[identifier]["currency"])


class NoBodyProducesA500(MockServerCase):
    """A wrong JSON type is the caller's mistake, and has to read like one.

    Every one of these used to reach `sqlite3` and come back as a 500 with a
    traceback, which tells the caller nothing about which field they got
    wrong - and a 500 from a mock is indistinguishable from the mock being
    broken, which is the worst thing a test double can do to whoever is
    debugging with it. `check()` now types the text fields, and this walks the
    bodies a JSON client plausibly sends by accident.
    """

    # The field that is wrong, and a body that gets it wrong. Naming the field
    # in the data rather than deriving it is the point: the assertion is that
    # the refusal names the field the caller has to go and fix.
    HOSTILE = [
        ("id", {"id": 5}),
        ("id", {"id": ["X"]}),
        ("id", {"id": None}),
        ("iban", {"iban": {"value": "NL30MOCK0000000005"}}),
        ("iban", {"iban": 30000000005}),
        ("name", {"name": {"a": 1}}),
        ("name", {"name": [1]}),
        ("bic", {"bic": 12345678}),
        ("currency", {"currency": 978}),
        ("behaviour", {"behaviour": None}),
        ("balance", {"balance": {"amount": 1}}),
        ("closed", {"closed": "maybe"}),
        ("parameters", {"parameters": [1, 2]}),
    ]

    def setUp(self):
        self.addCleanup(self.post, "/_mock/reset")

    def body(self, fields):
        """A body that would be valid but for the one field under test."""
        payload = {"id": "OTHER", "iban": "NL30MOCK0000000005"}
        payload.update(fields)
        return payload

    def test_creating_one_names_the_field_rather_than_failing(self):
        for field, fields in self.HOSTILE:
            with self.subTest(field=field, sent=fields):
                resp = self.post("/_mock/accounts", self.body(fields))
                self.assertEqual(resp.status, 400, resp.body)
                self.assertIn(field, resp.json()["error"])
        self.assertEqual([row["id"] for row in self.get("/_mock/accounts").json()],
                         ["ACME", "EURODIS", "GLOBEX", "INITECH"])

    def test_patching_one_names_the_field_rather_than_failing(self):
        for field, fields in self.HOSTILE:
            if field == "id":
                continue                # an id is not a field PATCH can change
            with self.subTest(field=field, sent=fields):
                resp = self.patch("/_mock/accounts/ACME", fields)
                self.assertEqual(resp.status, 400, resp.body)
                self.assertIn(field, resp.json()["error"])
        # Nothing got through on the way.
        self.assertEqual(self.get("/_mock/accounts/ACME").json()["name"],
                         "ACME Corporation")


class PutIsNotAPartialPatch(MockServerCase):
    """PUT replaces; PATCH changes some fields. This mock only does the latter.

    It used to accept a PUT and quietly treat it as a PATCH, while the 405 it
    answered other methods with advertised only GET and PATCH - so a client
    doing a full replace got a partial update and no word about it.
    """

    def test_it_is_refused_and_the_refusal_names_what_is_allowed(self):
        resp = self.put("/_mock/accounts/ACME", {"behaviour": "silent"})
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.json()["allowed"], ["GET", "PATCH"])
        self.assertEqual(self.get("/_mock/accounts/ACME").json()["behaviour"],
                         "accept")
