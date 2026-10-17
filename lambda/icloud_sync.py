"""Sync of the address book with iCloud Contacts over CardDAV.

Apple has no API for contacts other than the CardDAV server of iCloud
(contacts.icloud.com), which takes the Apple ID and an app-specific password.
The functions here are a CardDAV client of it; the cards go both ways as
vCard (vcard.py), the groups as the group cards of Apple.

The address book is the source of truth, as with Google: a field that a
device of Apple drops stays here (carddav.merge_device_card), and a card
changed on both sides between two syncs keeps the version here, which goes
to iCloud again.

Connecting. POST /api/icloud with the Apple ID and an app-specific password:
the server is asked for the address book at once, so that a wrong password
is told on the page. The password is an SSM SecureString of its own per user
(ICLOUD_PASSWORD_PREFIX/<user key>).

State, in the user's partition:
  sk = ICLOUD          connected, Apple ID, the URL of the address book, the
                       sync token, the groups (both sides), status, last sync
                       and error;
  sk = ILINK#<id>      a card and its iCloud card: URL, etag, the UID there,
                       the fingerprints of both sides at the last sync (lhash
                       here, rhash there) and the photo of each side.
pk = ICLOUD, sk = <email> lists the users to sync.

The fingerprint of a card here is taken from the card as vCard read back, so
that both sides are compared in the same terms, and each side only with
itself, as with Google. Every change is a line of the sync log and, for a
card, a version in its history with the action "icloud".
"""

import base64
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ElementTree

import carddav
import contacts
import vcard

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ICLOUD_URL = "https://contacts.icloud.com/"
LOCK_SECONDS = 280
# Writes of one sync; the rest go with the next one.
WRITES_PER_SYNC = 300
MULTIGET = 50
TIMEOUT = 30
NS = {"d": "DAV:", "card": "urn:ietf:params:xml:ns:carddav", "cs": "http://calendarserver.org/ns/"}
# The fields of a card the fingerprint takes; the rest stay here only.
SKIPPED = {"photo", "uid", "starred", "categories"}


class ICloudError(Exception):
    def __init__(self, status, message=""):
        super().__init__(f"{status} {message}".strip())
        self.status = status


def enabled():
    return bool(os.environ.get("ICLOUD_PASSWORD_PREFIX"))


def password_parameter(owner):
    return f"{os.environ['ICLOUD_PASSWORD_PREFIX']}/{contacts.user_key(owner)}"


def state_item(owner):
    return contacts.table().get_item(Key={"pk": contacts.user_pk(owner), "sk": "ICLOUD"}, ConsistentRead=True).get("Item") or {}


def save_state(owner, changes):
    item = {**state_item(owner), **changes, "pk": contacts.user_pk(owner), "sk": "ICLOUD"}
    contacts.table().put_item(Item=item)
    return item


def public_state(owner):
    item = state_item(owner)
    return {
        "enabled": enabled(),
        "connected": bool(item.get("connected")),
        "apple_id": item.get("apple_id", ""),
        "status": item.get("status", ""),
        "last_sync": item.get("last_sync", ""),
        "last_error": item.get("last_error", ""),
        "linked": int(item.get("linked", 0)),
    }


# Connecting ------------------------------------------------------------------------

def connect(owner, payload):
    if not enabled():
        raise contacts.ApiError(409, "icloud_off")
    apple_id = str(payload.get("apple_id") or "").strip()
    password = str(payload.get("password") or "").strip().replace(" ", "")
    if not contacts.EMAIL_RE.fullmatch(apple_id) or not 8 <= len(password) <= 64:
        raise contacts.ApiError(400, "bad_icloud")
    client = Client(apple_id, password)
    try:
        book = client.discover()
    except ICloudError as exc:
        if exc.status in (401, 403):
            raise contacts.ApiError(400, "icloud_auth") from None
        logger.warning("iCloud of %s not reached: %s", owner, exc)
        raise contacts.ApiError(502, "icloud_unreachable") from None
    ssm().put_parameter(Name=password_parameter(owner), Value=json.dumps({"apple_id": apple_id, "password": password}),
                                 Type="SecureString", Overwrite=True)
    previous = state_item(owner)
    # Another Apple ID starts over: its cards are matched afresh.
    if previous.get("apple_id") and previous.get("apple_id").lower() != apple_id.lower():
        drop_links(owner)
        previous = {}
    save_state(owner, {
        "connected": True, "apple_id": apple_id, "book": book, "status": "connected", "last_error": "",
        **({"sync_token": "", "groups": {}, "rgroups": {}} if not previous or previous.get("book") != book else {}),
    })
    contacts.table().put_item(Item={"pk": "ICLOUD", "sk": owner, "at": contacts.now_iso()})
    contacts.log_sync(owner, "icloud", "connected", detail=apple_id)
    start_sync(owner)
    return public_state(owner)


