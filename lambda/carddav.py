"""CardDAV server of the address book (RFC 6352, sync-collection of RFC 6578).

Apple Contacts on macOS and iOS, Thunderbird, DAVx5 on Android and the
Outlook CalDav Synchronizer read and change the same cards as the page. The
server is its own HTTP API on DAV_HOST, without CloudFront, which does not
pass PROPFIND and REPORT.

A client signs in with HTTP Basic: the email and an app password made in the
settings (contacts.py, sk = APPPASS#<id>). Every user has one address book:

  /.well-known/carddav   -> 301 to /dav/
  /dav/, /dav/principal/ the principal: where the address books are
  /dav/home/             the home: holds the one address book
  /dav/home/contacts/    the address book; its cards are <name>.vcf

A card is <id>.vcf, or the name a client gave it when it created the card
(kept as "dav" on the card). Apple's groups are resources of the same
address book: vCards of the kind "group" that list the UIDs of their members
(contacts.py, sk = GROUP#<id>). The etag of a card is its etag in the table, so
If-Match works as the check of the page does. The sync token is the time of
the latest change: a newer card is reported as changed, a newer DELETED# as
gone. A change from a client is a version in the history like any other,
with the action "sync".
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import quote, unquote, urlsplit

import contacts
import vcard

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DAV = "DAV:"
CARD = "urn:ietf:params:xml:ns:carddav"
CS = "http://calendarserver.org/ns/"
ElementTree.register_namespace("d", DAV)
ElementTree.register_namespace("card", CARD)
ElementTree.register_namespace("cs", CS)

ROOT = "/dav/"
PRINCIPAL = "/dav/principal/"
HOME = "/dav/home/"
BOOK = "/dav/home/contacts/"
def sync_prefix():
    return f"https://{vcard.site_domain()}/ns/sync/"
NAME_RE = re.compile(r"^[^/\x00-\x1f]{1,200}\.vcf$")
BODY_MAX = 3_000_000
# A Lambda answer is at most 6 MB; cards with photos past this get 507 and
# come in the next request.
RESPONSE_BUDGET = 5_000_000
AUTH_SECONDS = 300
USED_SECONDS = 3600
_auth = {}
# What a request did, for its line in the log: the device, the report, the
# card a PUT wrote or why it was refused. No contents of cards.
_note = {}


class DavError(Exception):
    def __init__(self, status, condition=None, text=""):
        super().__init__(text or str(status))
        self.status = status
        # A precondition element of the error body, such as {DAV:}valid-sync-token.
        self.condition = condition
        self.text = text


def q(namespace, name):
    return "{%s}%s" % (namespace, name)


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    path = event.get("rawPath") or "/"
    headers = {str(key).lower(): str(value) for key, value in (event.get("headers") or {}).items()}
    _note.clear()
    try:
        if method == "OPTIONS":
            result = respond(200, "", extra={"Allow": "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, PROPPATCH, REPORT"})
        elif path.rstrip("/") == "/.well-known/carddav":
            result = respond(301, "", extra={"Location": ROOT})
        else:
            owner = authenticate(headers)
            if not owner:
                result = respond(401, "Sign in with your email and an app password from the settings.",
                                 "text/plain; charset=utf-8", {"WWW-Authenticate": 'Basic realm="Contacts", charset="UTF-8"'})
            else:
                result = route(method, path, headers, body_text(event), owner)
    except DavError as exc:
        result = error_response(exc)
    except ElementTree.ParseError:
        result = respond(400, "The XML of the request is not valid.", "text/plain; charset=utf-8")
    except Exception:
        logger.exception("unhandled")
        result = respond(500, "Internal error", "text/plain; charset=utf-8")
    details = {key: headers[key] for key in ("depth", "if-match", "if-none-match") if key in headers}
    details.update(_note)
    details["agent"] = headers.get("user-agent", "")[:120]
    logger.info("%s %s -> %s %s", method, path, result["statusCode"], json.dumps(details, ensure_ascii=False))
    return result


def route(method, path, headers, body, owner):
    path = unquote(urlsplit(path).path)
    if path in ("/", "/dav", ROOT, PRINCIPAL.rstrip("/"), PRINCIPAL):
        kind = "principal"
    elif path in (HOME.rstrip("/"), HOME):
        kind = "home"
    elif path in (BOOK.rstrip("/"), BOOK):
        kind = "book"
    elif path.startswith(BOOK) and NAME_RE.fullmatch(path[len(BOOK):]):
        return card_route(method, path[len(BOOK):], headers, body, owner)
    else:
        raise DavError(404)
    if method == "PROPFIND":
        return propfind(kind, headers, body, owner)
    if method == "REPORT" and kind == "book":
        return report(body, owner)
    if method == "PROPPATCH":
        return proppatch(path, body)
    if method in ("GET", "HEAD"):
        return respond(200, "" if method == "HEAD" else "CardDAV address book of " + owner + "\n", "text/plain; charset=utf-8")
    raise DavError(405 if method in ("PUT", "DELETE", "POST", "REPORT") else 403)


# Sign-in ---------------------------------------------------------------------------

def authenticate(headers):
    value = headers.get("authorization", "")
    if not value.lower().startswith("basic "):
        return None
    try:
        email, _, password = base64.b64decode(value[6:].strip()).decode("utf-8").partition(":")
    except (ValueError, UnicodeError):
        return None
    email = email.strip().lower()
    if not contacts.EMAIL_RE.fullmatch(email) or not password:
        return None
    key = (email, hashlib.sha256(password.encode("utf-8")).hexdigest())
    cached = _auth.get(key)
    if cached and cached[0] > time.time():
        _note["device"] = cached[1]
        return email
    for item in contacts.app_password_items(email):
        if hmac.compare_digest(contacts.app_password_hash(item.get("salt", ""), password), item.get("hash", "")):
            if not user_enabled(email):
                return None
            mark_used(email, item)
            _auth[key] = (time.time() + AUTH_SECONDS, item.get("name", ""))
            _note["device"] = item.get("name", "")
            return email
    logger.warning("CardDAV sign-in of %s failed", email)
    return None


# A disabled or deleted account loses its clients too.
def user_enabled(email):
    try:
        user = contacts.cognito().admin_get_user(UserPoolId=contacts.env("USER_POOL_ID"), Username=email)
    except Exception as exc:
        if contacts.error_code(exc) == "UserNotFoundException":
            return False
        raise
    return bool(user.get("Enabled", True))


def mark_used(email, item):
    now = contacts.now_iso()
    used = item.get("used") or ""
    if used and (datetime.fromisoformat(now.replace("Z", "+00:00")) - datetime.fromisoformat(used.replace("Z", "+00:00"))).total_seconds() < USED_SECONDS:
        return
    try:
        contacts.table().update_item(
            Key={"pk": item["pk"], "sk": item["sk"]},
            UpdateExpression="SET used = :u",
            # A password revoked a moment ago stays revoked.
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":u": now},
        )
    except Exception as exc:
        if not contacts.conditional_failed(exc):
            raise


# Cards -----------------------------------------------------------------------------

def card_name(item):
    return item.get("dav") or item["id"] + ".vcf"


def href(name):
    return BOOK + quote(name)


def etag_of(item):
    return '"%s"' % item.get("etag", "")


def cards_by_name(owner):
    """Every resource of the address book by its name: the cards and Apple's
    groups (an item of a group has "members")."""
    resources = {card_name(item): item for item in contacts.all_current(owner)}
    resources.update({card_name(item): item for item in contacts.group_items(owner)})
    return resources


def is_group_item(item):
    return item.get("sk", "").startswith("GROUP#")


def tombstones(owner):
    """Deleted cards (DELETED#) and deleted groups (GONE#)."""
    from boto3.dynamodb.conditions import Key

    items = []
    for prefix in ("DELETED#", "GONE#"):
        kwargs = {"KeyConditionExpression": Key("pk").eq(contacts.user_pk(owner)) & Key("sk").begins_with(prefix)}
        while True:
            page = contacts.table().query(**kwargs)
            items.extend(page.get("Items", []))
            if not page.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


def card_text(item):
    if is_group_item(item):
        return vcard.group_to_card(item.get("name", ""), item.get("uid") or contacts.dav_uid(item), list(item.get("members") or []))
    contact = contacts.plain(item)
    photo = None
    if contact.get("photo"):
        try:
            answer = contacts.s3().get_object(Bucket=contacts.env("PHOTOS_BUCKET"), Key=contact["photo"].lstrip("/"))
            photo = (answer["Body"].read(), answer.get("ContentType") or "image/jpeg")
        except Exception:
            logger.warning("photo %s of %s not read", contact["photo"], item["id"], exc_info=True)
    if not contact.get("uid"):
        contact["uid"] = item["id"] + "@" + vcard.site_domain()
    return vcard.contact_to_card(contact, photo)


def card_route(method, name, headers, body, owner):
    if method == "PUT":
        return put_card(name, headers, body, owner)
    item = cards_by_name(owner).get(name)
    if method in ("GET", "HEAD"):
        if not item:
            raise DavError(404)
        text = card_text(item)
        return respond(200, "" if method == "HEAD" else text, "text/vcard; charset=utf-8", {"ETag": etag_of(item), "Last-Modified": http_date(item.get("modified"))})
    if method == "DELETE":
        if not item:
            raise DavError(404)
        check_match(headers, item)
        try:
            if is_group_item(item):
                contacts.delete_group(owner, item["id"], item["etag"])
                log(owner, "group_deleted", name=item.get("name", ""))
            else:
                contacts.delete_contact(owner, item["id"], item["etag"])
                log(owner, "deleted", item)
        except contacts.ApiError as exc:
            raise DavError(412 if exc.code == "changed" else 404) from None
        return respond(204, "")
    if method == "PROPFIND":
        if not item:
            raise DavError(404)
        wanted, _ = requested_props(body)
        return multistatus([prop_response(href(name), "card", wanted, owner, item=item)])
    if method == "PROPPATCH":
        return proppatch(href(name), body)
    raise DavError(405)


def check_match(headers, item):
    wanted = headers.get("if-match", "").strip()
    if wanted and wanted != "*" and wanted.removeprefix("W/") != etag_of(item):
        _note["refused"] = "etag " + etag_of(item)
        raise DavError(412)


# Properties of the card that only the page knows: a client that drops the
# vCard property it does not know must not erase them.
KEPT = {
    "pronouns": ("X-PRONOUNS",),
    "file_as": ("X-FILE-AS", "SORT-STRING"),
    "custom": ("X-CUSTOM",),
    "starred": ("CATEGORIES",),
    "uid": ("UID",),
}


def merge_device_card(current, contact, present, names):
    """A card as a device sent it over the card here: what the device may
    not know stays. present: the vCard properties of the card it sent;
    names: the names of the other cards by id, for the related links."""
    for key, props in KEPT.items():
        if not present & set(props) and current.get(key):
            contact[key] = current[key]
    # A client that drops the date labelled as a hidden birthday must not
    # lose it; a birthday it sends instead puts the card back into the
    # calendars.
    if current.get("hidden_birthday") and not contact.get("hidden_birthday") and not contact.get("birthday"):
        contact["hidden_birthday"] = current["hidden_birthday"]
    contact["related"] = contacts.keep_links(current.get("related"), contact.get("related"), names)
    return contact


def put_card(name, headers, body, owner):
    cards = vcard.split_cards(body)
    if len(cards) != 1:
        raise DavError(400, q(CARD, "valid-address-data"), "One vCard is expected.")
    props = cards[0]
    present = {prop_name for _, prop_name, _, _ in props}
    resources = cards_by_name(owner)
    item = resources.get(name)
    if headers.get("if-none-match", "").strip() == "*" and item:
        raise DavError(412)
    if headers.get("if-match") and not item:
        raise DavError(412)
    _note.update({"uid": next((value.strip() for _, prop, _, value in props if prop == "UID"), ""), "exists": bool(item)})
    if vcard.is_group(props):
        _note["kind"] = "group"
        return put_group(name, headers, props, item, owner)
    if item and is_group_item(item):
        raise DavError(409, None, "This name is a group.")
    contact, photo = vcard.card_to_contact(props)
    if item:
        check_match(headers, item)
        names = {other["id"]: vcard.display_name(contacts.plain(other)) for other in resources.values() if not is_group_item(other)}
        contact = merge_device_card(contacts.plain(item), contact, present, names)
    photo_hash = ""
    if photo:
        data, content_type = photo
        photo_hash = hashlib.sha256(data).hexdigest()
        if item and item.get("photo_hash") == photo_hash and item.get("photo"):
            contact["photo"] = item["photo"]
        elif len(data) <= contacts.photo_max_bytes():
            contact["photo"] = contacts.put_photo(owner, data, content_type)
        else:
            # Too large to keep: the photo the card had stays.
            contact["photo"], photo_hash = (item or {}).get("photo", ""), (item or {}).get("photo_hash", "")
    try:
        if item:
            changes = contacts.changed_fields(item, contacts.clean_contact(contact, owner, blank_ok=True))
            saved = contacts.save_contact(owner, item["id"], {**contact, "etag": item["etag"]}, "sync", photo_hash)
            status = 204
            if changes:
                log(owner, "changed", saved, changes)
        else:
            saved = contacts.create_contact(owner, contact, "sync", photo_hash, name)
            status = 201
            log(owner, "created", saved)
    except contacts.ApiError as exc:
        _note["refused"] = exc.code + (":" + exc.detail if exc.detail else "")
        if exc.code == "changed" or exc.code == "deleted":
            raise DavError(412) from None
        if exc.code == "too_many_contacts":
            raise DavError(507) from None
        raise DavError(400, q(CARD, "valid-address-data"), exc.message + (": " + exc.detail if exc.detail else "")) from None
    _note["id"] = saved["id"]
    return respond(status, "", extra={"ETag": '"%s"' % saved["etag"]})


# An Apple group: its name and its members by the UID of their cards.
def put_group(name, headers, props, item, owner):
    if item and not is_group_item(item):
        raise DavError(409, None, "This name is a card.")
    title, uid, members = vcard.card_to_group(props)
    try:
        title = contacts.clean_group_name(title)
        if item:
            check_match(headers, item)
            changes = {"name": title, "members": members}
            if uid:
                changes["uid"] = uid
            contacts.transact([contacts.group_put(item, changes, contacts.now_iso())])
            saved = contacts.get_group(owner, item["id"])
            status = 204
            if title != item.get("name"):
                log(owner, "group_renamed", name=title, detail=item.get("name", ""))
            if sorted(members) != sorted(item.get("members") or []):
                log(owner, "group_members", name=title)
        else:
            saved = contacts.store_group(owner, title, members, uid, name)
            status = 201
            log(owner, "group_created", name=title)
    except contacts.ApiError as exc:
        if exc.code == "too_many_groups":
            raise DavError(507) from None
        raise DavError(400, q(CARD, "valid-address-data"), exc.message) from None
    except Exception as exc:
        if contacts.conditional_failed(exc):
            raise DavError(412) from None
        raise
    return respond(status, "", extra={"ETag": etag_of(saved)})


def log(owner, action, card=None, fields=None, name="", detail=""):
    """A line of the sync log: what this device changed in the book."""
    contacts.log_sync(owner, "dav:" + (_note.get("device") or "CardDAV"), action, target="book",
                      name=name or (vcard.display_name(contacts.plain(card)) or contacts.first_value(card) if card else ""),
                      fields=fields, detail=detail, contact_id=(card or {}).get("id", ""))


# PROPFIND and PROPPATCH ------------------------------------------------------------

OWNER_PRIVILEGES = ["all", "read", "write", "write-content", "write-properties", "bind", "unbind", "read-acl", "read-current-user-privilege-set"]

ALLPROP = {
    "principal": [q(DAV, "resourcetype"), q(DAV, "displayname"), q(DAV, "current-user-principal"), q(CARD, "addressbook-home-set")],
    "home": [q(DAV, "resourcetype"), q(DAV, "displayname")],
    "book": [q(DAV, "resourcetype"), q(DAV, "displayname"), q(CS, "getctag"), q(DAV, "sync-token")],
    "card": [q(DAV, "resourcetype"), q(DAV, "getetag"), q(DAV, "getcontenttype"), q(DAV, "getlastmodified")],
}
PATHS = {"principal": PRINCIPAL, "home": HOME, "book": BOOK}


def requested_props(body):
    """The names of the properties asked for, and whether address-data is among them."""
    if not body.strip():
        return None, None
    root = ElementTree.fromstring(body)
    prop = root.find(q(DAV, "prop"))
    if prop is None:
        return None, None
    wanted = [child.tag for child in prop]
    data = prop.find(q(CARD, "address-data"))
    return wanted, data


def propfind(kind, headers, body, owner):
    wanted, _ = requested_props(body)
    _note["props"] = [name.split("}")[-1] for name in wanted or []][:20]
    depth = headers.get("depth", "1" if kind != "principal" else "0").strip()
    responses = [prop_response(PATHS[kind], kind, wanted, owner)]
    if depth != "0":
        if kind == "home":
            responses.append(prop_response(BOOK, "book", wanted, owner))
        elif kind == "book":
            for name, item in sorted(cards_by_name(owner).items()):
                responses.append(prop_response(href(name), "card", wanted, owner, item=item))
    return multistatus(responses)


def proppatch(path, body):
    root = ElementTree.fromstring(body) if body.strip() else ElementTree.Element(q(DAV, "propertyupdate"))
    names = [child.tag for element in root.iter() if element.tag == q(DAV, "prop") for child in element]
    response = ElementTree.Element(q(DAV, "response"))
    ElementTree.SubElement(response, q(DAV, "href")).text = path
    propstat = ElementTree.SubElement(response, q(DAV, "propstat"))
    prop = ElementTree.SubElement(propstat, q(DAV, "prop"))
    for name in names:
        ElementTree.SubElement(prop, name)
    ElementTree.SubElement(propstat, q(DAV, "status")).text = "HTTP/1.1 403 Forbidden"
    return multistatus([response])


def prop_response(path, kind, wanted, owner, item=None, address_data=None):
    found, missing = [], []
    for name in wanted or ALLPROP[kind]:
        element = prop_value(name, kind, owner, item, address_data)
        (found if element is not None else missing).append(element if element is not None else ElementTree.Element(name))
    response = ElementTree.Element(q(DAV, "response"))
    ElementTree.SubElement(response, q(DAV, "href")).text = path
    for elements, status in ((found, "200 OK"), (missing, "404 Not Found")):
        if not elements:
            continue
        propstat = ElementTree.SubElement(response, q(DAV, "propstat"))
        prop = ElementTree.SubElement(propstat, q(DAV, "prop"))
        prop.extend(elements)
        ElementTree.SubElement(propstat, q(DAV, "status")).text = "HTTP/1.1 " + status
    return response


def with_href(name, target):
    element = ElementTree.Element(name)
    ElementTree.SubElement(element, q(DAV, "href")).text = target
    return element


def text_element(name, text):
    element = ElementTree.Element(name)
    element.text = text
    return element


def privileges(name, kinds):
    element = ElementTree.Element(name)
    for kind in kinds:
        ElementTree.SubElement(ElementTree.SubElement(element, q(DAV, "privilege")), q(DAV, kind))
    return element


def prop_value(name, kind, owner, item, address_data):
    if name == q(DAV, "resourcetype"):
        element = ElementTree.Element(name)
        if kind != "card":
            ElementTree.SubElement(element, q(DAV, "collection"))
        if kind == "principal":
            ElementTree.SubElement(element, q(DAV, "principal"))
        if kind == "book":
            ElementTree.SubElement(element, q(CARD, "addressbook"))
        return element
    if name in (q(DAV, "current-user-principal"), q(DAV, "principal-URL"), q(DAV, "owner")):
        return with_href(name, PRINCIPAL)
    if name == q(DAV, "principal-collection-set") and kind == "principal":
        return with_href(name, ROOT)
    if name == q(CARD, "addressbook-home-set") and kind == "principal":
        return with_href(name, HOME)
    if name == q(DAV, "current-user-privilege-set"):
        # The owner may do everything, everywhere. Apple Contacts takes the
        # privileges of the home for its address book: with read alone there,
        # it adds new cards but keeps every edit of a card from the server as
        # a new linked card instead of changing it.
        return privileges(name, OWNER_PRIVILEGES)
    if name == q(DAV, "displayname") and kind != "card":
        return text_element(name, {"principal": owner, "home": owner, "book": os.environ.get("BOOK_NAME", "Contacts")}[kind])
    if kind == "book":
        if name in (q(CS, "getctag"), q(DAV, "sync-token")):
            return text_element(name, sync_token(owner))
        if name == q(CARD, "addressbook-description"):
            return text_element(name, vcard.site_domain())
        if name == q(CARD, "supported-address-data"):
            element = ElementTree.Element(name)
            ElementTree.SubElement(element, q(CARD, "address-data-type"), {"content-type": "text/vcard", "version": "3.0"})
            return element
        if name == q(CARD, "max-resource-size"):
            return text_element(name, str(BODY_MAX))
        if name == q(DAV, "supported-report-set"):
            element = ElementTree.Element(name)
            for report_name in (q(CARD, "addressbook-multiget"), q(CARD, "addressbook-query"), q(DAV, "sync-collection")):
                ElementTree.SubElement(ElementTree.SubElement(ElementTree.SubElement(element, q(DAV, "supported-report")), q(DAV, "report")), report_name)
            return element
    if kind == "card" and item is not None:
        if name == q(DAV, "getetag"):
            return text_element(name, etag_of(item))
        if name == q(DAV, "getcontenttype"):
            return text_element(name, "text/vcard; charset=utf-8")
        if name == q(DAV, "getlastmodified"):
            return text_element(name, http_date(item.get("modified")))
        if name == q(CARD, "address-data") and address_data is not None:
            return text_element(name, address_data)
    return None


# REPORT ----------------------------------------------------------------------------

def report(body, owner):
    root = ElementTree.fromstring(body)
    _note["report"] = root.tag.split("}")[-1]
    _note["hrefs"] = len(root.findall(q(DAV, "href")))
    _note["props"] = [name.split("}")[-1] for name in (requested_props(body)[0] or [])][:20]
    wanted, _ = requested_props(body)
    wanted = wanted or [q(DAV, "getetag")]
    if root.tag == q(CARD, "addressbook-multiget"):
        cards = cards_by_name(owner)
        names = []
        for element in root.findall(q(DAV, "href")):
            path = unquote(urlsplit((element.text or "").strip()).path)
            names.append(path[len(BOOK):] if path.startswith(BOOK) else path)
        return multistatus(card_responses([(name, cards.get(name)) for name in names], wanted, owner)[0])
    if root.tag == q(CARD, "addressbook-query"):
        # Clients ask with a filter only to list the cards; every card is sent.
        return multistatus(card_responses(sorted(cards_by_name(owner).items()), wanted, owner)[0])
    if root.tag == q(DAV, "sync-collection"):
        return sync_collection(root, wanted, owner)
    raise DavError(403, q(DAV, "supported-report"))


def card_responses(pairs, wanted, owner):
    """The responses for the cards, and whether some did not fit."""
    responses, used, full = [], 0, False
    for name, item in pairs:
        if item is None:
            responses.append(status_response(href(name), "404 Not Found"))
            continue
        text = card_text(item) if q(CARD, "address-data") in wanted else None
        used += len(text or "")
        if used > RESPONSE_BUDGET and text:
            # The client asks again for what did not fit.
            responses.append(status_response(href(name), "507 Insufficient Storage"))
            full = True
            continue
        responses.append(prop_response(href(name), "card", wanted, owner, item=item, address_data=text))
    return responses, full


def status_response(path, status):
    response = ElementTree.Element(q(DAV, "response"))
    ElementTree.SubElement(response, q(DAV, "href")).text = path
    ElementTree.SubElement(response, q(DAV, "status")).text = "HTTP/1.1 " + status
    return response


# Changes are timed to the millisecond, so a change in the millisecond of the
# token would be missed. The token is never newer than SETTLE_SECONDS ago: what
# changed since then comes once more in the next sync.
SETTLE_SECONDS = 2


def sync_token(owner, cards=None, gone=None):
    cards = cards if cards is not None else cards_by_name(owner)
    gone = gone if gone is not None else tombstones(owner)
    times = [item.get("modified", "") for item in cards.values()] + [item.get("at", "") for item in gone]
    newest = max(times) if times else "0"
    settled = (datetime.now(timezone.utc) - timedelta(seconds=SETTLE_SECONDS)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return sync_prefix() + min(newest, settled)


def sync_collection(root, wanted, owner):
    token = (root.findtext(q(DAV, "sync-token")) or "").strip()
    _note["token"] = token[len(sync_prefix()):] if token.startswith(sync_prefix()) else token
    cards = cards_by_name(owner)
    gone = tombstones(owner)
    since = ""
    if token:
        if not token.startswith(sync_prefix()):
            raise DavError(403, q(DAV, "valid-sync-token"))
        since = token[len(sync_prefix()):]
    changed = sorted((name, item) for name, item in cards.items() if item.get("modified", "") > since)
    removed = [status_response(href(card_name(item)), "404 Not Found") for item in gone if since and item.get("at", "") > since]
    responses, full = card_responses(changed, wanted, owner)
    _note.update({"changed": len(changed), "removed": len(removed)})
    # When cards did not fit, the old token makes the client ask again.
    new_token = (token or sync_prefix() + "0") if full else sync_token(owner, cards, gone)
    return multistatus(responses + removed, text_element(q(DAV, "sync-token"), new_token))


# Answers ---------------------------------------------------------------------------

def multistatus(responses, token=None):
    root = ElementTree.Element(q(DAV, "multistatus"))
    root.extend(responses)
    if token is not None:
        root.append(token)
    body = '<?xml version="1.0" encoding="utf-8"?>\n' + ElementTree.tostring(root, encoding="unicode")
    return respond(207, body, "application/xml; charset=utf-8")


def error_response(exc):
    if exc.condition:
        root = ElementTree.Element(q(DAV, "error"))
        ElementTree.SubElement(root, exc.condition)
        if exc.text:
            ElementTree.SubElement(root, q(DAV, "responsedescription")).text = exc.text
        return respond(exc.status, '<?xml version="1.0" encoding="utf-8"?>\n' + ElementTree.tostring(root, encoding="unicode"), "application/xml; charset=utf-8")
    return respond(exc.status, exc.text, "text/plain; charset=utf-8")


def respond(status, body, content_type=None, extra=None):
    headers = {"DAV": "1, 3, addressbook", "Cache-Control": "no-store"}
    if content_type:
        headers["Content-Type"] = content_type
    headers.update(extra or {})
    return {"statusCode": status, "headers": headers, "body": body}


def body_text(event):
    raw = event.get("body") or ""
    data = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode("utf-8")
    if len(data) > BODY_MAX:
        raise DavError(413)
    return data.decode("utf-8", "replace")


def http_date(value):
    try:
        return format_datetime(datetime.fromisoformat(str(value).replace("Z", "+00:00")), usegmt=True)
    except ValueError:
        return ""
