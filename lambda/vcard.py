"""vCard to contact and back.

Reads what Apple Contacts and Google Contacts export: vCard 3.0, with the
older 2.1 forms (bare TYPE parameters, quoted-printable) and the 4.0 ones
(data: URIs, ANNIVERSARY) as well. Apple names a value's label with an item
group, item1.TEL + item1.X-ABLabel:_$!<Mobile>!$_ or a label of its own, and
keeps dates and related names that way too; Google writes the same groups.
Apple's groups are vCards of their own (card_to_group, group_to_card).

A contact is a dict of the fields in contacts.py (FIELDS); labels are the
keys of LABELS where the vCard names a known one, else the text as written.
Writing gives vCard 3.0 in the form Apple writes, which Google reads too.
"""

import base64
import binascii
import os
import quopri
import re

# Apple's own labels, _$!<Name>!$_, and the TYPE values, as the label keys of
# the page (fields.js).
APPLE_LABELS = {
    "home": "home", "work": "work", "other": "other", "mobile": "mobile",
    "main": "main", "homefax": "home_fax", "workfax": "work_fax", "otherfax": "other_fax",
    "pager": "pager", "iphone": "iphone", "homepage": "homepage", "school": "school",
    "anniversary": "anniversary", "mother": "mother", "father": "father", "parent": "parent",
    "brother": "brother", "sister": "sister", "child": "child", "friend": "friend",
    "spouse": "spouse", "partner": "partner", "assistant": "assistant", "manager": "manager",
    "applewatch": "apple_watch", "icloud": "icloud", "blog": "blog", "profile": "profile",
    "son": "son", "daughter": "daughter", "husband": "husband", "wife": "wife",
}
LABEL_TO_APPLE = {
    "home_fax": "HomeFAX", "work_fax": "WorkFAX", "other_fax": "OtherFAX", "homepage": "HomePage",
    "apple_watch": "AppleWatch",
}
# Labels a TYPE parameter carries in vCard 3.0; the rest go to X-ABLabel.
TEL_TYPES = {"home": "HOME", "work": "WORK", "mobile": "CELL", "main": "MAIN", "pager": "PAGER", "iphone": "IPHONE", "other": "OTHER"}
FAX_TYPES = {"home_fax": "HOME", "work_fax": "WORK", "other_fax": "OTHER"}
PLAIN_TYPES = {"home": "HOME", "work": "WORK", "other": "OTHER"}
IGNORED_TYPES = {"pref", "voice", "internet", "x400", "msg", "bbs", "modem", "video", "dom", "intl", "postal", "parcel", "text"}
IM_PROPERTIES = {
    "X-AIM": "AIM", "X-ICQ": "ICQ", "X-JABBER": "Jabber", "X-MSN": "MSN", "X-YAHOO": "Yahoo",
    "X-SKYPE": "Skype", "X-SKYPE-USERNAME": "Skype", "X-GOOGLE-TALK": "Google Talk", "X-GTALK": "Google Talk",
    "X-QQ": "QQ", "X-GADUGADU": "Gadu-Gadu",
}
IM_SCHEMES = {
    "aim": "AIM", "icq": "ICQ", "xmpp": "Jabber", "msnim": "MSN", "ymsgr": "Yahoo", "skype": "Skype",
    "gtalk": "Google Talk", "x-apple": "", "qq": "QQ", "facebook": "Facebook", "tg": "Telegram",
    "telegram": "Telegram", "whatsapp": "WhatsApp", "sip": "SIP", "signal": "Signal",
}
SCHEME_OF = {
    "aim": "aim", "icq": "icq", "jabber": "xmpp", "msn": "msnim", "yahoo": "ymsgr", "skype": "skype",
    "google talk": "xmpp", "qq": "x-apple", "facebook": "xmpp", "telegram": "x-apple",
}
PHOTO_TYPES = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG": "image/png", b"GIF8": "image/gif", b"RIFF": "image/webp"}
# Apple writes a birthday without a year with this one.
NO_YEAR = "1604"
# A birthday kept out of the calendars goes out as a plain date with this
# label: devices and Google show it on the card, the birthday calendars,
# which read only BDAY and Google's birthdays, leave it out.
HIDDEN_BIRTHDAY_TITLE = "Birthday (not in calendar)"


