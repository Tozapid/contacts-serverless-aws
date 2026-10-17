import json
import os
import unittest
from pathlib import Path

import contacts
import contacts_test as fakes
import reset
from google_sync_test import FakeSsm

OWNER = "demo@example.com"
SEED = json.loads((Path(__file__).resolve().parent.parent / "seed" / "seed.json").read_text(encoding="utf-8"))


class ScanTable(fakes.FakeTable):
    # The reset scans the whole table, two items a page to exercise paging.
    def scan(self, ProjectionExpression=None, ExclusiveStartKey=None):
        keys = sorted(self.items)
        if ExclusiveStartKey:
            keys = [key for key in keys if key > (ExclusiveStartKey["pk"], ExclusiveStartKey["sk"])]
        page = [{"pk": pk, "sk": sk} for pk, sk in keys[:2]]
        return {"Items": page, **({"LastEvaluatedKey": page[-1]} if len(keys) > 2 else {})}


class ResetCognito(fakes.FakeCognito):
    def __init__(self):
        super().__init__()
        self.passwords, self.signed_out, self.mfa_off = {}, [], []

    def admin_create_user(self, UserPoolId, Username, **kwargs):
        self.users[Username] = {"Username": Username, "Enabled": True}

    def admin_set_user_password(self, UserPoolId, Username, Password, Permanent):
        self.passwords[Username] = Password

    def admin_enable_user(self, UserPoolId, Username):
        self.users[Username]["Enabled"] = True

    def admin_set_user_mfa_preference(self, UserPoolId, Username, SoftwareTokenMfaSettings):
        self.mfa_off.append(Username)

    def admin_delete_user_attributes(self, UserPoolId, Username, UserAttributeNames):
        pass

    def admin_user_global_sign_out(self, UserPoolId, Username):
        self.signed_out.append(Username)


