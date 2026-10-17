import base64
import os
import time
import unittest
import xml.etree.ElementTree as ElementTree

import carddav
import contacts
import contacts_test as fakes
import vcard

OWNER = "owner@example.com"
NS = {"d": "DAV:", "card": "urn:ietf:params:xml:ns:carddav", "cs": "http://calendarserver.org/ns/"}
CARD = "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Петрова;Анна;;;\r\nFN:Анна Петрова\r\nTEL;type=CELL:+7 916 000-00-01\r\nUID:ABC-1\r\nEND:VCARD\r\n"


class CardDavTest(unittest.TestCase):
    def setUp(self):
        self.table = fakes.FakeTable()
        self.s3 = fakes.FakeS3()
        self.cognito = fakes.FakeCognito()
        self.cognito.users[OWNER] = {"Username": OWNER, "Enabled": True}
        self.cognito.admin_get_user = lambda UserPoolId, Username: (
            {"Username": Username, "Enabled": self.cognito.users[Username].get("Enabled", True)}
            if Username in self.cognito.users else (_ for _ in ()).throw(fakes.ClientError("UserNotFoundException")))
        contacts._table = self.table
        contacts._dynamodb = fakes.FakeDynamo(self.table)
        contacts._s3 = self.s3
        contacts._cognito = self.cognito
        carddav._auth.clear()
        carddav.SETTLE_SECONDS = 0
        os.environ.update({"TABLE_NAME": "contacts", "PHOTOS_BUCKET": "photos", "USER_POOL_ID": "pool", "UID_DOMAIN": "contacts.example.com"})
        self.password = contacts.create_app_password(OWNER, {"name": "iPhone"})["password"]

    def call(self, method, path, body="", headers=None, password=None):
        auth = base64.b64encode(f"{OWNER}:{password or self.password}".encode()).decode()
        event = {
            "rawPath": path,
            "requestContext": {"http": {"method": method}},
            "headers": {"authorization": "Basic " + auth, **(headers or {})},
            "body": body,
        }
        return carddav.handler(event, None)

    def xml(self, result):
        self.assertEqual(result["statusCode"], 207, result["body"])
        return ElementTree.fromstring(result["body"].encode())

    def page_contact(self, **fields):
        return contacts.create_contact(OWNER, {"first": "Борис", "last": "Смирнов", **fields})

    def test_discovery(self):
        self.assertEqual(carddav.handler({"rawPath": "/", "requestContext": {"http": {"method": "OPTIONS"}}}, None)["statusCode"], 200)
        result = self.call("PROPFIND", "/.well-known/carddav")
        self.assertEqual((result["statusCode"], result["headers"]["Location"]), (301, "/dav/"))
        body = '<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav"><d:prop><d:current-user-principal/><card:addressbook-home-set/><d:getetag/></d:prop></d:propfind>'
        root = self.xml(self.call("PROPFIND", "/dav/", body, {"depth": "0"}))
        self.assertEqual(root.findtext(".//d:current-user-principal/d:href", namespaces=NS), "/dav/principal/")
        self.assertEqual(root.findtext(".//card:addressbook-home-set/d:href", namespaces=NS), "/dav/home/")
        self.assertIsNotNone(root.find(".//d:propstat[d:status='HTTP/1.1 404 Not Found']/d:prop/d:getetag", namespaces=NS))
        root = self.xml(self.call("PROPFIND", "/dav/home/", '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>', {"depth": "1"}))
        self.assertEqual([node.text for node in root.findall("d:response/d:href", NS)], ["/dav/home/", "/dav/home/contacts/"])
        self.assertIsNotNone(root.find("d:response[d:href='/dav/home/contacts/']//card:addressbook", NS))

    def test_the_owner_may_write_everywhere(self):
        body = '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-privilege-set/></d:prop></d:propfind>'
        root = self.xml(self.call("PROPFIND", "/dav/home/", body, {"depth": "1"}))
        for href in ("/dav/home/", "/dav/home/contacts/"):
            privileges = {node.tag.split("}")[1] for node in root.findall(f"d:response[d:href='{href}']//d:privilege/*", NS)}
            self.assertTrue({"write", "write-content", "bind", "unbind"} <= privileges, href)

    def test_sign_in(self):
        self.assertEqual(self.call("PROPFIND", "/dav/", password="wrong")["statusCode"], 401)
        self.assertIn("Basic", self.call("PROPFIND", "/dav/", password="wrong")["headers"]["WWW-Authenticate"])
        self.cognito.users[OWNER]["Enabled"] = False
        self.assertEqual(self.call("PROPFIND", "/dav/")["statusCode"], 401)
        self.cognito.users[OWNER]["Enabled"] = True
        self.assertEqual(self.call("PROPFIND", "/dav/")["statusCode"], 207)
        self.assertTrue(contacts.list_app_passwords(OWNER)[0]["used"])
        contacts.revoke_app_password(OWNER, contacts.list_app_passwords(OWNER)[0]["id"])
        carddav._auth.clear()
        self.assertEqual(self.call("PROPFIND", "/dav/")["statusCode"], 401)

    def test_book_lists_cards(self):
        made = self.page_contact()
        root = self.xml(self.call("PROPFIND", "/dav/home/contacts/", '<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/"><d:prop><d:getetag/><cs:getctag/></d:prop></d:propfind>', {"depth": "1"}))
        self.assertEqual(root.findtext(f"d:response[d:href='/dav/home/contacts/{made['id']}.vcf']//d:getetag", namespaces=NS), f'"{made["etag"]}"')
        self.assertTrue(root.findtext("d:response[d:href='/dav/home/contacts/']//cs:getctag", namespaces=NS).endswith(made["modified"]))

    def test_put_get_update_delete(self):
        result = self.call("PUT", "/dav/home/contacts/ABC-1.vcf", CARD, {"if-none-match": "*"})
        self.assertEqual(result["statusCode"], 201, result["body"])
        etag = result["headers"]["ETag"]
        item = next(contacts.all_current(OWNER))
        self.assertEqual((item["dav"], item["first"], item["uid"]), ("ABC-1.vcf", "Анна", "ABC-1"))
        self.assertEqual(contacts.revisions(OWNER, item["id"])[0]["action"], "sync")
        line = contacts.sync_log(OWNER)["items"][0]
        self.assertEqual((line["source"], line["action"], line["name"]), ("dav:iPhone", "created", "Анна Петрова"))
        self.assertEqual(self.call("PUT", "/dav/home/contacts/ABC-1.vcf", CARD, {"if-none-match": "*"})["statusCode"], 412)
        result = self.call("GET", "/dav/home/contacts/ABC-1.vcf")
        self.assertEqual((result["statusCode"], result["headers"]["ETag"]), (200, etag))
        self.assertIn("UID:ABC-1", result["body"])
        # The page adds pronouns, which the client does not know; its next save keeps them.
        saved = contacts.save_contact(OWNER, item["id"], {**contacts.public_contact(item), "pronouns": "she/her"})
        changed = CARD.replace("END:VCARD", "ORG:Ромашка\r\nEND:VCARD")
        self.assertEqual(self.call("PUT", "/dav/home/contacts/ABC-1.vcf", changed, {"if-match": etag})["statusCode"], 412)
        result = self.call("PUT", "/dav/home/contacts/ABC-1.vcf", changed, {"if-match": f'"{saved["etag"]}"'})
        self.assertEqual(result["statusCode"], 204)
        item = next(contacts.all_current(OWNER))
        self.assertEqual((item["company"], item["pronouns"], item["dav"]), ("Ромашка", "she/her", "ABC-1.vcf"))
        self.assertEqual(self.call("DELETE", "/dav/home/contacts/ABC-1.vcf", headers={"if-match": '"x"'})["statusCode"], 412)
        self.assertEqual(self.call("DELETE", "/dav/home/contacts/ABC-1.vcf")["statusCode"], 204)
        self.assertEqual(list(contacts.all_current(OWNER)), [])
        self.assertEqual(contacts.deleted_contacts(OWNER)[0]["dav"], "ABC-1.vcf")
        self.assertEqual(self.call("GET", "/dav/home/contacts/ABC-1.vcf")["statusCode"], 404)

    def test_put_photo_and_bad_cards(self):
        jpeg = b"\xff\xd8\xff\xe0photo"
        card = CARD.replace("END:VCARD", "PHOTO;ENCODING=b;TYPE=JPEG:" + base64.b64encode(jpeg).decode() + "\r\nEND:VCARD")
        self.assertEqual(self.call("PUT", "/dav/home/contacts/p.vcf", card)["statusCode"], 201)
        self.assertEqual(len(self.s3.objects), 1)
        text = self.call("GET", "/dav/home/contacts/p.vcf")["body"]
        self.assertEqual(vcard.card_to_contact(vcard.split_cards(text)[0])[1], (jpeg, "image/jpeg"))
        long = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:x\r\nTITLE:" + "x" * 300 + "\r\nEND:VCARD\r\n"
        self.assertEqual(self.call("PUT", "/dav/home/contacts/e.vcf", long)["statusCode"], 400)

    def test_apple_new_card_is_blank_first(self):
        blank = "BEGIN:VCARD\r\nVERSION:3.0\r\nPRODID:-//Apple Inc.//macOS 15.0//EN\r\nN:;;;;\r\nFN:\r\nUID:NEW-1\r\nEND:VCARD\r\n"
        result = self.call("PUT", "/dav/home/contacts/NEW-1.vcf", blank, {"if-none-match": "*"})
        self.assertEqual(result["statusCode"], 201, result["body"])
        filled = blank.replace("N:;;;;", "N:Тестов;Тест;;;").replace("FN:", "FN:Тест Тестов")
        result = self.call("PUT", "/dav/home/contacts/NEW-1.vcf", filled, {"if-match": result["headers"]["ETag"]})
        self.assertEqual(result["statusCode"], 204, result["body"])
        cards = list(contacts.all_current(OWNER))
        self.assertEqual([(item["first"], item["dav"]) for item in cards], [("Тест", "NEW-1.vcf")])

    def test_apple_groups(self):
        self.call("PUT", "/dav/home/contacts/ABC-1.vcf", CARD)
        made = self.page_contact()
        group = ("BEGIN:VCARD\r\nVERSION:3.0\r\nN:Друзья;;;;\r\nFN:Друзья\r\nX-ADDRESSBOOKSERVER-KIND:group\r\nUID:G-1\r\n"
                 f"X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:ABC-1\r\nX-ADDRESSBOOKSERVER-MEMBER:urn:uuid:{made['id']}@contacts.example.com\r\nEND:VCARD\r\n")
        result = self.call("PUT", "/dav/home/contacts/G-1.vcf", group, {"if-none-match": "*"})
        self.assertEqual(result["statusCode"], 201, result["body"])
        anna = next(item for item in contacts.all_current(OWNER) if item.get("uid") == "ABC-1")
        groups = contacts.list_groups(OWNER)
        self.assertEqual([(item["name"], sorted(item["members"])) for item in groups], [("Друзья", sorted([anna["id"], made["id"]]))])
        self.assertEqual(len(list(contacts.all_current(OWNER))), 2)
        text = self.call("GET", "/dav/home/contacts/G-1.vcf")["body"]
        self.assertIn("X-ADDRESSBOOKSERVER-KIND:group", text)
        self.assertIn("urn:uuid:ABC-1", text)
        # The page takes Борис out of the group; the client sees the group changed.
        ask = '<d:sync-collection xmlns:d="DAV:"><d:sync-token>{}</d:sync-token><d:prop><d:getetag/></d:prop></d:sync-collection>'
        token = self.xml(self.call("REPORT", "/dav/home/contacts/", ask.format(""))).findtext("d:sync-token", namespaces=NS)
        time.sleep(0.005)
        current = contacts.get_current(OWNER, made["id"])
        contacts.save_contact(OWNER, made["id"], {**contacts.public_contact(current), "group_ids": []})
        root = self.xml(self.call("REPORT", "/dav/home/contacts/", ask.format(token)))
        self.assertEqual([node.text for node in root.findall("d:response/d:href", NS)], ["/dav/home/contacts/G-1.vcf"])
        self.assertEqual(contacts.list_groups(OWNER)[0]["members"], [anna["id"]])
        token = root.findtext("d:sync-token", namespaces=NS)
        time.sleep(0.005)
        self.assertEqual(self.call("DELETE", "/dav/home/contacts/G-1.vcf")["statusCode"], 204)
        root = self.xml(self.call("REPORT", "/dav/home/contacts/", ask.format(token)))
        self.assertEqual(root.findtext("d:response[d:href='/dav/home/contacts/G-1.vcf']/d:status", namespaces=NS), "HTTP/1.1 404 Not Found")
        self.assertEqual(contacts.list_groups(OWNER), [])

    def test_a_client_keeps_related_links(self):
        mother = self.page_contact(first="Мария", last="Петрова")
        son = self.page_contact(first="Пётр", related=[{"label": "мама", "value": "Мария Петрова", "contact": mother["id"]}])
        text = self.call("GET", f"/dav/home/contacts/{son['id']}.vcf")["body"]
        self.assertIn("X-ABRELATEDNAMES:Мария Петрова", text)
        result = self.call("PUT", f"/dav/home/contacts/{son['id']}.vcf", text.replace("FN:", "TITLE:Инженер\r\nFN:"), {"if-match": f'"{son["etag"]}"'})
        self.assertEqual(result["statusCode"], 204, result["body"])
        item = contacts.get_current(OWNER, son["id"])
        self.assertEqual((item["title"], item["related"][0]["contact"]), ("Инженер", mother["id"]))

    def test_a_client_keeps_a_hidden_birthday(self):
        made = self.page_contact(first="Вера", hidden_birthday="--05-04")
        text = self.call("GET", f"/dav/home/contacts/{made['id']}.vcf")["body"]
        self.assertIn("item1.X-ABDATE:1604-05-04\r\nitem1.X-ABLabel:Birthday (not in calendar) - managed by contacts.example.com, do not edit", text.replace("\r\n ", ""))
        self.assertNotIn("BDAY", text.replace("X-ABDATE", ""))
        # A client that keeps it.
        result = self.call("PUT", f"/dav/home/contacts/{made['id']}.vcf", text.replace("FN:", "NOTE:Привет\r\nFN:"), {"if-match": f'"{made["etag"]}"'})
        self.assertEqual(result["statusCode"], 204, result["body"])
        item = contacts.get_current(OWNER, made["id"])
        self.assertEqual((item["hidden_birthday"], item.get("dates") or []), ("--05-04", []))
        # A client that drops it.
        text = self.call("GET", f"/dav/home/contacts/{made['id']}.vcf")["body"]
        edited = "\r\n".join(line for line in text.split("\r\n") if not line.startswith("item1."))
        made = item
        result = self.call("PUT", f"/dav/home/contacts/{made['id']}.vcf", edited.replace("FN:", "TITLE:Врач\r\nFN:"), {"if-match": f'"{made["etag"]}"'})
        self.assertEqual(result["statusCode"], 204, result["body"])
        item = contacts.get_current(OWNER, made["id"])
        self.assertEqual((item["title"], item["hidden_birthday"], item.get("birthday")), ("Врач", "--05-04", None))

    def test_multiget(self):
        made = self.page_contact()
        body = (f'<card:addressbook-multiget xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav"><d:prop><d:getetag/><card:address-data/></d:prop>'
                f'<d:href>/dav/home/contacts/{made["id"]}.vcf</d:href><d:href>/dav/home/contacts/none.vcf</d:href></card:addressbook-multiget>')
        root = self.xml(self.call("REPORT", "/dav/home/contacts/", body))
        data = root.findtext(".//card:address-data", namespaces=NS)
        self.assertIn("FN:Борис Смирнов", data)
        self.assertEqual(root.findtext("d:response[d:href='/dav/home/contacts/none.vcf']/d:status", namespaces=NS), "HTTP/1.1 404 Not Found")

    def test_sync_collection(self):
        first = self.page_contact()
        ask = '<d:sync-collection xmlns:d="DAV:"><d:sync-token>{}</d:sync-token><d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>'
        root = self.xml(self.call("REPORT", "/dav/home/contacts/", ask.format("")))
        self.assertEqual([node.text for node in root.findall("d:response/d:href", NS)], [f"/dav/home/contacts/{first['id']}.vcf"])
        token = root.findtext("d:sync-token", namespaces=NS)
        time.sleep(0.005)
        root = self.xml(self.call("REPORT", "/dav/home/contacts/", ask.format(token)))
        self.assertEqual(root.findall("d:response", NS), [])
        second = self.page_contact(first="Вера")
        time.sleep(0.005)
        contacts.delete_contact(OWNER, first["id"], first["etag"])
        root = self.xml(self.call("REPORT", "/dav/home/contacts/", ask.format(token)))
        self.assertEqual(root.findtext(f"d:response[d:href='/dav/home/contacts/{first['id']}.vcf']/d:status", namespaces=NS), "HTTP/1.1 404 Not Found")
        self.assertIsNotNone(root.find(f"d:response[d:href='/dav/home/contacts/{second['id']}.vcf']//d:getetag", NS))
        self.assertNotEqual(root.findtext("d:sync-token", namespaces=NS), token)
        result = self.call("REPORT", "/dav/home/contacts/", ask.format("bad"))
        self.assertEqual(result["statusCode"], 403)
        self.assertIn("valid-sync-token", result["body"])

    def test_recent_changes_come_again(self):
        carddav.SETTLE_SECONDS = 60
        made = self.page_contact()
        ask = '<d:sync-collection xmlns:d="DAV:"><d:sync-token>{}</d:sync-token><d:prop><d:getetag/></d:prop></d:sync-collection>'
        token = self.xml(self.call("REPORT", "/dav/home/contacts/", ask.format(""))).findtext("d:sync-token", namespaces=NS)
        self.assertLess(token[len(carddav.sync_prefix()):], made["modified"])
        root = self.xml(self.call("REPORT", "/dav/home/contacts/", ask.format(token)))
        self.assertEqual(len(root.findall("d:response", NS)), 1)

    def test_large_answers_are_cut(self):
        jpeg = b"\xff\xd8\xff\xe0" + os.urandom(900_000)
        names = []
        for number in range(8):
            self.s3.objects[f"photos/{contacts.user_key(OWNER)}/{number:032x}.jpg"] = (jpeg, "image/jpeg")
            made = self.page_contact(first=f"Имя{number}", photo=f"/photos/{contacts.user_key(OWNER)}/{number:032x}.jpg")
            names.append(f"/dav/home/contacts/{made['id']}.vcf")
        body = '<card:addressbook-multiget xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav"><d:prop><card:address-data/></d:prop>' + "".join(f"<d:href>{name}</d:href>" for name in names) + "</card:addressbook-multiget>"
        result = self.call("REPORT", "/dav/home/contacts/", body)
        self.assertLess(len(result["body"]), 6_000_000)
        self.assertIn("507 Insufficient Storage", result["body"])


if __name__ == "__main__":
    unittest.main()
