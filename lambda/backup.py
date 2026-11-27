"""The full export of an address book: a ZIP archive of everything it holds.

Settings → Export starts it with POST /api/backup; the API (contacts.py)
keeps the state as sk = BACKUP and starts this function in the background,
since a book with many photos takes longer than API Gateway waits. The page
asks GET /api/backup until the archive is there and then downloads it
through a presigned link; the archive is under exports/<user>/, which S3
removes after a day.

The archive:
  README.txt           what is where
  contacts.vcf         every card as vCard 3.0 with its photo and groups,
                       for Apple Contacts, Google Contacts and the rest
  contacts.json        every card with all its fields, as the API gives it
  history.json         every version of every card, the deleted ones too
  trash.json           the deleted cards
  groups.json          the groups and their members (card ids)
  drafts.json          edits not saved yet
  sync-log.json        the sync log (DynamoDB keeps 90 days of it)
  devices.json         the names of the app passwords (not the passwords)
  photos/<file>        every photo a card or a version names, under the
                       file name of its path ("photo" of a card)
"""

import json
import logging
import os
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone

import contacts
import vcard

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The archive stays as long as the exports of the bucket (s3.tf).
KEEP_HOURS = 24
LINK_SECONDS = 3600


def handler(event, context):
    owner, backup_id = event.get("owner"), event.get("id")
    if not owner or not backup_id:
        return {"skipped": "no owner"}
    return build(owner, backup_id)


# State ------------------------------------------------------------------------

def state_key(owner):
    return {"pk": contacts.user_pk(owner), "sk": "BACKUP"}


def state(owner):
    return contacts.table().get_item(Key=state_key(owner), ConsistentRead=True).get("Item") or {}


def save_state(owner, item):
    contacts.table().put_item(Item={**item, **state_key(owner)})


def start(owner):
    """POST /api/backup: a new archive, built in the background."""
    current = state(owner)
    if current.get("status") == "running" and current.get("started", "") > iso(datetime.now(timezone.utc) - timedelta(minutes=20)):
        return public_state(current)
    backup_id = os.urandom(8).hex()
    item = {"id": backup_id, "status": "running", "started": contacts.now_iso()}
    save_state(owner, item)
    name = os.environ.get("BACKUP_FUNCTION", "")
    if name:
        contacts.lambda_client().invoke(FunctionName=name, InvocationType="Event",
                                        Payload=json.dumps({"owner": owner, "id": backup_id}).encode())
    else:
        # Without the function (tests, a local run) the archive is built at once.
        build(owner, backup_id)
    return public_state(state(owner))


def public_state(item):
    """GET /api/backup: the state, and a link while the archive is there."""
    result = {key: contacts.plain(item[key]) for key in ("id", "status", "started", "finished", "size", "counts", "error") if key in item}
    if not item:
        return {"status": "none"}
    if item.get("status") == "done":
        finished = parse(item.get("finished"))
        if not finished or finished + timedelta(hours=KEEP_HOURS) < datetime.now(timezone.utc):
            return {**result, "status": "expired"}
        result["url"] = contacts.s3().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": contacts.env("PHOTOS_BUCKET"),
                "Key": item["key"],
                "ResponseContentDisposition": f'attachment; filename="{item["name"]}"',
                "ResponseContentType": "application/zip",
            },
            ExpiresIn=LINK_SECONDS,
        )
        result["name"] = item["name"]
    return result


# The archive -------------------------------------------------------------------

def build(owner, backup_id):
    try:
        path, counts = write_archive(owner)
        size = os.path.getsize(path)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"exports/{contacts.user_key(owner)}/{backup_id}.zip"
        contacts.s3().upload_file(path, contacts.env("PHOTOS_BUCKET"), key, ExtraArgs={"ContentType": "application/zip"})
        os.remove(path)
    except Exception as exc:
        logger.exception("backup of %s failed", owner)
        if state(owner).get("id") == backup_id:
            save_state(owner, {"id": backup_id, "status": "error", "started": state(owner).get("started", ""),
                               "finished": contacts.now_iso(), "error": str(exc)[:300]})
        return {"error": str(exc)[:300]}
    if state(owner).get("id") == backup_id:
        save_state(owner, {
            "id": backup_id, "status": "done", "started": state(owner).get("started", ""), "finished": contacts.now_iso(),
            "key": key, "name": f"contacts-backup-{stamp}.zip", "size": size, "counts": counts,
        })
    return {"size": size, "counts": counts}


