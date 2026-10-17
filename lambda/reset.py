"""Hourly reset of the public demo: back to a fresh deployment.

- Every item of the table goes: the address books, their history, groups,
  drafts, app passwords and sync logs; so do the photos and exports of the
  visitors.
- The examples of seed.json are written again through the code of the API,
  with times counted back from now, so the demo always looks recent: the
  cards with their earlier versions, a deleted card, the groups, the app
  passwords of a few devices and the sync log of Google, iCloud and devices.
- The demo user gets the published password back, two-factor authentication
  off, the default language, the administrator role, and is signed out
  everywhere; any other user is deleted.
"""

import json
import logging
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import contacts as app
import vcard

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Next to the code in the function's zip; in the repository, in seed/.
SEED = next(path for path in (Path(__file__).with_name("seed.json"), Path(__file__).parent.parent / "seed" / "seed.json")
            if path.exists())

_ssm = None


def handler(event, context):
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    removed = wipe_table()
    photos = wipe_photos()
    owner = os.environ["DEMO_EMAIL"].lower()
    written = write_seed(owner, seed, now)
    reset_users(owner)
    logger.info("reset: %s items and %s files removed, %s written", removed, photos, json.dumps(written))
    return {"removed": removed, "photos": photos, **written}


def wipe_table():
    keys = []
    kwargs = {"ProjectionExpression": "pk, sk"}
    while True:
        page = app.table().scan(**kwargs)
        keys.extend({"pk": item["pk"], "sk": item["sk"]} for item in page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    with app.table().batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key=key)
    return len(keys)


def wipe_photos():
    bucket = os.environ["PHOTOS_BUCKET"]
    keys = []
    for page in app.s3().get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=""):
        keys.extend({"Key": item["Key"]} for item in page.get("Contents", []))
    for start in range(0, len(keys), 1000):
        app.s3().delete_objects(Bucket=bucket, Delete={"Objects": keys[start:start + 1000], "Quiet": True})
    return len(keys)


def iso(moment):
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@contextmanager
def at(moment):
    """The API's own writes, dated as if they happened at that moment."""
    real = app.now_iso
    app.now_iso = lambda: iso(moment)
    try:
        yield
    finally:
        app.now_iso = real


def write_seed(owner, seed, now):
    ago = lambda minutes: now - timedelta(minutes=minutes)  # noqa: E731
    ids, versions = {}, 0
    for entry in seed.get("contacts", []):
        card = resolve(entry["card"], ids)
        first = {**card, **resolve(entry.get("first_version", {}), ids)}
        with at(ago(entry.get("created_minutes_ago", 0))):
            made = app.create_contact(owner, first)
        ids[entry["key"]] = made["id"]
        state = dict(first)
        for version in entry.get("versions", []):
            state.update(resolve(version["set"], ids))
            current = app.get_current(owner, made["id"])
            with at(ago(version["minutes_ago"])):
                app.save_contact(owner, made["id"], {**state, "etag": current["etag"]})
            versions += 1
        if state != card:
            current = app.get_current(owner, made["id"])
            app.save_contact(owner, made["id"], {**card, "etag": current["etag"]})
        if "deleted_minutes_ago" in entry:
            current = app.get_current(owner, made["id"])
            with at(ago(entry["deleted_minutes_ago"])):
                app.delete_contact(owner, made["id"], current["etag"])
    uids = app.card_uids(owner)
    for group in seed.get("groups", []):
        app.store_group(owner, group["name"], [uids[ids[key]] for key in group["members"] if ids[key] in uids])
    for device in seed.get("app_passwords", []):
        with at(ago(device.get("created_minutes_ago", 0))):
            made = app.create_app_password(owner, {"name": device["name"]})
        if "used_minutes_ago" in device:
            app.table().update_item(Key={"pk": app.user_pk(owner), "sk": "APPPASS#" + made["id"]},
                                    UpdateExpression="SET used = :u", ConditionExpression="attribute_exists(pk)",
                                    ExpressionAttributeValues={":u": iso(ago(device["used_minutes_ago"]))})
    names = {item["id"]: vcard.display_name(app.plain(item)) or app.first_value(item) for item in app.all_current(owner)}
    names.update({key: value for key, value in deleted_names(owner).items() if key not in names})
    lines = sorted(seed.get("sync_log", []), key=lambda line: -line["minutes_ago"])
    for line in lines:
        write_log(owner, line, ago(line["minutes_ago"]), ids, names)
    return {"contacts": len(ids), "versions": versions, "groups": len(seed.get("groups", [])), "log": len(lines)}


