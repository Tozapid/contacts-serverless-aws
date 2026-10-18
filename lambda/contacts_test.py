import base64
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone

from boto3.dynamodb.types import TypeDeserializer

import contacts
import vcard

OWNER = "owner@example.com"
OTHER = "other@example.com"


def key_match(condition, item):
    expr = condition.get_expression()
    op, values = expr["operator"], expr["values"]
    if op == "AND":
        return all(key_match(value, item) for value in values)
    name, value = values[0].name, values[1]
    if op == "=":
        return item.get(name) == value
    if op == "begins_with":
        return str(item.get(name, "")).startswith(value)
    raise AssertionError(op)


class ConditionFailed(Exception):
    def __init__(self, transaction=False):
        super().__init__("condition")
        if transaction:
            self.response = {"Error": {"Code": "TransactionCanceledException"}, "CancellationReasons": [{"Code": "ConditionalCheckFailed"}]}
        else:
            self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key, ConsistentRead=False):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def delete_item(self, Key):
        self.items.pop((Key["pk"], Key["sk"]), None)

    def query(self, **kwargs):
        items = [dict(item) for item in self.items.values() if key_match(kwargs["KeyConditionExpression"], item)]
        items.sort(key=lambda item: item["sk"], reverse=not kwargs.get("ScanIndexForward", True))
        if kwargs.get("Select") == "COUNT":
            return {"Count": len(items)}
        return {"Items": items[:kwargs.get("Limit", len(items))]}

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues):
        item = self.items.get((Key["pk"], Key["sk"]))
        if item is None:
            raise ConditionFailed()
        assert UpdateExpression == "SET used = :u"
        item["used"] = ExpressionAttributeValues[":u"]

    def batch_writer(self):
        table = self

        class Batch:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def delete_item(self, Key):
                table.delete_item(Key)

        return Batch()


class FakeDynamo:
    """transact_write_items over the fake table, with the conditions the API uses."""

    def __init__(self, fake_table):
        self.table = fake_table
        self.deserializer = TypeDeserializer()

    def plain(self, data):
        return {key: self.deserializer.deserialize(value) for key, value in data.items()}

    def check(self, key, action):
        condition = action.get("ConditionExpression")
        current = self.table.items.get(key)
        if condition == "attribute_not_exists(pk)":
            return current is None
        if condition == "etag = :etag":
            return current is not None and current.get("etag") == self.plain(action["ExpressionAttributeValues"])[":etag"]
        assert condition is None, condition
        return True

    def transact_write_items(self, TransactItems):
        writes = []
        for entry in TransactItems:
            kind, action = next(iter(entry.items()))
            data = self.plain(action["Item"] if kind == "Put" else action["Key"])
            key = (data["pk"], data["sk"])
            if not self.check(key, action):
                raise ConditionFailed(transaction=True)
            writes.append((kind, key, data))
        for kind, key, data in writes:
            if kind == "Put":
                self.table.items[key] = data
            else:
                self.table.items.pop(key, None)


