import json
import os
import unittest
import urllib.parse

import contacts
import contacts_test as fakes
import google_sync

OWNER = "owner@example.com"


class FakePeople:
    """People API in memory: contacts, groups and sync tokens."""

    def __init__(self):
        self.people = {}
        self.groups = {}
        self.version = 0
        self.changed = {}
        self.deleted = {}
        self.photos = {}
        self.counter = 0
        self.calls = []

    def bump(self, resource):
        self.version += 1
        self.changed[resource] = self.version

    def store(self, person, resource=None):
        if not resource:
            self.counter += 1
            resource = f"people/c{self.counter}"
        person = json.loads(json.dumps(person))
        person["resourceName"] = resource
        person["etag"] = f"e{self.version + 1}"
        if resource in self.photos:
            person["photos"] = [{"url": self.photos[resource]}]
        self.people[resource] = person
        self.bump(resource)
        return person

    def add_group(self, name):
        self.counter += 1
        resource = f"contactGroups/g{self.counter}"
        self.groups[resource] = {"resourceName": resource, "name": name, "groupType": "USER_CONTACT_GROUP", "etag": "g"}
        return resource

    def request(self, method, url, token=None, body=None, params=None, raw=False):
        if raw:
            return b"\xff\xd8\xff\xe0google-photo"
        path = url.replace(google_sync.PEOPLE, "")
        params = params or {}
        self.calls.append((method, path))
        if path == "contactGroups" and method == "GET":
            return {"contactGroups": list(self.groups.values()) + [{"resourceName": "contactGroups/myContacts", "groupType": "SYSTEM_CONTACT_GROUP"}]}
        if path == "contactGroups" and method == "POST":
            resource = self.add_group(body["contactGroup"]["name"])
            return self.groups[resource]
        if path.startswith("contactGroups/") and method == "PUT":
            self.groups[path]["name"] = body["contactGroup"]["name"]
            return self.groups[path]
        if path.startswith("contactGroups/") and method == "DELETE":
            del self.groups[path]
            return {}
        if path == "people/me/connections":
            if "syncToken" in params:
                since = int(params["syncToken"])
                items = [self.people[r] for r, v in self.changed.items() if v > since and r in self.people]
                items += [{"resourceName": r, "metadata": {"deleted": True}} for r, v in self.deleted.items() if v > since]
            else:
                items = list(self.people.values())
            return {"connections": json.loads(json.dumps(items)), "nextSyncToken": str(self.version)}
        if path == "people:batchCreateContacts":
            created = [{"person": self.store(entry["contactPerson"])} for entry in body["contacts"]]
            return {"createdPeople": created}
        if path == "people:batchUpdateContacts":
            result = {}
            for resource, person in body["contacts"].items():
                if person.get("etag") != self.people[resource]["etag"]:
                    raise google_sync.GoogleError(400, {"error": {"status": "FAILED_PRECONDITION"}})
                result[resource] = {"person": self.store(person, resource)}
            return {"updateResult": result}
        if path == "people:batchDeleteContacts":
            for resource in body["resourceNames"]:
                self.remove(resource)
            return {}
        if path.endswith(":updateContactPhoto"):
            resource = path.split(":")[0]
            self.photos[resource] = "https://lh3.googleusercontent.com/contacts/x=s100"
            return {"person": self.store(self.people[resource], resource)}
        if path.endswith(":deleteContactPhoto"):
            resource = path.split(":")[0]
            self.photos.pop(resource, None)
            self.people[resource].pop("photos", None)
            return {"person": self.store(self.people[resource], resource)}
        raise AssertionError((method, path))

    def remove(self, resource):
        self.people.pop(resource, None)
        self.version += 1
        self.deleted[resource] = self.version

    def edit(self, resource, change):
        person = self.people[resource]
        change(person)
        self.store(person, resource)


class FakeSsm:
    def __init__(self):
        self.values = {"/secret": "client-secret"}

    def get_parameter(self, Name, WithDecryption=False):
        if Name not in self.values:
            raise fakes.ClientError("ParameterNotFound")
        return {"Parameter": {"Value": self.values[Name]}}

    def put_parameter(self, Name, Value, Type, Overwrite):
        self.values[Name] = Value

    def delete_parameter(self, Name):
        self.values.pop(Name, None)