def site_domain():
    """The domain of the site: var.domain_name of the stack."""
    return os.environ["UID_DOMAIN"]


def hidden_birthday_label():
    return f"{HIDDEN_BIRTHDAY_TITLE} - managed by {site_domain()}, do not edit"


def is_hidden_birthday(label):
    # By the start, so that a label written under another domain still counts.
    label = str(label or "").strip().casefold()
    return label.startswith(HIDDEN_BIRTHDAY_TITLE.casefold())


# Reading -------------------------------------------------------------------

def split_cards(text):
    """The vCards of a file as lists of (group, name, params, value)."""
    cards, current = [], None
    for line in unfold(text):
        if not line.strip():
            continue
        prop = parse_line(line)
        if not prop:
            continue
        group, name, params, value = prop
        if name == "BEGIN" and value.strip().upper() == "VCARD":
            current = []
        elif name == "END" and value.strip().upper() == "VCARD":
            if current is not None:
                cards.append(current)
            current = None
        elif current is not None:
            current.append(prop)
    return cards


def unfold(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        elif lines and lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].split(":", 1)[0].upper():
            # A soft line break of quoted-printable (vCard 2.1).
            lines[-1] = lines[-1][:-1] + line
        else:
            lines.append(line)
    return lines


def parse_line(line):
    head, colon, value = split_unquoted(line, ":")
    if not colon:
        return None
    parts = split_params(head)
    name = parts[0]
    group = ""
    if "." in name:
        group, name = name.rsplit(".", 1)
    params = {}
    for part in parts[1:]:
        key, eq, val = part.partition("=")
        if not eq:
            # vCard 2.1: TEL;HOME;VOICE:...
            key, val = "TYPE", key
        key = key.strip().upper()
        values = [v.strip().strip('"') for v in val.split(",")]
        params.setdefault(key, []).extend(v for v in values if v)
    encoding = [v.upper() for v in params.get("ENCODING", [])]
    if "QUOTED-PRINTABLE" in encoding:
        charset = (params.get("CHARSET") or ["utf-8"])[0]
        try:
            value = quopri.decodestring(value.encode("latin-1", "replace")).decode(charset, "replace")
        except LookupError:
            value = quopri.decodestring(value.encode("latin-1", "replace")).decode("utf-8", "replace")
    return group.lower(), name.strip().upper(), params, value


def split_unquoted(text, sep):
    quoted = False
    for index, char in enumerate(text):
        if char == '"':
            quoted = not quoted
        elif char == sep and not quoted:
            return text[:index], sep, text[index + 1:]
    return text, "", ""


def split_params(head):
    parts, current, quoted = [], "", False
    for char in head:
        if char == '"':
            quoted = not quoted
        if char == ";" and not quoted:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


def unescape(value):
    out, index = [], 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            following = value[index + 1]
            out.append("\n" if following in "nN" else following)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def components(value):
    """A structured value (N, ADR, ORG) split at unescaped semicolons."""
    parts, current, index = [], [], 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            current.append(value[index:index + 2])
            index += 2
            continue
        if char == ";":
            parts.append(unescape("".join(current)))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append(unescape("".join(current)))
    return [part.strip() for part in parts]


def list_values(value):
    """A list value (CATEGORIES, NICKNAME) split at unescaped commas."""
    parts, current, index = [], [], 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            current.append(value[index:index + 2])
            index += 2
            continue
        if char == ",":
            parts.append(unescape("".join(current)).strip())
            current = []
        else:
            current.append(char)
        index += 1
    parts.append(unescape("".join(current)).strip())
    return [part for part in parts if part]