class Body:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = (Body, ContentType)

    def get_object(self, Bucket, Key):
        data, content_type = self.objects[Key]
        return {"Body": Body(data), "ContentType": content_type}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def delete_objects(self, Bucket, Delete):
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)
        return {}

    def get_paginator(self, name):
        return Pager(lambda Bucket, Prefix: {"Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]})

    def generate_presigned_post(self, Bucket, Key, Fields, Conditions, ExpiresIn):
        return {"url": f"https://{Bucket}.s3.amazonaws.com/", "fields": {"key": Key, **Fields}}

    def generate_presigned_url(self, method, Params, ExpiresIn):
        return f"https://{Params['Bucket']}.s3.amazonaws.com/{Params['Key']}?signed"


class FakeSes:
    def __init__(self):
        self.sent = []

    def send_email(self, FromEmailAddress, Destination, Content):
        self.sent.append({"from": FromEmailAddress, "to": Destination["ToAddresses"],
                          "subject": Content["Simple"]["Subject"]["Data"], "text": Content["Simple"]["Body"]["Text"]["Data"]})
        return {"MessageId": str(len(self.sent))}


class Pager:
    def __init__(self, fn):
        self.fn = fn

    def paginate(self, **kwargs):
        return [self.fn(**kwargs)]


class FakeCognito:
    def __init__(self):
        self.users = {}
        self.groups = {"admins": set()}

    def get_paginator(self, name):
        return Pager(getattr(self, name))

    def list_users(self, UserPoolId):
        return {"Users": [dict(user, Attributes=[{"Name": "email", "Value": email}]) for email, user in self.users.items()]}

    def list_users_in_group(self, UserPoolId, GroupName):
        return {"Users": [{"Username": email, "Attributes": [{"Name": "email", "Value": email}]} for email in self.groups[GroupName]]}

    def admin_create_user(self, UserPoolId, Username, **kwargs):
        if Username in self.users:
            raise ClientError("UsernameExistsException")
        self.users[Username] = {"Username": Username, "UserStatus": "FORCE_CHANGE_PASSWORD", "Enabled": True,
                                "UserCreateDate": datetime.now(timezone.utc)}

    def admin_delete_user(self, UserPoolId, Username):
        self.users.pop(Username)

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.groups[GroupName].add(Username)

    def admin_get_user(self, UserPoolId, Username):
        return {"Username": Username, "UserMFASettingList": []}


def event(method, path, body=None, params=None, email=OWNER, admin=False):
    claims = {"email": email, "email_verified": "true"}
    if admin:
        claims["cognito:groups"] = "[admins]"
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
        "queryStringParameters": params,
        "headers": {},
        "body": json.dumps(body) if body is not None else None,
    }


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        self.s3 = FakeS3()
        self.cognito = FakeCognito()
        contacts._table = self.table
        contacts._dynamodb = FakeDynamo(self.table)
        contacts._s3 = self.s3
        contacts._cognito = self.cognito
        os.environ.update({
            "TABLE_NAME": "contacts", "PHOTOS_BUCKET": "photos", "USER_POOL_ID": "pool",
            "CLOUDFRONT_KEY_PAIR_ID": "K1", "PHOTOS_KEY_PARAMETER_NAME": "/key", "UID_DOMAIN": "contacts.example.com",
        })

    def call(self, method, path, body=None, params=None, **kwargs):
        result = contacts.handler(event(method, path, body, params, **kwargs), None)
        return result["statusCode"], json.loads(result["body"])

    def create(self, **fields):
        status, data = self.call("POST", "/api/contacts", {"first": "Анна", "last": "Петрова", **fields})
        self.assertEqual(status, 201, data)
        return data

    def test_create_lists_and_reads(self):
        made = self.create(phones=[{"label": "mobile", "value": "+7 916 123-45-67"}], labels=["Семья"])
        status, data = self.call("GET", "/api/contacts")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in data["items"]], [made["id"]])
        self.assertNotIn("labels", made)
        self.assertEqual(made["emails"], [])
        status, one = self.call("GET", f"/api/contacts/{made['id']}")
        self.assertEqual(one["phones"], [{"label": "mobile", "value": "+7 916 123-45-67"}])

    def test_every_user_has_their_own_book(self):
        made = self.create()
        status, data = self.call("GET", "/api/contacts", email=OTHER)
        self.assertEqual(data["items"], [])
        status, _ = self.call("GET", f"/api/contacts/{made['id']}", email=OTHER)
        self.assertEqual(status, 404)

    def test_cursor_of_another_user_is_refused(self):
        cursor = contacts.encode_cursor({"pk": "USER#" + OTHER, "sk": "CONTACT#0"})
        status, data = self.call("GET", "/api/contacts", params={"cursor": cursor})
        self.assertEqual((status, data["code"]), (400, "bad_cursor"))

    def test_empty_card_and_bad_fields_are_refused(self):
        status, data = self.call("POST", "/api/contacts", {"notes": "только заметка"})
        self.assertEqual((status, data["code"]), (400, "empty_contact"))
        status, data = self.call("POST", "/api/contacts", {"first": "A", "birthday": "1990-13-01"})
        self.assertEqual((status, data["code"], data["detail"]), (400, "bad_date", "birthday"))
        status, data = self.call("POST", "/api/contacts", {"first": "A", "phones": "123"})
        self.assertEqual((status, data["detail"]), (400, "phones"))
        status, data = self.call("POST", "/api/contacts", {"first": "A", "photo": "/photos/" + "0" * 20 + "/" + "a" * 32 + ".jpg"})
        self.assertEqual(data["code"], "bad_photo")

    def test_birthday_without_year(self):
        made = self.create(birthday="--03-08", dates=[{"label": "anniversary", "value": "2010-06-01"}])
        self.assertEqual(made["birthday"], "--03-08")

    def test_hidden_birthday(self):
        made = self.create(hidden_birthday="1990-05-04")
        self.assertEqual((made["hidden_birthday"], made.get("birthday")), ("1990-05-04", None))
        card = vcard.contact_to_card(made, None, [])
        self.assertIn("item1.X-ABDATE:1990-05-04\r\nitem1.X-ABLabel:Birthday (not in calendar) - managed by contacts.example.com, do not edit", card.replace("\r\n ", ""))
        self.assertNotIn("BDAY", card.replace("X-ABDATE", ""))
        back, _ = vcard.card_to_contact(vcard.split_cards(card)[0])
        self.assertEqual((back["hidden_birthday"], back.get("birthday", "")), ("1990-05-04", ""))
        # A birthday given again takes the card back into the calendars.
        status, saved = self.call("PUT", f"/api/contacts/{made['id']}", {**made, "birthday": "1990-05-04"})
        self.assertEqual((status, saved["birthday"], saved.get("hidden_birthday")), (200, "1990-05-04", None))
        status, data = self.call("POST", "/api/contacts", {"first": "A", "hidden_birthday": "1990-13-01"})
        self.assertEqual((status, data["detail"]), (400, "hidden_birthday"))

    def test_empty_rows_are_dropped(self):
        made = self.create(phones=[{"label": "home", "value": " "}], addresses=[{"label": "home", "country_code": "ru"}])
        self.assertEqual((made["phones"], made["addresses"]), ([], []))

    def test_save_keeps_history_and_checks_etag(self):
        made = self.create()
        status, saved = self.call("PUT", f"/api/contacts/{made['id']}", {**made, "company": "ООО Ромашка"})
        self.assertEqual(status, 200)
        self.assertNotEqual(saved["etag"], made["etag"])
        status, data = self.call("PUT", f"/api/contacts/{made['id']}", {**made, "company": "Другое"})
        self.assertEqual((status, data["code"]), (409, "changed"))
        status, data = self.call("GET", f"/api/contacts/{made['id']}/revisions")
        self.assertEqual([item["action"] for item in data["items"]], ["save", "create"])
        self.assertEqual(data["items"][0]["changes"], ["company"])
        self.assertEqual(data["items"][1]["company"] if "company" in data["items"][1] else "", "")

    def test_unchanged_save_writes_no_revision(self):
        made = self.create()
        status, saved = self.call("PUT", f"/api/contacts/{made['id']}", dict(made))
        self.assertEqual(saved["etag"], made["etag"])
        _, data = self.call("GET", f"/api/contacts/{made['id']}/revisions")
        self.assertEqual(len(data["items"]), 1)

    def test_delete_restore_and_purge(self):
        made = self.create()
        status, data = self.call("DELETE", f"/api/contacts/{made['id']}", params={"etag": made["etag"]})
        self.assertEqual(status, 200)
        _, deleted = self.call("GET", "/api/deleted")
        self.assertEqual([(item["id"], item["name"]) for item in deleted["items"]], [(made["id"], "Анна Петрова")])
        _, revs = self.call("GET", f"/api/contacts/{made['id']}/revisions")
        create = revs["items"][-1]
        status, back = self.call("POST", f"/api/contacts/{made['id']}/restore", {"revision": create["revision"]})
        self.assertEqual((status, back["first"], back["created"]), (200, "Анна", made["created"]))
        _, deleted = self.call("GET", "/api/deleted")
        self.assertEqual(deleted["items"], [])
        status, data = self.call("DELETE", f"/api/deleted/{made['id']}")
        self.assertEqual((status, data["code"]), (409, "exists"))
        self.call("DELETE", f"/api/contacts/{made['id']}", params={"etag": back["etag"]})
        status, data = self.call("DELETE", f"/api/deleted/{made['id']}")
        self.assertEqual((status, data["versions"]), (200, 4))
        self.assertEqual([key for key in self.table.items if made["id"] in key[1]], [])

    def test_restore_over_current_card(self):
        made = self.create(company="Старая")
        _, saved = self.call("PUT", f"/api/contacts/{made['id']}", {**made, "company": "Новая"})
        _, revs = self.call("GET", f"/api/contacts/{made['id']}/revisions")
        status, data = self.call("POST", f"/api/contacts/{made['id']}/restore", {"revision": revs["items"][-1]["revision"], "etag": made["etag"]})
        self.assertEqual((status, data["code"]), (409, "changed"))
        status, back = self.call("POST", f"/api/contacts/{made['id']}/restore", {"revision": revs["items"][-1]["revision"], "etag": saved["etag"]})
        self.assertEqual((status, back["company"]), (200, "Старая"))
        _, revs = self.call("GET", f"/api/contacts/{made['id']}/revisions")
        self.assertEqual(revs["items"][0]["action"], "restore")
        self.assertEqual(revs["items"][0]["from"], revs["items"][-1]["at"])

    def test_restore_of_another_contacts_revision_is_refused(self):
        one, two = self.create(), self.create(first="Борис")
        _, revs = self.call("GET", f"/api/contacts/{one['id']}/revisions")
        status, data = self.call("POST", f"/api/contacts/{two['id']}/restore", {"revision": revs["items"][0]["revision"], "etag": two["etag"]})
        self.assertEqual((status, data["code"]), (400, "revision_not_found"))

    def test_search_in_history_finds_old_and_deleted(self):
        made = self.create(phones=[{"label": "mobile", "value": "8 (916) 123-45-67"}])
        self.call("PUT", f"/api/contacts/{made['id']}", {**made, "phones": [{"label": "mobile", "value": "+7 900 000-00-00"}]})
        gone = self.create(first="Пётр", last="Иванов", notes="Сосед по даче")
        self.call("DELETE", f"/api/contacts/{gone['id']}", params={"etag": gone["etag"]})
        _, data = self.call("GET", "/api/search", params={"q": "+7 916 123"})
        self.assertEqual([(item["id"], item["action"]) for item in data["items"]], [(made["id"], "create")])
        _, data = self.call("GET", "/api/search", params={"q": "петр дача"})
        self.assertEqual(data["items"], [])
        _, data = self.call("GET", "/api/search", params={"q": "петр сосед"})
        self.assertEqual([(item["id"], item["deleted"], item["versions"]) for item in data["items"]], [(gone["id"], True, 2)])
        _, data = self.call("GET", "/api/search", params={"q": "петр"}, email=OTHER)
        self.assertEqual(data["items"], [])

    def test_search_in_the_other_layout(self):
        olga = self.create(first="Ольга", last="Иванова")
        self.call("PUT", f"/api/contacts/{olga['id']}", {**olga, "first": "Оля"})
        maria = self.create(first="Maria", last="Rossi")
        _, data = self.call("GET", "/api/search", params={"q": "Jkmuf"})
        self.assertEqual([item["id"] for item in data["items"]], [olga["id"]])
        _, data = self.call("GET", "/api/search", params={"q": "ьфкшф кщыы"})
        self.assertEqual([item["id"] for item in data["items"]], [maria["id"]])

    def test_drafts_are_kept_and_removed_by_a_save(self):
        made = self.create()
        status, _ = self.call("PUT", f"/api/drafts/{made['id']}", {"draft": {"first": "Анна-Мария", "phones": [{"label": "mobile", "value": ""}]}, "etag": made["etag"]})
        self.assertEqual(status, 200)
        self.call("PUT", "/api/drafts/new", {"draft": {"first": "Нов"}})
        _, data = self.call("GET", "/api/drafts")
        self.assertEqual({item["key"]: item["draft"]["first"] for item in data["items"]}, {made["id"]: "Анна-Мария", "new": "Нов"})
        self.assertEqual(next(item for item in data["items"] if item["key"] == made["id"])["etag"], made["etag"])
        _, data = self.call("GET", "/api/drafts", email=OTHER)
        self.assertEqual(data["items"], [])
        self.call("PUT", f"/api/contacts/{made['id']}", {**made, "first": "Анна-Мария"})
        self.call("POST", "/api/contacts", {"first": "Нов"})
        _, data = self.call("GET", "/api/drafts")
        self.assertEqual(data["items"], [])

    def test_draft_checks(self):
        status, data = self.call("PUT", "/api/drafts/../x", {"draft": {}})
        self.assertEqual(status, 404)
        status, data = self.call("PUT", "/api/drafts/zzz", {"draft": {}})
        self.assertEqual((status, data["code"]), (400, "bad_draft"))
        status, data = self.call("PUT", "/api/drafts/new", {"draft": "text"})
        self.assertEqual(data["code"], "bad_draft")
        status, data = self.call("PUT", "/api/drafts/new", {"draft": {"notes": "x" * 150_000}})
        self.assertEqual((status, data["code"]), (413, "too_large"))
        made = self.create()
        self.call("PUT", f"/api/drafts/{made['id']}", {"draft": {"first": "x"}, "etag": made["etag"]})
        self.call("DELETE", f"/api/contacts/{made['id']}", params={"etag": made["etag"]})
        _, data = self.call("GET", "/api/drafts")
        self.assertEqual(data["items"], [])

    def test_merge_keeps_one_card_and_deletes_the_rest(self):
        keep = self.create(phones=[{"label": "mobile", "value": "+7 916 000-00-01"}])
        other = self.create(first="Аня", emails=[{"label": "work", "value": "anna@example.com"}])
        self.call("PUT", f"/api/drafts/{other['id']}", {"draft": {"first": "x"}, "etag": other["etag"]})
        card = {"first": "Анна", "last": "Петрова", "nickname": "Аня", "phones": keep["phones"], "emails": other["emails"]}
        status, data = self.call("POST", "/api/merge", {"target": {"id": keep["id"], "etag": other["etag"]}, "sources": [{"id": other["id"], "etag": other["etag"]}], "card": card})
        self.assertEqual((status, data["code"]), (409, "changed"))
        status, data = self.call("POST", "/api/merge", {"target": {"id": keep["id"], "etag": keep["etag"]}, "sources": [{"id": keep["id"], "etag": keep["etag"]}], "card": card})
        self.assertEqual((status, data["code"]), (400, "bad_merge"))
        status, data = self.call("POST", "/api/merge", {"target": {"id": keep["id"], "etag": keep["etag"]}, "sources": [{"id": other["id"], "etag": other["etag"]}], "card": card})
        self.assertEqual((status, data["merged"], data["contact"]["emails"]), (200, [other["id"]], other["emails"]))
        _, listed = self.call("GET", "/api/contacts")
        self.assertEqual([item["id"] for item in listed["items"]], [keep["id"]])
        _, deleted = self.call("GET", "/api/deleted")
        self.assertEqual([item["id"] for item in deleted["items"]], [other["id"]])
        _, drafts = self.call("GET", "/api/drafts")
        self.assertEqual(drafts["items"], [])
        _, revs = self.call("GET", f"/api/contacts/{keep['id']}/revisions")
        self.assertEqual(revs["items"][0]["action"], "merge")
        _, revs = self.call("GET", f"/api/contacts/{other['id']}/revisions")
        self.assertEqual(revs["items"][0]["action"], "merged")
        status, back = self.call("POST", f"/api/contacts/{other['id']}/restore", {"revision": revs["items"][1]["revision"]})
        self.assertEqual((status, back["first"]), (200, "Аня"))

    def test_not_duplicates_are_remembered(self):
        one, two = self.create(), self.create()
        status, data = self.call("PUT", "/api/duplicates/ignored", {"ids": [two["id"], one["id"]]})
        self.assertEqual(status, 200)
        _, data = self.call("GET", "/api/duplicates/ignored")
        self.assertEqual(data["items"], [sorted([one["id"], two["id"]])])
        status, data = self.call("PUT", "/api/duplicates/ignored", {"ids": [one["id"], one["id"]]})
        self.assertEqual(status, 400)

    def test_groups_from_the_page(self):
        one, two = self.create(), self.create(first="Борис")
        status, group = self.call("POST", "/api/groups", {"name": "Семья", "members": [one["id"]]})
        self.assertEqual((status, group["members"]), (201, [one["id"]]))
        status, data = self.call("POST", "/api/groups", {"name": "Чужие", "members": ["0" * 16]})
        self.assertEqual((status, data["code"]), (400, "bad_group"))
        status, saved = self.call("PUT", f"/api/contacts/{two['id']}", {**two, "group_ids": [group["id"]]})
        _, data = self.call("GET", "/api/groups")
        self.assertEqual(sorted(data["items"][0]["members"]), sorted([one["id"], two["id"]]))
        status, data = self.call("PUT", f"/api/groups/{group['id']}", {"name": "Родные", "members": [two["id"]], "etag": group["etag"]})
        self.assertEqual((status, data["code"]), (409, "changed"))
        current = self.call("GET", "/api/groups")[1]["items"][0]
        status, data = self.call("PUT", f"/api/groups/{group['id']}", {"name": "Родные", "members": [two["id"]], "etag": current["etag"]})
        self.assertEqual((status, data["name"], data["members"]), (200, "Родные", [two["id"]]))
        # A deleted card stays in the group and is back when restored.
        self.call("DELETE", f"/api/contacts/{two['id']}", params={"etag": saved["etag"]})
        self.assertEqual(self.call("GET", "/api/groups")[1]["items"][0]["members"], [])
        _, revs = self.call("GET", f"/api/contacts/{two['id']}/revisions")
        self.call("POST", f"/api/contacts/{two['id']}/restore", {"revision": revs["items"][1]["revision"]})
        self.assertEqual(self.call("GET", "/api/groups")[1]["items"][0]["members"], [two["id"]])
        status, _ = self.call("DELETE", f"/api/groups/{group['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/groups")[1]["items"], [])

    def test_merge_moves_group_membership(self):
        keep, other = self.create(), self.create(first="Аня")
        _, group = self.call("POST", "/api/groups", {"name": "Семья", "members": [other["id"]]})
        self.call("POST", "/api/merge", {"target": {"id": keep["id"], "etag": keep["etag"]}, "sources": [{"id": other["id"], "etag": other["etag"]}], "card": {"first": "Анна"}})
        self.assertEqual(self.call("GET", "/api/groups")[1]["items"][0]["members"], [keep["id"]])

    def test_import_makes_apple_groups(self):
        cards = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Анна\r\nUID:A1\r\nEND:VCARD\r\n"
                 "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Друзья\r\nX-ADDRESSBOOKSERVER-KIND:group\r\nX-ADDRESSBOOKSERVER-MEMBER:urn:uuid:A1\r\nEND:VCARD\r\n")
        _, data = self.call("POST", "/api/import", {"vcf": cards})
        self.assertEqual((data["created"], data["groups"]), (1, 1))
        _, listed = self.call("GET", "/api/contacts")
        _, groups = self.call("GET", "/api/groups")
        self.assertEqual([(item["name"], item["members"]) for item in groups["items"]], [("Друзья", [listed["items"][0]["id"]])])

    def test_import_turns_categories_into_groups_and_export_back(self):
        cards = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Анна\r\nCATEGORIES:Семья,Работа,starred\r\nEND:VCARD\r\n"
                 "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Борис\r\nCATEGORIES:семья\r\nEND:VCARD\r\n")
        _, data = self.call("POST", "/api/import", {"vcf": cards})
        self.assertEqual((data["created"], data["groups"]), (2, 2))
        _, listed = self.call("GET", "/api/contacts")
        ids = {item["first"]: item["id"] for item in listed["items"]}
        self.assertTrue(next(item for item in listed["items"] if item["first"] == "Анна")["starred"])
        _, groups = self.call("GET", "/api/groups")
        self.assertEqual({item["name"]: sorted(item["members"]) for item in groups["items"]},
                         {"Работа": [ids["Анна"]], "Семья": sorted([ids["Анна"], ids["Борис"]])})
        _, data = self.call("POST", "/api/export", {"ids": [ids["Анна"]]})
        text = self.s3.objects[data["url"].split(".com/", 1)[1].split("?")[0]][0].decode()
        line = next(line for line in text.split("\r\n") if line.startswith("CATEGORIES:"))
        self.assertEqual(sorted(line[len("CATEGORIES:"):].split(",")), sorted(["Семья", "Работа", "starred"]))

    def test_related_people_link_cards(self):
        mother = self.create(first="Мария", last="Петрова")
        son = self.create(first="Пётр", related=[{"label": "мама", "value": "Мария", "contact": mother["id"]}])
        self.assertEqual(son["related"], [{"label": "мама", "value": "Мария", "contact": mother["id"]}])
        status, data = self.call("POST", "/api/contacts", {"first": "X", "related": [{"label": "мама", "value": "Y", "contact": "../x"}]})
        self.assertEqual((status, data["detail"]), (400, "related"))
        # The export names her as her card does now; an import of the same
        # file keeps the link, which vCard does not carry.
        self.call("PUT", f"/api/contacts/{mother['id']}", {**mother, "first": "Мария Ивановна"})
        _, data = self.call("POST", "/api/export", {"ids": [son["id"]]})
        text = self.s3.objects[data["url"].split(".com/", 1)[1].split("?")[0]][0].decode()
        self.assertIn("X-ABRELATEDNAMES:Мария Ивановна Петрова", text)
        self.call("POST", "/api/import", {"vcf": text.replace("END:VCARD", "TITLE:Инженер\r\nEND:VCARD")})
        _, one = self.call("GET", f"/api/contacts/{son['id']}")
        self.assertEqual((one["title"], one["related"][0]["contact"]), ("Инженер", mother["id"]))
        _, revs = self.call("GET", f"/api/contacts/{son['id']}/revisions")
        self.assertEqual(revs["items"][0]["action"], "import")

    def test_keep_links_by_name(self):
        old = [{"label": "мама", "value": "Мария", "contact": "a" * 16}]
        self.assertEqual(contacts.keep_links(old, [{"label": "mother", "value": "мария"}]), [{"label": "mother", "value": "мария", "contact": "a" * 16}])
        self.assertEqual(contacts.keep_links(old, [{"label": "мама", "value": "Ольга"}]), [{"label": "мама", "value": "Ольга"}])

    def test_photo_upload_and_cookie_path(self):
        status, data = self.call("POST", "/api/photos", {"content_type": "image/jpeg", "size": 1000})
        self.assertEqual(status, 200)
        self.assertTrue(data["path"].startswith(f"/photos/{contacts.user_key(OWNER)}/"))
        made = self.create(photo=data["path"])
        self.assertEqual(made["photo"], data["path"])
        status, data = self.call("POST", "/api/photos", {"content_type": "image/svg+xml", "size": 1000})
        self.assertEqual(status, 400)
        status, other = self.call("POST", "/api/contacts", {"first": "X", "photo": made["photo"]}, email=OTHER)
        self.assertEqual(other["code"], "bad_photo")

    def test_import_creates_updates_and_skips_unchanged(self):
        card = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Петрова;Анна;;;\r\nFN:Анна Петрова\r\n"
            "TEL;type=CELL;type=VOICE:+7 916 123-45-67\r\n"
            "PHOTO;ENCODING=b;TYPE=JPEG:" + base64.b64encode(b"\xff\xd8\xff\xe0fake").decode() + "\r\n"
            "END:VCARD\r\n"
        )
        other = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Борис\r\nEMAIL:boris@example.com\r\nEND:VCARD\r\n"
        status, data = self.call("POST", "/api/import", {"vcf": card + other})
        self.assertEqual((status, data["created"], data["updated"]), (200, 2, 0), data)
        self.assertEqual(len(self.s3.objects), 1)
        _, data = self.call("POST", "/api/import", {"vcf": card})
        self.assertEqual((data["created"], data["updated"], data["unchanged"]), (0, 0, 1))
        self.assertEqual(len(self.s3.objects), 1)
        changed = card.replace("END:VCARD", "ORG:Ромашка\r\nEND:VCARD")
        _, data = self.call("POST", "/api/import", {"vcf": changed})
        self.assertEqual((data["created"], data["updated"]), (0, 1))
        _, listed = self.call("GET", "/api/contacts")
        anna = next(item for item in listed["items"] if item["first"] == "Анна")
        self.assertEqual(anna["company"], "Ромашка")
        self.assertTrue(anna["photo"])
        _, revs = self.call("GET", f"/api/contacts/{anna['id']}/revisions")
        self.assertEqual([item["action"] for item in revs["items"]], ["import", "import"])

    def test_import_reports_bad_cards(self):
        card = "BEGIN:VCARD\r\nVERSION:3.0\r\nNOTE:без имени\r\nEND:VCARD\r\n"
        _, data = self.call("POST", "/api/import", {"vcf": card})
        self.assertEqual((data["skipped"], data["errors"][0]["code"]), (1, "empty_contact"))
        status, data = self.call("POST", "/api/import", {"vcf": "hello"})
        self.assertEqual((status, data["code"]), (400, "bad_vcard"))

    def test_export_writes_vcards_with_photos(self):
        jpeg = b"\xff\xd8\xff\xe0photo"
        _, data = self.call("POST", "/api/photos", {"content_type": "image/jpeg", "size": len(jpeg)})
        self.s3.objects[data["path"].lstrip("/")] = (jpeg, "image/jpeg")
        made = self.create(photo=data["path"], emails=[{"label": "work", "value": "anna@example.com"}])
        self.create(first="Борис", last="")
        status, data = self.call("POST", "/api/export", {})
        self.assertEqual((status, data["count"]), (200, 2))
        key = data["url"].split(".com/", 1)[1].split("?")[0]
        text = self.s3.objects[key][0].decode()
        cards = vcard.split_cards(text)
        self.assertEqual(len(cards), 2)
        back, photo = vcard.card_to_contact(cards[0])
        self.assertEqual((back["first"], photo), ("Анна", (jpeg, "image/jpeg")))
        self.assertEqual(back["uid"], made["id"] + "@contacts.example.com")
        _, one = self.call("POST", "/api/export", {"ids": [made["id"]]})
        self.assertEqual((one["count"], one["name"]), (1, "Анна Петрова.vcf"))

    def test_admin_routes_need_the_group(self):
        status, data = self.call("GET", "/api/admin/users")
        self.assertEqual((status, data["code"]), (403, "admin_only"))
        self.cognito.users[OWNER] = {"Username": OWNER, "UserStatus": "CONFIRMED", "Enabled": True}
        self.create()
        status, data = self.call("GET", "/api/admin/users", admin=True)
        self.assertEqual((status, data["items"][0]["contacts"], data["items"][0]["self"]), (200, 1, True))

    def test_delete_user_removes_book_and_photos(self):
        self.cognito.users[OTHER] = {"Username": OTHER}
        made = self.create()
        self.call("POST", "/api/contacts", {"first": "Его"}, email=OTHER)
        self.s3.objects[f"photos/{contacts.user_key(OTHER)}/x.jpg"] = (b"", "image/jpeg")
        self.s3.objects[f"photos/{contacts.user_key(OWNER)}/y.jpg"] = (b"", "image/jpeg")
        status, data = self.call("DELETE", "/api/admin/users", params={"email": OTHER}, admin=True)
        self.assertEqual((status, data["items"], data["photos"]), (200, 2, 1))
        self.assertEqual(list(self.s3.objects), [f"photos/{contacts.user_key(OWNER)}/y.jpg"])
        self.assertTrue(self.table.get_item(Key={"pk": "USER#" + OWNER, "sk": "CONTACT#" + made["id"]}))
        status, data = self.call("DELETE", "/api/admin/users", params={"email": OWNER}, admin=True)
        self.assertEqual(data["code"], "not_yourself")

    def test_unverified_email_is_refused(self):
        bad = event("GET", "/api/me")
        bad["requestContext"]["authorizer"]["jwt"]["claims"]["email_verified"] = "false"
        self.assertEqual(contacts.handler(bad, None)["statusCode"], 403)