def resolve(fields, ids):
    """Fields of the seed: a related person given "to" another example is
    linked to its card, under its name."""
    fields = json.loads(json.dumps(fields))
    if "related" in fields:
        related = []
        for entry in fields["related"]:
            target = entry.pop("to", None)
            if target:
                item = app.plain(app.table().get_item(Key={"pk": "USER#" + os.environ["DEMO_EMAIL"].lower(), "sk": "CONTACT#" + ids[target]})["Item"])
                entry = {**entry, "value": vcard.display_name(item), "contact": ids[target]}
            related.append(entry)
        fields["related"] = related
    return fields


def deleted_names(owner):
    from boto3.dynamodb.conditions import Key

    page = app.table().query(KeyConditionExpression=Key("pk").eq(app.user_pk(owner)) & Key("sk").begins_with("DELETED#"))
    return {item["id"]: item.get("name", "") for item in page.get("Items", [])}


def write_log(owner, line, moment, ids, names):
    contact_id = ids.get(line.get("contact", ""), "")
    item = {
        "pk": app.user_pk(owner),
        "sk": f"SYNCLOG#{moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}#{uuid.uuid4().hex[:6]}",
        "at": iso(moment),
        "source": line["source"],
        "action": line["action"],
        "target": line.get("target", "book"),
        "expires": int(moment.timestamp()) + app.SYNC_LOG_DAYS * 86400,
    }
    name = line.get("name") or names.get(contact_id, "")
    for key, value in (("name", name), ("detail", line.get("detail", "")), ("contact", contact_id)):
        if value:
            item[key] = value
    if line.get("fields"):
        item["fields"] = list(line["fields"])
    app.table().put_item(Item=item)


def reset_users(owner):
    pool = os.environ["USER_POOL_ID"]
    cognito = app.cognito()
    username = None
    for page in cognito.get_paginator("list_users").paginate(UserPoolId=pool):
        for user in page.get("Users", []):
            address = next((attribute["Value"] for attribute in user.get("Attributes", []) if attribute["Name"] == "email"), "")
            if address.lower() == owner and username is None:
                username = user["Username"]
            else:
                cognito.admin_delete_user(UserPoolId=pool, Username=user["Username"])
    if username is None:
        username = owner
        cognito.admin_create_user(
            UserPoolId=pool,
            Username=owner,
            MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": owner}, {"Name": "email_verified", "Value": "true"}],
        )
    password = ssm().get_parameter(Name=os.environ["DEMO_PASSWORD_PARAMETER"], WithDecryption=True)["Parameter"]["Value"]
    cognito.admin_set_user_password(UserPoolId=pool, Username=username, Password=password, Permanent=True)
    cognito.admin_enable_user(UserPoolId=pool, Username=username)
    cognito.admin_set_user_mfa_preference(
        UserPoolId=pool,
        Username=username,
        SoftwareTokenMfaSettings={"Enabled": False, "PreferredMfa": False},
    )
    cognito.admin_add_user_to_group(UserPoolId=pool, Username=username, GroupName=app.admin_group())
    try:
        cognito.admin_delete_user_attributes(UserPoolId=pool, Username=username, UserAttributeNames=["locale"])
    except Exception:
        logger.info("no language to clear")
    cognito.admin_user_global_sign_out(UserPoolId=pool, Username=username)


def ssm():
    global _ssm
    if _ssm is None:
        import boto3
        _ssm = boto3.client("ssm")
    return _ssm
