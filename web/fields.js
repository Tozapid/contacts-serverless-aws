// The fields of a card: everything Google Contacts and Apple Contacts keep.
// The API checks the same names (FIELDS in lambda/contacts.py) and vCard
// import and export map them (lambda/vcard.py). Texts are in i18n.js under
// "field.<name>" and "label.<label>".
window.CONTACT_FIELDS = {
  // Single lines of text, in the order of the editor. "extra" ones are hidden
  // in the editor until they have a value or are added with "Add field", as
  // Apple and Google do it.
  name: [
    { key: "prefix", extra: true },
    { key: "first" },
    { key: "phonetic_first", extra: true },
    { key: "middle", extra: true },
    { key: "phonetic_middle", extra: true },
    { key: "last" },
    { key: "phonetic_last", extra: true },
    { key: "maiden", extra: true },
    { key: "suffix", extra: true },
    { key: "nickname", extra: true },
    { key: "file_as", extra: true },
    { key: "pronouns", extra: true },
  ],
  work: [
    { key: "company" },
    { key: "phonetic_company", extra: true },
    { key: "department", extra: true },
    { key: "title" },
  ],
  // Lists of labelled values. "parts" are the inputs of one row; "labels" the
  // labels to choose from (a label of one's own is always possible);
  // "services" the services to choose from.
  lists: {
    phones: {
      parts: [{ key: "value", type: "tel", autocomplete: "tel" }],
      labels: ["mobile", "iphone", "home", "work", "main", "home_fax", "work_fax", "other_fax", "pager", "other"],
    },
    emails: {
      parts: [{ key: "value", type: "email", autocomplete: "email" }],
      labels: ["home", "work", "icloud", "school", "other"],
    },
    addresses: {
      parts: [
        { key: "street", multiline: true },
        { key: "extended", extra: true },
        { key: "po_box", extra: true },
        { key: "city" },
        { key: "region" },
        { key: "postal_code" },
        { key: "country" },
      ],
      labels: ["home", "work", "school", "other"],
    },
    urls: {
      parts: [{ key: "value", type: "url" }],
      labels: ["homepage", "home", "work", "school", "blog", "profile", "other"],
    },
    dates: {
      parts: [{ key: "value", date: true }],
      labels: ["anniversary", "other"],
    },
    related: {
      parts: [{ key: "value" }],
      labels: ["mother", "father", "parent", "brother", "sister", "child", "son", "daughter", "spouse", "husband", "wife",
        "partner", "friend", "assistant", "manager", "other"],
    },
    im: {
      parts: [{ key: "value" }],
      services: ["Telegram", "WhatsApp", "Signal", "Skype", "Facebook", "Jabber", "Google Talk", "SIP", "ICQ", "AIM", "Yahoo", "MSN", "QQ", "Gadu-Gadu"],
      labels: ["home", "work", "other"],
    },
    social: {
      parts: [{ key: "user" }, { key: "url", type: "url" }],
      services: ["Telegram", "Facebook", "Instagram", "LinkedIn", "X", "Twitter", "VK", "GitHub", "Mastodon", "Threads", "Flickr", "Myspace", "Sina Weibo", "Yelp"],
    },
    custom: {
      parts: [{ key: "value" }],
      labels: [],
    },
  },
  // The order of the sections of a card and of the editor.
  order: ["phones", "emails", "addresses", "urls", "birthday", "dates", "related", "im", "social", "custom"],
};