class LockingTable(fakes.FakeTable):
    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues):
        if UpdateExpression == "SET used = :u":
            return super().update_item(Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues)
        item = self.items.get((Key["pk"], Key["sk"]))
        if item is None or int(item.get("lock_until") or 0) >= ExpressionAttributeValues[":now"]:
            raise fakes.ConditionFailed()
        item["lock_until"] = ExpressionAttributeValues[":until"]


class GoogleSyncTest(unittest.TestCase):
    def setUp(self):
        self.table = LockingTable()
        self.s3 = fakes.FakeS3()
        self.people = FakePeople()
        self.ssm = FakeSsm()
        contacts._table = self.table
        contacts._dynamodb = fakes.FakeDynamo(self.table)
        contacts._s3 = self.s3
        self.ses = fakes.FakeSes()
        contacts._ses = self.ses
        self.locale = {}
        self.cognito = fakes.FakeCognito()
        self.cognito.admin_get_user = lambda UserPoolId, Username: {
            "Username": Username, "UserAttributes": [{"Name": "locale", "Value": self.locale[Username]}] if Username in self.locale else []}
        contacts._cognito = self.cognito
        google_sync._ssm = self.ssm
        google_sync._secret = None
        google_sync.request = self.people.request
        google_sync.access_token = lambda owner: "token"
        os.environ.update({"TABLE_NAME": "contacts", "PHOTOS_BUCKET": "photos", "GOOGLE_CLIENT_ID": "client",
                           "GOOGLE_SECRET_PARAMETER": "/secret", "SITE_URL": "https://contacts.example.com",
                           "UID_DOMAIN": "contacts.example.com", "GOOGLE_TOKEN_PREFIX": "/contacts/google",
                           "SENDER": "contacts@example.com", "SYNC_ALERT_FAILURES": "3", "USER_POOL_ID": "pool"})
        os.environ.pop("GOOGLE_SYNC_FUNCTION", None)
        google_sync.save_state(OWNER, {"connected": True, "email": "me@gmail.com"})
        self.table.put_item(Item={"pk": "GOOGLE", "sk": OWNER})

    def sync(self):
        result = google_sync.sync_user(OWNER)
        self.assertIsInstance(result, dict, result)
        return result

    def cards(self):
        return {item["first"]: contacts.plain(item) for item in contacts.all_current(OWNER) if item.get("first")}

    def log(self):
        return contacts.sync_log(OWNER)["items"]

    def test_mapping_round_trip(self):
        card = {
            "prefix": "Др.", "first": "Анна", "middle": "С.", "last": "Петрова", "nickname": "Аня", "company": "Ромашка",
            "title": "Менеджер", "phones": [{"label": "mobile", "value": "+7 916"}, {"label": "iphone", "value": "1"}, {"label": "Болгария", "value": "2"}],
            "emails": [{"label": "icloud", "value": "a@icloud.com"}], "addresses": [{"label": "home", "street": "Ленина, 1", "city": "Москва", "country_code": "ru"}],
            "urls": [{"label": "homepage", "value": "https://a"}], "birthday": "--03-08", "dates": [{"label": "anniversary", "value": "2010-06-01"}],
            "related": [{"label": "мама", "value": "Мария", "contact": "a" * 16}], "im": [{"service": "Skype", "label": "home", "value": "anna"},
                                                                                 {"service": "Telegram", "label": "other", "value": "@anna"}, {"service": "SIP", "label": "work", "value": "sip:1"}],
            "custom": [{"label": "Код", "value": "42"}], "notes": "Заметка", "starred": True,
            # Kept here only.
            "maiden": "Сидорова", "pronouns": "she", "social": [{"service": "VK", "user": "anna"}],
        }
        person = google_sync.contact_to_person(card, ["contactGroups/g1"])
        back, groups = google_sync.person_to_contact(person)
        self.assertEqual(groups, ["contactGroups/g1"])
        self.assertEqual(google_sync.canon(back, ["Семья"]), google_sync.canon(card, ["Семья"]))
        self.assertNotIn("maiden", back)
        self.assertEqual(person["phoneNumbers"][1]["type"], "iPhone")
        self.assertEqual(person["birthdays"], [{"date": {"month": 3, "day": 8}}])

    def test_first_sync_matches_and_uploads(self):
        made = contacts.create_contact(OWNER, {"first": "Анна", "last": "Петрова", "phones": [{"label": "mobile", "value": "+7 916 000-00-01"}],
                                               "maiden": "Сидорова"})
        contacts.create_contact(OWNER, {"first": "Борис"})
        # Анна is in Google already, with an email the book lacks; Вера is there only.
        self.people.store({"names": [{"givenName": "Анна", "familyName": "Петрова"}],
                           "phoneNumbers": [{"value": "8 916 000-00-01", "type": "mobile"}],
                           "emailAddresses": [{"value": "anna@example.com", "type": "work"}]})
        self.people.store({"names": [{"givenName": "Вера"}]})
        self.sync()
        cards = self.cards()
        self.assertEqual(set(cards), {"Анна", "Борис", "Вера"})
        self.assertEqual(cards["Анна"]["id"], made["id"])
        self.assertEqual(cards["Анна"]["emails"], [{"label": "work", "value": "anna@example.com"}])
        self.assertEqual(cards["Анна"]["maiden"], "Сидорова")
        self.assertEqual(len(self.people.people), 3)
        anna = next(p for p in self.people.people.values() if p["names"][0]["givenName"] == "Анна")
        self.assertEqual([e["value"] for e in anna["phoneNumbers"]], ["+7 916 000-00-01"])
        actions = {(line["source"], line["action"]) for line in self.log()}
        self.assertTrue({("google", "matched"), ("google", "created"), ("to_google", "created")} <= actions, actions)
        # Nothing more to do the second time.
        calls = len(self.people.calls)
        result = self.sync()
        self.assertEqual((result["to_book"], result["to_google"]), (0, 0))
        self.assertFalse([c for c in self.people.calls[calls:] if "batch" in c[1]])

    def test_cards_with_only_an_email_are_matched(self):
        made = contacts.create_contact(OWNER, {"emails": [{"label": "home", "value": "Solo@Example.com"}]})
        self.people.store({"emailAddresses": [{"value": "solo@example.com", "type": "home"}]})
        # Another nameless contact with a different email is someone else.
        self.people.store({"emailAddresses": [{"value": "other@example.com", "type": "home"}]})
        self.sync()
        items = [contacts.plain(item) for item in contacts.all_current(OWNER)]
        self.assertEqual(len(items), 2)
        self.assertIn(made["id"], [item["id"] for item in items])
        self.assertEqual(len(self.people.people), 2)
        self.assertIn("matched", [line["action"] for line in self.log()])

    def test_changes_both_ways_and_conflicts(self):
        made = contacts.create_contact(OWNER, {"first": "Анна"})
        self.sync()
        resource = next(iter(self.people.people))
        # Google -> book.
        self.people.edit(resource, lambda p: p.update(organizations=[{"name": "Ромашка"}]))
        self.sync()
        self.assertEqual(self.cards()["Анна"]["company"], "Ромашка")
        line = next(line for line in self.log() if line["action"] == "changed" and line["source"] == "google")
        self.assertEqual(line["fields"], ["company"])
        revisions = contacts.revisions(OWNER, made["id"])
        self.assertEqual(revisions[0]["action"], "google")
        # Book -> Google.
        card = contacts.get_current(OWNER, made["id"])
        contacts.save_contact(OWNER, made["id"], {**contacts.public_contact(card), "title": "Инженер"})
        self.sync()
        self.assertEqual(self.people.people[resource]["organizations"][0].get("title"), "Инженер")
        # Both sides: the book wins and goes to Google.
        card = contacts.get_current(OWNER, made["id"])
        contacts.save_contact(OWNER, made["id"], {**contacts.public_contact(card), "company": "Лютик"})
        self.people.edit(resource, lambda p: p["organizations"][0].update(name="Василёк"))
        result = self.sync()
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(self.cards()["Анна"]["company"], "Лютик")
        self.assertEqual(self.people.people[resource]["organizations"][0]["name"], "Лютик")
        conflict = next(line for line in self.log() if line["action"] == "conflict")
        self.assertIn("company", conflict["fields"])

    def test_hidden_birthday_leaves_google(self):
        made = contacts.create_contact(OWNER, {"first": "Анна", "birthday": "1990-05-04"})
        self.sync()
        resource = next(iter(self.people.people))
        self.assertEqual(self.people.people[resource]["birthdays"], [{"date": {"year": 1990, "month": 5, "day": 4}}])
        card = contacts.get_current(OWNER, made["id"])
        body = {key: value for key, value in contacts.public_contact(card).items() if key != "birthday"}
        contacts.save_contact(OWNER, made["id"], {**body, "hidden_birthday": "1990-05-04"})
        self.sync()
        self.assertFalse(self.people.people[resource].get("birthdays"))
        self.assertEqual(self.people.people[resource]["userDefined"], [{"key": "Birthday (not in calendar) - managed by contacts.example.com, do not edit", "value": "1990-05-04"}])
        # Google's next change keeps the date here.
        self.people.edit(resource, lambda p: p.update(organizations=[{"name": "Ромашка"}]))
        self.sync()
        card = self.cards()["Анна"]
        self.assertEqual((card["company"], card["hidden_birthday"], card.get("birthday"), card.get("custom") or []), ("Ромашка", "1990-05-04", None, []))

    def test_deletions_both_ways(self):
        one = contacts.create_contact(OWNER, {"first": "Анна"})
        contacts.create_contact(OWNER, {"first": "Борис"})
        self.sync()
        anna = next(r for r, p in self.people.people.items() if p["names"][0]["givenName"] == "Анна")
        self.people.remove(anna)
        self.sync()
        self.assertEqual(set(self.cards()), {"Борис"})
        self.assertEqual([item["id"] for item in contacts.deleted_contacts(OWNER)], [one["id"]])
        boris = contacts.get_current(OWNER, self.cards()["Борис"]["id"])
        contacts.delete_contact(OWNER, boris["id"], boris["etag"])
        self.sync()
        self.assertEqual(self.people.people, {})
        self.assertIn(("to_google", "deleted"), {(line["source"], line["action"]) for line in self.log()})

    def test_groups_and_star(self):
        made = contacts.create_contact(OWNER, {"first": "Анна", "starred": True})
        contacts.create_group(OWNER, {"name": "Семья", "members": [made["id"]]})
        self.people.add_group("Работа")
        self.sync()
        person = next(iter(self.people.people.values()))
        names = {m["contactGroupMembership"]["contactGroupResourceName"] for m in person["memberships"]}
        family = next(r for r, g in self.people.groups.items() if g["name"] == "Семья")
        self.assertEqual(names, {google_sync.MY_CONTACTS, google_sync.STARRED, family})
        self.assertEqual({g["name"] for g in contacts.list_groups(OWNER)}, {"Семья", "Работа"})
        # Google renames a group and adds Анна to "Работа".
        work = next(r for r, g in self.people.groups.items() if g["name"] == "Работа")
        self.people.groups[work]["name"] = "Офис"
        resource = person["resourceName"]
        self.people.edit(resource, lambda p: p["memberships"].append({"contactGroupMembership": {"contactGroupResourceName": work}}))
        self.sync()
        groups = {g["name"]: g["members"] for g in contacts.list_groups(OWNER)}
        self.assertEqual(groups, {"Семья": [made["id"]], "Офис": [made["id"]]})

    def test_photos_both_ways(self):
        jpeg = b"\xff\xd8\xff\xe0mine"
        path = f"/photos/{contacts.user_key(OWNER)}/{'b' * 32}.jpg"
        self.s3.objects[path.lstrip("/")] = (jpeg, "image/jpeg")
        contacts.create_contact(OWNER, {"first": "Анна", "photo": path})
        self.people.store({"names": [{"givenName": "Вера"}]})
        resource_vera = next(iter(self.people.people))
        self.people.photos[resource_vera] = "https://lh3.googleusercontent.com/contacts/v=s100"
        self.people.store(self.people.people[resource_vera], resource_vera)
        self.sync()
        anna = next(r for r, p in self.people.people.items() if p["names"][0]["givenName"] == "Анна")
        self.assertIn(anna, self.people.photos)
        self.assertTrue(self.cards()["Вера"]["photo"].startswith("/photos/"))

    def test_lock_and_states(self):
        google_sync.save_state(OWNER, {"lock_until": 2_000_000_000})
        self.assertEqual(google_sync.sync_user(OWNER), "busy")
        google_sync.save_state(OWNER, {"lock_until": 0})
        self.assertIsInstance(google_sync.sync_user(OWNER), dict)
        self.assertEqual(google_sync.public_state(OWNER)["status"], "ok")

    def test_error_alerts(self):
        def fail(*args, **kwargs):
            raise google_sync.GoogleError(503, {"error": {"status": "UNAVAILABLE"}})
        working = google_sync.request
        google_sync.request = fail
        for _ in range(2):
            self.assertEqual(google_sync.sync_user(OWNER), "error")
        self.assertEqual(self.ses.sent, [])
        for _ in range(2):
            self.assertEqual(google_sync.sync_user(OWNER), "error")
        self.assertEqual(len(self.ses.sent), 1)
        # No language picked on the page: English.
        self.assertEqual((self.ses.sent[0]["to"], self.ses.sent[0]["from"]), ([OWNER], "Contacts <contacts@example.com>"))
        self.assertEqual(self.ses.sent[0]["subject"], "The sync with Google does not work")
        self.assertIn("UNAVAILABLE", self.ses.sent[0]["text"])
        google_sync.request = working
        self.sync()
        self.assertEqual(google_sync.state_item(OWNER)["failures"], 0)
        # The sign-in is gone: a mail at once, and only one, in the language
        # of the page.
        self.locale[OWNER] = "ru"
        google_sync.request = lambda *args, **kwargs: (_ for _ in ()).throw(google_sync.GoogleError(400, {"error": "invalid_grant"}))
        google_sync.sync_user(OWNER)
        google_sync.sync_user(OWNER)
        self.assertEqual(google_sync.public_state(OWNER)["status"], "reauth")
        self.assertEqual(len(self.ses.sent), 2)
        self.assertEqual(self.ses.sent[1]["subject"], "Синхронизация с Google не работает")
        self.assertIn("заново", self.ses.sent[1]["text"])

    def test_alert_language(self):
        for locale, subject in (("fr", "La synchronisation avec Google ne fonctionne pas"), ("it-IT", "La sincronizzazione con Google non funziona"),
                                ("de", "The sync with Google does not work")):
            self.locale[OWNER] = locale
            contacts.send_sync_alert(OWNER, "google", "error", "500")
            self.assertEqual(self.ses.sent[-1]["subject"], subject)
        def fail(UserPoolId, Username):
            raise fakes.ClientError("UserNotFoundException")
        self.cognito.admin_get_user = fail
        contacts.send_sync_alert(OWNER, "google", "reauth", "401")
        self.assertEqual(self.ses.sent[-1]["subject"], "The sync with Google does not work")

    def test_alert_not_sent_is_tried_again(self):
        def refuse(**kwargs):
            raise fakes.ClientError("MessageRejected")
        self.ses.send_email = refuse
        google_sync.request = lambda *args, **kwargs: (_ for _ in ()).throw(google_sync.GoogleError(400, {"error": "invalid_grant"}))
        google_sync.sync_user(OWNER)
        self.assertFalse(google_sync.state_item(OWNER).get("alerted"))
        del self.ses.send_email
        google_sync.sync_user(OWNER)
        self.assertEqual(len(self.ses.sent), 1)

    def test_sign_in_flow(self):
        start = google_sync.start(OWNER)["url"]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(start).query)
        self.assertEqual(query["redirect_uri"], ["https://contacts.example.com/api/oauth/google"])
        self.assertEqual(query["access_type"], ["offline"])
        state = query["state"][0]
        payload = urllib.parse.quote(json.dumps({"email": "me@gmail.com"}))
        token = "x." + __import__("base64").urlsafe_b64encode(json.dumps({"email": "me@gmail.com"}).encode()).decode().rstrip("=") + ".y"
        google_sync.post_form = lambda url, fields: {"refresh_token": "r1", "scope": google_sync.SCOPES, "id_token": token}
        answer = google_sync.callback({"state": state, "code": "c"})
        self.assertEqual(answer["headers"]["Location"], "https://contacts.example.com/#/google/connected")
        self.assertEqual(self.ssm.values[google_sync.token_parameter(OWNER)], "r1")
        self.assertEqual(google_sync.callback({"state": state, "code": "c"})["headers"]["Location"].rsplit("/", 1)[1], "expired")
        self.assertTrue(payload)
        google_sync.disconnect(OWNER)
        self.assertNotIn(google_sync.token_parameter(OWNER), self.ssm.values)
        self.assertFalse(google_sync.public_state(OWNER)["connected"])

    def test_api_routes(self):
        result = contacts.handler(fakes.event("GET", "/api/google"), None)
        self.assertEqual(json.loads(result["body"])["connected"], True)
        result = contacts.handler({"rawPath": "/api/oauth/google", "requestContext": {"http": {"method": "GET"}}, "queryStringParameters": {"state": "nope"}}, None)
        self.assertEqual(result["statusCode"], 302)
        result = contacts.handler(fakes.event("GET", "/api/sync-log"), None)
        self.assertEqual(result["statusCode"], 200)


if __name__ == "__main__":
    unittest.main()
