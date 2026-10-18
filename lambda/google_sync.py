"""Sync of the address book with Google Contacts (People API).

The address book is the source of truth. It keeps every field; Google gets
the fields it has and leaves out the rest (maiden name, pronouns, social
profiles, the links between related cards), and a sync never erases a field
here because Google lacks it. A card changed on both sides between two syncs
keeps the version here, which goes to Google again; the Google version is in
the sync log.

Sign-in. GET /api/google/start gives the consent page of Google with a state
kept as sk = OAUTH#<nonce>; Google comes back to GET /api/oauth/google (a
route without the Cognito authorizer), which trades the code for a refresh
token. The token is an SSM SecureString of its own per user
(GOOGLE_TOKEN_PREFIX/<user key>); the client secret is the SSM parameter
GOOGLE_SECRET_PARAMETER, which is put there by hand.

State, in the user's partition:
  sk = GOOGLE          connected, email, sync token, the groups and their
                       names at the last sync, status, last sync and error;
  sk = GLINK#<id>      a card and its Google contact: resource name, etag,
                       the fingerprints of both sides at the last sync
                       (lhash here, rhash there) and the photo of each side.
pk = GOOGLE, sk = <email> lists the users to sync.

A sync compares each side with its own fingerprint: a side changed when its
fingerprint differs. Comparing each side only with itself keeps a field that
Google writes a little differently from going back and forth.

Every change is a line of the sync log (contacts.log_sync) and, for a card,
a version in its history with the action "google".
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import contacts
import vcard

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
PEOPLE = "https://people.googleapis.com/v1/"
SCOPES = "openid email https://www.googleapis.com/auth/contacts"
STATE_SECONDS = 600
PERSON_FIELDS = ",".join([
    "names", "nicknames", "fileAses", "organizations", "phoneNumbers", "emailAddresses", "addresses", "urls",
    "birthdays", "events", "relations", "imClients", "sipAddresses", "userDefined", "biographies", "memberships",
    "photos", "metadata",
])
UPDATE_FIELDS = ",".join([
    "names", "nicknames", "fileAses", "organizations", "phoneNumbers", "emailAddresses", "addresses", "urls",
    "birthdays", "events", "relations", "imClients", "sipAddresses", "userDefined", "biographies", "memberships",
])
BATCH = 200
# Writes one sync makes at most; the rest wait for the next one, so that a
# first sync of a big book stays within the quota of Google and the timeout.
WRITES_PER_SYNC = 1000
PHOTOS_PER_SYNC = 40
LOCK_SECONDS = 280
MY_CONTACTS = "contactGroups/myContacts"
STARRED = "contactGroups/starred"

# Our labels and Google's types, both ways. A label Google does not know goes
# as the text of a custom type and comes back as that text.
TYPES = {
    "phones": {"mobile": "mobile", "home": "home", "work": "work", "main": "main", "home_fax": "homeFax",
               "work_fax": "workFax", "other_fax": "otherFax", "pager": "pager", "other": "other", "iphone": "iPhone",
               "apple_watch": "Apple Watch"},
    "emails": {"home": "home", "work": "work", "other": "other", "icloud": "iCloud", "school": "school"},
    "urls": {"homepage": "homePage", "home": "home", "work": "work", "blog": "blog", "profile": "profile",
             "other": "other", "school": "school"},
    "addresses": {"home": "home", "work": "work", "other": "other", "school": "school"},
    "dates": {"anniversary": "anniversary", "other": "other"},
    "related": {"mother": "mother", "father": "father", "parent": "parent", "brother": "brother", "sister": "sister",
                "child": "child", "spouse": "spouse", "partner": "partner", "friend": "friend",
                "assistant": "assistant", "manager": "manager"},
    "im": {"home": "home", "work": "work", "other": "other"},
}
PROTOCOLS = {"skype": "skype", "aim": "aim", "msn": "msn", "yahoo": "yahoo", "qq": "qq", "google talk": "googleTalk",
             "icq": "icq", "jabber": "jabber"}
SERVICE_OF = {"skype": "Skype", "aim": "AIM", "msn": "MSN", "yahoo": "Yahoo", "qq": "QQ", "googletalk": "Google Talk",
              "icq": "ICQ", "jabber": "Jabber", "netmeeting": "NetMeeting"}
# The fields of a card that go to Google; the others stay here only.
SYNCED = ["prefix", "first", "middle", "last", "suffix", "phonetic_first", "phonetic_middle", "phonetic_last",
          "nickname", "file_as", "company", "phonetic_company", "department", "title", "phones", "emails",
          "addresses", "urls", "birthday", "hidden_birthday", "dates", "related", "im", "custom", "notes", "starred"]


class GoogleError(Exception):
    def __init__(self, status, payload):
        super().__init__(f"{status} {payload}")
        self.status = status
        self.payload = payload

    @property
    def reason(self):
        error = self.payload.get("error") if isinstance(self.payload, dict) else None
        if isinstance(error, dict):
            return error.get("status") or error.get("message") or ""
        return str(error or "")


# Settings -------------------------------------------------------------------------

def client_id():
    return os.environ.get("GOOGLE_CLIENT_ID", "")


def enabled():
    return bool(client_id())


_secret = None


def client_secret():
    global _secret
    if _secret is None:
        _secret = ssm().get_parameter(Name=os.environ["GOOGLE_SECRET_PARAMETER"], WithDecryption=True)["Parameter"]["Value"]
    return _secret


def redirect_uri():
    return os.environ["SITE_URL"].rstrip("/") + "/api/oauth/google"


def token_parameter(owner):
    return f"{os.environ['GOOGLE_TOKEN_PREFIX']}/{contacts.user_key(owner)}"


def state_item(owner):
    return contacts.table().get_item(Key={"pk": contacts.user_pk(owner), "sk": "GOOGLE"}, ConsistentRead=True).get("Item") or {}


def save_state(owner, changes):
    item = {**state_item(owner), **changes, "pk": contacts.user_pk(owner), "sk": "GOOGLE"}
    contacts.table().put_item(Item=item)
    return item


def public_state(owner):
    item = state_item(owner)
    return {
        "enabled": enabled(),
        "connected": bool(item.get("connected")),
        "email": item.get("email", ""),
        "status": item.get("status", ""),
        "last_sync": item.get("last_sync", ""),
        "last_error": item.get("last_error", ""),
        "linked": int(item.get("linked", 0)),
    }


# Sign-in ---------------------------------------------------------------------------

def start(owner):
    if not enabled():
        raise contacts.ApiError(409, "google_off")
    nonce = secrets.token_urlsafe(24)
    contacts.table().put_item(Item={"pk": "OAUTH", "sk": nonce, "owner": owner, "expires": int(time.time()) + STATE_SECONDS})
    query = urllib.parse.urlencode({
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        # A refresh token comes only with a consent page that was shown.
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": nonce,
    })
    return {"url": AUTH_URL + "?" + query}


def callback(params):
    """Google's redirect after the consent page: a page to go back to."""
    site = os.environ["SITE_URL"].rstrip("/")
    back = lambda result: {"statusCode": 302, "headers": {"Location": f"{site}/#/google/{result}", "Cache-Control": "no-store"}, "body": ""}  # noqa: E731
    nonce = params.get("state") or ""
    state = contacts.table().get_item(Key={"pk": "OAUTH", "sk": nonce}).get("Item") if nonce else None
    if state:
        contacts.table().delete_item(Key={"pk": "OAUTH", "sk": nonce})
    if not state or int(state.get("expires", 0)) < time.time():
        return back("expired")
    if params.get("error") or not params.get("code"):
        return back("denied")
    owner = state["owner"]
    try:
        tokens = post_form(TOKEN_URL, {
            "code": params["code"], "client_id": client_id(), "client_secret": client_secret(),
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
        })
    except GoogleError:
        logger.exception("code of %s not traded", owner)
        return back("failed")
    if "contacts" not in tokens.get("scope", "") or not tokens.get("refresh_token"):
        return back("scope")
    ssm().put_parameter(Name=token_parameter(owner), Value=tokens["refresh_token"], Type="SecureString", Overwrite=True)
    email = id_token_email(tokens.get("id_token", ""))
    previous = state_item(owner)
    # Another Google account starts over: its contacts are matched afresh.
    fresh = previous.get("email") and previous.get("email") != email
    if fresh:
        drop_links(owner)
    save_state(owner, {
        "connected": True, "email": email, "status": "connected", "last_error": "",
        **contacts.sync_ok_state(),
        **({"sync_token": "", "groups": {}} if fresh or not previous else {}),
    })
    contacts.table().put_item(Item={"pk": "GOOGLE", "sk": owner, "at": contacts.now_iso()})
    contacts.log_sync(owner, "google", "connected", detail=email)
    start_sync(owner)
    return back("connected")