def label_text(value):
    """X-ABLabel as a label key or as the text the user typed."""
    value = unescape(value).strip()
    match = re.fullmatch(r"_\$!<(.+?)>!\$_", value)
    if match:
        key = match.group(1).replace(" ", "").lower()
        return APPLE_LABELS.get(key, match.group(1).lower())
    return value


def type_label(types, kind):
    types = [t.lower() for t in types if t.lower() not in IGNORED_TYPES]
    if kind == "tel":
        if "fax" in types:
            for name in ("home", "work"):
                if name in types:
                    return name + "_fax"
            return "other_fax"
        for name, key in (("iphone", "iphone"), ("cell", "mobile"), ("mobile", "mobile"), ("main", "main"),
                          ("pager", "pager"), ("work", "work"), ("home", "home"), ("other", "other")):
            if name in types:
                return key
        return "other" if types else "mobile"
    for name in ("home", "work", "school", "other", "icloud", "homepage", "blog", "profile"):
        if name in types:
            return name
    return types[0] if types else "other"


def parse_date(value):
    """YYYY-MM-DD or --MM-DD for a date without a year, else ""."""
    value = value.strip()
    match = re.match(r"^(\d{4}|--)-?(\d{2})-?(\d{2})", value)
    if not match:
        return ""
    year, month, day = match.groups()
    if year == NO_YEAR:
        year = "--"
    if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return ""
    return f"{year}-{month}-{day}" if year != "--" else f"--{month}-{day}"