class ResetTest(unittest.TestCase):
    def setUp(self):
        self.table = ScanTable()
        self.s3 = fakes.FakeS3()
        self.cognito = ResetCognito()
        self.cognito.users[OWNER] = {"Username": OWNER, "Enabled": False}
        self.cognito.users["visitor@example.org"] = {"Username": "visitor@example.org", "Enabled": True}
        contacts._table = self.table
        contacts._dynamodb = fakes.FakeDynamo(self.table)
        contacts._s3 = self.s3
        contacts._cognito = self.cognito
        ssm = FakeSsm()
        ssm.values["/p"] = "Demo-Password-1"
        reset._ssm = ssm
        os.environ.update({"TABLE_NAME": "contacts", "PHOTOS_BUCKET": "photos", "USER_POOL_ID": "pool", "UID_DOMAIN": "demo.example.com",
                           "DEMO_EMAIL": OWNER, "DEMO_PASSWORD_PARAMETER": "/p", "DEMO": "true"})

    def tearDown(self):
        os.environ.pop("DEMO", None)

    def call(self, method, path, body=None):
        result = contacts.handler(fakes.event(method, path, body, email=OWNER, admin=True), None)
        return result["statusCode"], json.loads(result["body"])

    def test_reset_writes_the_seed_and_wipes_the_rest(self):
        # What visitors left behind.
        contacts.create_contact(OWNER, {"first": "Visitor's card"})
        contacts.create_contact("visitor@example.org", {"first": "Other"})
        self.s3.objects["photos/abc/visitor.jpg"] = (b"x", "image/jpeg")
        result = reset.handler({}, None)
        self.assertEqual(result["contacts"], len(SEED["contacts"]))
        self.assertEqual(self.s3.objects, {})
        self.assertEqual(set(self.cognito.users), {OWNER})
        self.assertEqual(self.cognito.passwords[OWNER], "Demo-Password-1")
        self.assertTrue(self.cognito.users[OWNER]["Enabled"])
        self.assertEqual((self.cognito.signed_out, self.cognito.mfa_off), ([OWNER], [OWNER]))
        self.assertIn(OWNER, self.cognito.groups["admins"])
        # The address book as the page sees it.
        status, data = self.call("GET", "/api/contacts")
        names = {item.get("first") or item.get("company") or item["emails"][0]["value"] for item in data["items"]}
        self.assertEqual(status, 200)
        self.assertNotIn("Visitor's card", names)
        deleted = [entry for entry in SEED["contacts"] if "deleted_minutes_ago" in entry]
        self.assertEqual(len(data["items"]), len(SEED["contacts"]) - len(deleted))
        cards = {item.get("first") or item.get("company") or "": item for item in data["items"]}
        # Related people point at their cards.
        daniel = cards["Daniel"]
        self.assertEqual({entry["label"]: entry.get("value") for entry in daniel["related"]},
                         {"mother": "Margaret Walker", "father": "Robert Walker Sr.", "sister": "Emily Carter"})
        self.assertTrue(all(entry.get("contact") in {item["id"] for item in data["items"]} for entry in daniel["related"]))
        self.assertEqual(cards["Thomas"]["hidden_birthday"], "1989-12-24")
        # Earlier versions, dated in the past.
        status, history = self.call("GET", f"/api/contacts/{cards['Margaret']['id']}/revisions")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(history["items"]), 3)
        self.assertLess(min(item["at"] for item in history["items"]), contacts.now_iso()[:4] + "-99")
        # The deleted card, the groups, the devices, the sync log.
        status, gone = self.call("GET", "/api/deleted")
        self.assertEqual([item["name"] for item in gone["items"]], ["Noah Fischer"])
        status, groups = self.call("GET", "/api/groups")
        self.assertEqual({group["name"]: len(group["members"]) for group in groups["items"]},
                         {group["name"]: len(group["members"]) for group in SEED["groups"]})
        status, devices = self.call("GET", "/api/app-passwords")
        self.assertEqual(sorted(item["name"] for item in devices["items"]), ["MacBook", "Thunderbird", "iPhone"])
        status, log = self.call("GET", "/api/sync-log")
        self.assertEqual(len(log["items"]), len(SEED["sync_log"]))
        self.assertEqual(log["items"][0]["source"], "dav:iPhone")
        self.assertEqual(next(line for line in log["items"] if line["action"] == "deleted")["name"], "Noah Fischer")
        # A second reset gives the same.
        again = reset.handler({}, None)
        self.assertEqual(again["contacts"], result["contacts"])
        self.assertEqual(len(self.call("GET", "/api/contacts")[1]["items"]), len(data["items"]))

    def test_duplicates_are_there_to_merge(self):
        reset.handler({}, None)
        cards = [contacts.plain(item) for item in contacts.all_current(OWNER)]
        smiths = [card for card in cards if card.get("last") == "Smith"]
        self.assertEqual(len(smiths), 2)

    def test_demo_sync_is_shown_not_run(self):
        reset.handler({}, None)
        status, google = self.call("GET", "/api/google")
        self.assertEqual((status, google["connected"], google["status"], google["email"]), (200, True, "ok", "demo.contacts@gmail.com"))
        status, icloud = self.call("GET", "/api/icloud")
        self.assertEqual((status, icloud["connected"], icloud["apple_id"]), (200, True, "demo.contacts@icloud.com"))
        self.assertEqual(icloud["linked"], len(SEED["contacts"]) - 1)
        self.assertEqual(self.call("POST", "/api/google/sync")[0], 202)
        self.assertEqual(self.call("DELETE", "/api/google")[1]["code"], "demo_readonly")
        self.assertEqual(self.call("POST", "/api/icloud", {"apple_id": "a@b.c", "password": "x"})[1]["code"], "demo_readonly")
        self.assertEqual(self.call("POST", "/api/admin/users", {"email": "x@example.com"})[1]["code"], "demo_readonly")
        status, me = self.call("GET", "/api/me")
        self.assertEqual((me["demo"], me["dav_url"]), (True, "https://dav.example.com"))


if __name__ == "__main__":
    unittest.main()
