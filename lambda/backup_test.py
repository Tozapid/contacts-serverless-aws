import io
import json
import unittest
import zipfile
from unittest import mock

import backup
import contacts
import contacts_test as fakes
import vcard

OWNER = fakes.OWNER
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 300


class BackupTest(unittest.TestCase):
    # The fakes and helpers of the API tests, without their tests.
    setUp = fakes.ApiTest.setUp
    call = fakes.ApiTest.call
    create = fakes.ApiTest.create

    def archive(self):
        status, data = self.call("POST", "/api/backup")
        self.assertEqual((status, data["status"]), (200, "done"), data)
        key = data["url"].split(".amazonaws.com/")[1].split("?")[0]
        return data, zipfile.ZipFile(io.BytesIO(self.s3.objects[key][0]))

    def test_the_archive_holds_everything(self):
        photo = contacts.put_photo(OWNER, PNG, "image/png")
        anna = self.create(photo=photo, hidden=True, dates=[{"label": "anniversary", "value": "2010-06-01", "hidden": True}])
        self.call("PUT", f"/api/contacts/{anna['id']}", {**anna, "company": "Ромашка"})
        boris = self.create(first="Борис")
        contacts.create_group(OWNER, {"name": "Семья", "members": [anna["id"], boris["id"]]})
        self.call("DELETE", f"/api/contacts/{boris['id']}", params={"etag": boris["etag"]})
        self.call("PUT", "/api/drafts/new", {"draft": {"first": "Вера"}})
        contacts.log_sync(OWNER, "google", "created", name="Анна")
        contacts.create_app_password(OWNER, {"name": "iPhone"})

        data, archive = self.archive()
        self.assertEqual(data["counts"], {"contacts": 1, "versions": 4, "deleted": 1, "groups": 1, "photos": 1, "log": 1})
        self.assertTrue(data["name"].startswith("contacts-backup-") and data["name"].endswith(".zip"))
        names = set(archive.namelist())
        self.assertEqual(names, {"README.txt", "contacts.vcf", "contacts.json", "history.json", "trash.json", "groups.json",
                                 "drafts.json", "sync-log.json", "devices.json", "photos/" + photo.rsplit("/", 1)[1]})
        cards = json.loads(archive.read("contacts.json"))
        self.assertEqual((cards[0]["company"], cards[0]["hidden"], cards[0]["dates"][0]["hidden"]), ("Ромашка", True, True))
        history = json.loads(archive.read("history.json"))
        self.assertEqual([entry["action"] for entry in history[anna["id"]]], ["create", "save"])
        self.assertEqual([entry["action"] for entry in history[boris["id"]]], ["create", "delete"])
        self.assertEqual(json.loads(archive.read("trash.json"))[0]["card"]["first"], "Борис")
        groups = json.loads(archive.read("groups.json"))
        self.assertEqual((groups[0]["members"], len(groups[0]["other_members"])), ([anna["id"]], 1))
        self.assertEqual(json.loads(archive.read("drafts.json"))[0]["draft"], {"first": "Вера"})
        self.assertEqual(json.loads(archive.read("devices.json"))[0]["name"], "iPhone")
        self.assertNotIn("hash", archive.read("devices.json").decode())
        self.assertEqual(archive.read("photos/" + photo.rsplit("/", 1)[1]), PNG)
        cards_vcf = vcard.split_cards(archive.read("contacts.vcf").decode())
        contact, picture = vcard.card_to_contact(cards_vcf[0])
        self.assertEqual((contact["company"], contact["categories"], picture), ("Ромашка", ["Семья"], (PNG, "image/png")))

    def test_state_link_and_expiry(self):
        self.create()
        status, data = self.call("GET", "/api/backup")
        self.assertEqual(data, {"status": "none"})
        made, _ = self.archive()
        status, data = self.call("GET", "/api/backup")
        self.assertEqual((data["status"], data["id"], bool(data["url"])), ("done", made["id"], True))
        item = self.table.items[("USER#" + OWNER, "BACKUP")]
        item["finished"] = "2020-01-01T00:00:00.000Z"
        status, data = self.call("GET", "/api/backup")
        self.assertEqual((data["status"], data.get("url")), ("expired", None))

    def test_the_function_runs_in_the_background(self):
        self.create()
        invoked = []
        client = mock.Mock()
        client.invoke.side_effect = lambda **kwargs: invoked.append(json.loads(kwargs["Payload"]))
        with mock.patch.dict("os.environ", {"BACKUP_FUNCTION": "contacts-backup"}), mock.patch.object(contacts, "_lambda", client):
            status, data = self.call("POST", "/api/backup")
            self.assertEqual(data["status"], "running")
            # A second start while it runs is the same export.
            status, again = self.call("POST", "/api/backup")
            self.assertEqual((again["id"], len(invoked)), (data["id"], 1))
        self.assertEqual(invoked[0], {"owner": OWNER, "id": data["id"]})
        backup.handler(invoked[0], None)
        status, data = self.call("GET", "/api/backup")
        self.assertEqual((data["status"], data["counts"]["contacts"]), ("done", 1))

    def test_a_failure_is_shown(self):
        self.create()
        with mock.patch.object(backup, "write_archive", side_effect=RuntimeError("disk full")):
            status, data = self.call("POST", "/api/backup")
        self.assertEqual((data["status"], data["error"]), ("error", "disk full"))


if __name__ == "__main__":
    unittest.main()