def card_to_contact(props):
    """A contact dict and the photo of the card as (bytes, content type) or None."""
    contact = {
        "phones": [], "emails": [], "urls": [], "addresses": [], "dates": [], "related": [],
        "social": [], "im": [], "custom": [], "categories": [],
    }
    labels = {group: label_text(value) for group, name, params, value in props if group and name == "X-ABLABEL"}
    apple_countries = {group: unescape(value).strip().lower() for group, name, params, value in props if group and name == "X-ABADR"}
    photo = None
    full_name = ""

    def label_for(group, params, kind):
        if group and group in labels:
            return labels[group]
        return type_label(params.get("TYPE", []), kind)

    for group, name, params, value in props:
        if name == "N":
            parts = components(value) + [""] * 5
            contact.update({"last": parts[0], "first": parts[1], "middle": parts[2], "prefix": parts[3], "suffix": parts[4]})
        elif name == "FN":
            full_name = unescape(value).strip()
        elif name == "NICKNAME":
            contact["nickname"] = ", ".join(list_values(value))
        elif name == "X-PHONETIC-FIRST-NAME":
            contact["phonetic_first"] = unescape(value).strip()
        elif name == "X-PHONETIC-MIDDLE-NAME":
            contact["phonetic_middle"] = unescape(value).strip()
        elif name == "X-PHONETIC-LAST-NAME":
            contact["phonetic_last"] = unescape(value).strip()
        elif name == "X-PHONETIC-ORG":
            contact["phonetic_company"] = unescape(value).strip()
        elif name == "X-MAIDENNAME":
            contact["maiden"] = unescape(value).strip()
        elif name in ("X-FILE-AS", "SORT-STRING"):
            contact["file_as"] = unescape(value).strip()
        elif name == "X-PRONOUNS":
            contact["pronouns"] = unescape(value).strip()
        elif name == "ORG":
            parts = components(value) + ["", ""]
            contact["company"] = parts[0]
            contact["department"] = "; ".join(part for part in parts[1:] if part)
        elif name == "TITLE":
            contact["title"] = unescape(value).strip()
        elif name == "ROLE" and not contact.get("title"):
            contact["title"] = unescape(value).strip()
        elif name == "TEL":
            number = unescape(value).strip()
            if number.lower().startswith("tel:"):
                number = number[4:]
            if number:
                contact["phones"].append({"label": label_for(group, params, "tel"), "value": number})
        elif name == "EMAIL":
            address = unescape(value).strip()
            if address.lower().startswith("mailto:"):
                address = address[7:]
            if address:
                contact["emails"].append({"label": label_for(group, params, "email"), "value": address})
        elif name == "URL":
            url = unescape(value).strip()
            if url:
                contact["urls"].append({"label": label_for(group, params, "url"), "value": url})
        elif name == "ADR":
            parts = components(value) + [""] * 7
            address = {
                "label": label_for(group, params, "adr"),
                "po_box": parts[0], "extended": parts[1], "street": parts[2], "city": parts[3],
                "region": parts[4], "postal_code": parts[5], "country": parts[6],
                "country_code": apple_countries.get(group, "") if group else "",
            }
            if any(address[key] for key in ("po_box", "extended", "street", "city", "region", "postal_code", "country")):
                contact["addresses"].append(address)
        elif name == "BDAY":
            contact["birthday"] = parse_date(value)
        elif name in ("ANNIVERSARY", "X-ANNIVERSARY"):
            date = parse_date(value)
            if date:
                contact["dates"].append({"label": "anniversary", "value": date})
        elif name == "X-ABDATE":
            date = parse_date(value)
            label = labels.get(group, "other") if group else "other"
            if date and is_hidden_birthday(label):
                contact["hidden_birthday"] = date
            elif date:
                contact["dates"].append({"label": label, "value": date})
        elif name in ("X-ABRELATEDNAMES", "RELATED"):
            person = unescape(value).strip()
            if person:
                kind = labels.get(group) if group else None
                contact["related"].append({"label": kind or type_label(params.get("TYPE", []), "related"), "value": person})
        elif name in ("X-SPOUSE", "X-ASSISTANT", "X-MANAGER"):
            person = unescape(value).strip()
            if person:
                contact["related"].append({"label": name[2:].lower(), "value": person})
        elif name == "X-SOCIALPROFILE":
            service = (params.get("TYPE") or params.get("X-SERVICE-TYPE") or [""])[0]
            user = (params.get("X-USER") or [""])[0]
            url = unescape(value).strip()
            if service.lower().startswith("x-"):
                service = service[2:]
            if url or user:
                contact["social"].append({"service": service_name(service), "user": user, "url": url})
        elif name == "IMPP":
            raw = unescape(value).strip()
            scheme, colon, handle = raw.partition(":")
            if not colon:
                scheme, handle = "", raw
            service = (params.get("X-SERVICE-TYPE") or [""])[0] or IM_SCHEMES.get(scheme.lower(), scheme)
            if handle:
                contact["im"].append({"service": service_name(service), "label": label_for(group, params, "im"), "value": handle})
        elif name in IM_PROPERTIES:
            handle = unescape(value).strip()
            if handle:
                contact["im"].append({"service": IM_PROPERTIES[name], "label": label_for(group, params, "im"), "value": handle})
        elif name in ("X-SIP", "X-GOOGLE-SIP"):
            handle = unescape(value).strip()
            if handle:
                contact["im"].append({"service": "SIP", "label": label_for(group, params, "im"), "value": handle})
        elif name == "NOTE":
            note = unescape(value).strip()
            contact["notes"] = (contact.get("notes", "") + "\n\n" + note).strip() if contact.get("notes") else note
        elif name == "CATEGORIES":
            for item in list_values(value):
                if item not in contact["categories"] and item.lower() != "mycontacts" and item != "starred":
                    contact["categories"].append(item)
                if item == "starred":
                    contact["starred"] = True
        elif name == "UID":
            contact["uid"] = unescape(value).strip()[:200]
        elif name == "PHOTO":
            photo = photo or read_photo(params, value)
        elif name == "X-CUSTOM":
            contact["custom"].append({"label": labels.get(group, "other") if group else "other", "value": unescape(value).strip()})
        elif name == "X-ABUID" and not contact.get("uid"):
            contact["uid"] = unescape(value).strip()[:200]

    if not any(contact.get(key) for key in ("first", "last", "middle", "company", "nickname")) and full_name:
        # A card with only FN: the name goes where Apple shows it.
        parts = full_name.split(" ", 1)
        contact["first"] = parts[0]
        if len(parts) > 1:
            contact["last"] = parts[1]
    return contact, photo


