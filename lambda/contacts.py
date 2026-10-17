"""Address book API.

Every user has an address book of their own. The page calls /api/... through
API Gateway with a Cognito token: the account, the cards, their history,
search in the history, the deleted cards, photos, vCard import and export,
and the version of the site against the releases of the demo repository.
Administrators, the members of the Cognito group ADMIN_GROUP, also manage the
accounts at /api/admin/users.

DynamoDB, a partition per user, pk = USER#<email>:
sk = CONTACT#<id>  the current card: the fields of FIELDS, etag, created,
                   modified, photo_hash;
sk = REV#<id>#<time>#<rand>  every create, save, delete, restore and import:
                   the whole card as it became (as it was, for a delete),
                   action, at, by and changes, the names of the fields that
                   changed;
sk = DELETED#<id>  a deleted card: its name and when, so that it can be found
                   and restored with its history.

The page has every card of the user and searches them itself; the server
searches the history, which the page does not load.

Photos stay private in S3 under photos/<user>/ and are read through
CloudFront with a signed cookie for that prefix only. An export is written to
exports/<user>/ and fetched through a presigned link.
"""

import base64
import hashlib
import html.parser
import json
import logging
import os
import re
import secrets
import string
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote, unquote

import vcard

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_table = None
_dynamodb = None
_s3 = None
_cognito = None
_signing_key = None