class VcardTest(unittest.TestCase):
    APPLE = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\nPRODID:-//Apple Inc.//iPhone OS 18.0//EN\r\n"
        "N:Петрова;Анна;Сергеевна;Др.;мл.\r\nFN:Др. Анна Сергеевна Петрова мл.\r\n"
        "NICKNAME:Аня\r\nX-PHONETIC-FIRST-NAME:Anna\r\nX-MAIDENNAME:Сидорова\r\n"
        "ORG:Ромашка;Отдел продаж\r\nTITLE:Менеджер\r\n"
        "TEL;type=CELL;type=VOICE;type=pref:+7 916 123-45-67\r\n"
        "TEL;type=HOME;type=FAX:+7 495 000-00-00\r\n"
        "item1.TEL:+359 2 000 000\r\nitem1.X-ABLabel:Болгария\r\n"
        "EMAIL;type=INTERNET;type=WORK:anna@example.com\r\n"
        "item2.ADR;type=HOME;type=pref:;;ул. Ленина\\, 1\\nкв. 2;Москва;;101000;Россия\r\nitem2.X-ABADR:ru\r\n"
        "item3.URL:https://example.com\r\nitem3.X-ABLabel:_$!<HomePage>!$_\r\n"
        "BDAY;X-APPLE-OMIT-YEAR=1604:1604-03-08\r\n"
        "item4.X-ABDATE:2010-06-01\r\nitem4.X-ABLabel:_$!<Anniversary>!$_\r\n"
        "item5.X-ABRELATEDNAMES:Сергей\r\nitem5.X-ABLabel:_$!<Father>!$_\r\n"
        "X-SOCIALPROFILE;type=twitter;x-user=anna:https://twitter.com/anna\r\n"
        "IMPP;X-SERVICE-TYPE=Skype;type=HOME:skype:anna.p\r\n"
        "NOTE:Первая строка\\nвторая\r\n"
        "CATEGORIES:Семья,Работа\r\n"
        "UID:ABC-123\r\n"
        "END:VCARD\r\n"
    )

    def test_apple_card(self):
        contact, photo = vcard.card_to_contact(vcard.split_cards(self.APPLE)[0])
        self.assertIsNone(photo)
        self.assertEqual((contact["prefix"], contact["first"], contact["middle"], contact["last"], contact["suffix"]),
                         ("Др.", "Анна", "Сергеевна", "Петрова", "мл."))
        self.assertEqual((contact["nickname"], contact["phonetic_first"], contact["maiden"]), ("Аня", "Anna", "Сидорова"))
        self.assertEqual((contact["company"], contact["department"], contact["title"]), ("Ромашка", "Отдел продаж", "Менеджер"))
        self.assertEqual(contact["phones"], [
            {"label": "mobile", "value": "+7 916 123-45-67"},
            {"label": "home_fax", "value": "+7 495 000-00-00"},
            {"label": "Болгария", "value": "+359 2 000 000"},
        ])
        self.assertEqual(contact["emails"], [{"label": "work", "value": "anna@example.com"}])
        self.assertEqual(contact["addresses"][0], {
            "label": "home", "po_box": "", "extended": "", "street": "ул. Ленина, 1\nкв. 2", "city": "Москва",
            "region": "", "postal_code": "101000", "country": "Россия", "country_code": "ru",
        })
        self.assertEqual(contact["urls"], [{"label": "homepage", "value": "https://example.com"}])
        self.assertEqual(contact["birthday"], "--03-08")
        self.assertEqual(contact["dates"], [{"label": "anniversary", "value": "2010-06-01"}])
        self.assertEqual(contact["related"], [{"label": "father", "value": "Сергей"}])
        self.assertEqual(contact["social"], [{"service": "Twitter", "user": "anna", "url": "https://twitter.com/anna"}])
        self.assertEqual(contact["im"], [{"service": "Skype", "label": "home", "value": "anna.p"}])
        self.assertEqual(contact["notes"], "Первая строка\nвторая")
        self.assertEqual((contact["categories"], contact["uid"]), (["Семья", "Работа"], "ABC-123"))

    def test_round_trip(self):
        contact, _ = vcard.card_to_contact(vcard.split_cards(self.APPLE)[0])
        photo = (b"\x89PNG\r\n\x1a\n" + b"x" * 300, "image/png")
        text = vcard.contact_to_card(contact, photo, contact["categories"])
        self.assertTrue(all(len(line.encode()) <= 75 for line in text.split("\r\n")))
        again, back = vcard.card_to_contact(vcard.split_cards(text)[0])
        self.assertEqual(again, contact)
        self.assertEqual(back, photo)

    def test_vcard_21_and_40(self):
        old = (
            "BEGIN:VCARD\nVERSION:2.1\nN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=D0=98=D0=B2=D0=B0=D0=BD=D0=BE=D0=B2;=D0=98=\n=D0=B2=D0=B0=D0=BD\n"
            "TEL;WORK;VOICE:123\nEND:VCARD\n"
            "BEGIN:VCARD\nVERSION:4.0\nFN:Only Name\nTEL;VALUE=uri;TYPE=home:tel:+1-555\nANNIVERSARY:20100601\n"
            "PHOTO:data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff1").decode() + "\nEND:VCARD\n"
        )
        first, second = vcard.split_cards(old)
        contact, _ = vcard.card_to_contact(first)
        self.assertEqual((contact["last"], contact["first"], contact["phones"]), ("Иванов", "Иван", [{"label": "work", "value": "123"}]))
        contact, photo = vcard.card_to_contact(second)
        self.assertEqual((contact["first"], contact["last"]), ("Only", "Name"))
        self.assertEqual(contact["phones"], [{"label": "home", "value": "+1-555"}])
        self.assertEqual(contact["dates"], [{"label": "anniversary", "value": "2010-06-01"}])
        self.assertEqual(photo, (b"\xff\xd8\xff1", "image/jpeg"))

    def test_imported_card_passes_the_api_checks(self):
        contact, _ = vcard.card_to_contact(vcard.split_cards(self.APPLE)[0])
        self.assertEqual(contacts.clean_contact(contact, OWNER)["addresses"][0]["city"], "Москва")