def service_name(value):
    known = {
        "twitter": "Twitter", "x": "X", "facebook": "Facebook", "linkedin": "LinkedIn", "flickr": "Flickr",
        "myspace": "Myspace", "sinaweibo": "Sina Weibo", "tencentweibo": "Tencent Weibo", "yelp": "Yelp",
        "instagram": "Instagram", "telegram": "Telegram", "whatsapp": "WhatsApp", "github": "GitHub",
        "skype": "Skype", "aim": "AIM", "icq": "ICQ", "jabber": "Jabber", "msn": "MSN", "yahoo": "Yahoo",
        "gtalk": "Google Talk", "googletalk": "Google Talk", "qq": "QQ", "gadugadu": "Gadu-Gadu",
        "vk": "VK", "vkontakte": "VK", "signal": "Signal", "threads": "Threads", "mastodon": "Mastodon",
    }
    clean = value.strip()
    return known.get(clean.replace(" ", "").replace("-", "").lower(), clean)


def read_photo(params, value):
    value = value.strip()
    if value.startswith("data:"):
        _, _, value = value.partition(",")
    elif re.match(r"^https?://", value, re.I):
        return None
    try:
        data = base64.b64decode(re.sub(r"\s+", "", value), validate=False)
    except (binascii.Error, ValueError):
        return None
    content_type = photo_type(data)
    return (data, content_type) if content_type else None


def photo_type(data):
    for magic, content_type in PHOTO_TYPES.items():
        if data.startswith(magic):
            if content_type == "image/webp" and data[8:12] != b"WEBP":
                continue
            return content_type
    return None


# Writing -------------------------------------------------------------------