TEXT_FIELDS = {
    # Name
    "prefix": 100, "first": 200, "phonetic_first": 200, "middle": 200, "phonetic_middle": 200,
    "last": 200, "phonetic_last": 200, "maiden": 200, "suffix": 100, "nickname": 200,
    "file_as": 200, "pronouns": 100,
    # Work
    "company": 200, "phonetic_company": 200, "department": 200, "title": 200,
    # Other
    "birthday": 10, "notes": 20_000, "photo": 200, "uid": 200,
    # A birthday kept out of the calendars: it goes neither to Google nor into
    # the BDAY of a vCard, which the birthday calendars of Google and Apple
    # read. The card has one of the two.
    "hidden_birthday": 10,
}
# The lists of a card: the parts of an entry and their lengths.
LIST_FIELDS = {
    "phones": {"label": 100, "value": 100},
    "emails": {"label": 100, "value": 254},
    "urls": {"label": 100, "value": 2000},
    "addresses": {
        "label": 100, "street": 500, "po_box": 100, "extended": 200, "city": 200,
        "region": 200, "postal_code": 50, "country": 200, "country_code": 10,
    },
    "dates": {"label": 100, "value": 10},
    # contact: the id of the card of this person, if the page linked one.
    "related": {"label": 100, "value": 200, "contact": 16},
    "social": {"service": 100, "user": 200, "url": 2000},
    "im": {"service": 100, "label": 100, "value": 200},
    "custom": {"label": 100, "value": 2000},
}
# Groups are items of their own (sk = GROUP#<id>) that list their members, as
# Apple keeps them; Google's labels (CATEGORIES in vCard) become groups on
# import. Cards saved earlier may still hold "labels" or "groups"; they are
# not fields and are left out.
FIELDS = list(TEXT_FIELDS) + list(LIST_FIELDS) + ["starred"]
LIST_MAX = 50
GROUPS_MAX = 200
CONTACTS_MAX = 10_000
REVISIONS_MAX = 200
SEARCH_MAX = 200
COOKIE_SECONDS = 3600
# YYYY-MM-DD, or --MM-DD for a date without a year.
DATE_RE = re.compile(r"^(\d{4}|-)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
ID_RE = re.compile(r"^[0-9a-f]{16}$")
ETAG_RE = re.compile(r"^[0-9a-f]{32}$")
REV_RE = re.compile(r"^REV#[0-9a-f]{16}#[0-9TZ:.-]{20,40}#[0-9a-f]{8}$")
PHOTO_RE = re.compile(r"^/photos/[0-9a-f]{20}/[0-9a-f]{32}\.(jpg|png|gif|webp)$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}
IMPORT_MAX_BYTES = 5_000_000
IMPORT_CARDS_MAX = 200
SHA1_DIGEST_INFO = bytes.fromhex("3021300906052b0e03021a05000414")
PASSWORD_SYMBOLS = "!@#%&*-_=+"

ERRORS = {
    "not_found": "Not found",
    "contact_not_found": "No such contact",
    "revision_not_found": "No such version",
    "email_not_verified": "Email not verified",
    "bad_json": "Invalid JSON",
    "bad_json_object": "A JSON object was expected",
    "too_large": "Request too large",
    "internal": "Internal error",
    "bad_field": "A field of the card is not valid",
    "empty_contact": "A card needs at least a name, a company, a phone or an email",
    "bad_date": "A date is YYYY-MM-DD, or --MM-DD without a year",
    "bad_photo": "The photo must be JPEG, PNG, GIF or WebP and not larger than the limit",
    "changed": "The card has changed. Reload it and try again.",
    "deleted": "The card has been deleted",
    "exists": "The card exists; it is not deleted",
    "too_many_contacts": "The address book is full",
    "bad_draft": "The draft is not valid",
    "bad_merge": "Pick a card to keep and up to 10 cards to merge into it",
    "bad_app_password": "Name the device: up to 100 characters",
    "bad_group": "A group needs a name of up to 100 characters and cards of this address book",
    "google_off": "Google sync is not set up or not connected",
    "demo_readonly": "Not available in the demo",
    "icloud_off": "iCloud sync is not set up or not connected",
    "bad_icloud": "An Apple ID (email) and an app-specific password are needed",
    "icloud_auth": "iCloud did not accept the Apple ID or the app-specific password",
    "icloud_unreachable": "iCloud did not answer; try again later",
    "group_not_found": "No such group",
    "too_many_groups": "Too many groups",
    "too_many_app_passwords": "Too many app passwords; revoke one first",
    "app_password_not_found": "No such app password",
    "too_many_drafts": "Too many drafts; save or cancel some edits",
    "bad_vcard": "No vCard found in the file",
    "bad_cursor": "Invalid cursor",
    "admin_only": "Administrators only",
    "bad_email": "An email address is needed",
    "bad_flag": "A switch must be true or false",
    "user_exists": "This user already exists",
    "user_not_found": "No such user",
    "not_yourself": "You cannot do this to your own account",
    "not_configured": "The service is not configured",
}


class ApiError(Exception):
    def __init__(self, status, code, detail=""):
        super().__init__(ERRORS[code])
        self.status = status
        self.code = code
        self.message = ERRORS[code]
        # Which field was wrong, for the page to show.
        self.detail = detail


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    parts = [unquote(part) for part in (event.get("rawPath") or "/").split("/") if part]
    owner = None
    try:
        if parts[:1] != ["api"]:
            raise ApiError(404, "not_found")
        # Google comes back here from its consent page, without a token.
        if parts == ["api", "oauth", "google"] and method == "GET":
            import google_sync

            return google_sync.callback(event.get("queryStringParameters") or {})
        owner = actor(event)
        result = route(method, parts[1:], event, owner)
    except ApiError as exc:
        result = response(exc.status, {"error": exc.message, "code": exc.code, **({"detail": exc.detail} if exc.detail else {})})
    except json.JSONDecodeError:
        result = response(400, {"error": ERRORS["bad_json"], "code": "bad_json"})
    except Exception as exc:
        if conditional_failed(exc):
            result = response(409, {"error": ERRORS["changed"], "code": "changed"})
        else:
            logger.exception("unhandled")
            result = response(500, {"error": ERRORS["internal"], "code": "internal"})
    if 200 <= result["statusCode"] < 300 and owner:
        attach_photo_cookies(result, event, owner)
    logger.info("%s %s -> %s", method, event.get("rawPath"), result["statusCode"])
    return result


def route(method, parts, event, owner):
    params = event.get("queryStringParameters") or {}
    if parts == ["me"] and method == "GET":
        return response(200, {
            "email": owner,
            "admin": is_admin(event),
            "photo_max_bytes": photo_max_bytes(),
            # The CardDAV server for Apple Contacts, Thunderbird and the rest;
            # the demo shows an example address, as it has no server.
            "dav_url": os.environ.get("DAV_URL", "") or ("https://dav.example.com" if demo() else ""),
            "demo": demo(),
            "contacts_max": CONTACTS_MAX,
        })
    if parts == ["version"] and method == "GET":
        return response(200, version_info())
    if parts[:1] == ["admin"]:
        if not is_admin(event):
            raise ApiError(403, "admin_only")
        return admin_route(method, parts[1:], event, owner)
    if parts == ["contacts"] and method == "GET":
        return list_contacts(owner, params.get("cursor"))
    if parts == ["contacts"] and method == "POST":
        return response(201, create_contact(owner, body_json(event)))
    if len(parts) >= 2 and parts[0] == "contacts":
        contact_id = clean_id(parts[1])
        rest = parts[2:]
        if not rest and method == "GET":
            item = get_current(owner, contact_id)
            if not item:
                raise ApiError(404, "contact_not_found")
            return response(200, public_contact(item))
        if not rest and method == "PUT":
            return response(200, save_contact(owner, contact_id, body_json(event)))
        if not rest and method == "DELETE":
            return response(200, delete_contact(owner, contact_id, params.get("etag")))
        if rest == ["revisions"] and method == "GET":
            return response(200, {"items": revisions(owner, contact_id)})
        if rest == ["restore"] and method == "POST":
            return response(200, restore_contact(owner, contact_id, body_json(event)))
    if parts == ["merge"] and method == "POST":
        return response(200, merge_contacts(owner, body_json(event)))
    if parts == ["duplicates", "ignored"] and method == "GET":
        return response(200, {"items": ignored_duplicates(owner)})
    if parts == ["duplicates", "ignored"] and method == "PUT":
        return response(200, ignore_duplicates(owner, body_json(event)))
    if parts == ["groups"] and method == "GET":
        return response(200, {"items": list_groups(owner)})
    if parts == ["groups"] and method == "POST":
        return response(201, create_group(owner, body_json(event)))
    if len(parts) == 2 and parts[0] == "groups" and method == "PUT":
        return response(200, update_group(owner, clean_group_id(parts[1]), body_json(event)))
    if len(parts) == 2 and parts[0] == "groups" and method == "DELETE":
        return response(200, delete_group(owner, clean_group_id(parts[1]), params.get("etag")))
    if demo() and parts[:1] in (["google"], ["icloud"]):
        return demo_sync_route(method, parts, owner)
    if parts[:1] == ["google"]:
        import google_sync

        if parts == ["google"] and method == "GET":
            return response(200, google_sync.public_state(owner))
        if parts == ["google", "start"] and method == "POST":
            return response(200, google_sync.start(owner))
        if parts == ["google", "sync"] and method == "POST":
            if not google_sync.public_state(owner)["connected"]:
                raise ApiError(409, "google_off")
            google_sync.start_sync(owner)
            return response(202, {"started": True})
        if parts == ["google"] and method == "DELETE":
            return response(200, google_sync.disconnect(owner))
    if parts[:1] == ["icloud"]:
        import icloud_sync

        if parts == ["icloud"] and method == "GET":
            return response(200, icloud_sync.public_state(owner))
        if parts == ["icloud"] and method == "POST":
            return response(200, icloud_sync.connect(owner, body_json(event)))
        if parts == ["icloud", "sync"] and method == "POST":
            if not icloud_sync.public_state(owner)["connected"]:
                raise ApiError(409, "icloud_off")
            icloud_sync.start_sync(owner)
            return response(202, {"started": True})
        if parts == ["icloud"] and method == "DELETE":
            return response(200, icloud_sync.disconnect(owner))
    if parts == ["sync-log"] and method == "GET":
        return response(200, sync_log(owner, params.get("cursor")))
    if parts == ["app-passwords"] and method == "GET":
        return response(200, {"items": list_app_passwords(owner)})
    if parts == ["app-passwords"] and method == "POST":
        return response(201, create_app_password(owner, body_json(event)))
    if len(parts) == 2 and parts[0] == "app-passwords" and method == "DELETE":
        return response(200, revoke_app_password(owner, parts[1]))
    if parts == ["drafts"] and method == "GET":
        return response(200, {"items": list_drafts(owner)})
    if len(parts) == 2 and parts[0] == "drafts" and method == "PUT":
        return response(200, put_draft(owner, clean_draft_key(parts[1]), body_json(event)))
    if len(parts) == 2 and parts[0] == "drafts" and method == "DELETE":
        table().delete_item(Key={"pk": user_pk(owner), "sk": "DRAFT#" + clean_draft_key(parts[1])})
        return response(200, {"deleted": parts[1]})
    if parts == ["deleted"] and method == "GET":
        return response(200, {"items": deleted_contacts(owner)})
    if len(parts) == 2 and parts[0] == "deleted" and method == "DELETE":
        return response(200, purge_contact(owner, clean_id(parts[1])))
    if parts == ["search"] and method == "GET":
        return response(200, {"items": search_history(owner, params.get("q"))})
    if parts == ["photos"] and method == "POST":
        return response(200, presign_photo(owner, body_json(event)))
    if parts == ["import"] and method == "POST":
        return response(200, import_vcards(owner, body_json(event, IMPORT_MAX_BYTES + 100_000)))
    if parts == ["export"] and method == "POST":
        return response(200, export_vcards(owner, body_json(event)))
    raise ApiError(404, "not_found")


# Cards -----------------------------------------------------------------------

def user_pk(owner):
    return "USER#" + owner


def user_key(owner):
    """The folder of the user's photos and exports; the email stays out of S3."""
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:20]


def list_contacts(owner, cursor):
    from boto3.dynamodb.conditions import Key

    kwargs = {"KeyConditionExpression": Key("pk").eq(user_pk(owner)) & Key("sk").begins_with("CONTACT#")}
    start = decode_cursor(cursor, owner)
    if start:
        kwargs["ExclusiveStartKey"] = start
    # One query page is at most 1 MB, well within the Lambda response limit.
    page = table().query(**kwargs)
    return response(200, {
        "items": [public_contact(item) for item in page.get("Items", [])],
        "next_cursor": encode_cursor(page.get("LastEvaluatedKey")),
    })


def all_current(owner, fields=None):
    from boto3.dynamodb.conditions import Key

    kwargs = {"KeyConditionExpression": Key("pk").eq(user_pk(owner)) & Key("sk").begins_with("CONTACT#")}
    if fields:
        names = {f"#f{n}": field for n, field in enumerate(fields)}
        kwargs["ProjectionExpression"] = ", ".join(names)
        kwargs["ExpressionAttributeNames"] = names
    while True:
        page = table().query(**kwargs)
        yield from page.get("Items", [])
        if not page.get("LastEvaluatedKey"):
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def count_contacts(owner):
    from boto3.dynamodb.conditions import Key

    total, kwargs = 0, {
        "KeyConditionExpression": Key("pk").eq(user_pk(owner)) & Key("sk").begins_with("CONTACT#"),
        "Select": "COUNT",
    }
    while True:
        page = table().query(**kwargs)
        total += page.get("Count", 0)
        if not page.get("LastEvaluatedKey"):
            return total
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def get_current(owner, contact_id):
    return table().get_item(Key={"pk": user_pk(owner), "sk": "CONTACT#" + contact_id}, ConsistentRead=True).get("Item")


def create_contact(owner, payload, action="create", photo_hash="", dav=""):
    fields = clean_contact(payload, owner, blank_ok=action in ("sync", "google", "icloud"))
    if count_contacts(owner) >= CONTACTS_MAX:
        raise ApiError(409, "too_many_contacts")
    now = now_iso()
    contact_id = uuid.uuid4().hex[:16]
    item = current_item(owner, contact_id, fields, now, now, photo_hash, dav)
    transact([
        put_action(item, "attribute_not_exists(pk)"),
        put_action(revision_item(item, action, now, owner, changed_fields({}, fields)), None),
        *([drop_draft(owner, NEW_DRAFT)] if action == "create" else []),
        *membership_actions(owner, dav_uid(item), payload.get("group_ids"), now),
    ])
    return public_contact(item)


def save_contact(owner, contact_id, payload, action="save", photo_hash=None):
    etag = clean_etag(payload.get("etag"))
    fields = clean_contact(payload, owner, blank_ok=action in ("sync", "google", "icloud"))
    old = get_current(owner, contact_id)
    if not old:
        raise ApiError(409, "deleted")
    if old.get("etag") != etag:
        raise ApiError(409, "changed")
    changes = changed_fields(old, fields)
    groups = membership_actions(owner, dav_uid(old), payload.get("group_ids"), now_iso())
    if not changes:
        if groups:
            transact(groups)
        if action == "save":
            table().delete_item(Key={"pk": user_pk(owner), "sk": "DRAFT#" + contact_id})
        return public_contact(old)
    now = now_iso()
    if photo_hash is None:
        photo_hash = old.get("photo_hash", "") if fields.get("photo") == old.get("photo") else ""
    item = current_item(owner, contact_id, fields, old.get("created") or now, now, photo_hash, old.get("dav", ""))
    transact([
        put_action(item, "etag = :etag", {":etag": etag}),
        put_action(revision_item(item, action, now, owner, changes), None),
        *([drop_draft(owner, contact_id)] if action == "save" else []),
        *groups,
    ])
    return public_contact(item)


def delete_contact(owner, contact_id, etag):
    etag = clean_etag(etag)
    old = get_current(owner, contact_id)
    if not old:
        raise ApiError(404, "contact_not_found")
    if old.get("etag") != etag:
        raise ApiError(409, "changed")
    now = now_iso()
    transact([
        {"Delete": {
            "TableName": table_name(),
            "Key": marshal_item({"pk": user_pk(owner), "sk": "CONTACT#" + contact_id}),
            "ConditionExpression": "etag = :etag",
            "ExpressionAttributeValues": marshal_item({":etag": etag}),
        }},
        put_action(revision_item(old, "delete", now, owner, []), None),
        put_action({
            "pk": user_pk(owner),
            "sk": "DELETED#" + contact_id,
            "id": contact_id,
            "name": vcard.display_name(old) or first_value(old),
            "photo": old.get("photo", ""),
            "at": now,
            "by": owner,
            **dav_of(old),
        }, None),
        drop_draft(owner, contact_id),
    ])
    return {"deleted": contact_id}


# Duplicates ------------------------------------------------------------------------
# The page finds the duplicates (it has every card) and shows the merged card;
# the API saves it over the card kept and deletes the others in one
# transaction. A merged card goes to the deleted ones with its history, so a
# merge can be undone by restoring it.

MERGE_MAX = 10


def merge_contacts(owner, payload):
    target = payload.get("target") or {}
    sources = payload.get("sources")
    if not isinstance(target, dict) or not isinstance(sources, list) or not 1 <= len(sources) <= MERGE_MAX:
        raise ApiError(400, "bad_merge")
    if not all(isinstance(source, dict) for source in sources):
        raise ApiError(400, "bad_merge")
    target_id = clean_id(target.get("id"))
    source_ids = [clean_id(source.get("id")) for source in sources]
    if len(set(source_ids)) != len(source_ids) or target_id in source_ids:
        raise ApiError(400, "bad_merge")
    fields = clean_contact(payload.get("card"), owner)
    old = get_current(owner, target_id)
    if not old:
        raise ApiError(409, "deleted")
    if old.get("etag") != clean_etag(target.get("etag")):
        raise ApiError(409, "changed")
    merged = []
    for source, source_id in zip(sources, source_ids):
        item = get_current(owner, source_id)
        if not item:
            raise ApiError(409, "deleted")
        if item.get("etag") != clean_etag(source.get("etag")):
            raise ApiError(409, "changed")
        merged.append(item)
    photo_hash = next((item.get("photo_hash", "") for item in [old, *merged] if fields.get("photo") and item.get("photo") == fields["photo"]), "")
    now = now_iso()
    item = current_item(owner, target_id, fields, old.get("created") or now, now, photo_hash, old.get("dav", ""))
    actions = [
        put_action(item, "etag = :etag", {":etag": old["etag"]}),
        put_action(revision_item(item, "merge", now, owner, changed_fields(old, fields), {"merged": source_ids}), None),
        drop_draft(owner, target_id),
    ]
    for source in merged:
        actions += [
            {"Delete": {
                "TableName": table_name(),
                "Key": marshal_item({"pk": user_pk(owner), "sk": "CONTACT#" + source["id"]}),
                "ConditionExpression": "etag = :etag",
                "ExpressionAttributeValues": marshal_item({":etag": source["etag"]}),
            }},
            put_action(revision_item(source, "merged", now, owner, [], {"into": target_id}), None),
            put_action({
                "pk": user_pk(owner),
                "sk": "DELETED#" + source["id"],
                "id": source["id"],
                "name": vcard.display_name(source) or first_value(source),
                "photo": source.get("photo", ""),
                "at": now,
                "by": owner,
                "into": target_id,
                **dav_of(source),
            }, None),
            drop_draft(owner, source["id"]),
        ]
    # The groups of the merged cards get the card kept in their place.
    gone = {dav_uid(source) for source in merged}
    target_uid = dav_uid(item)
    for group in group_items(owner):
        members = list(group.get("members") or [])
        if gone & set(members):
            kept = [member for member in members if member not in gone]
            if target_uid not in kept:
                kept.append(target_uid)
            actions.append(group_put(group, {"members": kept}, now))
    transact(actions)
    return {"contact": public_contact(item), "merged": source_ids}


# Cards the user said are not duplicates of each other: the page leaves such
# a group out.
def ignored_duplicates(owner):
    from boto3.dynamodb.conditions import Key

    items, kwargs = [], {"KeyConditionExpression": Key("pk").eq(user_pk(owner)) & Key("sk").begins_with("NOTDUP#")}
    while True:
        page = table().query(**kwargs)
        items.extend(list(item.get("ids") or []) for item in page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def ignore_duplicates(owner, payload):
    ids = payload.get("ids")
    if not isinstance(ids, list) or not 2 <= len(ids) <= MERGE_MAX + 1:
        raise ApiError(400, "bad_merge")
    ids = sorted({clean_id(value) for value in ids})
    if len(ids) < 2:
        raise ApiError(400, "bad_merge")
    table().put_item(Item={"pk": user_pk(owner), "sk": "NOTDUP#" + "-".join(ids), "ids": ids, "at": now_iso()})
    return {"ids": ids}


# Sync log --------------------------------------------------------------------------
# What every sync changed and where: a line per card, group or photo that a
# CardDAV device or Google changed here, or that went from here to Google
# (sk = SYNCLOG#<time>#<rand>). DynamoDB drops a line after SYNC_LOG_DAYS
# (the attribute "expires").

SYNC_LOG_DAYS = 90
SYNC_LOG_PAGE = 100


def log_sync(owner, source, action, target="", name="", fields=None, detail="", contact_id=""):
    """source: "google", "to_google", "icloud", "to_icloud" or "dav:<device>"; action: created,
    changed, deleted, restored, conflict, group_created, group_renamed,
    group_deleted, photo, error."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    item = {
        "pk": user_pk(owner),
        "sk": f"SYNCLOG#{stamp}#{uuid.uuid4().hex[:6]}",
        "at": now_iso(),
        "source": source[:120],
        "action": action,
        "expires": int(time.time()) + SYNC_LOG_DAYS * 86400,
    }
    for key, value in (("target", target), ("name", name[:200] if name else ""), ("detail", detail[:500] if detail else ""), ("contact", contact_id)):
        if value:
            item[key] = value
    if fields:
        item["fields"] = list(fields)[:40]
    try:
        table().put_item(Item=item)
    except Exception:
        logger.exception("sync log not written")


def sync_log(owner, cursor=None):
    from boto3.dynamodb.conditions import Key

    kwargs = {
        "KeyConditionExpression": Key("pk").eq(user_pk(owner)) & Key("sk").begins_with("SYNCLOG#"),
        "ScanIndexForward": False,
        "Limit": SYNC_LOG_PAGE,
    }
    start = decode_cursor(cursor, owner)
    if start:
        kwargs["ExclusiveStartKey"] = start
    page = table().query(**kwargs)
    keys = ("at", "source", "action", "target", "name", "fields", "detail", "contact")
    return {
        "items": [{key: plain(item[key]) for key in keys if key in item} for item in page.get("Items", [])],
        "next_cursor": encode_cursor(page.get("LastEvaluatedKey")),
    }


# App passwords ---------------------------------------------------------------------
# CardDAV clients sign in with the email and a password of their own (HTTP
# Basic), not with Cognito: they know neither SRP nor the codes of two-factor
# sign-in. Each device gets a random password, shown once; the table keeps
# its SHA-256 with a salt. The password is random and long, so a slow hash is
# not needed, and every request of a client checks it.

APP_PASSWORDS_MAX = 20
APP_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
APP_PASSWORD_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def app_password_hash(salt, password):
    return hashlib.sha256((salt + ":" + password).encode("utf-8")).hexdigest()


def app_password_items(owner):
    from boto3.dynamodb.conditions import Key

    page = table().query(KeyConditionExpression=Key("pk").eq(user_pk(owner)) & Key("sk").begins_with("APPPASS#"))
    return page.get("Items", [])


def list_app_passwords(owner):
    items = [{"id": item["id"], "name": item.get("name", ""), "created": item.get("created", ""), "used": item.get("used", "")} for item in app_password_items(owner)]
    return sorted(items, key=lambda item: item["created"])


def create_app_password(owner, payload):
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise ApiError(400, "bad_app_password")
    if len(app_password_items(owner)) >= APP_PASSWORDS_MAX:
        raise ApiError(409, "too_many_app_passwords")
    raw = "".join(secrets.choice(APP_PASSWORD_ALPHABET) for _ in range(16))
    password = "-".join(raw[index:index + 4] for index in range(0, 16, 4))
    salt = secrets.token_hex(8)
    item_id = secrets.token_hex(6)
    now = now_iso()
    table().put_item(Item={
        "pk": user_pk(owner), "sk": "APPPASS#" + item_id, "id": item_id, "name": re.sub(r"\s+", " ", name).strip(),
        "salt": salt, "hash": app_password_hash(salt, password), "created": now,
    })
    return {"id": item_id, "name": name.strip(), "created": now, "password": password}


def revoke_app_password(owner, item_id):
    if not APP_PASSWORD_ID_RE.fullmatch(str(item_id)):
        raise ApiError(404, "app_password_not_found")
    key = {"pk": user_pk(owner), "sk": "APPPASS#" + item_id}
    if not table().get_item(Key=key).get("Item"):
        raise ApiError(404, "app_password_not_found")
    table().delete_item(Key=key)
    return {"revoked": item_id}


# Groups ----------------------------------------------------------------------------
# Apple's groups (sk = GROUP#<id>): a name and the members by the UID their
# card has in CardDAV (dav_uid), as Apple lists them. A deleted card stays in
# its groups and is back in them when it is restored; the page shows only the
# cards that exist. A deleted group leaves sk = GONE#<id> for CardDAV clients.

GROUP_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def uid_domain():
    return vcard.site_domain()


def dav_uid(item):
    return item.get("uid") or f"{item['id']}@{uid_domain()}"


def clean_group_id(value):
    if not GROUP_ID_RE.fullmatch(str(value or "")):
        raise ApiError(404, "group_not_found")
    return value


def group_items(owner):
    from boto3.dynamodb.conditions import Key

    items, kwargs = [], {"KeyConditionExpression": Key("pk").eq(user_pk(owner)) & Key("sk").begins_with("GROUP#")}
    while True:
        page = table().query(**kwargs)
        items.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def get_group(owner, group_id):
    return table().get_item(Key={"pk": user_pk(owner), "sk": "GROUP#" + group_id}, ConsistentRead=True).get("Item")


def card_uids(owner):
    """The CardDAV UID of every card by its id."""
    return {item["id"]: dav_uid(item) for item in all_current(owner, ["id", "uid"])}


def public_group(item, ids_by_uid):
    return {
        "id": item["id"],
        "name": item.get("name", ""),
        "members": [ids_by_uid[uid] for uid in item.get("members") or [] if uid in ids_by_uid],
        "etag": item.get("etag", ""),
        "modified": item.get("modified", ""),
    }


def list_groups(owner):
    ids = {uid: contact_id for contact_id, uid in card_uids(owner).items()}
    groups = [public_group(item, ids) for item in group_items(owner)]
    return sorted(groups, key=lambda group: fold(group["name"]))


def clean_group_name(value):
    name = re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""
    if not name or len(name) > 100:
        raise ApiError(400, "bad_group", "name")
    return name


def member_uids(owner, ids):
    if not isinstance(ids, list) or len(ids) > CONTACTS_MAX:
        raise ApiError(400, "bad_group", "members")
    uids = card_uids(owner)
    if not all(isinstance(value, str) and value in uids for value in ids):
        raise ApiError(400, "bad_group", "members")
    return list(dict.fromkeys(uids[value] for value in ids))


def group_put(group, changes, now, condition=True):
    item = {**group, **changes, "modified": now, "etag": uuid.uuid4().hex}
    return put_action(item, "etag = :etag" if condition else None, {":etag": group["etag"]} if condition else None)


def new_group(owner, name, members, uid="", dav="", now=None):
    now = now or now_iso()
    group_id = uuid.uuid4().hex[:16]
    item = {
        "pk": user_pk(owner), "sk": "GROUP#" + group_id, "id": group_id, "name": name, "members": members,
        "uid": uid or f"{group_id}@{uid_domain()}", "created": now, "modified": now, "etag": uuid.uuid4().hex,
    }
    if dav:
        item["dav"] = dav
    return item


def create_group(owner, payload):
    name = clean_group_name(payload.get("name"))
    item = store_group(owner, name, member_uids(owner, payload.get("members") or []))
    return public_group(item, {value: key for key, value in card_uids(owner).items()})


def store_group(owner, name, members, uid="", dav=""):
    """A new group with members by UID, as CardDAV and vCard files give them."""
    if len(group_items(owner)) >= GROUPS_MAX:
        raise ApiError(409, "too_many_groups")
    item = new_group(owner, name, members, uid, dav)
    table().put_item(Item=item)
    return item


def update_group(owner, group_id, payload):
    group = get_group(owner, group_id)
    if not group:
        raise ApiError(404, "group_not_found")
    if group.get("etag") != clean_etag(payload.get("etag")):
        raise ApiError(409, "changed")
    changes = {}
    if "name" in payload:
        changes["name"] = clean_group_name(payload.get("name"))
    if "members" in payload:
        # Members that are deleted cards now stay, so that a restore brings them back.
        uids = card_uids(owner)
        current = set(uids.values())
        kept = [uid for uid in group.get("members") or [] if uid not in current]
        changes["members"] = kept + member_uids(owner, payload["members"])
    now = now_iso()
    transact([group_put(group, changes, now)])
    item = {**group, **changes, "modified": now}
    return public_group(get_group(owner, group_id) or item, {value: key for key, value in card_uids(owner).items()})


def delete_group(owner, group_id, etag=None):
    group = get_group(owner, group_id)
    if not group:
        raise ApiError(404, "group_not_found")
    if etag and group.get("etag") != etag:
        raise ApiError(409, "changed")
    transact([
        {"Delete": {
            "TableName": table_name(),
            "Key": marshal_item({"pk": user_pk(owner), "sk": "GROUP#" + group_id}),
            "ConditionExpression": "etag = :etag",
            "ExpressionAttributeValues": marshal_item({":etag": group["etag"]}),
        }},
        put_action({"pk": user_pk(owner), "sk": "GONE#" + group_id, "id": group_id, "at": now_iso(), **dav_of(group)}, None),
    ])
    return {"deleted": group_id}


def membership_actions(owner, uid, group_ids, now):
    """The group changes that put a card into exactly these groups (ids)."""
    if group_ids is None:
        return []
    if not isinstance(group_ids, list) or not all(isinstance(value, str) for value in group_ids):
        raise ApiError(400, "bad_group", "groups")
    wanted = set(group_ids)
    groups = group_items(owner)
    if not wanted <= {group["id"] for group in groups}:
        raise ApiError(400, "bad_group", "groups")
    actions = []
    for group in groups:
        members = list(group.get("members") or [])
        inside = uid in members
        if (group["id"] in wanted) == inside:
            continue
        members = members + [uid] if not inside else [member for member in members if member != uid]
        actions.append(group_put(group, {"members": members}, now))
    return actions


# Drafts ----------------------------------------------------------------------------
# An unsaved edit of a card (key = its id) or of a new one (key = "new"),
# saved by the page as the user types, so that it survives a reload and is
# there on another device. A save of the card removes it in the same
# transaction.

NEW_DRAFT = "new"
DRAFTS_MAX = 30
DRAFT_MAX_BYTES = 100_000


def clean_draft_key(value):
    value = str(value or "")
    if value != NEW_DRAFT and not ID_RE.fullmatch(value):
        raise ApiError(400, "bad_draft")
    return value


def list_drafts(owner):
    from boto3.dynamodb.conditions import Key

    page = table().query(KeyConditionExpression=Key("pk").eq(user_pk(owner)) & Key("sk").begins_with("DRAFT#"))
    items = []
    for item in page.get("Items", []):
        try:
            draft = json.loads(item.get("data") or "{}")
        except ValueError:
            continue
        items.append({"key": item["key"], "draft": draft, "etag": item.get("etag", ""), "modified": item.get("modified", "")})
    return sorted(items, key=lambda item: item["modified"])


def put_draft(owner, key, payload):
    draft = payload.get("draft")
    etag = str(payload.get("etag") or "")
    if not isinstance(draft, dict) or (etag and not ETAG_RE.fullmatch(etag)):
        raise ApiError(400, "bad_draft")
    # Half-typed text is kept as typed; only the size is checked. The card is
    # checked when it is saved.
    data = json.dumps(draft, ensure_ascii=False)
    if len(data.encode("utf-8")) > DRAFT_MAX_BYTES:
        raise ApiError(413, "too_large")
    sk = "DRAFT#" + key
    if not table().get_item(Key={"pk": user_pk(owner), "sk": sk}).get("Item"):
        from boto3.dynamodb.conditions import Key

        count = table().query(KeyConditionExpression=Key("pk").eq(user_pk(owner)) & Key("sk").begins_with("DRAFT#"), Select="COUNT")
        if count.get("Count", 0) >= DRAFTS_MAX:
            raise ApiError(409, "too_many_drafts")
    now = now_iso()
    table().put_item(Item={"pk": user_pk(owner), "sk": sk, "key": key, "data": data, "etag": etag, "modified": now})
    return {"key": key, "modified": now}


def drop_draft(owner, key):
    return {"Delete": {"TableName": table_name(), "Key": marshal_item({"pk": user_pk(owner), "sk": "DRAFT#" + key})}}


# A version from the history becomes the card again: over the current card
# (with its etag), or in place of a deleted one.
def restore_contact(owner, contact_id, payload):
    revision = str(payload.get("revision") or "")
    if not REV_RE.fullmatch(revision) or not revision.startswith(f"REV#{contact_id}#"):
        raise ApiError(400, "revision_not_found")
    rev = table().get_item(Key={"pk": user_pk(owner), "sk": revision}).get("Item")
    if not rev:
        raise ApiError(404, "revision_not_found")
    fields = {key: rev[key] for key in FIELDS if key in rev}
    fields = clean_contact(plain(fields), owner, blank_ok=True)
    old = get_current(owner, contact_id)
    now = now_iso()
    if old:
        if old.get("etag") != clean_etag(payload.get("etag")):
            raise ApiError(409, "changed")
        changes = changed_fields(old, fields)
        if not changes:
            return public_contact(old)
        item = current_item(owner, contact_id, fields, old.get("created") or now, now, rev.get("photo_hash", ""), old.get("dav", ""))
        transact([
            put_action(item, "etag = :etag", {":etag": old["etag"]}),
            put_action(revision_item(item, "restore", now, owner, changes, {"from": rev.get("at", "")}), None),
        ])
        return public_contact(item)
    if count_contacts(owner) >= CONTACTS_MAX:
        raise ApiError(409, "too_many_contacts")
    created = first_created(owner, contact_id) or now
    item = current_item(owner, contact_id, fields, created, now, rev.get("photo_hash", ""), rev.get("dav", ""))
    transact([
        put_action(item, "attribute_not_exists(pk)"),
        put_action(revision_item(item, "restore", now, owner, changed_fields({}, fields), {"from": rev.get("at", "")}), None),
        {"Delete": {"TableName": table_name(), "Key": marshal_item({"pk": user_pk(owner), "sk": "DELETED#" + contact_id})}},
    ])
    return public_contact(item)


def first_created(owner, contact_id):
    from boto3.dynamodb.conditions import Key

    page = table().query(
        KeyConditionExpression=Key("pk").eq(user_pk(owner)) & Key("sk").begins_with(f"REV#{contact_id}#"),
        ProjectionExpression="created",
        Limit=1,
    )
    items = page.get("Items", [])
    return items[0].get("created") if items else None


def revisions(owner, contact_id):
    from boto3.dynamodb.conditions import Key

    page = table().query(
        KeyConditionExpression=Key("pk").eq(user_pk(owner)) & Key("sk").begins_with(f"REV#{contact_id}#"),
        ScanIndexForward=False,
        Limit=REVISIONS_MAX,
    )
    return [public_revision(item) for item in page.get("Items", [])]


def deleted_contacts(owner):
    from boto3.dynamodb.conditions import Key

    items, kwargs = [], {"KeyConditionExpression": Key("pk").eq(user_pk(owner)) & Key("sk").begins_with("DELETED#")}
    while True:
        page = table().query(**kwargs)
        items.extend({
            "id": item["id"], "name": item.get("name", ""), "photo": item.get("photo", ""), "at": item.get("at", ""),
            # The card a merged one went into.
            **({"into": item["into"]} if item.get("into") else {}),
            **dav_of(item),
        } for item in page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return sorted(items, key=lambda item: item["at"], reverse=True)


# A deleted card goes for good, with its history. Its photos stay in S3 only
# as old versions, which the lifecycle rule removes after 30 days.
def purge_contact(owner, contact_id):
    from boto3.dynamodb.conditions import Key

    if not table().get_item(Key={"pk": user_pk(owner), "sk": "DELETED#" + contact_id}).get("Item"):
        raise ApiError(409, "exists")
    keys = [{"pk": user_pk(owner), "sk": "DELETED#" + contact_id}, {"pk": user_pk(owner), "sk": "DRAFT#" + contact_id}]
    photos = set()
    kwargs = {
        "KeyConditionExpression": Key("pk").eq(user_pk(owner)) & Key("sk").begins_with(f"REV#{contact_id}#"),
        "ProjectionExpression": "pk, sk, photo",
    }
    while True:
        page = table().query(**kwargs)
        for item in page.get("Items", []):
            keys.append({"pk": item["pk"], "sk": item["sk"]})
            if item.get("photo"):
                photos.add(item["photo"])
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    with table().batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key=key)
    used = {item.get("photo") for item in all_current(owner, ["photo"])}
    for photo in photos - used:
        s3().delete_object(Bucket=env("PHOTOS_BUCKET"), Key=photo.lstrip("/"))
    return {"purged": contact_id, "versions": len(keys) - 2}


# The name a CardDAV client gave the card (carddav.py), kept through every
# save so that the client finds it under that name.
def dav_of(item):
    return {"dav": item["dav"]} if item.get("dav") else {}


def current_item(owner, contact_id, fields, created, modified, photo_hash, dav=""):
    item = {
        **fields,
        "pk": user_pk(owner),
        "sk": "CONTACT#" + contact_id,
        "id": contact_id,
        "created": created,
        "modified": modified,
        "etag": uuid.uuid4().hex,
    }
    if photo_hash and fields.get("photo"):
        item["photo_hash"] = photo_hash
    if dav:
        item["dav"] = dav
    return item


def revision_item(item, action, now, who, changes, extra=None):
    contact_id = item["id"]
    rev = {
        "pk": item["pk"],
        # Microseconds keep two saves made within one millisecond in order.
        "sk": f"REV#{contact_id}#{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}#{uuid.uuid4().hex[:8]}",
        "id": contact_id,
        "action": action,
        "at": now,
        "by": who,
        "changes": changes,
    }
    for key in FIELDS + ["created", "modified", "photo_hash", "dav"]:
        if key in item:
            rev[key] = item[key]
    rev.update(extra or {})
    return rev


def changed_fields(old, new):
    return [key for key in FIELDS if empty_as_none(plain(old.get(key))) != empty_as_none(new.get(key))]


def empty_as_none(value):
    return value if value not in ("", [], False, None) else None


def public_contact(item):
    contact = {key: plain(item[key]) for key in FIELDS if key in item}
    for key in LIST_FIELDS:
        contact.setdefault(key, [])
    contact["starred"] = bool(item.get("starred"))
    contact.update({"id": item["id"], "etag": item.get("etag", ""), "created": item.get("created", ""), "modified": item.get("modified", "")})
    return contact


def public_revision(item):
    revision = public_contact(item)
    revision.pop("etag", None)
    revision.update({
        "revision": item["sk"],
        "action": item.get("action", "save"),
        "at": item.get("at", ""),
        "by": item.get("by", ""),
        "changes": list(item.get("changes") or []),
    })
    if item.get("from"):
        revision["from"] = item["from"]
    return revision


def first_value(item):
    for key in ("emails", "phones"):
        for entry in item.get(key) or []:
            if entry.get("value"):
                return entry["value"]
    return ""


# Checking a card -----------------------------------------------------------------

def clean_contact(payload, owner, blank_ok=False):
    if not isinstance(payload, dict):
        raise ApiError(400, "bad_json_object")
    contact = {}
    for key, limit in TEXT_FIELDS.items():
        value = payload.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise ApiError(400, "bad_field", key)
        value = value.replace("\r\n", "\n").strip()
        value = value if key == "notes" else re.sub(r"\s+", " ", value)
        value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
        if len(value) > limit:
            raise ApiError(400, "bad_field", key)
        if value:
            contact[key] = value
    for key in ("birthday", "hidden_birthday"):
        if key in contact and not DATE_RE.fullmatch(contact[key]):
            raise ApiError(400, "bad_date", key)
    # A birthday given again by a device or Google shows the card in the
    # calendars once more.
    if "birthday" in contact:
        contact.pop("hidden_birthday", None)
    if "photo" in contact and not (PHOTO_RE.fullmatch(contact["photo"]) and contact["photo"].split("/")[2] == user_key(owner)):
        raise ApiError(400, "bad_photo", "photo")
    for key, parts in LIST_FIELDS.items():
        entries = payload.get(key) or []
        if not isinstance(entries, list) or len(entries) > LIST_MAX:
            raise ApiError(400, "bad_field", key)
        clean = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ApiError(400, "bad_field", key)
            row = {}
            for part, limit in parts.items():
                value = entry.get(part)
                if value in (None, ""):
                    continue
                if not isinstance(value, str):
                    raise ApiError(400, "bad_field", key)
                value = value.strip() if part == "street" else re.sub(r"\s+", " ", value).strip()
                value = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", "", value)
                if len(value) > limit:
                    raise ApiError(400, "bad_field", key)
                if value:
                    row[part] = value
            if not any(value for part, value in row.items() if part not in ("label", "service", "country_code")):
                continue
            if row.get("contact") and not ID_RE.fullmatch(row["contact"]):
                raise ApiError(400, "bad_field", key)
            if key == "dates":
                if not DATE_RE.fullmatch(row.get("value", "")):
                    raise ApiError(400, "bad_date", key)
            if "label" in parts:
                row.setdefault("label", "other")
            clean.append(row)
        if clean:
            contact[key] = clean
    starred = payload.get("starred", False)
    if not isinstance(starred, bool):
        raise ApiError(400, "bad_flag", "starred")
    if starred:
        contact["starred"] = True
    # Apple Contacts sends a new card before anything is typed in it, so a
    # card from CardDAV may be blank; the page asks for at least a name.
    if not blank_ok and not any(contact.get(key) for key in ("first", "last", "middle", "nickname", "company", "phones", "emails")):
        raise ApiError(400, "empty_contact")
    return contact


# Search in the history ---------------------------------------------------------

def fold(text):
    return str(text or "").lower().replace("ё", "е")


def digits(text):
    return re.sub(r"\D", "", str(text or ""))


def haystack(item):
    """The text of a card in which a search looks, the same as search() in app.js."""
    words, numbers = [], []

    def walk(value):
        if isinstance(value, dict):
            # The id of a linked card is not text to search.
            for name, part in value.items():
                if name != "contact":
                    walk(part)
        elif isinstance(value, list):
            for part in value:
                walk(part)
        elif isinstance(value, str):
            words.append(value)
    for key in FIELDS:
        if key in ("photo", "uid", "starred"):
            continue
        walk(plain(item.get(key)))
    for entry in item.get("phones") or []:
        numbers.append(phone_digits(entry.get("value")))
    return fold(" ".join(words)), " ".join(numbers)


def phone_digits(text):
    """The digits of a number; a Russian 8 at the start reads as +7."""
    number = digits(text)
    return "7" + number[1:] if len(number) == 11 and number.startswith("8") else number


def phone_match(query, numbers):
    """A query that looks like a phone number finds it by its digits: +7 916 123
    finds 8 (916) 123-45-67."""
    if not re.fullmatch(r"[\d\s()+.-]+", query):
        return False
    number = digits(query)
    if len(number) < 3:
        return False
    wanted = {number, "7" + number[1:]} if number.startswith("8") else {number}
    return any(want in numbers for want in wanted)


# A query typed in the other keyboard layout: Jkmuf finds Ольга, and
# Ьфкшф finds Maria. The same table as LAYOUT in app.js.
LATIN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./~{}:\"<>"
CYRILLIC = "ёйцукенгшщзхъфывапролджэячсмитьбю.ёхъжэбю"
TO_CYRILLIC = {ord(a): b for a, b in zip(LATIN, CYRILLIC)}
TO_LATIN = {ord(b): a for a, b in zip(LATIN[:34], CYRILLIC[:34])}


def query_variants(query):
    """The folded query as typed and as if typed in the other layout."""
    query = str(query or "").lower().strip()
    variants = []
    for text in (query, query.translate(TO_CYRILLIC), query.translate(TO_LATIN)):
        text = fold(text)
        words = text.split()[:8]
        if words and (text, words) not in variants:
            variants.append((text, words))
    return variants


def matches(variants, text, numbers):
    for query, words in variants:
        if phone_match(query, numbers) or all(word in text or phone_match(word, numbers) for word in words):
            return True
    return False


# Every version of every card, newest first; a card is found once, by its
# newest matching version. A card that is found by its current text is in the
# page's own results already, but is listed too, so that the page can say
# which version matched.
def search_history(owner, query):
    from boto3.dynamodb.conditions import Key

    variants = query_variants(query)
    if not variants:
        return []
    found = {}
    deleted = {item["id"]: item for item in deleted_contacts(owner)}
    kwargs = {"KeyConditionExpression": Key("pk").eq(user_pk(owner)) & Key("sk").begins_with("REV#")}
    while True:
        page = table().query(**kwargs)
        for item in page.get("Items", []):
            text, numbers = haystack(item)
            if not matches(variants, text, numbers):
                continue
            entry = found.get(item["id"])
            if entry and entry["at"] >= item.get("at", ""):
                entry["versions"] += 1
                continue
            found[item["id"]] = {
                "id": item["id"],
                "name": vcard.display_name(plain(item)) or first_value(item),
                "photo": item.get("photo", ""),
                "revision": item["sk"],
                "action": item.get("action", "save"),
                "at": item.get("at", ""),
                "versions": (entry or {}).get("versions", 0) + 1,
                "deleted": item["id"] in deleted,
            }
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return sorted(found.values(), key=lambda entry: entry["at"], reverse=True)[:SEARCH_MAX]


# Photos ----------------------------------------------------------------------------

def photo_max_bytes():
    return as_int(os.environ.get("PHOTO_MAX_BYTES"), 2_097_152)


def photo_key(owner, content_type):
    return f"photos/{user_key(owner)}/{uuid.uuid4().hex}.{PHOTO_TYPES[content_type]}"


def presign_photo(owner, payload):
    content_type = str(payload.get("content_type") or "").lower()
    size = as_int(payload.get("size"))
    if content_type not in PHOTO_TYPES or not 1 <= size <= photo_max_bytes():
        raise ApiError(400, "bad_photo")
    key = photo_key(owner, content_type)
    post = s3().generate_presigned_post(
        Bucket=env("PHOTOS_BUCKET"),
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, photo_max_bytes()]],
        ExpiresIn=300,
    )
    return {"post": post, "path": "/" + key}


def put_photo(owner, data, content_type):
    key = photo_key(owner, content_type)
    s3().put_object(Bucket=env("PHOTOS_BUCKET"), Key=key, Body=data, ContentType=content_type)
    return "/" + key


# vCard ------------------------------------------------------------------------------

def import_vcards(owner, payload):
    """Cards of a vCard file, a part of it at a time (the page splits a big file).

    A card matches an existing one by its UID, else by the same name with a
    shared phone or email (or neither having any); a match is updated with
    the fields of the file, keeping its photo and star when the file has
    none, and is left as it is when nothing differs. The CATEGORIES of a card
    (Google's labels) put it into the groups of those names.
    """
    text = payload.get("vcf")
    if not isinstance(text, str):
        raise ApiError(400, "bad_vcard")
    if len(text.encode("utf-8")) > IMPORT_MAX_BYTES:
        raise ApiError(413, "too_large")
    cards = vcard.split_cards(text)
    if not cards:
        raise ApiError(400, "bad_vcard")
    if len(cards) > IMPORT_CARDS_MAX:
        raise ApiError(413, "too_large")
    existing = list(all_current(owner))
    result = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0, "photos_skipped": 0, "groups": 0, "errors": []}
    group_cards = []
    # The CATEGORIES of each card: (its CardDAV UID, the names).
    categories = []
    for index, props in enumerate(cards):
        if vcard.is_group(props):
            group_cards.append(props)
            continue
        contact, photo = vcard.card_to_contact(props)
        name = vcard.display_name(contact) or first_value(contact)
        try:
            photo_hash = ""
            if photo:
                data, content_type = photo
                if len(data) > photo_max_bytes():
                    result["photos_skipped"] += 1
                    photo = None
                else:
                    photo_hash = hashlib.sha256(data).hexdigest()
            match = find_match(existing, contact)
            if match:
                merged = merge_import(plain(match), contact, {item["id"]: vcard.display_name(plain(item)) for item in existing})
                if photo and match.get("photo_hash") != photo_hash:
                    merged["photo"] = put_photo(owner, *photo)
                else:
                    photo_hash = match.get("photo_hash", "")
                categories.append((dav_uid(match), contact["categories"]))
                if not changed_fields(match, clean_contact(merged, owner)):
                    result["unchanged"] += 1
                    continue
                saved = save_contact(owner, match["id"], {**merged, "etag": match["etag"]}, "import", photo_hash)
                existing[existing.index(match)] = {**saved, "photo_hash": photo_hash}
                result["updated"] += 1
            else:
                if photo:
                    contact["photo"] = put_photo(owner, *photo)
                saved = create_contact(owner, contact, "import", photo_hash)
                categories.append((dav_uid(saved), contact["categories"]))
                existing.append({**saved, "photo_hash": photo_hash})
                result["created"] += 1
        except ApiError as exc:
            result["skipped"] += 1
            if len(result["errors"]) < 20:
                result["errors"].append({"index": index, "name": name, "code": exc.code, "detail": exc.detail})
    # Groups come after the cards: their members are the UIDs of the cards.
    # A group with the same UID, else the same name, gets the members added.
    groups = group_items(owner)
    for props in group_cards:
        name, uid, members = vcard.card_to_group(props)
        if not name:
            continue
        group = next((item for item in groups if uid and item.get("uid") == uid), None) \
            or next((item for item in groups if fold(item.get("name")) == fold(name)), None)
        try:
            if group:
                merged = list(dict.fromkeys(list(group.get("members") or []) + members))
                if merged != list(group.get("members") or []):
                    transact([group_put(group, {"members": merged}, now_iso())])
            else:
                groups.append(store_group(owner, clean_group_name(name), members, uid))
            result["groups"] += 1
        except ApiError as exc:
            if len(result["errors"]) < 20:
                result["errors"].append({"index": -1, "name": name, "code": exc.code, "detail": exc.detail})
    wanted = {}
    for uid, names in categories:
        for name in names:
            wanted.setdefault(name, []).append(uid)
    for name, uids in wanted.items():
        group = next((item for item in groups if fold(item.get("name")) == fold(name)), None)
        try:
            if group:
                merged = list(dict.fromkeys(list(group.get("members") or []) + uids))
                if merged != list(group.get("members") or []):
                    transact([group_put(group, {"members": merged}, now_iso())])
            else:
                groups.append(store_group(owner, clean_group_name(name), list(dict.fromkeys(uids))))
                result["groups"] += 1
        except ApiError as exc:
            if len(result["errors"]) < 20:
                result["errors"].append({"index": -1, "name": name, "code": exc.code, "detail": exc.detail})
    return result


def name_key(contact):
    words = fold(" ".join(contact.get(key, "") for key in ("first", "middle", "last"))).split()
    return words or fold(contact.get("company", "")).split()


def find_match(existing, contact):
    uid = contact.get("uid")
    if uid:
        for item in existing:
            if item.get("uid") == uid:
                return item
    key = name_key(contact)
    phones = {digits(entry["value"])[-9:] for entry in contact.get("phones", []) if digits(entry["value"])}
    emails = {str(entry.get("value", "")).strip().lower() for entry in contact.get("emails", []) if entry.get("value")}
    for item in existing:
        their_key = name_key(item)
        their_phones = {digits(entry.get("value"))[-9:] for entry in item.get("phones") or [] if digits(entry.get("value"))}
        their_emails = {str(entry.get("value", "")).strip().lower() for entry in item.get("emails") or [] if entry.get("value")}
        shared = phones & their_phones or emails & their_emails
        if not key or not their_key:
            # A card without a name, such as one with only an email, is the
            # same person when it shares an email or a phone.
            if shared:
                return item
            continue
        if their_key != key:
            continue
        if shared or not (phones or emails or their_phones or their_emails):
            return item
    return None


def merge_import(current, imported, names=None):
    merged = {key: value for key, value in imported.items() if key in FIELDS and value not in ("", [], None)}
    for key in ("photo", "starred", "custom"):
        if not merged.get(key) and current.get(key):
            merged[key] = current[key]
    if not merged.get("uid") and current.get("uid"):
        merged["uid"] = current["uid"]
    merged["related"] = keep_links(current.get("related"), merged.get("related"), names)
    return merged


def keep_links(old, new, names=None):
    """Related people of a vCard with the card links the old entries had.

    vCard and CardDAV clients know a related person by name only, so a link
    to a card stays with the entry of the same name: the name it was saved
    with, or the name the linked card has now (names: id -> name), which is
    what an export wrote.
    """
    links = {}
    for entry in old or []:
        if entry.get("contact"):
            links[fold(entry.get("value"))] = entry["contact"]
            if names and names.get(entry["contact"]):
                links[fold(names[entry["contact"]])] = entry["contact"]
    result = []
    for entry in new or []:
        entry = dict(entry)
        link = links.get(fold(entry.get("value")))
        if link and not entry.get("contact"):
            entry["contact"] = link
        result.append(entry)
    return result


def export_vcards(owner, payload):
    wanted = payload.get("ids")
    if wanted is not None and (not isinstance(wanted, list) or not all(isinstance(value, str) for value in wanted)):
        raise ApiError(400, "bad_field", "ids")
    wanted = set(wanted) if wanted is not None else None
    every = list(all_current(owner))
    items = [item for item in every if wanted is None or item["id"] in wanted]
    # A related person linked to a card goes by the name the card has now.
    names = {item["id"]: vcard.display_name(plain(item)) for item in every}
    names_by_uid = {}
    for group in group_items(owner):
        for uid in group.get("members") or []:
            names_by_uid.setdefault(uid, []).append(group.get("name", ""))
    items.sort(key=lambda item: fold(vcard.display_name(plain(item))))
    parts = []
    for item in items:
        contact = plain(item)
        photo = None
        if contact.get("photo"):
            try:
                answer = s3().get_object(Bucket=env("PHOTOS_BUCKET"), Key=contact["photo"].lstrip("/"))
                photo = (answer["Body"].read(), answer.get("ContentType") or "image/jpeg")
            except Exception:
                logger.warning("photo %s of %s not read", contact["photo"], item["id"], exc_info=True)
        if not contact.get("uid"):
            # Another address book that reads the file can match it later.
            contact["uid"] = item["id"] + "@" + uid_domain()
        contact["related"] = [{**entry, "value": names.get(entry.get("contact")) or entry.get("value", "")} for entry in contact.get("related") or []]
        # The groups go as CATEGORIES, which Google reads as its labels.
        parts.append(vcard.contact_to_card(contact, photo, names_by_uid.get(dav_uid(item), [])))
    key = f"exports/{user_key(owner)}/{uuid.uuid4().hex}.vcf"
    name = "contacts.vcf" if len(items) != 1 else (vcard.display_name(plain(items[0])) or "contact").replace("/", "-")[:80] + ".vcf"
    s3().put_object(Bucket=env("PHOTOS_BUCKET"), Key=key, Body="".join(parts).encode("utf-8"), ContentType="text/vcard; charset=utf-8")
    url = s3().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": env("PHOTOS_BUCKET"),
            "Key": key,
            # The name in UTF-8 for the browser, and a plain one for the rest.
            "ResponseContentDisposition": f"attachment; filename=\"contacts.vcf\"; filename*=UTF-8''{quote(name)}",
            "ResponseContentType": "text/vcard; charset=utf-8",
        },
        ExpiresIn=300,
    )
    return {"url": url, "count": len(items), "name": name}


# Administration -------------------------------------------------------------

def admin_route(method, parts, event, me):
    params = event.get("queryStringParameters") or {}
    # The demo shows the users; changing them is not for visitors.
    if demo() and method != "GET":
        raise ApiError(403, "demo_readonly")
    if parts == ["users"] and method == "GET":
        return response(200, {"items": list_users(me)})
    if parts == ["users"] and method == "POST":
        payload = body_json(event)
        return response(201, create_user(clean_email(payload.get("email")), payload.get("admin") is True))
    if parts == ["users"] and method == "PUT":
        return response(200, update_user(body_json(event), me))
    if parts == ["users"] and method == "DELETE":
        email = clean_email(params.get("email"))
        if email == me:
            raise ApiError(400, "not_yourself")
        return response(200, delete_user(email))
    raise ApiError(404, "not_found")


# Only the groups Cognito put into the token count.
def is_admin(event):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        # API Gateway passes a list claim as a string such as "[admins other]".
        groups = groups.strip("[]").replace(",", " ").split()
    return admin_group() in groups


def admin_group():
    return os.environ.get("ADMIN_GROUP", "admins")


def list_users(me):
    pool = env("USER_POOL_ID")
    admins = set()
    for page in cognito().get_paginator("list_users_in_group").paginate(UserPoolId=pool, GroupName=admin_group()):
        admins.update(user_email(user) for user in page.get("Users", []))
    users = []
    for page in cognito().get_paginator("list_users").paginate(UserPoolId=pool):
        for user in page.get("Users", []):
            email = user_email(user)
            users.append({
                "email": email,
                "status": user.get("UserStatus", ""),
                "enabled": bool(user.get("Enabled", True)),
                "created": iso(user.get("UserCreateDate")),
                "admin": email in admins,
                "mfa": has_mfa(pool, user.get("Username")),
                "contacts": count_contacts(email),
                "self": email == me,
            })
    users.sort(key=lambda user: user["email"])
    return users


# ListUsers does not say whether a user has an authenticator app, so each user
# is asked for separately. There are a handful of users.
def has_mfa(pool, username):
    user = cognito().admin_get_user(UserPoolId=pool, Username=username)
    return "SOFTWARE_TOKEN_MFA" in (user.get("UserMFASettingList") or [])


def create_user(email, admin):
    pool = env("USER_POOL_ID")
    password = temporary_password()
    try:
        cognito().admin_create_user(
            UserPoolId=pool,
            Username=email,
            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
            TemporaryPassword=password,
            DesiredDeliveryMediums=["EMAIL"],
        )
    except Exception as exc:
        if error_code(exc) == "UsernameExistsException":
            raise ApiError(409, "user_exists") from None
        raise
    if admin:
        cognito().admin_add_user_to_group(UserPoolId=pool, Username=email, GroupName=admin_group())
    return {"email": email, "temporary_password": password}


def update_user(payload, me):
    email = clean_email(payload.get("email"))
    pool = env("USER_POOL_ID")
    own = email == me
    result = {"email": email}
    for flag in ("enabled", "admin", "reset_mfa", "reset_password"):
        if flag in payload and not isinstance(payload[flag], bool):
            raise ApiError(400, "bad_flag")
    try:
        if "enabled" in payload:
            if own and not payload["enabled"]:
                raise ApiError(400, "not_yourself")
            if payload["enabled"]:
                cognito().admin_enable_user(UserPoolId=pool, Username=email)
            else:
                cognito().admin_disable_user(UserPoolId=pool, Username=email)
        if "admin" in payload:
            if own and not payload["admin"]:
                raise ApiError(400, "not_yourself")
            if payload["admin"]:
                cognito().admin_add_user_to_group(UserPoolId=pool, Username=email, GroupName=admin_group())
            else:
                cognito().admin_remove_user_from_group(UserPoolId=pool, Username=email, GroupName=admin_group())
        # A user who lost their phone signs in with the password alone and can
        # set up the app again in the settings.
        if payload.get("reset_mfa"):
            cognito().admin_set_user_mfa_preference(
                UserPoolId=pool,
                Username=email,
                SoftwareTokenMfaSettings={"Enabled": False, "PreferredMfa": False},
            )
        if payload.get("reset_password"):
            password = temporary_password()
            cognito().admin_set_user_password(UserPoolId=pool, Username=email, Password=password, Permanent=False)
            result["temporary_password"] = password
    except ApiError:
        raise
    except Exception as exc:
        if error_code(exc) == "UserNotFoundException":
            raise ApiError(404, "user_not_found") from None
        raise
    return result


# The account goes, and with it the address book, its history and photos.
def delete_user(email):
    from boto3.dynamodb.conditions import Key

    # Google stops syncing the book that goes.
    if os.environ.get("GOOGLE_CLIENT_ID"):
        import google_sync

        if google_sync.state_item(email):
            google_sync.disconnect(email)
    if os.environ.get("ICLOUD_PASSWORD_PREFIX"):
        import icloud_sync

        if icloud_sync.state_item(email):
            icloud_sync.disconnect(email)
    try:
        cognito().admin_delete_user(UserPoolId=env("USER_POOL_ID"), Username=email)
    except Exception as exc:
        if error_code(exc) != "UserNotFoundException":
            raise
    keys = []
    kwargs = {"KeyConditionExpression": Key("pk").eq(user_pk(email)), "ProjectionExpression": "pk, sk"}
    while True:
        page = table().query(**kwargs)
        keys.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    with table().batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key={"pk": key["pk"], "sk": key["sk"]})
    photos = []
    for page in s3().get_paginator("list_objects_v2").paginate(Bucket=env("PHOTOS_BUCKET"), Prefix=f"photos/{user_key(email)}/"):
        photos.extend({"Key": obj["Key"]} for obj in page.get("Contents", []))
    for start in range(0, len(photos), 1000):
        s3().delete_objects(Bucket=env("PHOTOS_BUCKET"), Delete={"Objects": photos[start:start + 1000], "Quiet": True})
    return {"deleted": email, "items": len(keys), "photos": len(photos)}


def temporary_password():
    alphabet = string.ascii_letters + string.digits + PASSWORD_SYMBOLS
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(16))
        if (any(c.islower() for c in password) and any(c.isupper() for c in password)
                and any(c.isdigit() for c in password) and any(c in PASSWORD_SYMBOLS for c in password)):
            return password


def user_email(user):
    for attribute in user.get("Attributes", []):
        if attribute.get("Name") == "email":
            return attribute.get("Value", "").lower()
    return user.get("Username", "")


def clean_email(value):
    email = str(value or "").strip().lower()
    if not EMAIL_RE.fullmatch(email) or len(email) > 254:
        raise ApiError(400, "bad_email")
    return email


# Helpers ----------------------------------------------------------------------

# Only a verified email from the Cognito token counts: an unverified one could
# be any address.
def actor(event):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    address = str(claims.get("email") or "").lower()
    if not address or str(claims.get("email_verified")).lower() != "true":
        raise ApiError(403, "email_not_verified")
    return address


def clean_id(value):
    if not ID_RE.fullmatch(str(value or "")):
        raise ApiError(404, "contact_not_found")
    return value


def clean_etag(value):
    value = str(value or "")
    if not ETAG_RE.fullmatch(value):
        raise ApiError(409, "changed")
    return value


def encode_cursor(key):
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(key, ensure_ascii=False).encode("utf-8")).decode("ascii")


def decode_cursor(value, owner):
    if not value:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ApiError(400, "bad_cursor") from exc
    # A cursor reads on in the caller's own partition only.
    if not isinstance(data, dict) or set(data) != {"pk", "sk"} or data["pk"] != user_pk(owner):
        raise ApiError(400, "bad_cursor")
    return data


def body_json(event, limit=200_000):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if len(raw) > limit:
        raise ApiError(413, "too_large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ApiError(400, "bad_json_object")
    return payload


def response(status, payload):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"},
        "body": json.dumps(plain(payload), ensure_ascii=False),
    }


def plain(value):
    """DynamoDB's Decimals as ints or floats, for JSON."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(item) for item in value]
    return value


def demo():
    return os.environ.get("DEMO", "false") == "true"


# The demo shows the sync with Google and iCloud without running it: both are
# connected, synced a moment ago, with every card linked, and the sync log
# holds the examples of the seed.
DEMO_ACCOUNTS = {"google": ("email", "demo.contacts@gmail.com"), "icloud": ("apple_id", "demo.contacts@icloud.com")}


def demo_sync_route(method, parts, owner):
    kind = parts[0]
    if parts == [kind] and method == "GET":
        now = datetime.now(timezone.utc)
        last = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
        key, account = DEMO_ACCOUNTS[kind]
        return response(200, {
            "enabled": True, "connected": True, key: account, "status": "ok", "last_error": "", "demo": True,
            "last_sync": last.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "linked": sum(1 for _ in all_current(owner, ["id"])),
        })
    if parts == [kind, "sync"] and method == "POST":
        return response(202, {"started": True, "demo": True})
    raise ApiError(403, "demo_readonly")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso(value):
    if not value:
        return ""
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def error_code(exc):
    data = getattr(exc, "response", None)
    return data.get("Error", {}).get("Code", "") if isinstance(data, dict) else ""


def conditional_failed(exc):
    data = getattr(exc, "response", None)
    if not isinstance(data, dict):
        return False
    code = data.get("Error", {}).get("Code", "")
    if code == "ConditionalCheckFailedException":
        return True
    if code != "TransactionCanceledException":
        return False
    return any(reason.get("Code") == "ConditionalCheckFailed" for reason in data.get("CancellationReasons", []))


def put_action(item, condition, values=None):
    action = {"TableName": table_name(), "Item": marshal_item(item)}
    if condition:
        action["ConditionExpression"] = condition
    if values:
        action["ExpressionAttributeValues"] = marshal_item(values)
    return {"Put": action}


def transact(actions):
    dynamodb().transact_write_items(TransactItems=actions)


def marshal_item(item):
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in item.items()}


def env(name):
    value = os.environ.get(name)
    if not value:
        raise ApiError(500, "not_configured")
    return value


def table_name():
    return env("TABLE_NAME")


def table():
    global _table
    if _table is None:
        import boto3
        _table = boto3.resource("dynamodb").Table(table_name())
    return _table


def dynamodb():
    global _dynamodb
    if _dynamodb is None:
        import boto3
        _dynamodb = boto3.client("dynamodb")
    return _dynamodb


def cognito():
    global _cognito
    if _cognito is None:
        import boto3
        _cognito = boto3.client("cognito-idp")
    return _cognito


def s3():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config

        # Sign against the regional endpoint. The global s3.amazonaws.com host
        # answers with a redirect, and a signed POST cannot follow it.
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-central-1"
        _s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3


# Photo cookies -------------------------------------------------------------------

def viewer_host(event):
    headers = {str(key).lower(): value for key, value in (event.get("headers") or {}).items()}
    host = headers.get("x-viewer-host") or ""
    host = host.split(",")[0].strip().split(":")[0]
    if not host or "execute-api" in host:
        return ""
    return host


def attach_photo_cookies(result, event, owner):
    host = viewer_host(event)
    if not host:
        return
    try:
        result["cookies"] = photo_cookies(host, owner)
    except Exception:
        logger.exception("photo cookie was not issued")


# The cookie opens the photos of its user only.
def photo_cookies(host, owner, now=None):
    expires = int(now if now is not None else time.time()) + COOKIE_SECONDS
    policy = json.dumps({
        "Statement": [{
            "Resource": f"https://{host}/photos/{user_key(owner)}/*",
            "Condition": {"DateLessThan": {"AWS:EpochTime": expires}},
        }]
    }, separators=(",", ":")).encode("utf-8")
    signature = rsa_sha1_sign(policy, signing_key())
    attributes = f"Path=/photos; Secure; HttpOnly; SameSite=Lax; Max-Age={COOKIE_SECONDS}"
    return [
        f"CloudFront-Policy={cloudfront_b64(policy)}; {attributes}",
        f"CloudFront-Signature={cloudfront_b64(signature)}; {attributes}",
        f"CloudFront-Key-Pair-Id={env('CLOUDFRONT_KEY_PAIR_ID')}; {attributes}",
    ]


def cloudfront_b64(data):
    return base64.b64encode(data).decode("ascii").replace("+", "-").replace("=", "_").replace("/", "~")


def signing_key():
    global _signing_key
    if _signing_key is None:
        import boto3

        parameter = boto3.client("ssm").get_parameter(Name=env("PHOTOS_KEY_PARAMETER_NAME"), WithDecryption=True)
        _signing_key = load_rsa_private_key(parameter["Parameter"]["Value"])
    return _signing_key


# CloudFront wants RSASSA-PKCS1-v1_5 with SHA-1. The runtime has no crypto
# package, so the key is read from its DER form and signed with plain pow().
def der_read(data, offset):
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    return tag, data[offset:offset + length], offset + length


def der_sequence(data):
    items = []
    offset = 0
    while offset < len(data):
        tag, value, offset = der_read(data, offset)
        items.append((tag, value))
    return items


def load_rsa_private_key(pem):
    lines = [line.strip() for line in pem.strip().splitlines()]
    der = base64.b64decode("".join(line for line in lines if line and not line.startswith("-----")))
    _, body, _ = der_read(der, 0)
    items = der_sequence(body)
    if len(items) == 3 and items[2][0] == 0x04:
        # PKCS#8 wraps the PKCS#1 key in an OCTET STRING.
        _, body, _ = der_read(items[2][1], 0)
        items = der_sequence(body)
    numbers = [int.from_bytes(value, "big") for tag, value in items if tag == 0x02]
    _, n, e, d, p, q, dp, dq, qinv = numbers[:9]
    return {"n": n, "e": e, "d": d, "p": p, "q": q, "dp": dp, "dq": dq, "qinv": qinv}


def rsa_sha1_sign(message, key):
    size = (key["n"].bit_length() + 7) // 8
    digest = SHA1_DIGEST_INFO + hashlib.sha1(message).digest()
    encoded = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    m = int.from_bytes(encoded, "big")
    s1 = pow(m, key["dp"], key["p"])
    s2 = pow(m, key["dq"], key["q"])
    h = (key["qinv"] * (s1 - s2)) % key["p"]
    return (s2 + h * key["q"]).to_bytes(size, "big")


# Versions -----------------------------------------------------------------

# The version of this deployment (VERSION) and the releases of the demo
# repository (RELEASES_REPOSITORY, owner/name) that came after it. They come
# from the Atom feed of the repository's GitHub releases, a public page that
# the hourly limit of the REST API, shared by every function behind Lambda's
# addresses, does not touch. The list stays an hour in the warm function, and
# the last one read serves while GitHub does not answer.
RELEASES_SECONDS = 3600
VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
ATOM = "{http://www.w3.org/2005/Atom}"
_releases = None


def version_info():
    current = os.environ.get("VERSION", "")
    repository = os.environ.get("RELEASES_REPOSITORY", "")
    info = {
        "version": current,
        "repository": repository,
        "url": f"https://github.com/{repository}/releases" if repository else None,
        "latest": None,
        "newer": [],
        "checked_at": None,
        "error": None,
    }
    if not repository:
        return info
    try:
        releases, checked_at = github_releases(repository)
    except Exception:
        logger.exception("releases of %s not read", repository)
        info["error"] = "unavailable"
        return info
    info["checked_at"] = checked_at
    info["latest"] = releases[0]["version"] if releases else None
    info["newer"] = [release for release in releases if version_key(release["version"]) > version_key(current)]
    return info


def version_key(value):
    match = VERSION_RE.match(value or "")
    return tuple(int(part) for part in match.groups()) if match else ()


def github_releases(repository):
    global _releases
    now = time.time()
    if _releases and _releases[0] == repository and now - _releases[1] < RELEASES_SECONDS:
        return _releases[2], _releases[3]
    try:
        request = urllib.request.Request(
            f"https://github.com/{repository}/releases.atom",
            headers={"Accept": "application/atom+xml", "User-Agent": "release-check (+https://github.com/" + repository + ")"},
        )
        with urllib.request.urlopen(request, timeout=5) as answer:
            releases = parse_releases(answer.read(2_000_000))
    except Exception:
        if _releases and _releases[0] == repository:
            logger.warning("releases of %s not read; the list of %s serves", repository, _releases[3], exc_info=True)
            return _releases[2], _releases[3]
        raise
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    _releases = (repository, now, releases, checked_at)
    return releases, checked_at


# The entries of the feed whose tag is a version, newest version first, with
# their notes as plain text.
def parse_releases(feed):
    import xml.etree.ElementTree as ElementTree

    releases = []
    for entry in ElementTree.fromstring(feed).findall(ATOM + "entry"):
        link = entry.find(ATOM + "link")
        url = link.get("href", "") if link is not None else ""
        version = unquote(url.rstrip("/").rsplit("/", 1)[-1])
        if not version_key(version):
            continue
        releases.append({
            "version": version,
            "title": (entry.findtext(ATOM + "title") or version).strip(),
            "date": (entry.findtext(ATOM + "updated") or "")[:10],
            "url": url,
            "notes": release_notes(entry.findtext(ATOM + "content") or "")[:4000],
        })
    releases.sort(key=lambda release: version_key(release["version"]), reverse=True)
    return releases


class ReleaseNotes(html.parser.HTMLParser):
    BREAKS = {"p", "div", "br", "ul", "ol", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n• ")
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def release_notes(value):
    parser = ReleaseNotes()
    parser.feed(value)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