def disconnect(owner):
    """The links go; the cards here and in iCloud both stay. The app-specific
    password is revoked on the Apple ID page."""
    try:
        ssm().delete_parameter(Name=password_parameter(owner))
    except Exception:
        logger.warning("password parameter of %s not deleted", owner, exc_info=True)
    drop_links(owner)
    contacts.table().delete_item(Key={"pk": contacts.user_pk(owner), "sk": "ICLOUD"})
    contacts.table().delete_item(Key={"pk": "ICLOUD", "sk": owner})
    contacts.log_sync(owner, "icloud", "disconnected")
    return {"disconnected": True}


def drop_links(owner):
    keys = [{"pk": item["pk"], "sk": item["sk"]} for item in link_items(owner)]
    with contacts.table().batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key=key)


def start_sync(owner):
    """A sync in the background, by the function of the schedule."""
    name = os.environ.get("ICLOUD_SYNC_FUNCTION", "")
    if not name:
        return
    lambda_client().invoke(FunctionName=name, InvocationType="Event", Payload=json.dumps({"action": "sync", "owner": owner}).encode())


# CardDAV client --------------------------------------------------------------------

def transport(method, url, headers, body):
    """(status, headers with lower-case names, body) of one request."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with OPENER.open(request, timeout=TIMEOUT) as answer:
            return answer.status, {k.lower(): v for k, v in answer.headers.items()}, answer.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ICloudError(0, str(exc)) from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # PROPFIND and REPORT are not redirected by urllib; Client.call follows
    # every redirect itself with the same method.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def text_of(element, path):
    found = element.find(path, NS)
    return (found.text or "").strip() if found is not None and found.text else ""


class Client:
    def __init__(self, apple_id, password, book=""):
        self.auth = "Basic " + base64.b64encode(f"{apple_id}:{password}".encode()).decode()
        self.book = book

    def call(self, method, url, body=None, headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        for _ in range(4):
            status, answer_headers, content = transport(method, url, {
                "Authorization": self.auth, "User-Agent": "contacts-sync/1.0",
                **({"Content-Type": "application/xml; charset=utf-8"} if data and method in ("PROPFIND", "REPORT") else {}),
                **(headers or {}),
            }, data)
            if status in (301, 302, 307, 308) and answer_headers.get("location"):
                url = urllib.parse.urljoin(url, answer_headers["location"])
                continue
            if status == 401:
                raise ICloudError(401, "the Apple ID or the app-specific password is not accepted")
            return status, answer_headers, content, url
        raise ICloudError(310, "too many redirects")

    def xml(self, method, url, body, depth):
        status, _, content, url = self.call(method, url, body, {"Depth": depth})
        if status != 207:
            raise ICloudError(status, content[:200].decode("utf-8", "replace"))
        return ElementTree.fromstring(content), url

    def discover(self):
        """The URL of the address book: principal, its home, the book in it."""
        root, url = self.xml("PROPFIND", ICLOUD_URL, '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>', "0")
        principal = text_of(root, ".//d:current-user-principal/d:href")
        if not principal:
            raise ICloudError(404, "no principal")
        root, url = self.xml("PROPFIND", urllib.parse.urljoin(url, principal),
                             '<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav"><d:prop>'
                             '<card:addressbook-home-set/></d:prop></d:propfind>', "0")
        home = text_of(root, ".//card:addressbook-home-set/d:href")
        if not home:
            raise ICloudError(404, "no address book home")
        home = urllib.parse.urljoin(url, home)
        root, url = self.xml("PROPFIND", home, '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>', "1")
        for response in root.findall("d:response", NS):
            if response.find(".//d:resourcetype/card:addressbook", NS) is not None:
                self.book = urllib.parse.urljoin(url, text_of(response, "d:href"))
                return self.book
        raise ICloudError(404, "no address book")

    def href(self, href):
        return urllib.parse.urljoin(self.book, href)

    def changes(self, token):
        """The cards changed since the token ({url: etag}), the removed ones,
        the new token, and whether it is a full listing."""
        body = ('<d:sync-collection xmlns:d="DAV:"><d:sync-token>{}</d:sync-token><d:sync-level>1</d:sync-level>'
                '<d:prop><d:getetag/></d:prop></d:sync-collection>')
        status, _, content, _ = self.call("REPORT", self.book, body.format(escape_xml(token or "")), {"Depth": "1"})
        if status != 207 and token:
            # An old token is refused: everything is read again.
            return self.changes("")
        if status != 207:
            raise ICloudError(status, content[:200].decode("utf-8", "replace"))
        root = ElementTree.fromstring(content)
        changed, removed = {}, set()
        book_path = urllib.parse.urlsplit(self.book).path.rstrip("/")
        for response in root.findall("d:response", NS):
            url = self.href(text_of(response, "d:href"))
            if urllib.parse.urlsplit(url).path.rstrip("/") == book_path:
                continue
            if " 404 " in text_of(response, "d:status") + " ":
                removed.add(url)
            else:
                changed[url] = text_of(response, ".//d:getetag")
        return changed, removed, text_of(root, "d:sync-token"), not token

    def multiget(self, urls):
        """{url: (etag, vCard text)} of the cards."""
        found = {}
        for start in range(0, len(urls), MULTIGET):
            hrefs = "".join(f"<d:href>{escape_xml(urllib.parse.urlsplit(url).path)}</d:href>" for url in urls[start:start + MULTIGET])
            root, _ = self.xml("REPORT", self.book, '<card:addressbook-multiget xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
                               f'<d:prop><d:getetag/><card:address-data/></d:prop>{hrefs}</card:addressbook-multiget>', "1")
            for response in root.findall("d:response", NS):
                data = response.find(".//card:address-data", NS)
                if data is not None and data.text:
                    found[self.href(text_of(response, "d:href"))] = (text_of(response, ".//d:getetag"), data.text)
        return found

    def put(self, url, text, etag=None):
        """True when written; False when the card there has changed since."""
        headers = {"Content-Type": "text/vcard; charset=utf-8"}
        headers.update({"If-Match": etag} if etag else {"If-None-Match": "*"})
        status, _, content, _ = self.call("PUT", url, text, headers)
        if status == 412:
            return False
        if status not in (200, 201, 204):
            raise ICloudError(status, content[:200].decode("utf-8", "replace"))
        return True

    def photo(self, props):
        """The photo of a card that iCloud gives as a link, not inline."""
        for _, name, _, value in props:
            if name == "PHOTO" and value.strip().lower().startswith("https://"):
                status, _, content, _ = self.call("GET", value.strip())
                content_type = vcard.photo_type(content) if status == 200 else None
                return (content, content_type) if content_type else None
        return None

    def delete(self, url):
        status, _, content, _ = self.call("DELETE", url)
        if status not in (200, 204, 404, 410):
            raise ICloudError(status, content[:200].decode("utf-8", "replace"))


def escape_xml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Mapping ---------------------------------------------------------------------------

def fingerprint(contact, extra=""):
    """The fingerprint of what one side has of the fields that go to iCloud."""
    subset = {key: value for key, value in contact.items() if key not in SKIPPED and value not in (None, "", [], False)}
    if "related" in subset:
        subset["related"] = [{k: v for k, v in entry.items() if k != "contact"} for entry in subset["related"]]
    data = json.dumps({"c": subset, "x": extra}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def as_vcard_reads(card):
    """A card here as iCloud would give it back: written as vCard and read again."""
    props = vcard.split_cards(vcard.contact_to_card({k: v for k, v in card.items() if k != "photo"}))[0]
    return vcard.card_to_contact(props)[0]


def remote_contact(props):
    contact, photo = vcard.card_to_contact(props)
    contact.pop("categories", None)
    return contact, photo


def merge_first(card, remote):
    """A card and the iCloud card matched to it at the first sync: the values
    of the card stay, iCloud adds what the card lacks."""
    merged = dict(card)
    for key, value in remote.items():
        if key in SKIPPED or value in (None, "", [], False) or key not in contacts.FIELDS:
            continue
        if isinstance(value, list):
            have = list(card.get(key) or [])
            seen = {json.dumps({k: v for k, v in entry.items() if k not in ("label", "contact")}, sort_keys=True, ensure_ascii=False) for entry in have}
            merged[key] = have + [entry for entry in value
                                  if json.dumps({k: v for k, v in entry.items() if k not in ("label", "contact")}, sort_keys=True, ensure_ascii=False) not in seen]
        elif not card.get(key):
            merged[key] = value
    merged["related"] = contacts.keep_links(card.get("related"), merged.get("related"))
    return merged


def display(contact):
    return vcard.display_name(contact) or contacts.first_value(contact) or "—"


def group_hash(name, members):
    return hashlib.sha256(json.dumps([name, sorted(set(members))], ensure_ascii=False).encode("utf-8")).hexdigest()


# Sync -------------------------------------------------------------------------------

def handler(event, context):
    if not enabled():
        return {"skipped": "off"}
    if event.get("action") == "sync" and event.get("owner"):
        return sync_user(event["owner"])
    results = {}
    for owner in registered():
        try:
            results[owner] = sync_user(owner)
        except Exception:
            logger.exception("sync of %s failed", owner)
            results[owner] = "error"
    return results


def registered():
    from boto3.dynamodb.conditions import Key

    page = contacts.table().query(KeyConditionExpression=Key("pk").eq("ICLOUD"))
    return [item["sk"] for item in page.get("Items", [])]


def link_items(owner):
    from boto3.dynamodb.conditions import Key

    items, kwargs = [], {"KeyConditionExpression": Key("pk").eq(contacts.user_pk(owner)) & Key("sk").begins_with("ILINK#")}
    while True:
        page = contacts.table().query(**kwargs)
        items.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def put_link(owner, contact_id, link):
    contacts.table().put_item(Item={**{k: v for k, v in link.items() if k not in ("pk", "sk", "id")},
                                    "pk": contacts.user_pk(owner), "sk": "ILINK#" + contact_id, "id": contact_id})


def drop_link(owner, contact_id):
    contacts.table().delete_item(Key={"pk": contacts.user_pk(owner), "sk": "ILINK#" + contact_id})


def lock(owner):
    """One sync of a user at a time: the schedule and "Sync now" may meet."""
    now = int(time.time())
    try:
        contacts.table().update_item(
            Key={"pk": contacts.user_pk(owner), "sk": "ICLOUD"},
            UpdateExpression="SET lock_until = :until",
            ConditionExpression="attribute_exists(pk) AND (attribute_not_exists(lock_until) OR lock_until < :now)",
            ExpressionAttributeValues={":until": now + LOCK_SECONDS, ":now": now},
        )
        return True
    except Exception as exc:
        if contacts.conditional_failed(exc):
            return False
        raise


def credentials(owner):
    value = ssm().get_parameter(Name=password_parameter(owner), WithDecryption=True)["Parameter"]["Value"]
    data = json.loads(value)
    return data["apple_id"], data["password"]


def sync_user(owner):
    state = state_item(owner)
    if not state.get("connected"):
        return "not connected"
    if not lock(owner):
        return "busy"
    run = None
    try:
        apple_id, password = credentials(owner)
        client = Client(apple_id, password, state.get("book", ""))
        if not client.book:
            client.discover()
        run = Run(owner, state, client)
        run.go()
        save_state(owner, {"status": "ok", "last_sync": contacts.now_iso(), "last_error": "", "book": client.book,
                           "sync_token": run.sync_token, "groups": run.groups_state, "rgroups": run.rgroups,
                           "linked": len(run.links), "lock_until": 0})
        return run.summary
    except ICloudError as exc:
        save_state(owner, {"status": "reauth" if exc.status == 401 else "error", "last_error": str(exc)[:300], "lock_until": 0,
                           **({"groups": run.groups_state, "rgroups": run.rgroups} if run else {})})
        contacts.log_sync(owner, "icloud", "error", detail=str(exc)[:500])
        logger.exception("sync of %s failed", owner)
        return "error"
    except Exception as exc:
        save_state(owner, {"status": "error", "last_error": str(exc)[:300], "lock_until": 0})
        contacts.log_sync(owner, "icloud", "error", detail=str(exc)[:500])
        raise


class Run:
    """One sync of one user."""

    def __init__(self, owner, state, client):
        self.owner = owner
        self.client = client
        self.sync_token = state.get("sync_token", "")
        # Groups here and their iCloud cards: {group id: {url, uid, lhash, rhash}}.
        self.groups_state = {key: dict(value) for key, value in (state.get("groups") or {}).items()}
        # The group cards of iCloud: {url: {uid, name, members, etag}}.
        self.rgroups = {key: dict(value) for key, value in (state.get("rgroups") or {}).items()}
        self.links = {}
        self.writes = 0
        self.summary = {"to_book": 0, "to_icloud": 0, "conflicts": 0, "deleted": 0}

    def log(self, source, action, contact=None, fields=None, detail="", name=""):
        contacts.log_sync(self.owner, source, action, target="icloud" if source == "to_icloud" else "book",
                          name=name or (display(contact) if contact else ""), fields=fields, detail=detail,
                          contact_id=(contact or {}).get("id", ""))

    def go(self):
        self.cards = {item["id"]: contacts.plain(item) for item in contacts.all_current(self.owner)}
        self.links = {item["id"]: contacts.plain(item) for item in link_items(self.owner)}
        self.load_groups()
        changed, removed, token, full = self.client.changes(self.sync_token)
        fetched = self.client.multiget(list(changed))
        cards, groups = {}, {}
        for url, (etag, text) in fetched.items():
            found = vcard.split_cards(text)
            if not found:
                continue
            (groups if vcard.is_group(found[0]) else cards)[url] = (etag or changed.get(url, ""), found[0])
        self.pull(cards, removed, set(changed) if full else None)
        self.pull_groups(groups, removed, set(changed) if full else None)
        pushed = self.push()
        pushed += self.push_groups()
        self.read_back(pushed)
        self.sync_token = token

    # Maps --------------------------------------------------------------------

    def load_groups(self):
        self.groups = {item["id"]: contacts.plain(item) for item in contacts.group_items(self.owner)}

    def remote_uid_of(self, card_id):
        link = self.links.get(card_id)
        return (link or {}).get("uid") or contacts.dav_uid(self.cards[card_id])

    def local_uids(self, remote_uids):
        """Members of an iCloud group as UIDs of the cards here."""
        by_remote = {link.get("uid"): card_id for card_id, link in self.links.items() if link.get("uid")}
        return [contacts.dav_uid(self.cards[by_remote[uid]]) if by_remote.get(uid) in self.cards else uid for uid in remote_uids]

    def remote_uids(self, local_uids):
        """Members of a group here as UIDs of the cards in iCloud; cards not
        there yet are left out until they are."""
        by_local = {contacts.dav_uid(card): card_id for card_id, card in self.cards.items()}
        return [self.remote_uid_of(by_local[uid]) for uid in local_uids if by_local.get(uid) in self.links]

    def local_hash(self, card):
        return fingerprint(as_vcard_reads(card), card.get("photo") or "")

    def card_text(self, card, uid):
        photo = None
        if card.get("photo"):
            try:
                answer = contacts.s3().get_object(Bucket=contacts.env("PHOTOS_BUCKET"), Key=card["photo"].lstrip("/"))
                photo = (answer["Body"].read(), answer.get("ContentType") or "image/jpeg")
            except Exception:
                logger.warning("photo of %s not read", card["id"], exc_info=True)
        names = {card_id: vcard.display_name(other) for card_id, other in self.cards.items()}
        related = [{**entry, "value": names.get(entry.get("contact")) or entry.get("value", "")} for entry in card.get("related") or []]
        return vcard.contact_to_card({**card, "uid": uid, "related": related}, photo)

    # iCloud -> book --------------------------------------------------------------

    def pull(self, cards, removed, listed):
        by_url = {link["url"]: card_id for card_id, link in self.links.items()}
        for url, (etag, props) in cards.items():
            remote, photo = remote_contact(props)
            photo = photo or self.client.photo(props)
            present = {name for _, name, _, _ in props}
            rhash = fingerprint(remote)
            rsha = hashlib.sha256(photo[0]).hexdigest() if photo else ""
            card_id = by_url.get(url)
            if card_id and card_id in self.cards:
                self.remote_changed(card_id, etag, remote, present, photo, rhash, rsha)
            elif not card_id:
                self.remote_new(url, etag, remote, photo, rhash, rsha)
        gone = {by_url[url] for url in removed if url in by_url}
        if listed is not None:
            # A full listing has every card: a linked one missing from it was
            # deleted in iCloud.
            gone |= {card_id for card_id, link in self.links.items() if link["url"] not in listed}
        for card_id in gone:
            self.remote_deleted(card_id)

    def save(self, card_id, payload, photo_hash=None):
        current = contacts.get_current(self.owner, card_id)
        changes = contacts.changed_fields(current, contacts.clean_contact(payload, self.owner, blank_ok=True))
        contacts.save_contact(self.owner, card_id, {**payload, "etag": current["etag"]}, "icloud",
                              current.get("photo_hash") if photo_hash is None else photo_hash)
        saved = contacts.plain(contacts.get_current(self.owner, card_id))
        self.cards[card_id] = saved
        return saved, changes

    def remote_changed(self, card_id, etag, remote, present, photo, rhash, rsha):
        link = self.links[card_id]
        card = self.cards[card_id]
        link["etag"] = etag
        if rhash != link.get("rhash"):
            if self.local_hash(card) != link.get("lhash"):
                # Both sides changed: the address book wins and goes to
                # iCloud again in push(); the iCloud version is in the log.
                self.summary["conflicts"] += 1
                theirs = sorted(key for key in set(remote) | set(as_vcard_reads(card))
                                if key not in SKIPPED and (remote.get(key) or None) != (as_vcard_reads(card).get(key) or None))
                self.log("icloud", "conflict", card, fields=theirs, detail="kept_book")
                link["rhash"] = rhash
                put_link(self.owner, card_id, link)
                return
            names = {cid: vcard.display_name(other) for cid, other in self.cards.items()}
            merged = carddav.merge_device_card(card, dict(remote), present, names)
            merged["photo"] = card.get("photo", "")
            saved, changes = self.save(card_id, merged)
            if changes:
                self.summary["to_book"] += 1
                self.log("icloud", "changed", saved, fields=changes)
            link.update({"rhash": rhash, "lhash": self.local_hash(saved), "name": display(saved)})
        self.pull_photo(card_id, photo, rsha)
        put_link(self.owner, card_id, link)

    def remote_new(self, url, etag, remote, photo, rhash, rsha):
        unlinked = [card for card_id, card in self.cards.items() if card_id not in self.links]
        uid = remote.get("uid", "")
        match = next((card for card in unlinked if uid and contacts.dav_uid(card) == uid), None) or contacts.find_match(unlinked, remote)
        if match:
            saved, changes = self.save(match["id"], merge_first(match, remote))
            self.log("icloud", "matched", saved, fields=changes)
            # The merged card differs from iCloud's: push() sends it.
            lhash = ""
        else:
            created = contacts.create_contact(self.owner, remote, "icloud")
            saved = contacts.plain(contacts.get_current(self.owner, created["id"]))
            self.cards[saved["id"]] = saved
            self.log("icloud", "created", saved)
            lhash = self.local_hash(saved)
        self.summary["to_book"] += 1
        self.links[saved["id"]] = {"url": url, "etag": etag, "uid": uid or contacts.dav_uid(saved), "rhash": rhash, "lhash": lhash,
                                   "photo_local": "", "photo_remote": "", "name": display(saved)}
        self.pull_photo(saved["id"], photo, rsha)
        put_link(self.owner, saved["id"], self.links[saved["id"]])

    def remote_deleted(self, card_id):
        card = self.cards.get(card_id)
        link = self.links.pop(card_id, None)
        drop_link(self.owner, card_id)
        if not card:
            return
        if link and self.local_hash(card) != link.get("lhash"):
            # Changed here since: the card stays and goes to iCloud as new.
            self.log("icloud", "conflict", card, detail="deleted_changed")
            return
        current = contacts.get_current(self.owner, card_id)
        contacts.delete_contact(self.owner, card_id, current["etag"])
        self.cards.pop(card_id, None)
        self.summary["deleted"] += 1
        self.log("icloud", "deleted", card, detail="restorable")

    def pull_photo(self, card_id, photo, rsha):
        link = self.links[card_id]
        if link.get("photo_sent"):
            # The photo that went with the last write, as iCloud keeps it.
            link["photo_remote"], link["photo_sent"] = rsha, False
            return
        if rsha == link.get("photo_remote"):
            return
        card = self.cards[card_id]
        # A photo here that has not gone to iCloud yet wins; push() sends it.
        if (card.get("photo") or "") != (link.get("photo_local") or ""):
            return
        if photo and len(photo[0]) <= contacts.photo_max_bytes():
            data, content_type = photo
            path = contacts.put_photo(self.owner, data, content_type)
            saved, _ = self.save(card_id, {**contacts.public_contact(card), "photo": path}, hashlib.sha256(data).hexdigest())
            link["photo_local"] = path
            link["lhash"] = self.local_hash(saved)
            self.log("icloud", "photo", saved)
        link["photo_remote"] = rsha

    # Groups ----------------------------------------------------------------------

    def pull_groups(self, groups, removed, listed):
        by_url = {known["url"]: group_id for group_id, known in self.groups_state.items()}
        for url, (etag, props) in groups.items():
            name, uid, members = vcard.card_to_group(props)
            self.rgroups[url] = {"uid": uid, "name": name, "members": members, "etag": etag}
        gone = {url for url in removed if url in self.rgroups}
        if listed is not None:
            gone |= {url for url in self.rgroups if url not in listed}
        for url in gone:
            self.rgroups.pop(url, None)
            group_id = by_url.get(url)
            known = self.groups_state.pop(group_id, None) if group_id else None
            group = self.groups.get(group_id)
            if not group:
                continue
            if known and group_hash(group["name"], group.get("members") or []) != known.get("lhash"):
                self.log("icloud", "conflict", name=group["name"], detail="deleted_changed")
                continue
            contacts.delete_group(self.owner, group_id)
            self.log("icloud", "group_deleted", name=group["name"])
        for url in groups:
            remote = self.rgroups[url]
            name = contacts.clean_group_name(remote["name"] or "—")
            members = self.local_uids(remote["members"])
            rhash = group_hash(name, members)
            group_id = by_url.get(url)
            if group_id and group_id in self.groups:
                known = self.groups_state[group_id]
                if rhash == known.get("rhash"):
                    continue
                group = self.groups[group_id]
                if group_hash(group["name"], group.get("members") or []) != known.get("lhash"):
                    self.summary["conflicts"] += 1
                    self.log("icloud", "conflict", name=group["name"], detail="kept_book")
                    known["rhash"] = rhash
                    continue
                # Members of cards that are not in iCloud stay.
                kept = [uid for uid in group.get("members") or [] if uid not in self.remote_side_uids()]
                merged = list(dict.fromkeys(kept + members))
                contacts.transact([contacts.group_put(group, {"name": name, "members": merged}, contacts.now_iso())])
                if name != group["name"]:
                    self.log("icloud", "group_renamed", name=name, detail=group["name"])
                if sorted(merged) != sorted(group.get("members") or []):
                    self.log("icloud", "group_members", name=name)
                self.load_groups()
                known.update({"rhash": rhash, "lhash": group_hash(name, merged)})
            elif not group_id:
                mapped = set(self.groups_state)
                match = next((group for group_id, group in self.groups.items()
                              if group_id not in mapped and contacts.fold(group["name"]) == contacts.fold(name)), None)
                if match:
                    merged = list(dict.fromkeys((match.get("members") or []) + members))
                    contacts.transact([contacts.group_put(match, {"members": merged}, contacts.now_iso())])
                    self.groups_state[match["id"]] = {"url": url, "uid": remote["uid"], "rhash": rhash, "lhash": ""}
                else:
                    item = contacts.store_group(self.owner, name, members, remote["uid"])
                    self.groups_state[item["id"]] = {"url": url, "uid": remote["uid"], "rhash": rhash, "lhash": group_hash(name, members)}
                    self.log("icloud", "group_created", name=name)
                self.load_groups()

    def remote_side_uids(self):
        """UIDs here of the cards that are in iCloud."""
        return {contacts.dav_uid(self.cards[card_id]) for card_id in self.links if card_id in self.cards}

    # Book -> iCloud --------------------------------------------------------------

    def push(self):
        pushed = []
        for card_id in [card_id for card_id in self.links if card_id not in self.cards]:
            if self.writes >= WRITES_PER_SYNC:
                return pushed
            link = self.links.pop(card_id)
            self.client.delete(link["url"])
            self.writes += 1
            drop_link(self.owner, card_id)
            self.summary["to_icloud"] += 1
            contacts.log_sync(self.owner, "to_icloud", "deleted", target="icloud", name=link.get("name", ""), contact_id=card_id)
        for card_id, card in self.cards.items():
            link = self.links.get(card_id)
            lhash = self.local_hash(card)
            if link and lhash == link.get("lhash"):
                continue
            if self.writes >= WRITES_PER_SYNC:
                return pushed
            self.writes += 1
            if link:
                if not self.client.put(link["url"], self.card_text(card, link.get("uid") or contacts.dav_uid(card)), link.get("etag") or None):
                    # Changed in iCloud since: the next sync reads it first.
                    self.log("to_icloud", "error", card, detail="changed_there")
                    continue
                self.log("to_icloud", "changed", card)
            else:
                uid = contacts.dav_uid(card)
                url = urllib.parse.urljoin(self.client.book, uuid.uuid4().hex.upper() + ".vcf")
                if not self.client.put(url, self.card_text(card, uid)):
                    continue
                link = {"url": url, "uid": uid, "photo_local": "", "photo_remote": ""}
                self.log("to_icloud", "created", card)
            link.update({"lhash": lhash, "etag": "", "name": display(card), "photo_local": card.get("photo") or "",
                         "photo_sent": bool(card.get("photo"))})
            self.links[card_id] = link
            self.summary["to_icloud"] += 1
            put_link(self.owner, card_id, link)
            pushed.append(link["url"])
        return pushed

    def push_groups(self):
        pushed = []
        for group_id in [group_id for group_id in self.groups_state if group_id not in self.groups]:
            known = self.groups_state.pop(group_id)
            self.client.delete(known["url"])
            name = self.rgroups.pop(known["url"], {}).get("name", "")
            self.log("to_icloud", "group_deleted", name=name)
        for group_id, group in self.groups.items():
            known = self.groups_state.get(group_id)
            lhash = group_hash(group["name"], group.get("members") or [])
            if known and lhash == known.get("lhash"):
                continue
            if self.writes >= WRITES_PER_SYNC:
                return pushed
            self.writes += 1
            uid = (known or {}).get("uid") or group.get("uid") or group_id
            url = (known or {}).get("url") or urllib.parse.urljoin(self.client.book, uuid.uuid4().hex.upper() + ".vcf")
            etag = self.rgroups.get(url, {}).get("etag") if known else None
            text = vcard.group_to_card(group["name"], uid, self.remote_uids(group.get("members") or []))
            if not self.client.put(url, text, etag or None):
                self.log("to_icloud", "error", name=group["name"], detail="changed_there")
                continue
            self.log("to_icloud", "group_renamed" if known and self.rgroups.get(url, {}).get("name") not in (None, group["name"])
                     else "group_members" if known else "group_created", name=group["name"])
            self.groups_state[group_id] = {"url": url, "uid": uid, "lhash": lhash, "rhash": (known or {}).get("rhash", "")}
            pushed.append(url)
        return pushed

    def read_back(self, urls):
        """The written cards as iCloud keeps them: their etags and fingerprints."""
        if not urls:
            return
        by_url = {link["url"]: card_id for card_id, link in self.links.items()}
        groups_by_url = {known["url"]: group_id for group_id, known in self.groups_state.items()}
        for url, (etag, text) in self.client.multiget(urls).items():
            found = vcard.split_cards(text)
            if not found:
                continue
            props = found[0]
            if vcard.is_group(props):
                name, uid, members = vcard.card_to_group(props)
                self.rgroups[url] = {"uid": uid, "name": name, "members": members, "etag": etag}
                group_id = groups_by_url.get(url)
                if group_id:
                    self.groups_state[group_id]["rhash"] = group_hash(contacts.clean_group_name(name or "—"), self.local_uids(members))
                continue
            card_id = by_url.get(url)
            if not card_id:
                continue
            remote, photo = remote_contact(props)
            photo = photo or self.client.photo(props)
            link = self.links[card_id]
            link.update({"etag": etag, "rhash": fingerprint(remote),
                         "photo_remote": hashlib.sha256(photo[0]).hexdigest() if photo else "", "photo_sent": False})
            put_link(self.owner, card_id, link)


_ssm = None
_lambda = None


def ssm():
    global _ssm
    if _ssm is None:
        import boto3
        _ssm = boto3.client("ssm")
    return _ssm


def lambda_client():
    global _lambda
    if _lambda is None:
        import boto3
        _lambda = boto3.client("lambda")
    return _lambda