def id_token_email(token):
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(data.get("email", ""))
    except (IndexError, ValueError):
        return ""


def disconnect(owner):
    """The links go; the cards here and the contacts in Google both stay."""
    try:
        refresh = ssm().get_parameter(Name=token_parameter(owner), WithDecryption=True)["Parameter"]["Value"]
        post_form(REVOKE_URL, {"token": refresh})
    except Exception:
        logger.warning("token of %s not revoked", owner, exc_info=True)
    try:
        ssm().delete_parameter(Name=token_parameter(owner))
    except Exception:
        logger.warning("token parameter of %s not deleted", owner, exc_info=True)
    drop_links(owner)
    contacts.table().delete_item(Key={"pk": contacts.user_pk(owner), "sk": "GOOGLE"})
    contacts.table().delete_item(Key={"pk": "GOOGLE", "sk": owner})
    contacts.log_sync(owner, "google", "disconnected")
    return {"disconnected": True}


def drop_links(owner):
    keys = [{"pk": item["pk"], "sk": item["sk"]} for item in link_items(owner)]
    with contacts.table().batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key=key)


def start_sync(owner):
    """A sync in the background, by the function of the schedule."""
    name = os.environ.get("GOOGLE_SYNC_FUNCTION", "")
    if not name:
        return
    lambda_client().invoke(FunctionName=name, InvocationType="Event", Payload=json.dumps({"action": "sync", "owner": owner}).encode())


# HTTP ------------------------------------------------------------------------------

def request(method, url, token=None, body=None, params=None, raw=False):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as answer:
            content = answer.read()
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {"error": text[:300]}
        raise GoogleError(exc.code, payload) from None
    if raw:
        return content
    return json.loads(content) if content else {}