class CookieTest(unittest.TestCase):
    def test_signed_cookie_is_valid_and_limited_to_the_user(self):
        with tempfile.TemporaryDirectory() as folder:
            key_path = os.path.join(folder, "key.pem")
            subprocess.run(["openssl", "genrsa", "-out", key_path, "2048"], check=True, capture_output=True)
            with open(key_path) as handle:
                contacts._signing_key = contacts.load_rsa_private_key(handle.read())
            os.environ["CLOUDFRONT_KEY_PAIR_ID"] = "K1"
            cookies = contacts.photo_cookies("contacts.example.com", OWNER, now=1000)
            values = dict(cookie.split(";")[0].split("=", 1) for cookie in cookies)
            policy = base64.b64decode(values["CloudFront-Policy"].replace("-", "+").replace("_", "=").replace("~", "/"))
            self.assertIn(f"/photos/{contacts.user_key(OWNER)}/*", policy.decode())
            signature = base64.b64decode(values["CloudFront-Signature"].replace("-", "+").replace("_", "=").replace("~", "/"))
            with open(os.path.join(folder, "policy"), "wb") as handle:
                handle.write(policy)
            with open(os.path.join(folder, "sig"), "wb") as handle:
                handle.write(signature)
            subprocess.run(["openssl", "rsa", "-in", key_path, "-pubout", "-out", os.path.join(folder, "pub.pem")], check=True, capture_output=True)
            check = subprocess.run(
                ["openssl", "dgst", "-sha1", "-verify", os.path.join(folder, "pub.pem"), "-signature", os.path.join(folder, "sig"), os.path.join(folder, "policy")],
                capture_output=True, text=True,
            )
            self.assertIn("Verified OK", check.stdout)
            contacts._signing_key = None


if __name__ == "__main__":
    unittest.main()