def escape(value):
    return (str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,"))


def escape_text(value):
    """A single text value: commas and semicolons may stay, as Apple writes them."""
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n")


def fold(line):
    """Lines of at most 75 octets, continued with a space, not splitting a character."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    pieces, current, size, limit = [], "", 0, 75
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            pieces.append(current)
            current, size, limit = "", 0, 74
        current += char
        size += width
    pieces.append(current)
    return "\r\n ".join(pieces)


def apple_label(label):
    if label in APPLE_LABELS.values():
        return "_$!<" + LABEL_TO_APPLE.get(label, label[:1].upper() + label[1:]) + ">!$_"
    return escape_text(label)


def display_name(contact):
    parts = [contact.get(key, "") for key in ("prefix", "first", "middle", "last", "suffix")]
    name = " ".join(part for part in parts if part)
    return name or contact.get("company") or contact.get("nickname") or ""


def contact_to_card(contact, photo=None, categories=()):
    """vCard 3.0 lines of one contact; photo is (bytes, content type) or None;
    categories are the names of its groups, for a file that Google reads."""
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"PRODID:-//{site_domain()}//EN"]
    item = [0]

    def grouped(prop, value, label):
        item[0] += 1
        name = f"item{item[0]}"
        lines.append(f"{name}.{prop}:{value}")
        lines.append(f"{name}.X-ABLabel:{apple_label(label)}")

    lines.append("N:" + ";".join(escape(contact.get(key, "")) for key in ("last", "first", "middle", "prefix", "suffix")))
    lines.append("FN:" + escape_text(display_name(contact)))
    for key, prop in (("nickname", "NICKNAME"), ("phonetic_first", "X-PHONETIC-FIRST-NAME"),
                      ("phonetic_middle", "X-PHONETIC-MIDDLE-NAME"), ("phonetic_last", "X-PHONETIC-LAST-NAME"),
                      ("maiden", "X-MAIDENNAME"), ("phonetic_company", "X-PHONETIC-ORG"),
                      ("file_as", "X-FILE-AS"), ("pronouns", "X-PRONOUNS"), ("title", "TITLE")):
        if contact.get(key):
            lines.append(f"{prop}:{escape_text(contact[key])}")
    if contact.get("company") or contact.get("department"):
        lines.append("ORG:" + escape(contact.get("company", "")) + ";" + escape(contact.get("department", "")))
        if contact.get("company") and not any(contact.get(key) for key in ("first", "last", "middle")):
            lines.append("X-ABShowAs:COMPANY")

    for entry in contact.get("phones", []):
        label = entry.get("label") or "other"
        value = escape_text(entry["value"])
        if label in TEL_TYPES:
            lines.append(f"TEL;type={TEL_TYPES[label]};type=VOICE:{value}")
        elif label in FAX_TYPES:
            lines.append(f"TEL;type={FAX_TYPES[label]};type=FAX:{value}")
        else:
            grouped("TEL", value, label)
    for entry in contact.get("emails", []):
        label = entry.get("label") or "other"
        value = escape_text(entry["value"])
        if label in PLAIN_TYPES:
            lines.append(f"EMAIL;type=INTERNET;type={PLAIN_TYPES[label]}:{value}")
        else:
            item[0] += 1
            lines.append(f"item{item[0]}.EMAIL;type=INTERNET:{value}")
            lines.append(f"item{item[0]}.X-ABLabel:{apple_label(label)}")
    for entry in contact.get("addresses", []):
        label = entry.get("label") or "other"
        value = ";".join(escape(entry.get(key, "")) for key in ("po_box", "extended", "street", "city", "region", "postal_code", "country"))
        item[0] += 1
        name = f"item{item[0]}"
        if label in PLAIN_TYPES:
            lines.append(f"{name}.ADR;type={PLAIN_TYPES[label]}:{value}")
        else:
            lines.append(f"{name}.ADR:{value}")
            lines.append(f"{name}.X-ABLabel:{apple_label(label)}")
        if entry.get("country_code"):
            lines.append(f"{name}.X-ABADR:{escape_text(entry['country_code'])}")
    for entry in contact.get("urls", []):
        label = entry.get("label") or "other"
        value = escape_text(entry["value"])
        if label in PLAIN_TYPES:
            lines.append(f"URL;type={PLAIN_TYPES[label]}:{value}")
        else:
            grouped("URL", value, label)
    if contact.get("birthday"):
        birthday = contact["birthday"]
        if birthday.startswith("--"):
            lines.append(f"BDAY;X-APPLE-OMIT-YEAR={NO_YEAR}:{NO_YEAR}{birthday[1:]}")
        else:
            lines.append(f"BDAY;VALUE=date:{birthday}")
    # Not a BDAY, so that no birthday calendar shows it.
    hidden = [{"label": hidden_birthday_label(), "value": contact["hidden_birthday"]}] if contact.get("hidden_birthday") else []
    for entry in hidden + contact.get("dates", []):
        date = entry["value"]
        if date.startswith("--"):
            date = NO_YEAR + date[1:]
        grouped("X-ABDATE", date, entry.get("label") or "other")
    for entry in contact.get("related", []):
        grouped("X-ABRELATEDNAMES", escape_text(entry["value"]), entry.get("label") or "other")
    for entry in contact.get("social", []):
        service = entry.get("service") or "other"
        # Apple writes the services it knows in lower case, such as twitter.
        kind = service.replace(" ", "").lower() if service_name(service.lower()) == service else service
        params = f";type={escape_param(kind)}"
        if entry.get("user"):
            params += f";x-user={escape_param(entry['user'])}"
        url = entry.get("url") or social_url(service, entry.get("user", ""))
        lines.append(f"X-SOCIALPROFILE{params}:{escape_text(url)}")
    for entry in contact.get("im", []):
        service = entry.get("service") or ""
        scheme = SCHEME_OF.get(service.lower(), "x-apple")
        label = entry.get("label") or "other"
        prop = f"IMPP;X-SERVICE-TYPE={escape_param(service)}" if service else "IMPP"
        if label in PLAIN_TYPES:
            lines.append(f"{prop};type={PLAIN_TYPES[label]}:{scheme}:{escape_text(entry['value'])}")
        else:
            grouped(prop, f"{scheme}:{escape_text(entry['value'])}", label)
    for entry in contact.get("custom", []):
        grouped("X-CUSTOM", escape_text(entry["value"]), entry.get("label") or "other")
    if contact.get("notes"):
        lines.append("NOTE:" + escape_text(contact["notes"]))
    names = list(categories)
    if contact.get("starred"):
        names.append("starred")
    if names:
        lines.append("CATEGORIES:" + ",".join(escape(name) for name in names))
    if contact.get("uid"):
        lines.append("UID:" + escape_text(contact["uid"]))
    if photo:
        data, content_type = photo
        kind = {"image/jpeg": "JPEG", "image/png": "PNG", "image/gif": "GIF", "image/webp": "WEBP"}.get(content_type, "JPEG")
        lines.append(f"PHOTO;ENCODING=b;TYPE={kind}:" + base64.b64encode(data).decode("ascii"))
    lines.append("END:VCARD")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def escape_param(value):
    value = str(value).replace('"', "'")
    return f'"{value}"' if re.search(r"[;:,]", value) else value


def social_url(service, user):
    bases = {
        "twitter": "https://twitter.com/", "x": "https://x.com/", "facebook": "https://www.facebook.com/",
        "linkedin": "https://www.linkedin.com/in/", "instagram": "https://www.instagram.com/",
        "telegram": "https://t.me/", "github": "https://github.com/", "flickr": "https://www.flickr.com/photos/",
        "vk": "https://vk.com/",
    }
    base = bases.get(service.lower())
    return base + user if base and user else user


# Groups ------------------------------------------------------------------------
# Apple keeps a group as a vCard of its own: the name, the kind and a line
# per member with the UID of its card. Google's labels are CATEGORIES of the
# cards instead; card_to_contact gives them as "categories", which an import
# turns into groups.

def is_group(props):
    for _, name, _, value in props:
        if name == "X-ADDRESSBOOKSERVER-KIND" and value.strip().lower() == "group":
            return True
        if name == "KIND" and value.strip().lower() == "group":
            return True
    return False


def card_to_group(props):
    """The name, the UID and the member UIDs of a group card."""
    name, uid, members = "", "", []
    for _, prop, _, value in props:
        if prop == "FN":
            name = unescape(value).strip()
        elif prop == "N" and not name:
            name = components(value)[0]
        elif prop == "UID":
            uid = unescape(value).strip()[:200]
        elif prop in ("X-ADDRESSBOOKSERVER-MEMBER", "MEMBER"):
            member = unescape(value).strip()
            for prefix in ("urn:uuid:", "urn:uid:"):
                if member.lower().startswith(prefix):
                    member = member[len(prefix):]
            if member and member not in members:
                members.append(member[:200])
    return name, uid, members


def group_to_card(name, uid, members):
    lines = [
        "BEGIN:VCARD", "VERSION:3.0", f"PRODID:-//{site_domain()}//EN",
        "N:" + escape(name) + ";;;;", "FN:" + escape_text(name),
        "X-ADDRESSBOOKSERVER-KIND:group", "UID:" + escape_text(uid),
    ]
    lines += ["X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:" + escape_text(member) for member in members]
    lines.append("END:VCARD")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"