def post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as answer:
            content = answer.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            payload = {}
        raise GoogleError(exc.code, payload) from None
    return json.loads(content) if content else {}


def access_token(owner):
    try:
        refresh = ssm().get_parameter(Name=token_parameter(owner), WithDecryption=True)["Parameter"]["Value"]
    except Exception as exc:
        raise GoogleError(401, {"error": "invalid_grant"}) from exc
    answer = post_form(TOKEN_URL, {"client_id": client_id(), "client_secret": client_secret(),
                                   "refresh_token": refresh, "grant_type": "refresh_token"})
    return answer["access_token"]


# Mapping -------------------------------------------------------------------------------

def to_type(key, label):
    return TYPES.get(key, {}).get(label, label or "other")


def from_type(key, value):
    value = str(value or "")
    for ours, theirs in TYPES.get(key, {}).items():
        if theirs.lower() == value.lower():
            return ours
    return value or "other"


def to_date(value):
    match = re.fullmatch(r"(\d{4}|-)-(\d{2})-(\d{2})", value or "")
    if not match:
        return None
    year, month, day = match.groups()
    date = {"month": int(month), "day": int(day)}
    if year != "-":
        date["year"] = int(year)
    return date


def from_date(date):
    if not isinstance(date, dict) or not date.get("month") or not date.get("day"):
        return ""
    year = f"{int(date['year']):04d}" if date.get("year") else "-"
    return f"{year}-{int(date['month']):02d}-{int(date['day']):02d}"


def contact_to_person(contact, group_resources):
    """The Google person of a card: the fields Google has; group_resources are
    the contact groups of the card."""
    c = contact
    person = {}
    name = {key: c.get(ours) for key, ours in (
        ("honorificPrefix", "prefix"), ("givenName", "first"), ("middleName", "middle"), ("familyName", "last"),
        ("honorificSuffix", "suffix"), ("phoneticGivenName", "phonetic_first"), ("phoneticMiddleName", "phonetic_middle"),
        ("phoneticFamilyName", "phonetic_last")) if c.get(ours)}
    person["names"] = [name] if name else []
    person["nicknames"] = [{"value": c["nickname"]}] if c.get("nickname") else []
    person["fileAses"] = [{"value": c["file_as"]}] if c.get("file_as") else []
    organization = {key: c.get(ours) for key, ours in (("name", "company"), ("phoneticName", "phonetic_company"),
                                                        ("department", "department"), ("title", "title")) if c.get(ours)}
    person["organizations"] = [organization] if organization else []
    person["phoneNumbers"] = [{"value": e["value"], "type": to_type("phones", e.get("label"))} for e in c.get("phones") or []]
    person["emailAddresses"] = [{"value": e["value"], "type": to_type("emails", e.get("label"))} for e in c.get("emails") or []]
    person["addresses"] = [{
        "type": to_type("addresses", e.get("label")),
        **{key: e[ours] for key, ours in (("streetAddress", "street"), ("extendedAddress", "extended"), ("poBox", "po_box"),
                                          ("city", "city"), ("region", "region"), ("postalCode", "postal_code"),
                                          ("country", "country"), ("countryCode", "country_code")) if e.get(ours)},
    } for e in c.get("addresses") or []]
    person["urls"] = [{"value": e["value"], "type": to_type("urls", e.get("label"))} for e in c.get("urls") or []]
    birthday = to_date(c.get("birthday"))
    person["birthdays"] = [{"date": birthday}] if birthday else []
    person["events"] = [{"date": to_date(e["value"]), "type": to_type("dates", e.get("label"))} for e in c.get("dates") or [] if to_date(e.get("value"))]
    person["relations"] = [{"person": e["value"], "type": to_type("related", e.get("label"))} for e in c.get("related") or [] if e.get("value")]
    person["imClients"] = [{
        "username": e["value"], "type": to_type("im", e.get("label")),
        "protocol": PROTOCOLS.get((e.get("service") or "").lower(), e.get("service") or "other"),
    } for e in c.get("im") or [] if (e.get("service") or "").lower() != "sip"]
    person["sipAddresses"] = [{"value": e["value"], "type": to_type("im", e.get("label"))} for e in c.get("im") or [] if (e.get("service") or "").lower() == "sip"]
    person["userDefined"] = [{"key": e.get("label") or "other", "value": e["value"]} for e in c.get("custom") or []]
    # A custom field, not an event: the birthdays calendar of Google may show
    # the events of a contact too.
    if c.get("hidden_birthday"):
        person["userDefined"].insert(0, {"key": vcard.hidden_birthday_label(), "value": c["hidden_birthday"]})
    person["biographies"] = [{"value": c["notes"], "contentType": "TEXT_PLAIN"}] if c.get("notes") else []
    memberships = [MY_CONTACTS, *sorted(group_resources)]
    if c.get("starred"):
        memberships.append(STARRED)
    person["memberships"] = [{"contactGroupMembership": {"contactGroupResourceName": name}} for name in memberships]
    return person


def own(entries):
    """The entries a contact holds itself, not those of a Google profile."""
    return [e for e in entries or [] if (e.get("metadata") or {}).get("source", {}).get("type", "CONTACT") == "CONTACT"]


