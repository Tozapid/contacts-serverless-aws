import base64
import json
import os
import unittest
import urllib.parse

import carddav
import contacts
import contacts_test as fakes
import icloud_sync
import vcard
from google_sync_test import FakeSsm, LockingTable

OWNER = "owner@example.com"
# iCloud in these tests is the CardDAV server of this app, with an address
# book of its own: the client talks to it as it talks to iCloud.
APPLE_ID = "apple@example.com"
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 300


class ICloudSyncTest(unittest.TestCase):
    def setUp(self):
        self.table = LockingTable()
        self.s3 = fakes.FakeS3()
        self.ssm = FakeSsm()
        self.cognito = fakes.FakeCognito()
        self.cognito.admin_get_user = lambda UserPoolId, Username: {"Username": Username, "Enabled": True}
        contacts._table = self.table
        contacts._dynamodb = fakes.FakeDynamo(self.table)
        contacts._s3 = self.s3
        contacts._cognito = self.cognito
        self.ses = fakes.FakeSes()
        contacts._ses = self.ses
        icloud_sync._ssm = self.ssm
        carddav._auth.clear()
        carddav.SETTLE_SECONDS = 0
        os.environ.update({"TABLE_NAME": "contacts", "PHOTOS_BUCKET": "photos", "USER_POOL_ID": "pool",
                           "UID_DOMAIN": "contacts.example.com", "ICLOUD_PASSWORD_PREFIX": "/contacts/icloud",
                           "SITE_URL": "https://contacts.example.com", "SENDER": "contacts@example.com"})
        os.environ.pop("ICLOUD_SYNC_FUNCTION", None)
        self.password = contacts.create_app_password(APPLE_ID, {"name": "iCloud"})["password"]
        self.requests = []
        icloud_sync.transport = self.transport

    def tearDown(self):
        icloud_sync.transport = TRANSPORT

    def transport(self, method, url, headers, body):
        self.requests.append((method, url))
        parts = urllib.parse.urlsplit(url)
        event = {
            "rawPath": parts.path,
            "requestContext": {"http": {"method": method}},
            "headers": {key.lower(): value for key, value in headers.items()},
            "body": (body or b"").decode("utf-8"),
        }
        result = carddav.handler(event, None)
        return result["statusCode"], {k.lower(): v for k, v in result.get("headers", {}).items()}, result["body"].encode("utf-8")

    def connect(self, password=None):
        return icloud_sync.connect(OWNER, {"apple_id": APPLE_ID, "password": password or self.password})

    def sync(self):
        result = icloud_sync.sync_user(OWNER)
        self.assertIsInstance(result, dict, result)
        return result

    # The two sides.
    def mine(self):
        return {item["first"]: contacts.plain(item) for item in contacts.all_current(OWNER) if item.get("first")}

    def theirs(self):
        return {item["first"]: contacts.plain(item) for item in contacts.all_current(APPLE_ID) if item.get("first")}

    def their_put(self, name, text, etag=None):
        auth = base64.b64encode(f"{APPLE_ID}:{self.password}".encode()).decode()
        headers = {"authorization": "Basic " + auth, "user-agent": "iPhone"}
        headers.update({"if-match": etag} if etag else {"if-none-match": "*"})
        result = carddav.handler({"rawPath": "/dav/home/contacts/" + name, "requestContext": {"http": {"method": "PUT"}},
                                  "headers": headers, "body": text}, None)
        self.assertIn(result["statusCode"], (201, 204), result["body"])
        return result["headers"]["ETag"]

    def their_card(self, first, extra="", uid=None):
        uid = uid or first.upper() + "-UID"
        return f"BEGIN:VCARD\r\nVERSION:3.0\r\nN:;{first};;;\r\nFN:{first}\r\n{extra}UID:{uid}\r\nEND:VCARD\r\n"

    def log(self):
        return contacts.sync_log(OWNER)["items"]

    def test_connect(self):
        with self.assertRaises(contacts.ApiError) as caught:
            self.connect("wrong-password-1")
        self.assertEqual(caught.exception.code, "icloud_auth")
        state = self.connect()
        self.assertEqual((state["connected"], state["apple_id"]), (True, APPLE_ID))
        self.assertTrue(icloud_sync.state_item(OWNER)["book"].endswith("/dav/home/contacts/"))
        self.assertIn("/contacts/icloud/", next(iter(self.ssm.values.keys() - {"/secret"})))
        self.assertEqual(self.log()[0]["action"], "connected")
        icloud_sync.disconnect(OWNER)
        self.assertFalse(icloud_sync.public_state(OWNER)["connected"])
        self.assertEqual(self.ssm.values.keys() - {"/secret"}, set())

    def test_first_sync_matches_and_uploads(self):
        contacts.create_contact(OWNER, {"first": "Анна", "maiden": "Сидорова", "phones": [{"label": "mobile", "value": "+7 916 000-00-01"}]})
        contacts.create_contact(OWNER, {"first": "Борис", "pronouns": "he"})
        # Анна is in iCloud with an email the book lacks; Вера is there only.
        self.their_put("anna.vcf", self.their_card("Анна", "TEL;type=CELL:8 916 000-00-01\r\nEMAIL;type=WORK:anna@example.com\r\n"))
        self.their_put("vera.vcf", self.their_card("Вера"))
        self.connect()
        self.sync()
        mine, theirs = self.mine(), self.theirs()
        self.assertEqual(set(mine), {"Анна", "Борис", "Вера"})
        self.assertEqual(set(theirs), {"Анна", "Борис", "Вера"})
        self.assertEqual(mine["Анна"]["emails"], [{"label": "work", "value": "anna@example.com"}])
        self.assertEqual(mine["Анна"]["maiden"], "Сидорова")
        self.assertEqual(theirs["Анна"]["maiden"], "Сидорова")
        self.assertEqual(mine["Вера"]["uid"], "ВЕРА-UID")
        self.assertEqual(theirs["Борис"]["uid"], contacts.dav_uid(mine["Борис"]))
        actions = sorted((line["source"], line["action"]) for line in self.log() if line["action"] != "connected")
        self.assertIn(("icloud", "matched"), actions)
        self.assertIn(("icloud", "created"), actions)
        self.assertIn(("to_icloud", "created"), actions)
        # Nothing changed: the next sync writes nothing.
        writes = len([1 for method, _ in self.requests if method in ("PUT", "DELETE")])
        self.assertEqual(self.sync(), {"to_book": 0, "to_icloud": 0, "conflicts": 0, "deleted": 0})
        self.assertEqual(len([1 for method, _ in self.requests if method in ("PUT", "DELETE")]), writes)

    def test_changes_both_ways_and_conflicts(self):
        made = contacts.create_contact(OWNER, {"first": "Анна"})
        self.connect()
        self.sync()
        theirs = contacts.get_current(APPLE_ID, self.theirs()["Анна"]["id"])
        # iCloud -> book; a field iCloud does not keep (pronouns) stays here.
        card = contacts.get_current(OWNER, made["id"])
        contacts.save_contact(OWNER, made["id"], {**contacts.public_contact(card), "pronouns": "she"})
        self.sync()
        theirs = contacts.get_current(APPLE_ID, theirs["id"])
        text = vcard.contact_to_card({**contacts.public_contact(theirs), "company": "Ромашка"})
        self.their_put(theirs["dav"], "\r\n".join(line for line in text.split("\r\n") if not line.startswith("X-PRONOUNS")), '"%s"' % theirs["etag"])
        self.sync()
        mine = self.mine()["Анна"]
        self.assertEqual((mine["company"], mine["pronouns"]), ("Ромашка", "she"))
        revisions = contacts.revisions(OWNER, made["id"])
        self.assertEqual(revisions[0]["action"], "icloud")
        # Book -> iCloud.
        card = contacts.get_current(OWNER, made["id"])
        contacts.save_contact(OWNER, made["id"], {**contacts.public_contact(card), "title": "Инженер"})
        self.sync()
        self.assertEqual(self.theirs()["Анна"]["title"], "Инженер")
        # Both sides: the book wins and goes to iCloud.
        card = contacts.get_current(OWNER, made["id"])
        contacts.save_contact(OWNER, made["id"], {**contacts.public_contact(card), "company": "Лютик"})
        theirs = contacts.get_current(APPLE_ID, self.theirs()["Анна"]["id"])
        self.their_put(theirs["dav"], vcard.contact_to_card({**contacts.public_contact(theirs), "company": "Василёк"}), '"%s"' % theirs["etag"])
        result = self.sync()
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual((self.mine()["Анна"]["company"], self.theirs()["Анна"]["company"]), ("Лютик", "Лютик"))
        conflict = next(line for line in self.log() if line["action"] == "conflict")
        self.assertIn("company", conflict["fields"])

    def test_deletions_both_ways(self):
        anna = contacts.create_contact(OWNER, {"first": "Анна"})
        contacts.create_contact(OWNER, {"first": "Борис"})
        self.connect()
        self.sync()
        # Deleted here: deleted there.
        contacts.delete_contact(OWNER, anna["id"], contacts.get_current(OWNER, anna["id"])["etag"])
        self.sync()
        self.assertEqual(set(self.theirs()), {"Борис"})
        # Deleted there: deleted here, restorable.
        boris = contacts.get_current(APPLE_ID, self.theirs()["Борис"]["id"])
        contacts.delete_contact(APPLE_ID, boris["id"], boris["etag"])
        result = self.sync()
        self.assertEqual((result["deleted"], set(self.mine())), (1, set()))
        self.assertEqual(next(line for line in self.log() if line["action"] == "deleted")["detail"], "restorable")

    def test_groups_both_ways(self):
        anna = contacts.create_contact(OWNER, {"first": "Анна"})
        contacts.store_group(OWNER, "Семья", [contacts.dav_uid(contacts.get_current(OWNER, anna["id"]))])
        self.their_put("boris.vcf", self.their_card("Борис"))
        self.their_put("work.vcf", vcard.group_to_card("Работа", "WORK-UID", ["БОРИС-UID"]))
        self.connect()
        self.sync()
        def groups(owner):
            uids = {value: key for key, value in contacts.card_uids(owner).items()}
            names = {item["id"]: item.get("first") for item in contacts.all_current(owner)}
            return {group["name"]: sorted(names[uids[uid]] for uid in group.get("members") or [] if uid in uids) for group in contacts.group_items(owner)}
        self.assertEqual(groups(OWNER), {"Семья": ["Анна"], "Работа": ["Борис"]})
        self.assertEqual(groups(APPLE_ID), {"Семья": ["Анна"], "Работа": ["Борис"]})
        # Борис joins Семья here; Работа is renamed there.
        family = next(group for group in contacts.group_items(OWNER) if group["name"] == "Семья")
        boris = self.mine()["Борис"]
        contacts.transact([contacts.group_put(family, {"members": family["members"] + [contacts.dav_uid(boris)]}, contacts.now_iso())])
        work = next(group for group in contacts.group_items(APPLE_ID) if group["name"] == "Работа")
        self.their_put(work["dav"], vcard.group_to_card("Офис", work["uid"], work["members"]), carddav.etag_of(work))
        self.sync()
        self.assertEqual(groups(OWNER), {"Семья": ["Анна", "Борис"], "Офис": ["Борис"]})
        self.assertEqual(groups(APPLE_ID), {"Семья": ["Анна", "Борис"], "Офис": ["Борис"]})
        self.assertEqual(self.sync(), {"to_book": 0, "to_icloud": 0, "conflicts": 0, "deleted": 0})

    def test_photos_both_ways(self):
        made = contacts.create_contact(OWNER, {"first": "Анна", "photo": contacts.put_photo(OWNER, PNG, "image/png")})
        self.their_put("boris.vcf", self.their_card("Борис", "PHOTO;ENCODING=b;TYPE=PNG:" + base64.b64encode(PNG + b"b").decode() + "\r\n"))
        self.connect()
        self.sync()
        theirs, mine = self.theirs(), self.mine()
        self.assertTrue(theirs["Анна"].get("photo"))
        self.assertEqual(self.s3.objects[mine["Борис"]["photo"].lstrip("/")][0], PNG + b"b")
        self.assertEqual(self.sync(), {"to_book": 0, "to_icloud": 0, "conflicts": 0, "deleted": 0})
        self.assertEqual(self.mine()["Анна"]["photo"], made["photo"])

    def test_a_photo_given_as_a_link(self):
        self.their_put("vera.vcf", self.their_card("Вера"))
        served = {"https://photos.example.com/vera": PNG + b"v"}
        dav = self.transport
        icloud_sync.transport = lambda method, url, headers, body: (200, {}, served[url]) if url in served else dav(method, url, headers, body)
        # iCloud gives the photo as a link; the server of these tests keeps
        # only inline photos, so the link is put into its answer.
        multiget = icloud_sync.Client.multiget
        def with_link(client, urls):
            return {url: (etag, text.replace("END:VCARD", "PHOTO;VALUE=uri:https://photos.example.com/vera\r\nEND:VCARD") if "Вера" in text else text)
                    for url, (etag, text) in multiget(client, urls).items()}
        icloud_sync.Client.multiget = with_link
        self.addCleanup(setattr, icloud_sync.Client, "multiget", multiget)
        self.connect()
        self.sync()
        self.assertEqual(self.s3.objects[self.mine()["Вера"]["photo"].lstrip("/")][0], PNG + b"v")

    def test_hidden_birthday_goes_as_a_date(self):
        contacts.create_contact(OWNER, {"first": "Анна", "hidden_birthday": "1990-05-04"})
        self.connect()
        self.sync()
        theirs = self.theirs()["Анна"]
        self.assertEqual((theirs.get("birthday"), theirs["hidden_birthday"]), (None, "1990-05-04"))
        self.assertEqual(self.sync()["to_icloud"], 0)

    def test_busy_and_reauth(self):
        self.connect()
        self.assertTrue(icloud_sync.lock(OWNER))
        self.assertEqual(icloud_sync.sync_user(OWNER), "busy")
        icloud_sync.save_state(OWNER, {"lock_until": 0})
        contacts.revoke_app_password(APPLE_ID, contacts.list_app_passwords(APPLE_ID)[0]["id"])
        carddav._auth.clear()
        self.assertEqual(icloud_sync.sync_user(OWNER), "error")
        self.assertEqual(icloud_sync.public_state(OWNER)["status"], "reauth")
        self.assertEqual(len(self.ses.sent), 1)
        self.assertEqual((self.ses.sent[0]["to"], self.ses.sent[0]["from"]), ([OWNER], "Contacts <contacts@example.com>"))
        self.assertIn("iCloud", self.ses.sent[0]["subject"])
        icloud_sync.sync_user(OWNER)
        self.assertEqual(len(self.ses.sent), 1)

    def test_api_routes(self):
        def call(method, path, body=None):
            result = contacts.handler(fakes.event(method, path, body), None)
            return result["statusCode"], json.loads(result["body"])
        status, data = call("GET", "/api/icloud")
        self.assertEqual((status, data["enabled"], data["connected"]), (200, True, False))
        status, data = call("POST", "/api/icloud", {"apple_id": "not-an-email", "password": "x"})
        self.assertEqual((status, data["code"]), (400, "bad_icloud"))
        status, data = call("POST", "/api/icloud", {"apple_id": APPLE_ID, "password": self.password})
        self.assertEqual((status, data["connected"]), (200, True))
        status, _ = call("POST", "/api/icloud/sync")
        self.assertEqual(status, 202)
        status, data = call("DELETE", "/api/icloud")
        self.assertEqual((status, data["disconnected"]), (200, True))


TRANSPORT = icloud_sync.transport


if __name__ == "__main__":
    unittest.main()