def items_with_prefix(owner, prefix):
    from boto3.dynamodb.conditions import Key

    kwargs = {"KeyConditionExpression": Key("pk").eq(contacts.user_pk(owner)) & Key("sk").begins_with(prefix)}
    while True:
        page = contacts.table().query(**kwargs)
        yield from page.get("Items", [])
        if not page.get("LastEvaluatedKey"):
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def card_json(item):
    card = contacts.public_contact(item)
    for key in ("dav", "photo_hash"):
        if item.get(key):
            card[key] = item[key]
    return card


def write_archive(owner):
    cards = [contacts.plain(item) for item in contacts.all_current(owner)]
    cards.sort(key=lambda item: contacts.fold(vcard.display_name(item)))
    versions = [contacts.plain(item) for item in items_with_prefix(owner, "REV#")]
    ids_by_uid = {contacts.dav_uid(item): item["id"] for item in cards}
    groups = [{
        "id": group["id"], "name": group.get("name", ""), "uid": group.get("uid", ""),
        "members": [ids_by_uid[uid] for uid in group.get("members") or [] if uid in ids_by_uid],
        # Members that are deleted cards, by their CardDAV UID.
        "other_members": [uid for uid in group.get("members") or [] if uid not in ids_by_uid],
        "created": group.get("created", ""), "modified": group.get("modified", ""),
    } for group in (contacts.plain(item) for item in contacts.group_items(owner))]
    history = {}
    for item in versions:
        history.setdefault(item["id"], []).append(contacts.public_revision(item))
    for entries in history.values():
        entries.sort(key=lambda entry: entry["revision"])
    trash = contacts.deleted_contacts(owner)
    drafts = contacts.list_drafts(owner)
    log = [{key: contacts.plain(item[key]) for key in ("at", "source", "action", "target", "name", "fields", "detail", "contact") if key in item}
           for item in items_with_prefix(owner, "SYNCLOG#")]
    log.sort(key=lambda line: line.get("at", ""))
    devices = contacts.list_app_passwords(owner)
    photos = sorted({item["photo"] for item in cards + versions + [entry["card"] for entry in trash] if item.get("photo")})

    handle, path = tempfile.mkstemp(suffix=".zip")
    os.close(handle)
    stored = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        def put_json(name, data):
            archive.writestr(name, json.dumps(data, ensure_ascii=False, indent=1, default=str))

        archive.writestr("contacts.vcf", contacts.vcard_file(owner, cards))
        put_json("contacts.json", [card_json(item) for item in cards])
        put_json("history.json", history)
        put_json("trash.json", trash)
        put_json("groups.json", groups)
        put_json("drafts.json", drafts)
        put_json("sync-log.json", log)
        put_json("devices.json", [{"name": item["name"], "created": item["created"], "used": item["used"]} for item in devices])
        for photo in photos:
            try:
                answer = contacts.s3().get_object(Bucket=contacts.env("PHOTOS_BUCKET"), Key=photo.lstrip("/"))
            except Exception:
                # A photo of an old version that S3 no longer keeps.
                logger.info("photo %s not in the bucket", photo)
                continue
            # Photos are compressed already.
            archive.writestr("photos/" + photo.rsplit("/", 1)[-1], answer["Body"].read(), zipfile.ZIP_STORED)
            stored += 1
        counts = {"contacts": len(cards), "versions": len(versions), "deleted": len(trash), "groups": len(groups),
                  "photos": stored, "log": len(log)}
        archive.writestr("README.txt", README.format(at=contacts.now_iso(), owner=owner, **counts))
    return path, counts


README = """Address book of {owner}, exported {at}

contacts.vcf    {contacts} cards as vCard 3.0, with photos and groups: import it into
                Apple Contacts, Google Contacts, Outlook, or back into the address book
contacts.json   the same cards with every field, as the address book keeps them
history.json    {versions} versions of the cards, by card id, oldest first
trash.json      {deleted} deleted cards, as they were deleted
groups.json     {groups} groups; members are card ids
drafts.json     edits not saved yet
sync-log.json   {log} lines of the sync log
devices.json    the devices with an app password (names only)
photos/         {photos} photos; a card's "photo" ends with the file name here
"""


def iso(moment):
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