def person_to_contact(person):
    """The fields of SYNCED from a Google person, and its contact groups."""
    c = {}
    names = own(person.get("names"))
    if names:
        n = names[0]
        for key, ours in (("honorificPrefix", "prefix"), ("givenName", "first"), ("middleName", "middle"),
                          ("familyName", "last"), ("honorificSuffix", "suffix"), ("phoneticGivenName", "phonetic_first"),
                          ("phoneticMiddleName", "phonetic_middle"), ("phoneticFamilyName", "phonetic_last")):
            if n.get(key):
                c[ours] = n[key]
    if own(person.get("nicknames")):
        c["nickname"] = own(person["nicknames"])[0].get("value", "")
    if own(person.get("fileAses")):
        c["file_as"] = own(person["fileAses"])[0].get("value", "")
    if own(person.get("organizations")):
        o = own(person["organizations"])[0]
        for key, ours in (("name", "company"), ("phoneticName", "phonetic_company"), ("department", "department"), ("title", "title")):
            if o.get(key):
                c[ours] = o[key]
    c["phones"] = [{"label": from_type("phones", e.get("type")), "value": e["value"]} for e in own(person.get("phoneNumbers")) if e.get("value")]
    c["emails"] = [{"label": from_type("emails", e.get("type")), "value": e["value"]} for e in own(person.get("emailAddresses")) if e.get("value")]
    c["addresses"] = []
    for e in own(person.get("addresses")):
        address = {"label": from_type("addresses", e.get("type"))}
        for key, ours in (("streetAddress", "street"), ("extendedAddress", "extended"), ("poBox", "po_box"), ("city", "city"),
                          ("region", "region"), ("postalCode", "postal_code"), ("country", "country"), ("countryCode", "country_code")):
            if e.get(key):
                address[ours] = e[key]
        if len(address) > 1:
            c["addresses"].append(address)
    c["urls"] = [{"label": from_type("urls", e.get("type")), "value": e["value"]} for e in own(person.get("urls")) if e.get("value")]
    birthdays = [from_date(e.get("date")) for e in own(person.get("birthdays"))]
    if any(birthdays):
        c["birthday"] = next(date for date in birthdays if date)
    c["dates"] = [{"label": from_type("dates", e.get("type")), "value": from_date(e.get("date"))} for e in own(person.get("events")) if from_date(e.get("date"))]
    c["related"] = [{"label": from_type("related", e.get("type")), "value": e["person"]} for e in own(person.get("relations")) if e.get("person")]
    c["im"] = [{
        "service": SERVICE_OF.get(str(e.get("protocol", "")).lower(), e.get("protocol") or ""),
        "label": from_type("im", e.get("type")), "value": e["username"],
    } for e in own(person.get("imClients")) if e.get("username")]
    c["im"] += [{"service": "SIP", "label": from_type("im", e.get("type")), "value": e["value"]} for e in own(person.get("sipAddresses")) if e.get("value")]
    custom = [e for e in own(person.get("userDefined")) if e.get("value")]
    hidden = next((vcard.parse_date(e["value"]) for e in custom if vcard.is_hidden_birthday(e.get("key"))), "")
    if hidden:
        c["hidden_birthday"] = hidden
    c["custom"] = [{"label": e.get("key") or "other", "value": e["value"]} for e in custom if not vcard.is_hidden_birthday(e.get("key"))]
    if own(person.get("biographies")):
        c["notes"] = own(person["biographies"])[0].get("value", "")
    groups = []
    for membership in person.get("memberships") or []:
        name = (membership.get("contactGroupMembership") or {}).get("contactGroupResourceName", "")
        if name == STARRED:
            c["starred"] = True
        elif name and name != MY_CONTACTS:
            groups.append(name)
    return c, groups


def merge_first(card, remote):
    """A card and the Google contact matched to it at the first sync: the
    values of the card stay, Google adds what the card lacks."""
    merged = dict(card)
    for key in SYNCED:
        value = remote.get(key)
        if value in (None, "", [], False):
            continue
        if isinstance(value, list):
            have = list(card.get(key) or [])
            seen = {canon_entry(entry) for entry in have}
            merged[key] = have + [entry for entry in value if canon_entry(entry) not in seen]
        elif not card.get(key):
            merged[key] = value
    merged["related"] = contacts.keep_links(card.get("related"), merged.get("related"))
    return merged


def canon_entry(entry):
    """An entry as Google may write it back: digits of a phone, the email in
    lower case, the rest without its label."""
    value = {k: v for k, v in entry.items() if k not in ("label", "contact")}
    if "value" in value and re.fullmatch(r"[\d\s()+.-]+", str(value["value"])):
        return "tel:" + contacts.phone_digits(value["value"])
    return json.dumps({k: str(v).strip().lower() for k, v in value.items()}, sort_keys=True, ensure_ascii=False)


def canon(contact, groups):
    """The fingerprint of what one side has of the synced fields."""
    subset = {key: contact.get(key) for key in SYNCED if contact.get(key) not in (None, "", [], False)}
    for key in ("related",):
        if key in subset:
            subset[key] = [{k: v for k, v in entry.items() if k != "contact"} for entry in subset[key]]
    data = json.dumps({"c": subset, "g": sorted(groups)}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def photo_of(person):
    for photo in person.get("photos") or []:
        if not photo.get("default") and (photo.get("metadata") or {}).get("source", {}).get("type", "CONTACT") == "CONTACT":
            return photo.get("url", "")
    return ""


def display(contact):
    return vcard.display_name(contact) or contacts.first_value(contact) or "—"


# Sync -------------------------------------------------------------------------------

def handler(event, context):
    if not enabled():
        return {"skipped": "off"}
    if event.get("action") == "sync" and event.get("owner"):
        return sync_user(event["owner"])
    results = {}
    for item in registered():
        try:
            results[item] = sync_user(item)
        except Exception:
            logger.exception("sync of %s failed", item)
            results[item] = "error"
    return results


def registered():
    from boto3.dynamodb.conditions import Key

    page = contacts.table().query(KeyConditionExpression=Key("pk").eq("GOOGLE"))
    return [item["sk"] for item in page.get("Items", [])]


def link_items(owner):
    from boto3.dynamodb.conditions import Key

    items, kwargs = [], {"KeyConditionExpression": Key("pk").eq(contacts.user_pk(owner)) & Key("sk").begins_with("GLINK#")}
    while True:
        page = contacts.table().query(**kwargs)
        items.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def put_link(owner, contact_id, **fields):
    contacts.table().put_item(Item={"pk": contacts.user_pk(owner), "sk": "GLINK#" + contact_id, "id": contact_id, **fields})


def lock(owner):
    """One sync of a user at a time: the schedule and "Sync now" may meet."""
    now = int(time.time())
    try:
        contacts.table().update_item(
            Key={"pk": contacts.user_pk(owner), "sk": "GOOGLE"},
            UpdateExpression="SET lock_until = :until",
            ConditionExpression="attribute_exists(pk) AND (attribute_not_exists(lock_until) OR lock_until < :now)",
            ExpressionAttributeValues={":until": now + LOCK_SECONDS, ":now": now},
        )
        return True
    except Exception as exc:
        if contacts.conditional_failed(exc):
            return False
        raise


def sync_user(owner):
    state = state_item(owner)
    if not state.get("connected"):
        return "not connected"
    if not lock(owner):
        return "busy"
    run = Run(owner, state)
    try:
        run.go()
        save_state(owner, {"status": "ok", "last_sync": contacts.now_iso(), "last_error": "", "sync_token": run.sync_token,
                           "groups": run.group_state, "linked": len(run.links), "lock_until": 0, **contacts.sync_ok_state()})
        return run.summary
    except GoogleError as exc:
        reauth = exc.status in (400, 401) and "invalid_grant" in json.dumps(exc.payload)
        status = "reauth" if reauth else "error"
        save_state(owner, {"status": status, "last_error": str(exc)[:300], "lock_until": 0,
                           "sync_token": run.sync_token if run.sync_token is not None else state.get("sync_token", ""),
                           **contacts.sync_failed_state(owner, "google", state, status, str(exc)[:300])})
        contacts.log_sync(owner, "google", "error", detail=str(exc)[:500])
        logger.exception("sync of %s failed", owner)
        return "error"
    except Exception as exc:
        save_state(owner, {"status": "error", "last_error": str(exc)[:300], "lock_until": 0,
                           **contacts.sync_failed_state(owner, "google", state, "error", str(exc)[:300])})
        contacts.log_sync(owner, "google", "error", detail=str(exc)[:500])
        raise


class Run:
    """One sync of one user."""

    def __init__(self, owner, state):
        self.owner = owner
        self.state = state
        self.token = None
        self.sync_token = state.get("sync_token", "")
        self.group_state = dict(state.get("groups") or {})
        self.links = {}
        self.writes = 0
        self.summary = {"to_book": 0, "to_google": 0, "conflicts": 0, "deleted": 0}

    def call(self, method, path, body=None, params=None):
        return request(method, PEOPLE + path, self.token, body, params)

    def log(self, source, action, contact=None, fields=None, detail="", name=""):
        contacts.log_sync(self.owner, source, action, target="google" if source == "to_google" else "book",
                          name=name or (display(contact) if contact else ""), fields=fields, detail=detail,
                          contact_id=(contact or {}).get("id", ""))

    def go(self):
        self.token = access_token(self.owner)
        self.cards = {item["id"]: contacts.plain(item) for item in contacts.all_current(self.owner)}
        self.links = {item["id"]: contacts.plain(item) for item in link_items(self.owner)}
        self.sync_groups()
        people, full = self.changed_people()
        self.pull(people, full)
        self.push()
        self.photos()

    # Groups ------------------------------------------------------------------

    def sync_groups(self):
        remote = {}
        page_token = None
        while True:
            answer = self.call("GET", "contactGroups", params={"groupFields": "name,groupType", "pageSize": 1000, **({"pageToken": page_token} if page_token else {})})
            for group in answer.get("contactGroups") or []:
                if group.get("groupType") == "USER_CONTACT_GROUP":
                    remote[group["resourceName"]] = group
            page_token = answer.get("nextPageToken")
            if not page_token:
                break
        local = {item["id"]: contacts.plain(item) for item in contacts.group_items(self.owner)}
        state = self.group_state
        for group_id, known in list(state.items()):
            mine, theirs = local.get(group_id), remote.get(known["resource"])
            if mine and theirs:
                if mine["name"] != known["name"]:
                    self.call("PUT", known["resource"], {"contactGroup": {"name": mine["name"], "etag": theirs.get("etag")}})
                    self.log("to_google", "group_renamed", name=mine["name"], detail=known["name"])
                elif theirs["name"] != known["name"]:
                    contacts.update_group(self.owner, group_id, {"name": contacts.clean_group_name(theirs["name"]), "etag": mine["etag"]})
                    self.log("google", "group_renamed", name=theirs["name"], detail=known["name"])
                    mine["name"] = theirs["name"]
                state[group_id] = {"resource": known["resource"], "name": mine["name"]}
            elif mine and not theirs:
                contacts.delete_group(self.owner, group_id)
                self.log("google", "group_deleted", name=mine["name"])
                del state[group_id]
                local.pop(group_id)
            elif theirs and not mine:
                self.call("DELETE", known["resource"], params={"deleteContacts": "false"})
                self.log("to_google", "group_deleted", name=theirs["name"])
                del state[group_id]
                remote.pop(known["resource"])
            else:
                del state[group_id]
        mapped = {known["resource"] for known in state.values()}
        by_name = {contacts.fold(group["name"]): resource for resource, group in remote.items() if resource not in mapped}
        for group_id, mine in local.items():
            if group_id in state:
                continue
            resource = by_name.pop(contacts.fold(mine["name"]), None)
            if not resource:
                resource = self.call("POST", "contactGroups", {"contactGroup": {"name": mine["name"]}})["resourceName"]
                self.log("to_google", "group_created", name=mine["name"])
            state[group_id] = {"resource": resource, "name": mine["name"]}
        mapped = {known["resource"] for known in state.values()}
        for resource, group in remote.items():
            if resource in mapped:
                continue
            item = contacts.store_group(self.owner, contacts.clean_group_name(group["name"]), [])
            state[item["id"]] = {"resource": resource, "name": item["name"]}
            self.log("google", "group_created", name=item["name"])
        self.group_of_resource = {known["resource"]: group_id for group_id, known in state.items()}
        self.resource_of_group = {group_id: known["resource"] for group_id, known in state.items()}
        self.groups = {item["id"]: item for item in contacts.group_items(self.owner)}

    def card_groups(self, card):
        uid = contacts.dav_uid(card)
        return [group_id for group_id, group in self.groups.items() if uid in (group.get("members") or [])]

    def card_resources(self, card):
        return [self.resource_of_group[group_id] for group_id in self.card_groups(card) if group_id in self.resource_of_group]

    # Google -> book -------------------------------------------------------------

    def changed_people(self):
        """The people changed since the last sync, or all of them (full)."""
        people, page_token = [], None
        token = self.sync_token
        while True:
            params = {"personFields": PERSON_FIELDS, "pageSize": 1000, "requestSyncToken": "true"}
            if page_token:
                params["pageToken"] = page_token
            if token:
                params["syncToken"] = token
            try:
                answer = self.call("GET", "people/me/connections", params=params)
            except GoogleError as exc:
                # An expired sync token starts a full read.
                if token and exc.status in (400, 410):
                    token, people, page_token = "", [], None
                    continue
                raise
            people.extend(answer.get("connections") or [])
            page_token = answer.get("nextPageToken")
            if not page_token:
                self.sync_token = answer.get("nextSyncToken", "")
                return people, not token

    def pull(self, people, full):
        by_resource = {link["resource"]: card_id for card_id, link in self.links.items()}
        seen = set()
        for person in people:
            resource = person.get("resourceName", "")
            seen.add(resource)
            card_id = by_resource.get(resource)
            if (person.get("metadata") or {}).get("deleted"):
                if card_id:
                    self.remote_deleted(card_id)
                continue
            remote, resources = person_to_contact(person)
            group_ids = [self.group_of_resource[name] for name in resources if name in self.group_of_resource]
            rhash = canon(remote, [self.groups.get(g, {}).get("name", "") for g in group_ids])
            if card_id and card_id in self.cards:
                self.remote_changed(card_id, person, remote, group_ids, rhash)
            elif card_id:
                # Deleted here since the last sync: it goes in Google too (push).
                continue
            else:
                self.remote_new(person, remote, group_ids, rhash)
        if full:
            # A full read lists every contact: a linked one missing from it
            # was deleted in Google.
            for card_id, link in list(self.links.items()):
                if link["resource"] not in seen and card_id in self.cards:
                    self.remote_deleted(card_id)

    def local_hash(self, card):
        return canon(card, [self.groups[g]["name"] for g in self.card_groups(card)])

    def apply_remote(self, card, remote, group_ids):
        """The Google fields over a card; the fields Google lacks stay."""
        merged = {key: value for key, value in card.items() if key not in SYNCED}
        merged.update({key: value for key, value in remote.items() if key in SYNCED})
        merged["related"] = contacts.keep_links(card.get("related"), remote.get("related"),
                                                {cid: vcard.display_name(other) for cid, other in self.cards.items()})
        current = contacts.get_current(self.owner, card["id"])
        changes = contacts.changed_fields(current, contacts.clean_contact(merged, self.owner, blank_ok=True))
        saved = contacts.save_contact(self.owner, card["id"], {**merged, "etag": current["etag"], "group_ids": group_ids}, "google",
                                      current.get("photo_hash"))
        saved = contacts.plain(contacts.get_current(self.owner, card["id"]) or saved)
        self.cards[card["id"]] = saved
        self.groups = {item["id"]: item for item in contacts.group_items(self.owner)}
        return saved, changes

    def remote_changed(self, card_id, person, remote, group_ids, rhash):
        link = self.links[card_id]
        card = self.cards[card_id]
        if rhash == link.get("rhash"):
            link["etag"] = person.get("etag", link.get("etag"))
            put_link(self.owner, card_id, **{k: v for k, v in link.items() if k not in ("pk", "sk", "id")})
            return
        local_changed = self.local_hash(card) != link.get("lhash")
        if local_changed:
            # Both sides changed: the address book wins and goes to Google
            # again in push(); the Google version is in the log.
            self.summary["conflicts"] += 1
            theirs = [key for key in SYNCED if (remote.get(key) or None) != (card.get(key) or None)]
            self.log("google", "conflict", card, fields=theirs, detail="kept_book")
            link["etag"] = person.get("etag", link.get("etag"))
            link["rhash"] = rhash
            put_link(self.owner, card_id, **{k: v for k, v in link.items() if k not in ("pk", "sk", "id")})
            return
        group_changes = sorted(set(group_ids) ^ set(self.card_groups(card)))
        saved, changes = self.apply_remote(card, remote, group_ids)
        self.summary["to_book"] += 1
        self.log("google", "changed", saved, fields=changes + (["groups"] if group_changes else []))
        self.links[card_id] = {**link, "etag": person.get("etag", ""), "rhash": rhash, "lhash": self.local_hash(saved)}
        put_link(self.owner, card_id, **{k: v for k, v in self.links[card_id].items() if k not in ("pk", "sk", "id")})
        self.pull_photo(card_id, person)

    def remote_new(self, person, remote, group_ids, rhash):
        # The first sync matches the contacts that are already here.
        unlinked = [card for card_id, card in self.cards.items() if card_id not in self.links]
        match = contacts.find_match(unlinked, remote)
        if match:
            merged = merge_first(match, remote)
            current = contacts.get_current(self.owner, match["id"])
            changes = contacts.changed_fields(current, contacts.clean_contact(merged, self.owner, blank_ok=True))
            keep = sorted(set(group_ids) | set(self.card_groups(match)))
            saved = contacts.save_contact(self.owner, match["id"], {**merged, "etag": current["etag"], "group_ids": keep}, "google",
                                          current.get("photo_hash"))
            saved = contacts.plain(contacts.get_current(self.owner, match["id"]) or saved)
            self.cards[saved["id"]] = saved
            self.groups = {item["id"]: item for item in contacts.group_items(self.owner)}
            self.log("google", "matched", saved, fields=changes)
        else:
            saved = contacts.create_contact(self.owner, {**remote, "group_ids": group_ids}, "google")
            saved = contacts.plain(contacts.get_current(self.owner, saved["id"]))
            self.cards[saved["id"]] = saved
            self.groups = {item["id"]: item for item in contacts.group_items(self.owner)}
            self.log("google", "created", saved)
        self.summary["to_book"] += 1
        # The local hash of a merged card differs from Google's: push() sends it.
        lhash = self.local_hash(saved) if not match else ""
        self.links[saved["id"]] = {"resource": person["resourceName"], "etag": person.get("etag", ""), "rhash": rhash, "lhash": lhash,
                                   "photo_local": "", "photo_remote": ""}
        put_link(self.owner, saved["id"], **self.links[saved["id"]])
        self.pull_photo(saved["id"], person)

    def remote_deleted(self, card_id):
        card = self.cards.get(card_id)
        link = self.links.pop(card_id, None)
        contacts.table().delete_item(Key={"pk": contacts.user_pk(self.owner), "sk": "GLINK#" + card_id})
        if not card:
            return
        if link and self.local_hash(card) != link.get("lhash"):
            # Changed here since: the card stays and goes to Google as new.
            self.log("google", "conflict", card, detail="deleted_changed")
            return
        current = contacts.get_current(self.owner, card_id)
        contacts.delete_contact(self.owner, card_id, current["etag"])
        self.cards.pop(card_id, None)
        self.summary["deleted"] += 1
        self.log("google", "deleted", card, detail="restorable")

    def pull_photo(self, card_id, person):
        link = self.links.get(card_id)
        url = photo_of(person)
        if not link or url == link.get("photo_remote"):
            return
        card = self.cards[card_id]
        # A photo here that has not gone to Google yet wins; photos() sends it.
        if card.get("photo") and card["photo"] != (link.get("photo_local") or ""):
            return
        if not url:
            link["photo_remote"] = ""
        else:
            data = request("GET", re.sub(r"=s\d+(-c)?$", "=s1024", url), raw=True)
            content_type = vcard.photo_type(data)
            if content_type and len(data) <= contacts.photo_max_bytes():
                path = contacts.put_photo(self.owner, data, content_type)
                current = contacts.get_current(self.owner, card_id)
                contacts.save_contact(self.owner, card_id, {**contacts.public_contact(current), "photo": path, "etag": current["etag"]},
                                      "google", hashlib.sha256(data).hexdigest())
                self.cards[card_id] = contacts.plain(contacts.get_current(self.owner, card_id))
                link["photo_local"] = path
                self.log("google", "photo", card)
            link["photo_remote"] = url
        put_link(self.owner, card_id, **{k: v for k, v in link.items() if k not in ("pk", "sk", "id")})

    # Book -> Google -------------------------------------------------------------

    def push(self):
        creates, updates = [], []
        for card_id, card in self.cards.items():
            link = self.links.get(card_id)
            if not link:
                creates.append(card)
            elif self.local_hash(card) != link.get("lhash"):
                updates.append(card)
        gone = [card_id for card_id in self.links if card_id not in self.cards]
        for start in range(0, len(gone), 500):
            if self.writes >= WRITES_PER_SYNC:
                return
            chunk = gone[start:start + 500]
            self.call("POST", "people:batchDeleteContacts", {"resourceNames": [self.links[card_id]["resource"] for card_id in chunk]})
            self.writes += len(chunk)
            for card_id in chunk:
                link = self.links.pop(card_id)
                contacts.table().delete_item(Key={"pk": contacts.user_pk(self.owner), "sk": "GLINK#" + card_id})
                self.summary["to_google"] += 1
                contacts.log_sync(self.owner, "to_google", "deleted", target="google", name=link.get("name", ""), contact_id=card_id)
        for start in range(0, len(creates), BATCH):
            if self.writes >= WRITES_PER_SYNC:
                return
            chunk = creates[start:start + BATCH]
            answer = self.call("POST", "people:batchCreateContacts", {
                "contacts": [{"contactPerson": contact_to_person(card, self.card_resources(card))} for card in chunk],
                "readMask": PERSON_FIELDS,
            })
            self.writes += len(chunk)
            for card, created in zip(chunk, answer.get("createdPeople") or []):
                person = created.get("person") or {}
                if not person.get("resourceName"):
                    self.log("to_google", "error", card, detail=json.dumps(created.get("status", {}))[:300])
                    continue
                self.linked(card, person)
                self.summary["to_google"] += 1
                self.log("to_google", "created", card, fields=[key for key in SYNCED if card.get(key)])
        for start in range(0, len(updates), BATCH):
            if self.writes >= WRITES_PER_SYNC:
                return
            chunk = updates[start:start + BATCH]
            body = {
                "contacts": {self.links[card["id"]]["resource"]: {**contact_to_person(card, self.card_resources(card)),
                                                                  "etag": self.links[card["id"]].get("etag", "")} for card in chunk},
                "updateMask": UPDATE_FIELDS,
                "readMask": PERSON_FIELDS,
            }
            try:
                answer = self.call("POST", "people:batchUpdateContacts", body)
            except GoogleError as exc:
                # A contact changed in Google since: the next sync reads it first.
                self.log("to_google", "error", detail=str(exc)[:300])
                continue
            self.writes += len(chunk)
            results = answer.get("updateResult") or {}
            for card in chunk:
                link = self.links[card["id"]]
                person = (results.get(link["resource"]) or {}).get("person")
                if not person:
                    continue
                before = link.get("synced") or {}
                self.linked(card, person)
                self.summary["to_google"] += 1
                self.log("to_google", "changed", card, fields=[key for key in SYNCED if (card.get(key) or None) != (before.get(key) or None)] or None)

    def linked(self, card, person):
        remote, resources = person_to_contact(person)
        group_names = [self.groups.get(self.group_of_resource.get(name), {}).get("name", "") for name in resources]
        previous = self.links.get(card["id"]) or {}
        self.links[card["id"]] = {
            "resource": person["resourceName"], "etag": person.get("etag", ""),
            "lhash": self.local_hash(card), "rhash": canon(remote, [name for name in group_names if name]),
            "photo_local": previous.get("photo_local", ""), "photo_remote": previous.get("photo_remote", photo_of(person)),
            "name": display(card),
            # The synced fields as they went, for the log of the next change.
            "synced": {key: card.get(key) for key in SYNCED if card.get(key)},
        }
        put_link(self.owner, card["id"], **self.links[card["id"]])

    def photos(self):
        done = 0
        for card_id, link in self.links.items():
            card = self.cards.get(card_id)
            if not card or (card.get("photo") or "") == (link.get("photo_local") or ""):
                continue
            if done >= PHOTOS_PER_SYNC:
                return
            done += 1
            if card.get("photo"):
                answer = contacts.s3().get_object(Bucket=contacts.env("PHOTOS_BUCKET"), Key=card["photo"].lstrip("/"))
                data = answer["Body"].read()
                person = self.call("PATCH", link["resource"] + ":updateContactPhoto",
                                   {"photoBytes": base64.b64encode(data).decode("ascii"), "personFields": "photos,metadata"}).get("person") or {}
                link["photo_remote"] = photo_of(person)
            else:
                person = self.call("DELETE", link["resource"] + ":deleteContactPhoto", params={"personFields": "photos,metadata"}).get("person") or {}
                link["photo_remote"] = ""
            if person.get("etag"):
                link["etag"] = person["etag"]
            link["photo_local"] = card.get("photo") or ""
            put_link(self.owner, card_id, **{k: v for k, v in link.items() if k not in ("pk", "sk", "id")})
            self.log("to_google", "photo", card)


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
