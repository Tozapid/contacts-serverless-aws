// Address book: cards with every field of Google and Apple Contacts, the
// history of every change, search, and vCard import and export.
// The page signs in with Cognito and calls /api with the ID token; the API is
// in lambda/contacts.py. The fields are in fields.js, the texts in i18n.js.
(function () {
  const config = window.CONTACTS_CONFIG || {};
  const i18n = window.CONTACTS_I18N;
  const FIELDS = window.CONTACT_FIELDS;
  const root = document.getElementById("root");
  const state = {
    me: null,
    contacts: new Map(),
    loaded: false,
    query: "",
    // Whether the search also looks in the history of the cards (the API
    // searches it; the page has only the current cards).
    inHistory: false,
    history: { query: "", items: null, error: "" },
    filter: "all",
    // Unsaved edits by key: the id of a card, or "new" (the API keeps them).
    drafts: new Map(),
    // Apple's groups: { id, name, members (card ids), etag }.
    groups: [],
    // The draft of the open editor while it has edits not yet sent.
    unsaved: null,
  };
  // The site's domain (var.domain_name) names the authenticator entry and
  // the Apple profile.
  const ISSUER = "Contacts " + config.domain;
  const REPOSITORY = "https://github.com/Tozapid/contacts-serverless-aws";
  const PROFILE_ID = String(config.domain).split(".").reverse().join(".");
  // The language is the Cognito attribute "locale" of the user; the browser
  // keeps a copy so that the sign-in page speaks it too.
  const LANGUAGE_KEY = "contacts-language";
  const ORDER_KEY = "contacts-name-order";
  const SORT_KEY = "contacts-sort";
  const PHONES_KEY = "contacts-only-phones";
  const HISTORY_KEY = "contacts-search-history";
  const SEARCH_DELAY = 300;
  const DRAFT_DELAY = 800;
  const NEW_DRAFT = "new";
  // The keys of the Russian and the English layout: a query typed in the
  // wrong one is searched in the other too (Jkmuf finds Ольга). The same table
  // as LATIN and CYRILLIC in lambda/contacts.py.
  const LATIN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./~{}:\"<>";
  const CYRILLIC = "ёйцукенгшщзхъфывапролджэячсмитьбю.ёхъжэбю";
  // A part of a vCard file the API takes at once: it reads up to 5 MB and
  // 200 cards in one request.
  const IMPORT_BYTES = 3_000_000;
  const IMPORT_CARDS = 150;
  const PHOTO_SIZE = 1024;
  const MONTHS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
  let language = i18n.fallback;
  let pool = null;
  let toastTimer = null;
  let searchTimer = null;
  let listNode = null;
  let detailNode = null;
  let appNode = null;

  // Language ---------------------------------------------------------------

  function known(code) {
    return Boolean(code && i18n.strings[code]);
  }

  // The text of a key in the current language with its {name} parts filled.
  function t(key, params) {
    let text = i18n.strings[language][key] ?? i18n.strings[i18n.fallback][key] ?? key;
    if (typeof text === "object") {
      text = text[new Intl.PluralRules(language).select(params.count)] ?? text.other;
    }
    return text.replace(/\{(\w+)\}/g, (part, name) => (params && name in params ? String(params[name]) : part));
  }

  function has(key) {
    return key in i18n.strings[i18n.fallback];
  }

  function stored(key) {
    try {
      return localStorage.getItem(key);
    } catch (error) {
      return null;
    }
  }

  function store(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (error) {
      // Private windows may refuse storage; the choice holds until reload.
    }
  }

  function setLanguage(code) {
    if (!known(code)) return;
    language = code;
    document.documentElement.lang = code;
    document.title = t("brand");
    store(LANGUAGE_KEY, code);
  }

  // The first language of the browser this page speaks, else the fallback.
  function browserLanguage() {
    const found = (navigator.languages || [navigator.language]).map((code) => String(code).slice(0, 2)).find(known);
    return found || i18n.fallback;
  }

  async function loadLanguage() {
    try {
      const { user } = await currentUser();
      const attributes = await cognito((done) => user.getUserAttributes(done));
      const locale = (attributes || []).find((attribute) => attribute.getName() === "locale");
      if (locale) setLanguage(locale.getValue());
    } catch (error) {
      // The language of this browser stays.
    }
  }

  async function saveLanguage(code) {
    setLanguage(code);
    try {
      const { user } = await currentUser();
      await cognito((done) => user.updateAttributes([{ Name: "locale", Value: code }], done));
      toast(t("language.saved"));
    } catch (error) {
      toast(authMessage(error));
    }
  }

  // Page helpers -----------------------------------------------------------

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value == null || value === false) return;
      if (key === "class") node.className = value;
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else if (key === "value") node.value = value;
      else node.setAttribute(key, value === true ? "" : value);
    });
    [].concat(children || []).forEach((child) => {
      if (child == null || child === false) return;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    });
    return node;
  }

  // replaceChildren() turns null into the text "null"; empty parts are left out.
  function fill(node, children) {
    node.replaceChildren(...[].concat(children).filter(Boolean));
  }

  function toast(message) {
    let node = document.querySelector(".toast");
    if (!node) {
      node = el("div", { class: "toast", role: "status" });
      document.body.append(node);
    }
    node.textContent = message;
    node.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.add("hidden"), 3500);
  }

  function when(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString(language, { dateStyle: "medium", timeStyle: "short" });
  }

  // A modal window over the page; fill() replaces its contents.
  function dialog(title, onClose) {
    const body = el("div", { class: "modal-body" });
    const heading = el("h2", {}, title);
    const node = el("dialog", { class: "modal" }, [
      el("div", { class: "modal-head" }, [
        heading,
        el("button", { type: "button", class: "icon-button close", "aria-label": t("close"), onclick: () => node.close() }, "×"),
      ]),
      body,
    ]);
    node.addEventListener("close", () => {
      node.remove();
      if (onClose) onClose();
    });
    node.addEventListener("click", (event) => {
      if (event.target === node) node.close();
    });
    document.body.append(node);
    node.showModal();
    return {
      node,
      close: () => node.close(),
      title: (text) => { heading.textContent = text; },
      fill: (children) => fill(body, children),
    };
  }

  // The photo of a card as it is stored (up to 1024 points).
  function showPhoto(contact) {
    const view = dialog(displayName(contact));
    view.node.classList.add("photo-modal");
    view.fill(el("img", { class: "photo-full", src: contact.photo, alt: displayName(contact) }));
  }

  // Names and values -------------------------------------------------------

  function nameOrder() {
    return stored(ORDER_KEY) === "last" ? "last" : "first";
  }

  // Cards without a phone number are left out of the list; a link to one
  // still opens it.
  function onlyPhones() {
    return stored(PHONES_KEY) === "1";
  }

  function hasPhone(contact) {
    return (contact.phones || []).some((phone) => phone.value && phone.value.trim());
  }

  function sortBy() {
    return stored(SORT_KEY) === "first" ? "first" : "last";
  }

  // The name as the settings order it; a card without a name is shown by its
  // company, nickname, email or phone.
  function personName(contact) {
    const given = [contact.first, contact.middle].filter(Boolean).join(" ");
    const parts = nameOrder() === "last" ? [contact.last, given] : [given, contact.last];
    return [contact.prefix, ...parts, contact.suffix].filter(Boolean).join(" ");
  }

  function displayName(contact) {
    return personName(contact) || contact.company || contact.nickname
      || (contact.emails && contact.emails[0] && contact.emails[0].value)
      || (contact.phones && contact.phones[0] && contact.phones[0].value)
      || t("contact.noName");
  }

  function sortKey(contact) {
    const name = contact.file_as || (sortBy() === "first"
      ? [contact.first, contact.middle, contact.last].filter(Boolean).join(" ")
      : [contact.last, contact.first, contact.middle].filter(Boolean).join(" "));
    return (name || displayName(contact)).toLocaleLowerCase(language);
  }

  function initials(contact) {
    const words = personName(contact) ? [contact.first, contact.last].filter(Boolean) : [displayName(contact)];
    return words.map((word) => Array.from(word.trim())[0] || "").join("").toLocaleUpperCase(language).slice(0, 2) || "?";
  }

  function avatar(contact, size) {
    const label = displayName(contact);
    if (contact.photo) return el("img", { class: "avatar " + (size || ""), src: contact.photo, alt: "", loading: "lazy" });
    return el("span", { class: "avatar initials " + (size || ""), "aria-hidden": "true", title: label }, initials(contact));
  }

  function labelText(label) {
    if (!label) return "";
    return has("label." + label) ? t("label." + label) : label;
  }

  function fieldName(key) {
    return t("field." + key);
  }

  // A date of the card: YYYY-MM-DD, or --MM-DD without a year.
  function dateText(value) {
    const match = /^(\d{4}|-)-(\d{2})-(\d{2})$/.exec(value || "");
    if (!match) return value || "";
    const [, year, month, day] = match;
    const date = new Date(Date.UTC(year === "-" ? 2000 : Number(year), Number(month) - 1, Number(day), 12));
    return date.toLocaleDateString(language, year === "-"
      ? { day: "numeric", month: "long", timeZone: "UTC" }
      : { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
  }

  function age(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    if (!match) return null;
    const now = new Date();
    let years = now.getFullYear() - Number(match[1]);
    if (now.getMonth() + 1 < Number(match[2]) || (now.getMonth() + 1 === Number(match[2]) && now.getDate() < Number(match[3]))) years -= 1;
    return years >= 0 && years < 150 ? years : null;
  }

  function monthName(month) {
    return new Date(Date.UTC(2000, month - 1, 1, 12)).toLocaleDateString(language, { month: "long", timeZone: "UTC" });
  }

  function addressLines(address) {
    const cityLine = [address.postal_code, address.city].filter(Boolean).join(" ");
    return [address.street, address.extended, address.po_box ? t("address.poBox", { box: address.po_box }) : "", cityLine, address.region, address.country]
      .filter(Boolean).join("\n");
  }

  // One field of a card as text, for the history and its differences.
  function valueText(key, value) {
    if (value == null || value === "" || (Array.isArray(value) && !value.length)) return "";
    if (key === "starred") return value ? t("contact.starred") : "";
    if (key === "birthday" || key === "hidden_birthday") return dateText(value);
    if (key === "photo") return t("history.photo");
    if (!Array.isArray(value)) return String(value);
    return value.map((entry) => {
      if (key === "addresses") return `${labelText(entry.label)}: ${addressLines(entry).replace(/\n/g, ", ")}`;
      if (key === "dates") return `${labelText(entry.label)}: ${dateText(entry.value)}`;
      if (key === "social") return `${entry.service || ""}: ${[entry.user, entry.url].filter(Boolean).join(" ")}`;
      if (key === "im") return `${entry.service || labelText(entry.label)}: ${entry.value}`;
      return `${labelText(entry.label)}: ${entry.value}`;
    }).join("\n");
  }

  // Search -------------------------------------------------------------------
  // The same rules as haystack() and matches() in lambda/contacts.py.

  function foldText(text) {
    return String(text || "").toLocaleLowerCase("ru").replace(/ё/g, "е");
  }

  function digits(text) {
    return String(text || "").replace(/\D/g, "");
  }

  function phoneDigits(text) {
    const number = digits(text);
    return number.length === 11 && number.startsWith("8") ? "7" + number.slice(1) : number;
  }

  function phoneMatch(query, numbers) {
    if (!/^[\d\s()+.-]+$/.test(query)) return false;
    const number = digits(query);
    if (number.length < 3) return false;
    const wanted = number.startsWith("8") ? [number, "7" + number.slice(1)] : [number];
    return wanted.some((want) => numbers.includes(want));
  }

  const SKIPPED = new Set(["photo", "uid", "starred", "id", "etag", "created", "modified"]);

  function haystack(contact) {
    const words = [];
    const walk = (value) => {
      if (Array.isArray(value)) value.forEach(walk);
      else if (value && typeof value === "object") Object.values(value).forEach(walk);
      else if (typeof value === "string") words.push(value);
    };
    Object.entries(contact).forEach(([key, value]) => {
      if (SKIPPED.has(key)) return;
      // The id of a linked card is not text to search.
      if (key === "related") walk((value || []).map(({ contact: link, ...entry }) => entry));
      else walk(value);
    });
    // The groups of the card and the labels of its fields as the page shows
    // them are found too: "мобильный".
    if (contact.id) groupsOf(contact.id).forEach((group) => words.push(group.name));
    ["phones", "emails", "urls", "addresses", "dates", "related", "im"].forEach((key) => (contact[key] || []).forEach((entry) => words.push(labelText(entry.label))));
    return { text: foldText(words.join(" ")), numbers: (contact.phones || []).map((entry) => phoneDigits(entry.value)).join(" ") };
  }

  function convert(text, from, to) {
    return Array.from(text).map((char) => {
      const index = from.indexOf(char);
      return index < 0 ? char : to[index];
    }).join("");
  }

  // The query as typed and as if typed in the other layout.
  function queryVariants(query) {
    const lower = String(query || "").toLocaleLowerCase("ru").trim();
    const variants = [];
    [lower, convert(lower, LATIN, CYRILLIC), convert(lower, CYRILLIC.slice(0, 34), LATIN.slice(0, 34))].forEach((text) => {
      const folded = foldText(text);
      const words = folded.split(/\s+/).filter(Boolean).slice(0, 8);
      if (words.length && !variants.some((variant) => variant.query === folded)) variants.push({ query: folded, words });
    });
    return variants;
  }

  // The query in the other layout that found something, for the list to say so.
  let otherLayout = "";

  function search(query) {
    const variants = queryVariants(query);
    const found = [];
    otherLayout = "";
    state.contacts.forEach((contact) => {
      const { text, numbers } = haystack(contact);
      const hit = variants.find(({ query: whole, words }) => phoneMatch(whole, numbers) || words.every((word) => text.includes(word) || phoneMatch(word, numbers)));
      if (hit) {
        if (hit !== variants[0]) otherLayout = hit.query;
        const name = foldText(displayName(contact) + " " + (contact.nickname || ""));
        found.push({ contact, rank: hit.words.every((word) => name.includes(word)) ? 0 : 1 });
      }
    });
    found.sort((a, b) => a.rank - b.rank || sortKey(a.contact).localeCompare(sortKey(b.contact), language));
    return found.map((entry) => entry.contact);
  }

  function scheduleHistorySearch() {
    clearTimeout(searchTimer);
    const query = state.query.trim();
    if (!state.inHistory || !query) {
      state.history = { query: "", items: null, error: "" };
      return;
    }
    if (state.history.query === query) return;
    state.history = { query, items: null, error: "" };
    searchTimer = setTimeout(async () => {
      let items = null;
      let error = "";
      try {
        items = (await api("/search?q=" + encodeURIComponent(query))).items || [];
      } catch (failure) {
        error = failure.message;
      }
      if (state.history.query !== query) return;
      state.history = { query, items, error };
      renderList();
    }, SEARCH_DELAY);
  }

  // Sign-in ----------------------------------------------------------------

  function userPool() {
    if (!pool) {
      pool = new window.AmazonCognitoIdentity.CognitoUserPool({ UserPoolId: config.userPoolId, ClientId: config.clientId });
    }
    return pool;
  }

  // The signed-in user with a valid session, which the account calls need.
  function currentUser() {
    return new Promise((resolve, reject) => {
      const user = userPool().getCurrentUser();
      if (!user) return reject(new Error("auth"));
      user.getSession((error, session) => {
        if (error || !session || !session.isValid()) reject(error || new Error("auth"));
        else resolve({ user, session });
      });
    });
  }

  async function currentSession() {
    return (await currentUser()).session;
  }

  // Wraps a callback-style call of the Cognito library.
  function cognito(call) {
    return new Promise((resolve, reject) => call((error, result) => (error ? reject(error) : resolve(result))));
  }

  function authMessage(error) {
    const code = error && (error.code || error.name);
    if (code === "NotAuthorizedException" || code === "UserNotFoundException") return t("auth.wrong");
    if (code === "InvalidPasswordException") return t("auth.passwordRules");
    if (code === "CodeMismatchException" || code === "EnableSoftwareTokenMFAException") return t("auth.wrongCode");
    if (code === "ExpiredCodeException") return t("auth.expiredCode");
    if (code === "LimitExceededException" || code === "TooManyRequestsException") return t("auth.tooMany");
    return (error && error.message) || t("failed");
  }

  function loginCard(children, onsubmit) {
    root.replaceChildren(el("main", { class: "login" }, el("form", {
      class: "login-card",
      onsubmit: (event) => {
        event.preventDefault();
        onsubmit();
      },
    }, children)));
  }

  function signInCallbacks(user, error) {
    return {
      onSuccess: () => boot(),
      onFailure: (failure) => { error.textContent = authMessage(failure); },
      newPasswordRequired: () => showNewPassword(user),
      totpRequired: () => showTotp(user),
    };
  }

  // The language menu of the sign-in page and of the settings.
  function languagePicker(onchange) {
    return el("select", { class: "lang-select", "aria-label": t("language.title"), onchange: (event) => onchange(event.target.value) },
      i18n.languages.map(([code, name]) => el("option", { value: code, selected: code === language }, name)));
  }

  // In the public demo (config.demo) the sign-in is shown and filled in.
  function showLogin(message) {
    const demo = config.demo || {};
    const error = el("p", { class: "error", role: "alert" }, message || "");
    const email = el("input", { type: "email", autocomplete: "username", required: true, value: demo.email || "" });
    const password = el("input", { type: "password", autocomplete: "current-password", required: true, value: demo.password || "" });
    loginCard([
      el("img", { class: "login-mark", src: "/favicon.svg", alt: "" }),
      el("h1", {}, t("brand")),
      demo.email ? el("p", { class: "demo-note" }, [
        t("demo.signIn", { email: demo.email, password: demo.password }),
        demo.reset ? " " + t("demo.reset") : "",
      ]) : null,
      el("label", {}, [t("login.email"), email]),
      el("label", {}, [t("login.password"), password]),
      el("button", { class: "primary", type: "submit" }, t("login.signIn")),
      error,
      el("div", { class: "login-lang" }, [
        el("a", { href: REPOSITORY, rel: "noopener" }, t("login.source")),
        languagePicker((code) => {
          setLanguage(code);
          showLogin();
        }),
      ]),
    ], () => {
      error.textContent = "";
      const user = new window.AmazonCognitoIdentity.CognitoUser({ Username: email.value.trim(), Pool: userPool() });
      user.authenticateUser(new window.AmazonCognitoIdentity.AuthenticationDetails({
        Username: email.value.trim(),
        Password: password.value,
      }), signInCallbacks(user, error));
    });
    email.focus();
  }

  function showNewPassword(user) {
    const error = el("p", { class: "error", role: "alert" });
    const password = el("input", { type: "password", autocomplete: "new-password", required: true, minlength: "12" });
    loginCard([
      el("h1", {}, t("newPassword.title")),
      el("p", { class: "lede" }, t("newPassword.hint")),
      el("label", {}, [t("password.new"), password]),
      el("button", { class: "primary", type: "submit" }, t("newPassword.submit")),
      error,
    ], () => user.completeNewPasswordChallenge(password.value, {}, signInCallbacks(user, error)));
    password.focus();
  }

  // The second step when two-factor sign-in is on: the code of the
  // authenticator app.
  function showTotp(user) {
    const error = el("p", { class: "error", role: "alert" });
    const code = el("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", pattern: "[0-9]{6}", maxlength: "6", required: true });
    loginCard([
      el("h1", {}, t("totp.title")),
      el("p", { class: "lede" }, t("totp.hint")),
      el("label", {}, [t("totp.code"), code]),
      el("button", { class: "primary", type: "submit" }, t("login.signIn")),
      error,
    ], () => {
      error.textContent = "";
      user.sendMFACode(code.value.trim(), signInCallbacks(user, error), "SOFTWARE_TOKEN_MFA");
    });
    code.focus();
  }

  function signOut() {
    const user = pool && pool.getCurrentUser();
    if (user) user.signOut();
    document.querySelectorAll("dialog.modal").forEach((node) => node.close());
    document.querySelector(".dialog-wrap")?.remove();
    state.contacts.clear();
    state.drafts.clear();
    state.groups = [];
    state.loaded = false;
    state.unsaved = null;
    showLogin();
  }

  async function api(path, options) {
    const session = await currentSession().catch(() => null);
    if (!session) {
      showLogin(t("sessionEnded"));
      throw new Error(t("sessionEnded"));
    }
    const response = await fetch(config.apiBase + path, {
      method: (options && options.method) || "GET",
      headers: {
        Authorization: "Bearer " + session.getIdToken().getJwtToken(),
        ...(options && options.body ? { "Content-Type": "application/json" } : {}),
      },
      body: options && options.body ? JSON.stringify(options.body) : undefined,
    });
    let data = {};
    const text = await response.text();
    if (text) {
      try { data = JSON.parse(text); } catch (error) { data = { error: text }; }
    }
    if (response.status === 401) {
      signOut();
      throw new Error(t("sessionEnded"));
    }
    if (!response.ok) {
      // The API names the error with a code; its own text is the English one.
      const message = data.code && has("api." + data.code) ? t("api." + data.code) : data.error || t("requestFailed");
      const error = new Error(data.detail && has("field." + data.detail) ? `${message}: ${fieldName(data.detail)}` : message);
      error.code = data.code;
      throw error;
    }
    return data;
  }

  // Data -------------------------------------------------------------------

  async function loadContacts() {
    const found = new Map();
    let cursor = null;
    do {
      const data = await api("/contacts" + (cursor ? "?cursor=" + encodeURIComponent(cursor) : ""));
      (data.items || []).forEach((contact) => found.set(contact.id, contact));
      cursor = data.next_cursor;
    } while (cursor);
    state.contacts = found;
    // The drafts come with the cards, so that the list marks them.
    try {
      state.drafts = new Map(((await api("/drafts")).items || []).map((item) => [item.key, item]));
      await loadGroups();
    } catch (error) {
      toast(error.message);
    }
    state.loaded = true;
  }

  async function reload() {
    try {
      await loadContacts();
    } catch (error) {
      toast(error.message);
    }
    renderList();
    renderDetail();
  }

  // The groups a card is in (Apple's groups, kept apart from the cards).
  function groupsOf(id) {
    return state.groups.filter((group) => group.members.includes(id));
  }

  async function loadGroups() {
    try {
      state.groups = (await api("/groups")).items || [];
    } catch (error) {
      toast(error.message);
    }
  }

  // Routes: #/c/<id>, #/c/<id>/edit, #/c/<id>/history, #/new, #/deleted.
  function route() {
    const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
    if (parts[0] === "c" && parts[1]) return { view: parts[2] || "card", id: parts[1] };
    if (parts[0] === "new") return { view: "new" };
    if (parts[0] === "deleted") return { view: "deleted" };
    if (parts[0] === "duplicates") return { view: "duplicates" };
    if (parts[0] === "groups" && parts[1]) return { view: "group", group: parts[1] };
    if (parts[0] === "google") return { view: "google", result: parts[1] || "" };
    if (parts[0] === "groups") return { view: "groups" };
    return { view: "none" };
  }

  // Goes to a route; edits not yet sent go to their draft first.
  function go(hash) {
    if (state.unsaved) state.unsaved.flush();
    if (location.hash === hash) renderDetail();
    else location.hash = hash;
    return true;
  }

  // Page -------------------------------------------------------------------

  function render() {
    listNode = el("aside", { class: "list-pane" });
    detailNode = el("section", { class: "detail-pane", "aria-live": "polite" });
    appNode = el("div", { class: "app" }, [
      el("header", { class: "top" }, [
        el("a", { class: "brand", href: "#", onclick: (event) => { event.preventDefault(); go("#"); } }, [
          el("img", { class: "brand-mark", src: "/favicon.svg", alt: "" }),
          el("strong", {}, t("brand")),
          el("span", {}, location.hostname),
        ]),
        el("div", { class: "who" }, [
          el("span", { title: state.me.email }, state.me.email),
          el("button", { type: "button", class: "icon-button", onclick: () => showAccount() }, t("menu.account")),
          el("button", { type: "button", class: "icon-button", onclick: signOut }, t("menu.signOut")),
        ]),
      ]),
      el("main", { class: "panes" }, [listNode, detailNode]),
    ]);
    root.replaceChildren(appNode);
    renderList();
    renderDetail();
  }

  function renderList() {
    if (!listNode) return;
    const focused = document.activeElement && document.activeElement.classList.contains("search") ? document.activeElement.selectionStart : null;
    const searchBox = el("input", {
      type: "search",
      class: "search",
      value: state.query,
      placeholder: t("search.placeholder"),
      "aria-label": t("search.placeholder"),
      oninput: (event) => {
        state.query = event.target.value;
        scheduleHistorySearch();
        renderList();
      },
      onkeydown: (event) => {
        if (event.key === "Enter") {
          const first = visible()[0];
          if (first) go("#/c/" + first.id);
        }
        if (event.key === "Escape" && state.query) {
          state.query = "";
          scheduleHistorySearch();
          renderList();
        }
      },
    });
    const historyBox = el("input", {
      type: "checkbox",
      checked: state.inHistory,
      onchange: (event) => {
        state.inHistory = event.target.checked;
        store(HISTORY_KEY, state.inHistory ? "1" : "");
        state.history = { query: "", items: null, error: "" };
        scheduleHistorySearch();
        renderList();
      },
    });
    const allGroups = state.groups;
    const filter = el("select", {
      class: "filter",
      "aria-label": t("list.filter"),
      onchange: (event) => {
        if (event.target.value === "__groups__") {
          event.target.value = state.filter;
          go("#/groups");
          return;
        }
        state.filter = event.target.value;
        renderList();
      },
    }, [
      el("option", { value: "all", selected: state.filter === "all" }, t("list.all", { count: state.contacts.size })),
      el("option", { value: "starred", selected: state.filter === "starred" }, t("list.starred")),
      el("optgroup", { label: fieldName("groups") }, [
        el("option", { value: "nogroup", selected: state.filter === "nogroup" }, `${t("list.noGroup")} (${ungrouped(Array.from(state.contacts.values())).length})`),
        ...allGroups.map((group) => el("option", { value: "group:" + group.id, selected: state.filter === "group:" + group.id }, `${group.name} (${group.members.length})`)),
        el("option", { value: "__groups__" }, t("groups.manage")),
      ]),
    ]);
    if (state.filter.startsWith("group:") && !allGroups.some((group) => group.id === state.filter.slice(6))) state.filter = "all";

    const current = route();
    const items = visible();
    const searching = Boolean(state.query.trim());
    const rows = el("ul", { class: "contacts", role: "list" });
    let letter = null;
    items.forEach((contact) => {
      if (!searching) {
        const first = (Array.from(sortKey(contact))[0] || "#").toLocaleUpperCase(language);
        const head = /\p{L}/u.test(first) ? first : "#";
        if (head !== letter) {
          letter = head;
          rows.append(el("li", { class: "letter", "aria-hidden": "true" }, head));
        }
      }
      rows.append(contactRow(contact, current.id === contact.id));
    });

    fill(listNode, [
      el("div", { class: "list-tools" }, [
        searchBox,
        el("div", { class: "list-options" }, [
          el("label", { class: "check small" }, [historyBox, t("search.inHistory")]),
          el("select", {
            class: "sort",
            "aria-label": t("sort.label"),
            onchange: (event) => {
              store(SORT_KEY, event.target.value);
              renderList();
            },
          }, ["last", "first"].map((option) => el("option", { value: option, selected: sortBy() === option }, t("sort." + option)))),
        ]),
        el("div", { class: "list-actions" }, [
          filter,
          el("button", { type: "button", class: "primary", onclick: () => go("#/new") }, t("list.new")),
          el("details", { class: "menu" }, [
            el("summary", { class: "icon-button", "aria-label": t("list.more") }, "⋯"),
            el("div", { class: "menu-items" }, [
              el("button", { type: "button", onclick: (event) => { event.target.closest("details").open = false; showImport(); } }, t("import.title")),
              el("button", { type: "button", onclick: (event) => { event.target.closest("details").open = false; exportCards(null); } }, t("export.all")),
              el("button", { type: "button", onclick: (event) => { event.target.closest("details").open = false; go("#/groups"); } }, t("groups.title")),
              el("button", { type: "button", onclick: (event) => { event.target.closest("details").open = false; go("#/duplicates"); } }, t("dup.title")),
              el("button", { type: "button", onclick: (event) => { event.target.closest("details").open = false; go("#/deleted"); } }, t("deleted.title")),
            ]),
          ]),
        ]),
      ]),
      searching ? el("p", { class: "muted small found" }, [
        items.length ? t("search.found", { count: items.length }) : t("search.none"),
        otherLayout ? " " + t("search.layout", { query: otherLayout }) : "",
      ]) : null,
      !searching && state.drafts.has(NEW_DRAFT) ? el("a", {
        class: "contact-row new-draft" + (current.view === "new" ? " active" : ""),
        href: "#/new",
        onclick: (event) => {
          event.preventDefault();
          go("#/new");
        },
      }, [
        el("span", { class: "avatar initials small", "aria-hidden": "true" }, "+"),
        el("span", { class: "row-main" }, [el("span", { class: "row-title" }, t("editor.newTitle")), el("span", { class: "row-sub" }, el("span", { class: "badge warn" }, t("draft.badge")))]),
      ]) : null,
      !state.loaded ? el("p", { class: "muted" }, t("loading"))
        : !state.contacts.size ? el("div", { class: "empty" }, [
          el("p", {}, t("list.empty")),
          el("div", { class: "actions start" }, [
            el("button", { type: "button", class: "primary", onclick: () => go("#/new") }, t("list.new")),
            el("button", { type: "button", class: "ghost", onclick: showImport }, t("import.title")),
          ]),
        ]) : rows,
      historyResults(items),
    ]);
    appNode.classList.toggle("detail-open", current.view !== "none");
    if (focused !== null) {
      searchBox.focus();
      searchBox.setSelectionRange(focused, focused);
    }
  }

  // The smart group "No group": the cards that are in no group, worked out
  // from the groups, so it follows them by itself.
  function ungrouped(items) {
    const grouped = new Set(state.groups.flatMap((group) => group.members));
    return items.filter((contact) => !grouped.has(contact.id));
  }

  function showUngrouped() {
    state.filter = "nogroup";
    state.query = "";
    renderList();
    go("#");
  }

  function visible() {
    let items = state.query.trim() ? search(state.query)
      : Array.from(state.contacts.values()).sort((a, b) => sortKey(a).localeCompare(sortKey(b), language));
    if (onlyPhones()) items = items.filter(hasPhone);
    if (state.filter === "starred") items = items.filter((contact) => contact.starred);
    else if (state.filter === "nogroup") items = ungrouped(items);
    else if (state.filter.startsWith("group:")) {
      const group = state.groups.find((item) => item.id === state.filter.slice(6));
      items = items.filter((contact) => group && group.members.includes(contact.id));
    }
    return items;
  }

  function contactRow(contact, active) {
    const sub = [contact.company, contact.title].filter(Boolean).join(", ")
      || (contact.phones[0] && contact.phones[0].value) || (contact.emails[0] && contact.emails[0].value) || "";
    return el("li", {}, el("a", {
      class: "contact-row" + (active ? " active" : ""),
      href: "#/c/" + contact.id,
      "aria-current": active ? "page" : null,
      onclick: (event) => {
        event.preventDefault();
        go("#/c/" + contact.id);
      },
    }, [
      avatar(contact, "small"),
      el("span", { class: "row-main" }, [
        el("span", { class: "row-title" }, [
          displayName(contact),
          contact.starred ? el("span", { class: "star-mark", "aria-label": t("contact.starred") }, " ★") : null,
          state.drafts.has(contact.id) ? el("span", { class: "badge warn draft-mark" }, t("draft.badge")) : null,
        ]),
        sub ? el("span", { class: "row-sub muted" }, sub) : null,
      ]),
    ]));
  }

  // What the search found in the history: cards that matched only in an
  // older version, and deleted ones.
  function historyResults(shown) {
    if (!state.inHistory || !state.query.trim()) return null;
    const history = state.history;
    const ids = new Set(shown.map((contact) => contact.id));
    let body;
    if (history.error) body = el("p", { class: "error" }, history.error);
    else if (!history.items) body = el("p", { class: "muted small" }, t("search.inHistoryLoading"));
    else {
      const items = history.items.filter((item) => !ids.has(item.id));
      body = items.length ? el("ul", { class: "contacts", role: "list" }, items.map((item) => {
        const contact = state.contacts.get(item.id);
        return el("li", {}, el("a", {
          class: "contact-row",
          href: `#/c/${item.id}/history`,
          onclick: (event) => {
            event.preventDefault();
            state.highlight = item.revision;
            go(`#/c/${item.id}/history`);
          },
        }, [
          avatar(contact || { first: item.name, photo: item.photo, phones: [], emails: [] }, "small"),
          el("span", { class: "row-main" }, [
            el("span", { class: "row-title" }, contact ? displayName(contact) : item.name || t("contact.noName")),
            el("span", { class: "row-sub muted" }, [
              item.deleted ? el("span", { class: "badge warn" }, t("search.deleted")) : null,
              " ",
              t("search.version", { date: when(item.at), action: t("action." + item.action) }),
            ]),
          ]),
        ]));
      })) : el("p", { class: "muted small" }, t("search.inHistoryNone"));
    }
    return el("section", { class: "history-found" }, [el("h3", {}, t("search.inHistoryTitle")), body]);
  }

  function renderDetail() {
    if (!detailNode) return;
    const current = route();
    appNode.classList.toggle("detail-open", current.view !== "none");
    listNode.querySelectorAll(".contact-row.active").forEach((node) => node.classList.remove("active"));
    if (current.id) listNode.querySelector(`.contact-row[href="#/c/${current.id}"]`)?.classList.add("active");
    if (current.view === "none") {
      fill(detailNode, el("div", { class: "placeholder muted" }, state.contacts.size ? t("detail.pick") : ""));
      return;
    }
    if (current.view === "new") return showEditor(null);
    if (current.view === "deleted") return showDeleted();
    if (current.view === "duplicates") return showDuplicates();
    if (current.view === "groups") return showGroups();
    if (current.view === "google") {
      // Back from the consent page of Google.
      toast(t(has("google.result." + current.result) ? "google.result." + current.result : "google.result.failed"));
      history.replaceState(null, "", location.pathname);
      renderDetail();
      showAccount("sync");
      return;
    }
    if (current.view === "group") return showGroup(current.group);
    const contact = state.contacts.get(current.id);
    if (current.view === "history") return showHistory(current.id);
    if (!contact) {
      if (!state.loaded) fill(detailNode, el("p", { class: "muted" }, t("loading")));
      // A card that is gone may still have its history.
      else showHistory(current.id);
      return;
    }
    if (current.view === "edit") return showEditor(contact);
    showCard(contact);
  }

  function backButton() {
    return el("button", { type: "button", class: "icon-button back", onclick: () => go("#") }, "← " + t("detail.back"));
  }

  // A card ---------------------------------------------------------------------

  function showCard(contact) {
    const sections = [];
    const add = (key, rows) => {
      if (rows.length) sections.push(el("section", { class: "card-section" }, [el("h3", {}, fieldName(key)), el("dl", { class: "values" }, rows.flat())]));
    };
    const pair = (label, value) => [el("dt", {}, label), el("dd", {}, value)];
    FIELDS.order.forEach((key) => {
      if (key === "birthday") {
        const birthday = contact.birthday || contact.hidden_birthday;
        if (birthday) {
          const years = age(birthday);
          add("birthday", [pair(fieldName("birthday"), [
            dateText(birthday) + (years !== null ? " · " + t("contact.age", { count: years }) : ""),
            contact.hidden_birthday ? el("span", { class: "muted small" }, " · " + t("birthday.hiddenMark")) : null,
          ])]);
        }
        return;
      }
      const entries = contact[key] || [];
      add(key, entries.map((entry) => {
        if (key === "phones") return pair(labelText(entry.label), el("a", { href: "tel:" + entry.value.replace(/[^\d+*#,;]/g, "") }, entry.value));
        if (key === "emails") return pair(labelText(entry.label), el("a", { href: "mailto:" + entry.value }, entry.value));
        if (key === "urls") return pair(labelText(entry.label), link(entry.value));
        if (key === "addresses") {
          const text = addressLines(entry);
          return pair(labelText(entry.label), [
            el("span", { class: "address" }, text),
            el("a", { class: "small", href: "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(text.replace(/\n/g, ", ")), target: "_blank", rel: "noopener noreferrer" }, t("contact.map")),
          ]);
        }
        if (key === "dates") return pair(labelText(entry.label), dateText(entry.value));
        if (key === "related") return pair(labelText(entry.label), personLink(entry));
        if (key === "im") return pair([entry.service || "", entry.label && entry.label !== "other" ? " · " + labelText(entry.label) : ""].join("") || labelText(entry.label), entry.value);
        if (key === "social") return pair(entry.service || t("field.social"), entry.url ? link(entry.url, entry.user || entry.url) : entry.user);
        return pair(labelText(entry.label), entry.value);
      }));
    });
    const facts = [
      contact.nickname ? [fieldName("nickname"), contact.nickname] : null,
      contact.maiden ? [fieldName("maiden"), contact.maiden] : null,
      contact.pronouns ? [fieldName("pronouns"), contact.pronouns] : null,
      [contact.phonetic_first, contact.phonetic_middle, contact.phonetic_last].some(Boolean)
        ? [t("contact.phonetic"), [contact.phonetic_first, contact.phonetic_middle, contact.phonetic_last].filter(Boolean).join(" ")] : null,
      contact.phonetic_company ? [fieldName("phonetic_company"), contact.phonetic_company] : null,
      contact.file_as ? [fieldName("file_as"), contact.file_as] : null,
    ].filter(Boolean);
    if (facts.length) sections.unshift(el("section", { class: "card-section" }, [el("h3", {}, t("section.name")), el("dl", { class: "values" }, facts.map(([label, value]) => pair(label, value)).flat())]));
    // A group filters the list when clicked.
    const chips = (key, entries) => {
      if (!entries.length) return;
      sections.push(el("section", { class: "card-section" }, [el("h3", {}, fieldName(key)), el("div", { class: "chips" }, entries.map(([filter, text]) => el("button", {
        type: "button",
        class: "chip",
        onclick: () => {
          state.filter = filter;
          state.query = "";
          renderList();
          // On a phone the list is behind the card.
          if (window.matchMedia("(max-width: 760px)").matches) go("#");
        },
      }, text)))]));
    };
    chips("groups", groupsOf(contact.id).map((group) => ["group:" + group.id, group.name]));
    // The cards that name this one among their related people.
    const incoming = incomingOf(contact.id);
    if (incoming.length) {
      sections.push(el("section", { class: "card-section" }, [
        el("h3", {}, t("related.incoming")),
        el("dl", { class: "values" }, incoming.map(({ from, label }) => pair(labelText(label) || "—", personLink({ contact: from.id, value: displayName(from) }))).flat()),
      ]));
    }
    if (contact.notes) sections.push(el("section", { class: "card-section" }, [el("h3", {}, fieldName("notes")), el("p", { class: "notes" }, contact.notes)]));

    const star = el("button", {
      type: "button",
      class: "icon-button star" + (contact.starred ? " on" : ""),
      "aria-pressed": String(Boolean(contact.starred)),
      title: t(contact.starred ? "contact.unstar" : "contact.star"),
      "aria-label": t(contact.starred ? "contact.unstar" : "contact.star"),
      onclick: async () => {
        star.disabled = true;
        try {
          const saved = await api("/contacts/" + contact.id, { method: "PUT", body: { ...contact, starred: !contact.starred } });
          state.contacts.set(saved.id, saved);
        } catch (error) {
          toast(error.message);
          if (error.code === "changed") await reload();
        }
        renderList();
        renderDetail();
      },
    }, contact.starred ? "★" : "☆");

    fill(detailNode, el("article", { class: "card contact-card" }, [
      backButton(),
      el("header", { class: "card-head" }, [
        contact.photo ? el("button", {
          type: "button",
          class: "avatar-open",
          "aria-label": t("photo.open"),
          onclick: () => showPhoto(contact),
        }, avatar(contact, "large")) : avatar(contact, "large"),
        el("div", { class: "card-title" }, [
          el("h2", {}, [displayName(contact), " ", star]),
          [contact.title, contact.department, personName(contact) ? contact.company : ""].some(Boolean)
            ? el("p", { class: "muted" }, [contact.title, contact.department, personName(contact) ? contact.company : ""].filter(Boolean).join(" · ")) : null,
        ]),
      ]),
      el("div", { class: "actions start" }, [
        el("button", { type: "button", class: "primary", onclick: () => go(`#/c/${contact.id}/edit`) }, t("contact.edit")),
        el("button", { type: "button", class: "ghost", onclick: () => go(`#/c/${contact.id}/history`) }, t("contact.history")),
        el("button", { type: "button", class: "ghost", onclick: () => showGraph(contact.id) }, t("graph.open")),
        el("button", { type: "button", class: "ghost", onclick: () => exportCards([contact.id]) }, t("contact.export")),
        el("button", {
          type: "button",
          class: "danger",
          onclick: async () => {
            if (!window.confirm(t("contact.deleteConfirm", { name: displayName(contact) }))) return;
            try {
              await api(`/contacts/${contact.id}?etag=${contact.etag}`, { method: "DELETE" });
              state.contacts.delete(contact.id);
              toast(t("contact.deleted"));
              go("#");
              renderList();
            } catch (error) {
              toast(error.message);
              if (error.code === "changed") await reload();
            }
          },
        }, t("contact.delete")),
      ]),
      ...(sections.length ? sections : [el("p", { class: "muted" }, t("contact.emptyCard"))]),
      el("p", { class: "muted small stamps" }, t("contact.stamps", { created: when(contact.created), modified: when(contact.modified) })),
    ]));
  }

  // A related person: a link to the card when one is linked, else the name.
  function personLink(entry) {
    const target = entry.contact && state.contacts.get(entry.contact);
    if (!target) return entry.value || "";
    return el("a", {
      href: "#/c/" + target.id,
      onclick: (event) => {
        event.preventDefault();
        go("#/c/" + target.id);
      },
    }, displayName(target));
  }

  function linksOf(contact) {
    return (contact.related || []).filter((entry) => entry.contact && state.contacts.has(entry.contact) && entry.contact !== contact.id);
  }

  function incomingOf(id) {
    const found = [];
    state.contacts.forEach((from) => {
      linksOf(from).forEach((entry) => {
        if (entry.contact === id) found.push({ from, label: entry.label });
      });
    });
    return found.sort((a, b) => sortKey(a.from).localeCompare(sortKey(b.from), language));
  }

  // The family graph around a card: its relatives and theirs, two steps out,
  // with the relation written on each arrow ("A → B: мама" is what A's card
  // says B is). A click on a person opens their card.
  function showGraph(id) {
    const center = state.contacts.get(id);
    const depth = new Map([[id, 0]]);
    const parent = new Map();
    const edges = new Map();
    let ring = [id];
    for (let step = 1; step <= 2; step += 1) {
      const next = [];
      ring.forEach((node) => {
        const neighbours = [
          ...linksOf(state.contacts.get(node)).map((entry) => [node, entry.contact, entry.label]),
          ...incomingOf(node).map(({ from, label }) => [from.id, node, label]),
        ];
        neighbours.forEach(([from, to, label]) => {
          edges.set(`${from}>${to}>${label}`, { from, to, label: labelText(label) || "" });
          const other = from === node ? to : from;
          if (!depth.has(other)) {
            depth.set(other, step);
            parent.set(other, node);
            next.push(other);
          }
        });
      });
      ring = next;
    }
    // The last ring's own links to each other are drawn too.
    depth.forEach((_, node) => linksOf(state.contacts.get(node)).forEach((entry) => {
      if (depth.has(entry.contact)) edges.set(`${node}>${entry.contact}>${entry.label}`, { from: node, to: entry.contact, label: labelText(entry.label) || "" });
    }));

    const view = dialog(t("graph.title", { name: displayName(center) }));
    view.node.classList.add("graph-dialog");
    if (depth.size < 2) {
      view.fill(el("p", { class: "muted" }, t("graph.empty")));
      return;
    }
    // Rings around the card: the relatives evenly on the first, theirs near
    // the one who links them, on the second.
    const place = new Map([[id, { x: 0, y: 0, angle: 0 }]]);
    const first = [...depth].filter(([, d]) => d === 1).map(([node]) => node);
    first.forEach((node, index) => {
      const angle = (index / first.length) * 2 * Math.PI - Math.PI / 2;
      place.set(node, { x: 190 * Math.cos(angle), y: 190 * Math.sin(angle), angle });
    });
    const slice = (2 * Math.PI) / Math.max(first.length, 1);
    first.forEach((node) => {
      const children = [...depth].filter(([other, d]) => d === 2 && parent.get(other) === node).map(([other]) => other);
      children.forEach((child, index) => {
        const angle = place.get(node).angle + (children.length > 1 ? (index / (children.length - 1) - 0.5) * slice * 0.8 : 0);
        place.set(child, { x: 360 * Math.cos(angle), y: 360 * Math.sin(angle), angle });
      });
    });
    const svgNs = "http://www.w3.org/2000/svg";
    const svg = (tag, attrs, children) => {
      const node = document.createElementNS(svgNs, tag);
      Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, value));
      [].concat(children || []).forEach((child) => node.append(child.nodeType ? child : document.createTextNode(String(child))));
      return node;
    };
    const points = [...place.values()];
    const pad = 70;
    const minX = Math.min(...points.map((p) => p.x)) - pad - 40;
    const maxX = Math.max(...points.map((p) => p.x)) + pad + 40;
    const minY = Math.min(...points.map((p) => p.y)) - pad;
    const maxY = Math.max(...points.map((p) => p.y)) + pad + 20;
    const radius = 28;
    const root = svg("svg", { viewBox: `${minX} ${minY} ${maxX - minX} ${maxY - minY}`, class: "graph", role: "img", "aria-label": t("graph.title", { name: displayName(center) }) }, [
      svg("defs", {}, svg("marker", { id: "graph-arrow", viewBox: "0 0 10 10", refX: "10", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" }, svg("path", { d: "M0 0L10 5L0 10z", class: "graph-arrow" }))),
    ]);
    // Two arrows between the same people (each names the other) bend apart.
    const pairs = new Map();
    edges.forEach((edge) => {
      const key = [edge.from, edge.to].sort().join("|");
      pairs.set(key, (pairs.get(key) || 0) + 1);
    });
    const seen = new Map();
    edges.forEach((edge) => {
      const a = place.get(edge.from);
      const b = place.get(edge.to);
      if (!a || !b) return;
      const key = [edge.from, edge.to].sort().join("|");
      const nth = seen.get(key) || 0;
      seen.set(key, nth + 1);
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const length = Math.hypot(dx, dy) || 1;
      const ux = dx / length;
      const uy = dy / length;
      const bend = pairs.get(key) > 1 ? (nth % 2 ? -1 : 1) * 24 * (edge.from < edge.to ? 1 : -1) : 0;
      const start = { x: a.x + ux * radius, y: a.y + uy * radius };
      const end = { x: b.x - ux * (radius + 3), y: b.y - uy * (radius + 3) };
      const mid = { x: (start.x + end.x) / 2 - uy * bend, y: (start.y + end.y) / 2 + ux * bend };
      root.append(svg("path", { d: `M${start.x} ${start.y} Q${mid.x} ${mid.y} ${end.x} ${end.y}`, class: "graph-edge", "marker-end": "url(#graph-arrow)" }));
      if (edge.label) {
        const label = { x: (start.x + 2 * mid.x + end.x) / 4, y: (start.y + 2 * mid.y + end.y) / 4 };
        const text = edge.label.length > 18 ? edge.label.slice(0, 17) + "…" : edge.label;
        root.append(svg("g", { class: "graph-label" }, [
          svg("rect", { x: label.x - text.length * 3.6 - 6, y: label.y - 10, width: text.length * 7.2 + 12, height: 20, rx: 10 }),
          svg("text", { x: label.x, y: label.y + 4, "text-anchor": "middle" }, text),
        ]));
      }
    });
    place.forEach((point, node) => {
      const contact = state.contacts.get(node);
      const name = displayName(contact);
      const group = svg("g", { class: "graph-node" + (node === id ? " center" : ""), tabindex: "0", role: "link", "aria-label": name, transform: `translate(${point.x} ${point.y})` }, [
        svg("circle", { r: radius }),
        svg("text", { class: "graph-initials", y: 5, "text-anchor": "middle" }, initials(contact)),
        svg("text", { class: "graph-name", y: radius + 18, "text-anchor": "middle" }, name.length > 22 ? name.slice(0, 21) + "…" : name),
      ]);
      const open = () => {
        view.close();
        go("#/c/" + node);
      };
      group.addEventListener("click", open);
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter") open();
      });
      root.append(group);
    });
    view.fill([el("p", { class: "muted small" }, t("graph.hint")), el("div", { class: "graph-wrap" }, root)]);
  }

  function link(url, text) {
    const href = /^[a-z][a-z0-9+.-]*:/i.test(url) ? url : "https://" + url;
    if (!/^https?:/i.test(href)) return text || url;
    return el("a", { href, target: "_blank", rel: "noopener noreferrer" }, text || url);
  }

  // The editor ---------------------------------------------------------------

  // Unsaved edits go to the API a moment after typing stops, as on the wiki,
  // so that they survive a reload and are there on another device.
  function draftSaver(key, draft, etag) {
    let timer = null;
    let running = Promise.resolve();
    // After a save: the editor goes, and the change event its focused field
    // still sends must not bring the draft back.
    let closed = false;
    const saver = {
      schedule() {
        if (closed) return;
        clearTimeout(timer);
        timer = setTimeout(send, DRAFT_DELAY);
        state.unsaved = saver;
      },
      flush() {
        if (!timer) return running;
        clearTimeout(timer);
        return send();
      },
      // Before a save or a discard: nothing more goes out.
      // After a failed save the edits go on.
      resume() {
        closed = false;
      },
      async stop() {
        closed = true;
        clearTimeout(timer);
        timer = null;
        if (state.unsaved === saver) state.unsaved = null;
        await running.catch(() => {});
      },
    };
    function send() {
      timer = null;
      if (state.unsaved === saver) state.unsaved = null;
      const body = { draft: JSON.parse(JSON.stringify(draft)), etag: etag || "" };
      const fresh = !state.drafts.has(key);
      state.drafts.set(key, { key, draft: body.draft, etag: body.etag, modified: new Date().toISOString() });
      if (fresh) renderList();
      running = running.then(() => api("/drafts/" + key, { method: "PUT", body })).catch((error) => toast(error.message));
      return running;
    }
    return saver;
  }

  async function discardDraft(key, saver) {
    if (saver) await saver.stop();
    state.drafts.delete(key);
    try {
      await api("/drafts/" + key, { method: "DELETE" });
    } catch (error) {
      toast(error.message);
    }
    renderList();
  }

  function showEditor(contact) {
    const key = contact ? contact.id : NEW_DRAFT;
    const kept = state.drafts.get(key);
    const draft = JSON.parse(JSON.stringify(kept ? kept.draft : contact || { phones: [], emails: [] }));
    // The card the draft began from: a save over a card that has changed
    // since is refused.
    const etag = contact ? (kept && kept.etag) || contact.etag : "";
    FIELDS.order.forEach((key) => {
      if (key !== "birthday") draft[key] = draft[key] || [];
    });
    // Rows the editor shows empty, so that there is somewhere to type.
    if (!contact && !kept) {
      draft.phones = [{ label: "mobile", value: "" }];
      draft.emails = [{ label: "home", value: "" }];
    }
    // Labels are gone; an older draft may still hold them.
    delete draft.labels;
    delete draft.groups;
    draft.group_ids = draft.group_ids || (contact ? groupsOf(contact.id).map((group) => group.id) : []);
    const shownExtra = new Set([...FIELDS.name, ...FIELDS.work].filter((field) => field.extra && draft[field.key]).map((field) => field.key));
    if (draft.birthday || draft.hidden_birthday) shownExtra.add("birthday");
    // Notes are always there.
    shownExtra.add("notes");
    const error = el("p", { class: "error", role: "alert" });
    const save = el("button", { class: "primary", type: "submit" }, t(contact ? "editor.save" : "editor.create"));
    const saver = draftSaver(key, draft, etag);
    const markDirty = () => saver.schedule();
    const discard = async () => {
      if (state.drafts.has(key) && !window.confirm(t("draft.discardConfirm"))) return false;
      await discardDraft(key, saver);
      return true;
    };
    const photoBox = el("div", { class: "photo-edit" });
    const form = el("form", {
      class: "editor",
      oninput: markDirty,
      onchange: markDirty,
      // An image pasted anywhere in the editor becomes the photo; text pasted
      // into a field stays text.
      onpaste: (event) => {
        const data = event.clipboardData;
        const image = data && Array.from(data.files || []).find((file) => file.type.startsWith("image/"));
        if (!image) return;
        const typing = event.target.matches && event.target.matches("input, textarea") && data.types.includes("text/plain");
        if (typing) return;
        event.preventDefault();
        takePhoto(image);
      },
      onsubmit: async (event) => {
        event.preventDefault();
        error.textContent = "";
        const body = cleanDraft(draft);
        // A date with a day but no month, or the other way round.
        if (body.birthday === "invalid" || body.hidden_birthday === "invalid" || body.dates.some((entry) => entry.value === "invalid")) {
          error.textContent = t("api.bad_date");
          return;
        }
        save.disabled = true;
        // The save removes the draft; a draft sent after it would bring it back.
        await saver.stop();
        try {
          const saved = contact
            ? await api("/contacts/" + contact.id, { method: "PUT", body: { ...body, etag } })
            : await api("/contacts", { method: "POST", body });
          state.contacts.set(saved.id, saved);
          state.drafts.delete(key);
          await loadGroups();
          toast(t(contact ? "editor.saved" : "editor.created"));
          renderList();
          go("#/c/" + saved.id);
        } catch (failure) {
          error.textContent = failure.message;
          save.disabled = false;
          saver.resume();
          saver.schedule();
          // The card changed elsewhere: start again from it, without this draft.
          if (failure.code === "changed") {
            error.append(" ", el("button", {
              type: "button",
              class: "link-button",
              onclick: async () => {
                await discardDraft(key, saver);
                await reload();
              },
            }, t("editor.reload")));
          }
        }
      },
    });

    async function takePhoto(file) {
      fill(photoBox, [avatar(draft, "large"), el("span", { class: "muted small" }, t("photo.uploading"))]);
      try {
        draft.photo = await uploadPhoto(file);
        markDirty();
      } catch (failure) {
        toast(failure.message);
      }
      drawPhoto();
    }

    // The button asks the browser for the clipboard; Firefox and some others
    // do not give it, and ⌘V / Ctrl+V in the editor works there too.
    async function pastePhoto() {
      let found = null;
      try {
        for (const item of await navigator.clipboard.read()) {
          const type = item.types.find((kind) => kind.startsWith("image/"));
          if (type) {
            found = new File([await item.getType(type)], "clipboard." + type.split("/")[1], { type });
            break;
          }
        }
      } catch (error) {
        toast(t("photo.pasteKeys"));
        return;
      }
      if (found) takePhoto(found);
      else toast(t("photo.noImage"));
    }

    function drawPhoto() {
      const input = el("input", {
        type: "file",
        accept: "image/jpeg,image/png,image/webp,image/gif,image/heic",
        class: "hidden",
        onchange: (event) => {
          if (event.target.files[0]) takePhoto(event.target.files[0]);
        },
      });
      fill(photoBox, [
        avatar(draft, "large"),
        el("div", { class: "actions start" }, [
          input,
          el("button", { type: "button", class: "ghost", onclick: () => input.click() }, t(draft.photo ? "photo.change" : "photo.add")),
          navigator.clipboard && navigator.clipboard.read ? el("button", { type: "button", class: "ghost", title: t("photo.pasteKeys"), onclick: pastePhoto }, t("photo.paste")) : null,
          draft.photo ? el("button", { type: "button", class: "danger", onclick: () => { delete draft.photo; markDirty(); drawPhoto(); } }, t("photo.remove")) : null,
        ]),
      ]);
    }
    drawPhoto();

    const textInput = (key, attrs) => el("label", {}, [fieldName(key), el("input", {
      type: "text",
      value: draft[key] || "",
      autocomplete: { first: "given-name", middle: "additional-name", last: "family-name", prefix: "honorific-prefix", suffix: "honorific-suffix", nickname: "nickname", company: "organization", title: "organization-title" }[key] || "off",
      oninput: (event) => { draft[key] = event.target.value; },
      ...(attrs || {}),
    })]);

    const nameBox = el("div", { class: "fields" });
    const workBox = el("div", { class: "fields" });
    const listsBox = el("div", { class: "lists" });
    const extraBox = el("div", { class: "fields" });

    function drawFields(box, fields) {
      fill(box, fields.filter((field) => !field.extra || shownExtra.has(field.key)).map((field) => textInput(field.key)));
    }

    function drawExtra() {
      const nodes = [];
      if (shownExtra.has("birthday")) {
        // The checkbox moves the date between birthday and hidden_birthday:
        // the calendars of Google and Apple show only the first.
        const hidden = () => Boolean(draft.hidden_birthday) || draft.birthday_hidden === true;
        const put = (value, quiet) => {
          delete draft.birthday;
          delete draft.hidden_birthday;
          if (value) draft[quiet ? "hidden_birthday" : "birthday"] = value;
          draft.birthday_hidden = quiet;
        };
        nodes.push(el("div", { class: "wide" }, [
          el("label", {}, [fieldName("birthday"), dateInput(draft.birthday || draft.hidden_birthday, (value) => put(value, hidden()))]),
          el("label", { class: "check small" }, [
            el("input", {
              type: "checkbox",
              checked: hidden(),
              onchange: (event) => put(draft.birthday || draft.hidden_birthday, event.target.checked),
            }),
            t("birthday.hide"),
          ]),
          el("p", { class: "muted small" }, t("birthday.hideHint")),
        ]));
      }
      if (shownExtra.has("notes")) {
        nodes.push(el("label", { class: "wide" }, [fieldName("notes"), el("textarea", { rows: "4", oninput: (event) => { draft.notes = event.target.value; } }, draft.notes || "")]));
      }
      fill(extraBox, nodes);
    }

    function drawLists() {
      fill(listsBox, FIELDS.order.filter((key) => key !== "birthday").map((key) => listEditor(key, draft, markDirty)));
    }

    // "Add field": the hidden name and work fields, the birthday and notes.
    const addable = () => [
      ...FIELDS.name.filter((field) => field.extra && !shownExtra.has(field.key)).map((field) => field.key),
      ...FIELDS.work.filter((field) => field.extra && !shownExtra.has(field.key)).map((field) => field.key),
      ...["birthday", "notes"].filter((key) => !shownExtra.has(key)),
    ];
    const addField = el("select", {
      class: "add-field",
      "aria-label": t("editor.addField"),
      onchange: (event) => {
        const key = event.target.value;
        if (!key) return;
        shownExtra.add(key);
        drawAll();
        form.querySelector(`[data-key="${key}"] input, [data-key="${key}"] textarea, [data-key="${key}"] select`)?.focus();
      },
    });

    function drawAddField() {
      fill(addField, [el("option", { value: "" }, t("editor.addField")), ...addable().map((key) => el("option", { value: key }, fieldName(key)))]);
      addField.classList.toggle("hidden", !addable().length);
    }

    function drawAll() {
      drawFields(nameBox, FIELDS.name);
      drawFields(workBox, FIELDS.work);
      drawExtra();
      drawAddField();
      // Every label names its field for "Add field" to focus it.
      [...nameBox.children, ...workBox.children, ...extraBox.children].forEach((node, index) => {
        const keys = [
          ...FIELDS.name.filter((field) => !field.extra || shownExtra.has(field.key)).map((field) => field.key),
          ...FIELDS.work.filter((field) => !field.extra || shownExtra.has(field.key)).map((field) => field.key),
          ...["birthday", "notes"].filter((key) => shownExtra.has(key)),
        ];
        node.dataset.key = keys[index];
      });
    }
    drawAll();
    drawLists();

    const starred = el("input", { type: "checkbox", checked: Boolean(draft.starred), onchange: (event) => { draft.starred = event.target.checked; } });

    fill(form, [
      el("section", { class: "edit-section" }, [photoBox]),
      el("section", { class: "edit-section" }, [el("h3", {}, t("section.name")), nameBox]),
      el("section", { class: "edit-section" }, [el("h3", {}, t("section.work")), workBox]),
      listsBox,
      el("section", { class: "edit-section" }, [extraBox, addField]),
      el("section", { class: "edit-section" }, [el("h3", {}, fieldName("groups")), groupsEditor(draft, markDirty)]),
      el("label", { class: "check" }, [starred, t("contact.starredField")]),
      el("div", { class: "actions sticky-actions" }, [
        error,
        el("button", {
          type: "button",
          class: "ghost",
          onclick: async () => {
            if (await discard()) go(contact ? "#/c/" + contact.id : "#");
          },
        }, t("editor.cancel")),
        save,
      ]),
    ]);
    fill(detailNode, el("article", { class: "card" }, [
      backButton(),
      el("h2", {}, contact ? t("editor.editTitle", { name: displayName(contact) }) : t("editor.newTitle")),
      kept ? el("p", { class: "note" }, [
        t(contact && kept.etag && kept.etag !== contact.etag ? "draft.outdated" : "draft.restored", { date: when(kept.modified) }),
        " ",
        el("button", {
          type: "button",
          class: "link-button",
          onclick: async () => {
            if (await discard()) renderDetail();
          },
        }, t("draft.discard")),
      ]) : null,
      form,
    ]));
    form.querySelector("input[type=text]")?.focus();
  }

  // The rows of one list of the editor: a label (or service), the inputs,
  // and a button that removes the row.
  function listEditor(key, draft, markDirty) {
    const spec = FIELDS.lists[key];
    const rows = el("div", { class: "list-rows" });
    const draw = () => fill(rows, draft[key].map((entry, index) => {
      const remove = el("button", {
        type: "button",
        class: "icon-button remove",
        "aria-label": t("editor.removeRow"),
        title: t("editor.removeRow"),
        onclick: () => {
          draft[key].splice(index, 1);
          markDirty();
          draw();
        },
      }, "−");
      // Parts such as the PO box show once they have a value or are asked for.
      const shown = (part) => !part.extra || entry[part.key] || (entry.shownParts || []).includes(part.key);
      const inputs = spec.parts.filter(shown).map((part) => {
        if (part.date) return dateInput(entry.value, (value) => { entry.value = value; });
        if (key === "related") return personPicker(entry, draft.id, markDirty);
        const attrs = {
          type: part.type || "text",
          value: entry[part.key] || "",
          placeholder: fieldName(key + "." + part.key),
          "aria-label": fieldName(key + "." + part.key),
          autocomplete: part.autocomplete || "off",
          oninput: (event) => { entry[part.key] = event.target.value; },
        };
        return part.multiline ? el("textarea", { ...attrs, rows: "2", type: null, value: null }, entry[part.key] || "") : el("input", attrs);
      });
      const extras = spec.parts.filter((part) => !shown(part));
      return el("div", { class: "list-row" + (key === "addresses" ? " address-row" : "") }, [
        el("div", { class: "row-label" }, [
          spec.services ? choice(spec.services, entry.service, (value) => { entry.service = value; }, t("editor.service")) : null,
          key === "related" ? relationInput(spec.labels, entry)
            : spec.labels && (spec.labels.length || key === "custom") ? labelChoice(spec.labels, entry, key === "custom") : null,
        ]),
        el("div", { class: "row-inputs" }, [
          ...inputs,
          extras.length ? el("div", { class: "row-more" }, extras.map((part) => el("button", {
            type: "button",
            class: "link-button small",
            onclick: () => {
              entry.shownParts = [...(entry.shownParts || []), part.key];
              draw();
            },
          }, "+ " + fieldName(key + "." + part.key)))) : null,
        ]),
        remove,
      ]);
    }));
    draw();
    return el("section", { class: "edit-section" }, [
      el("h3", {}, fieldName(key)),
      rows,
      el("button", {
        type: "button",
        class: "link-button add-row",
        onclick: () => {
          const entry = {};
          // A relation is typed in each time; the others start with a label.
          if (spec.labels && spec.labels.length && key !== "related") entry.label = draft[key].length ? spec.labels[Math.min(draft[key].length, spec.labels.length - 1)] : spec.labels[0];
          if (spec.services) entry.service = spec.services[0];
          draft[key].push(entry);
          markDirty();
          draw();
          rows.lastElementChild?.querySelector("input, textarea")?.focus();
        },
      }, t("editor.addRow." + key)),
    ]);
  }

  // A relation as the user writes it ("мама", "двоюродный брат"), with the
  // usual ones offered; one of those is kept by its key, so it is translated.
  function relationInput(labels, entry) {
    const id = "relations-" + Math.random().toString(36).slice(2);
    return el("span", { class: "service" }, [
      el("input", {
        type: "text",
        class: "label-input",
        list: id,
        maxlength: "100",
        value: entry.label && entry.label !== "other" ? labelText(entry.label) : "",
        placeholder: t("related.relation"),
        "aria-label": t("related.relation"),
        oninput: (event) => {
          const text = event.target.value.trim();
          entry.label = labels.find((label) => labelText(label).toLocaleLowerCase(language) === text.toLocaleLowerCase(language)) || text;
        },
      }),
      el("datalist", { id }, labels.filter((label) => label !== "other").map((label) => el("option", { value: labelText(label) }))),
    ]);
  }

  // The name of a related person: typed in, or a card of the book picked
  // from the suggestions, which links the two.
  function personPicker(entry, selfId, markDirty) {
    const linked = () => (entry.contact ? state.contacts.get(entry.contact) : null);
    const list = el("ul", { class: "picker-list hidden", role: "listbox" });
    const badge = el("div", { class: "link-state" });
    const input = el("input", {
      type: "text",
      maxlength: "200",
      autocomplete: "off",
      value: linked() ? displayName(linked()) : entry.value || "",
      placeholder: t("related.pick"),
      "aria-label": t("related.pick"),
      oninput: (event) => {
        entry.value = event.target.value;
        if (entry.contact) {
          delete entry.contact;
          drawBadge();
        }
        suggest();
      },
      onfocus: () => suggest(),
      onblur: () => setTimeout(() => list.classList.add("hidden"), 150),
      onkeydown: (event) => {
        if (event.key === "Escape") list.classList.add("hidden");
        if (event.key === "Enter" && !list.classList.contains("hidden")) {
          event.preventDefault();
          list.querySelector("button")?.dispatchEvent(new MouseEvent("mousedown"));
        }
      },
    });
    const pick = (contact) => {
      entry.contact = contact.id;
      entry.value = displayName(contact);
      input.value = entry.value;
      list.classList.add("hidden");
      markDirty();
      drawBadge();
    };
    function suggest() {
      const query = input.value.trim();
      const found = query && !linked() ? search(query).filter((contact) => contact.id !== selfId).slice(0, 6) : [];
      fill(list, found.map((contact) => el("li", {}, el("button", {
        type: "button",
        class: "contact-row pick",
        // Before the input loses focus, which hides the list.
        onmousedown: (event) => {
          event.preventDefault();
          pick(contact);
        },
      }, [avatar(contact, "small"), el("span", { class: "row-main" }, el("span", { class: "row-title" }, displayName(contact)))]))));
      list.classList.toggle("hidden", !found.length);
    }
    function drawBadge() {
      fill(badge, linked() ? [
        el("span", { class: "badge ok" }, t("related.linked")),
        el("button", {
          type: "button",
          class: "link-button small",
          onclick: () => {
            delete entry.contact;
            markDirty();
            drawBadge();
          },
        }, t("related.unlink")),
      ] : []);
    }
    drawBadge();
    return el("div", { class: "person-picker" }, [input, list, badge]);
  }

  // A label from the list, or one of the user's own ("Custom…").
  function labelChoice(labels, entry, free) {
    if (free) {
      return el("input", {
        type: "text",
        class: "label-input",
        value: entry.label && entry.label !== "other" ? entry.label : "",
        placeholder: t("editor.label"),
        "aria-label": t("editor.label"),
        oninput: (event) => { entry.label = event.target.value || "other"; },
      });
    }
    const own = entry.label && !labels.includes(entry.label) ? entry.label : null;
    const select = el("select", {
      "aria-label": t("editor.label"),
      onchange: (event) => {
        if (event.target.value === "__custom__") {
          const text = window.prompt(t("editor.customLabel"), own || "");
          if (text && text.trim()) entry.label = text.trim();
          select.replaceWith(labelChoice(labels, entry, false));
          return;
        }
        entry.label = event.target.value;
      },
    }, [
      ...labels.map((label) => el("option", { value: label, selected: entry.label === label }, labelText(label))),
      own ? el("option", { value: own, selected: true }, own) : null,
      el("option", { value: "__custom__" }, t("editor.customLabelOption")),
    ]);
    return select;
  }

  // A service from the list, or one typed in.
  function choice(options, value, onchange, label) {
    const id = "services-" + Math.random().toString(36).slice(2);
    return el("span", { class: "service" }, [
      el("input", { type: "text", list: id, value: value || "", placeholder: label, "aria-label": label, oninput: (event) => onchange(event.target.value.trim()) }),
      el("datalist", { id }, options.map((option) => el("option", { value: option }))),
    ]);
  }

  // Day, month and an optional year; a date without a year is --MM-DD, as in
  // vCard and the API.
  function dateInput(value, onchange) {
    const match = /^(\d{4}|-)-(\d{2})-(\d{2})$/.exec(value || "") || [];
    const day = el("input", { type: "number", min: "1", max: "31", inputmode: "numeric", class: "day", value: match[3] ? String(Number(match[3])) : "", placeholder: t("date.day"), "aria-label": t("date.day") });
    const month = el("select", { "aria-label": t("date.month") }, [
      el("option", { value: "" }, t("date.month")),
      ...MONTHS.map((number) => el("option", { value: String(number), selected: match[2] && Number(match[2]) === number }, monthName(number))),
    ]);
    const year = el("input", { type: "number", min: "1800", max: "2200", inputmode: "numeric", class: "year", value: match[1] && match[1] !== "-" ? match[1] : "", placeholder: t("date.year"), "aria-label": t("date.year") });
    const update = () => {
      const d = Number(day.value);
      const m = Number(month.value);
      if (!d || !m) {
        onchange(day.value || month.value || year.value ? "invalid" : "");
        return;
      }
      const pad = (number) => String(number).padStart(2, "0");
      onchange(`${year.value ? String(year.value).padStart(4, "0") : "-"}-${pad(m)}-${pad(d)}`);
    };
    [day, month, year].forEach((node) => node.addEventListener("input", update));
    month.addEventListener("change", update);
    return el("span", { class: "date-input" }, [day, month, year]);
  }

  // The groups of the book as switches, and a field that makes a new one.
  function groupsEditor(draft, markDirty) {
    const box = el("div", { class: "chips" });
    const input = el("input", {
      type: "text",
      class: "group-input",
      maxlength: "100",
      placeholder: t("editor.newGroup"),
      "aria-label": t("editor.newGroup"),
      onkeydown: (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        addGroup();
      },
    });
    const addGroup = async () => {
      const name = input.value.trim();
      if (!name) return;
      try {
        const group = await api("/groups", { method: "POST", body: { name, members: [] } });
        state.groups.push(group);
        draft.group_ids.push(group.id);
        input.value = "";
        markDirty();
        renderList();
      } catch (error) {
        toast(error.message);
      }
      draw();
    };
    const draw = () => fill(box, [
      ...state.groups.map((group) => el("button", {
        type: "button",
        class: "chip" + (draft.group_ids.includes(group.id) ? " on" : ""),
        "aria-pressed": String(draft.group_ids.includes(group.id)),
        onclick: () => {
          if (draft.group_ids.includes(group.id)) draft.group_ids.splice(draft.group_ids.indexOf(group.id), 1);
          else draft.group_ids.push(group.id);
          markDirty();
          draw();
        },
      }, group.name)),
      el("span", { class: "group-add" }, [input, el("button", { type: "button", class: "ghost", onclick: addGroup }, t("editor.addGroup"))]),
    ]);
    draw();
    return box;
  }

  // The card as the API takes it: no empty rows, no editor-only fields.
  function cleanDraft(draft) {
    const body = {};
    [...FIELDS.name, ...FIELDS.work].forEach((field) => {
      if (draft[field.key] && draft[field.key].trim()) body[field.key] = draft[field.key].trim();
    });
    ["birthday", "hidden_birthday", "notes", "photo", "uid"].forEach((key) => {
      if (draft[key] && String(draft[key]).trim()) body[key] = String(draft[key]).trim();
    });
    Object.keys(FIELDS.lists).forEach((key) => {
      body[key] = (draft[key] || []).map((entry) => {
        const row = {};
        Object.entries(entry).forEach(([part, value]) => {
          if (typeof value === "string" && value.trim()) row[part] = value.trim();
        });
        return row;
      }).filter((row) => Object.keys(row).some((part) => !["label", "service", "country_code"].includes(part)));
    });
    if (draft.group_ids) body.group_ids = draft.group_ids;
    body.starred = Boolean(draft.starred);
    return body;
  }

  // A photo, shrunk to PHOTO_SIZE pixels on its longer side as JPEG, goes
  // straight to S3 through a presigned POST; the card keeps its path.
  async function uploadPhoto(file) {
    let blob = file;
    try {
      const bitmap = await createImageBitmap(file);
      const scale = Math.min(1, PHOTO_SIZE / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(bitmap.width * scale);
      canvas.height = Math.round(bitmap.height * scale);
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.88));
    } catch (error) {
      // A format the browser cannot draw goes as it is, if the API takes it.
    }
    if (!blob) throw new Error(t("api.bad_photo"));
    if (blob.size > state.me.photo_max_bytes) throw new Error(t("api.bad_photo"));
    const target = await api("/photos", { method: "POST", body: { content_type: blob.type, size: blob.size } });
    const form = new FormData();
    Object.entries(target.post.fields).forEach(([key, value]) => form.append(key, value));
    form.append("file", blob);
    const response = await fetch(target.post.url, { method: "POST", body: form });
    if (!response.ok) throw new Error(t("photo.failed"));
    return target.path;
  }

  // History --------------------------------------------------------------------

  async function showHistory(id) {
    fill(detailNode, el("article", { class: "card" }, [backButton(), el("p", { class: "muted" }, t("loading"))]));
    let items;
    try {
      items = (await api(`/contacts/${id}/revisions`)).items || [];
    } catch (error) {
      fill(detailNode, el("article", { class: "card" }, [backButton(), el("p", { class: "error" }, error.message)]));
      return;
    }
    if (route().id !== id || route().view === "edit") return;
    const contact = state.contacts.get(id);
    if (!items.length) {
      fill(detailNode, el("article", { class: "card" }, [backButton(), el("p", { class: "muted" }, t("history.none"))]));
      return;
    }
    // The version the search found is open, else the newest one that is not
    // a deletion.
    const highlight = state.highlight;
    state.highlight = null;
    const opened = highlight || (items.find((item) => item.action !== "delete") || {}).revision;
    const name = contact ? displayName(contact) : displayName(items.find((item) => item.action !== "delete") || items[0]);
    const list = el("ol", { class: "revisions" }, items.map((item, index) => {
      // The version before this one: what this one changed.
      const before = items.slice(index + 1).find((older) => older.action !== "delete") || null;
      const node = el("li", { class: highlight === item.revision ? "highlight" : null }, el("details", { open: opened === item.revision || null }, [
        el("summary", {}, [
          el("strong", {}, when(item.at)),
          el("span", { class: "badge " + (item.action === "delete" ? "warn" : "ok") }, t("action." + item.action)),
          item.from ? el("span", { class: "muted small" }, t("history.from", { date: when(item.from) })) : null,
          item.changes && item.changes.length && item.action !== "create" && item.action !== "delete"
            ? el("span", { class: "muted small" }, item.changes.map(fieldName).join(", ")) : null,
        ]),
        el("div", { class: "revision-body" }, [
          item.action === "delete" ? el("p", { class: "muted small" }, t("history.deletedHint")) : differences(before, item),
          revisionActions(id, item, index, contact),
        ]),
      ]));
      return node;
    }));
    fill(detailNode, el("article", { class: "card" }, [
      backButton(),
      el("h2", {}, t("history.title", { name })),
      contact ? el("div", { class: "actions start" }, [el("button", { type: "button", class: "ghost", onclick: () => go("#/c/" + id) }, t("history.toCard"))])
        : el("p", { class: "note" }, t("history.deletedNote")),
      el("p", { class: "muted small" }, t("history.hint")),
      list,
    ]));
    detailNode.querySelector(".revisions .highlight")?.scrollIntoView({ block: "center" });
  }

  function revisionActions(id, item, index, contact) {
    if (item.action === "delete") return null;
    // The newest version of a card that exists is the card itself.
    if (contact && index === 0) return null;
    return el("div", { class: "actions start" }, [el("button", {
      type: "button",
      class: "primary",
      onclick: async (event) => {
        if (!window.confirm(t(contact ? "history.restoreConfirm" : "history.undeleteConfirm", { date: when(item.at) }))) return;
        event.target.disabled = true;
        try {
          const saved = await api(`/contacts/${id}/restore`, { method: "POST", body: { revision: item.revision, etag: contact ? contact.etag : undefined } });
          state.contacts.set(saved.id, saved);
          toast(t("history.restored"));
          renderList();
          go("#/c/" + id);
        } catch (error) {
          toast(error.message);
          event.target.disabled = false;
          if (error.code === "changed") await reload();
        }
      },
    }, t(contact ? "history.restore" : "history.undelete"))]);
  }

  // What one version changed against the one before: a row per field, the
  // old value struck out above the new one.
  function differences(before, item) {
    const keys = [...FIELDS.name, ...FIELDS.work].map((field) => field.key).concat(FIELDS.order, ["notes", "photo", "starred"]);
    const rows = [];
    keys.forEach((key) => {
      const old = before ? valueText(key, before[key]) : "";
      const now = valueText(key, item[key]);
      if (old === now && !(key === "photo" && before && before.photo !== item.photo)) return;
      rows.push(el("tr", {}, [
        el("th", {}, fieldName(key)),
        el("td", {}, [
          old ? el("del", {}, old) : null,
          now ? el("ins", {}, key === "photo" && item.photo ? el("img", { class: "avatar small", src: item.photo, alt: t("history.photo") }) : now) : null,
          !now && old ? el("span", { class: "muted small" }, " " + t("history.removed")) : null,
        ]),
      ]));
    });
    if (!rows.length) return el("p", { class: "muted small" }, t("history.same"));
    return el("div", { class: "table-wrap" }, el("table", { class: "diff" }, el("tbody", {}, rows)));
  }

  // Groups ----------------------------------------------------------------------------
  // Apple's groups: their names and members. Here they are made, renamed and
  // deleted, and their members added and taken out; a card's editor puts it
  // into groups too.

  function showGroups() {
    const name = el("input", { type: "text", maxlength: "100", required: true, placeholder: t("editor.newGroup"), "aria-label": t("editor.newGroup") });
    fill(detailNode, el("article", { class: "card" }, [
      backButton(),
      el("h2", {}, t("groups.title")),
      el("p", { class: "muted small" }, t("groups.hint")),
      el("form", {
        class: "invite",
        onsubmit: async (event) => {
          event.preventDefault();
          try {
            const group = await api("/groups", { method: "POST", body: { name: name.value.trim(), members: [] } });
            await loadGroups();
            renderList();
            go("#/groups/" + group.id);
          } catch (error) {
            toast(error.message);
          }
        },
      }, [name, el("button", { type: "submit", class: "primary" }, t("editor.addGroup"))]),
      el("ul", { class: "contacts", role: "list" }, [el("li", {}, el("a", {
        class: "contact-row",
        href: "#",
        onclick: (event) => {
          event.preventDefault();
          showUngrouped();
        },
      }, [
        el("span", { class: "avatar initials small smart", "aria-hidden": "true" }, "∅"),
        el("span", { class: "row-main" }, [
          el("span", { class: "row-title" }, t("list.noGroup")),
          el("span", { class: "row-sub muted" }, t("groups.smart", { count: ungrouped(Array.from(state.contacts.values())).length })),
        ]),
      ]))]),
      state.groups.length ? el("ul", { class: "contacts", role: "list" }, state.groups.map((group) => el("li", {}, el("a", {
        class: "contact-row",
        href: "#/groups/" + group.id,
        onclick: (event) => {
          event.preventDefault();
          go("#/groups/" + group.id);
        },
      }, [
        el("span", { class: "avatar initials small", "aria-hidden": "true" }, initials({ first: group.name })),
        el("span", { class: "row-main" }, [
          el("span", { class: "row-title" }, group.name),
          el("span", { class: "row-sub muted" }, t("groups.members", { count: group.members.length })),
        ]),
      ])))) : el("p", { class: "muted" }, t("groups.none")),
    ]));
    name.focus();
  }

  // One group: its name, its members and the cards to add to it.
  function showGroup(id) {
    const group = state.groups.find((item) => item.id === id);
    if (!group) {
      if (state.loaded) go("#/groups");
      return;
    }
    // Changes go one after another, each with the etag the one before left.
    const save = async (changes, done) => {
      try {
        const saved = await api("/groups/" + group.id, { method: "PUT", body: { ...changes, etag: group.etag } });
        Object.assign(group, saved);
        if (done) toast(done);
      } catch (error) {
        toast(error.message);
        await loadGroups();
      }
      renderList();
      showGroup(id);
    };
    const members = group.members.map((member) => state.contacts.get(member)).filter(Boolean)
      .sort((a, b) => sortKey(a).localeCompare(sortKey(b), language));
    const results = el("ul", { class: "contacts picker", role: "list" });
    const find = el("input", {
      type: "search",
      placeholder: t("groups.addPlaceholder"),
      "aria-label": t("groups.addPlaceholder"),
      oninput: () => drawResults(),
      onkeydown: (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        results.querySelector("button")?.click();
      },
    });
    const drawResults = () => {
      const query = find.value.trim();
      const outside = (query ? search(query) : []).filter((contact) => !group.members.includes(contact.id)).slice(0, 8);
      fill(results, query && !outside.length ? [el("li", { class: "muted small" }, t("search.none"))] : outside.map((contact) => el("li", {}, el("button", {
        type: "button",
        class: "contact-row pick",
        onclick: () => save({ members: [...group.members, contact.id] }, t("groups.added", { name: displayName(contact) })),
      }, [
        avatar(contact, "small"),
        el("span", { class: "row-main" }, [el("span", { class: "row-title" }, displayName(contact)), el("span", { class: "row-sub muted" }, "+ " + t("groups.add"))]),
      ]))));
    };
    fill(detailNode, el("article", { class: "card" }, [
      el("button", { type: "button", class: "icon-button back-groups", onclick: () => go("#/groups") }, "← " + t("groups.title")),
      el("header", { class: "card-head" }, [
        el("span", { class: "avatar initials large", "aria-hidden": "true" }, initials({ first: group.name })),
        el("div", { class: "card-title" }, [
          el("h2", {}, group.name),
          el("p", { class: "muted" }, t("groups.members", { count: members.length })),
        ]),
      ]),
      el("div", { class: "actions start" }, [
        el("button", {
          type: "button",
          class: "ghost",
          onclick: () => {
            state.filter = "group:" + group.id;
            state.query = "";
            renderList();
            if (window.matchMedia("(max-width: 760px)").matches) go("#");
          },
        }, t("groups.show")),
        el("button", {
          type: "button",
          class: "ghost",
          onclick: () => {
            const text = window.prompt(t("groups.renamePrompt"), group.name);
            if (text && text.trim() && text.trim() !== group.name) save({ name: text.trim() }, t("groups.renamed"));
          },
        }, t("groups.rename")),
        el("button", {
          type: "button",
          class: "danger",
          onclick: async () => {
            if (!window.confirm(t("groups.deleteConfirm", { name: group.name }))) return;
            try {
              await api(`/groups/${group.id}?etag=${group.etag}`, { method: "DELETE" });
              toast(t("groups.deleted"));
            } catch (error) {
              toast(error.message);
            }
            if (state.filter === "group:" + group.id) state.filter = "all";
            await loadGroups();
            renderList();
            go("#/groups");
          },
        }, t("groups.delete")),
      ]),
      el("section", { class: "card-section" }, [el("h3", {}, t("groups.add")), find, results]),
      el("section", { class: "card-section" }, [
        el("h3", {}, t("groups.membersTitle")),
        members.length ? el("ul", { class: "contacts", role: "list" }, members.map((contact) => el("li", { class: "member" }, [
          contactRow(contact, false).firstChild,
          el("button", {
            type: "button",
            class: "icon-button remove",
            title: t("groups.remove"),
            "aria-label": t("groups.removeOf", { name: displayName(contact) }),
            onclick: () => save({ members: group.members.filter((member) => member !== contact.id) }, t("groups.removed", { name: displayName(contact) })),
          }, "−"),
        ]))) : el("p", { class: "muted" }, t("groups.empty")),
      ]),
    ]));
  }

  // Duplicates ----------------------------------------------------------------------
  // Cards that share a phone, an email or the whole name are one group; the
  // page finds them among the cards it has. The user picks the card to keep,
  // sees the card the merge gives, and the API saves it and deletes the rest
  // (they stay among the deleted, with their history).

  const MERGE_MAX = 10;

  function duplicateKeys(contact) {
    const keys = [];
    (contact.phones || []).forEach((entry) => {
      const number = phoneDigits(entry.value);
      if (number.length >= 7) keys.push(["phone", number.slice(-10)]);
    });
    (contact.emails || []).forEach((entry) => {
      if (entry.value) keys.push(["email", entry.value.trim().toLowerCase()]);
    });
    // A name alone joins cards only when it has a first and a last name.
    if (contact.first && contact.last) {
      keys.push(["name", foldText([contact.first, contact.middle, contact.last].filter(Boolean).join(" ")).split(/\s+/).sort().join(" ")]);
    }
    return keys;
  }

  function findDuplicates(ignored) {
    const parent = new Map();
    const root = (id) => {
      while (parent.get(id) !== id) id = parent.get(id);
      return id;
    };
    const owners = new Map();
    const reasons = new Map();
    state.contacts.forEach((contact) => {
      parent.set(contact.id, contact.id);
      duplicateKeys(contact).forEach(([kind, value]) => {
        const key = kind + ":" + value;
        if (!owners.has(key)) owners.set(key, []);
        owners.get(key).push({ id: contact.id, kind });
      });
    });
    owners.forEach((members) => {
      const ids = Array.from(new Set(members.map((member) => member.id)));
      if (ids.length < 2) return;
      ids.slice(1).forEach((id) => parent.set(root(id), root(ids[0])));
      ids.forEach((id) => {
        if (!reasons.has(id)) reasons.set(id, new Set());
        reasons.get(id).add(members[0].kind);
      });
    });
    const groups = new Map();
    state.contacts.forEach((contact) => {
      const top = root(contact.id);
      if (!groups.has(top)) groups.set(top, []);
      groups.get(top).push(contact);
    });
    const skipped = ignored.map((ids) => new Set(ids));
    return Array.from(groups.values())
      .filter((group) => group.length > 1 && !skipped.some((ids) => group.every((contact) => ids.has(contact.id))))
      .map((group) => ({
        contacts: group.sort((a, b) => filled(b) - filled(a) || String(a.created).localeCompare(String(b.created))),
        reasons: Array.from(new Set(group.flatMap((contact) => Array.from(reasons.get(contact.id) || [])))),
      }))
      .sort((a, b) => sortKey(a.contacts[0]).localeCompare(sortKey(b.contacts[0]), language));
  }

  // How much a card holds: the fullest one is kept by default.
  function filled(contact) {
    return Object.entries(contact).reduce((total, [key, value]) => {
      if (SKIPPED.has(key) && key !== "photo") return total;
      if (Array.isArray(value)) return total + value.length;
      return total + (value ? 1 : 0);
    }, 0);
  }

  // The card a merge gives: the fields of the card kept, the empty ones
  // filled from the others, every list joined without repeats, the notes of
  // all cards one after another.
  function mergeCards(main, others) {
    const card = JSON.parse(JSON.stringify(cleanDraft(main)));
    [...FIELDS.name, ...FIELDS.work].map((field) => field.key).concat(["birthday", "hidden_birthday", "photo", "uid"]).forEach((key) => {
      // One birthday: the kept card's, shown in the calendars or not.
      if ((key === "birthday" || key === "hidden_birthday") && (card.birthday || card.hidden_birthday)) return;
      if (!card[key]) {
        const found = others.find((other) => other[key]);
        if (found) card[key] = found[key];
      }
    });
    const same = {
      phones: (entry) => phoneDigits(entry.value),
      emails: (entry) => String(entry.value).trim().toLowerCase(),
      urls: (entry) => String(entry.value).trim().toLowerCase().replace(/^https?:\/\//, "").replace(/\/$/, ""),
    };
    Object.keys(FIELDS.lists).forEach((key) => {
      const seen = new Set();
      card[key] = [main, ...others].flatMap((contact) => contact[key] || []).filter((entry) => {
        const { label, ...rest } = entry;
        const id = same[key] ? same[key](entry) : foldText(JSON.stringify(rest));
        if (seen.has(id)) return false;
        seen.add(id);
        return true;
      }).map((entry) => ({ ...entry }));
    });
    delete card.group_ids;
    card.starred = [main, ...others].some((contact) => contact.starred);
    const notes = Array.from(new Set([main, ...others].map((contact) => (contact.notes || "").trim()).filter(Boolean)));
    if (notes.length) card.notes = notes.join("\n\n");
    return card;
  }

  async function showDuplicates() {
    fill(detailNode, el("article", { class: "card" }, [backButton(), el("h2", {}, t("dup.title")), el("p", { class: "muted" }, t("loading"))]));
    let ignored = [];
    try {
      ignored = (await api("/duplicates/ignored")).items || [];
    } catch (error) {
      toast(error.message);
    }
    if (route().view !== "duplicates") return;
    const groups = findDuplicates(ignored);
    fill(detailNode, el("article", { class: "card" }, [
      backButton(),
      el("h2", {}, t("dup.title")),
      el("p", { class: "muted small" }, t("dup.hint")),
      groups.length ? el("p", { class: "small" }, t("dup.count", { count: groups.length })) : el("p", { class: "muted" }, t("dup.none")),
      ...groups.map((group) => el("section", { class: "dup-group" }, [
        el("div", { class: "chips" }, group.reasons.map((reason) => el("span", { class: "badge warn" }, t("dup.reason." + reason)))),
        el("ul", { class: "contacts", role: "list" }, group.contacts.map((contact) => contactRow(contact, false))),
        el("div", { class: "actions start" }, [
          el("button", {
            type: "button",
            class: "primary",
            disabled: group.contacts.length > MERGE_MAX + 1,
            onclick: () => showMerge(group.contacts),
          }, t("dup.merge")),
          el("button", {
            type: "button",
            class: "ghost",
            onclick: async () => {
              try {
                await api("/duplicates/ignored", { method: "PUT", body: { ids: group.contacts.map((contact) => contact.id) } });
                toast(t("dup.ignored"));
              } catch (error) {
                toast(error.message);
              }
              showDuplicates();
            },
          }, t("dup.ignore")),
        ]),
      ])),
    ]));
  }

  function showMerge(contacts) {
    const view = dialog(t("dup.mergeTitle"));
    let mainId = contacts[0].id;
    const draw = () => {
      const main = contacts.find((contact) => contact.id === mainId);
      const others = contacts.filter((contact) => contact.id !== mainId);
      const card = mergeCards(main, others);
      const rows = [...FIELDS.name, ...FIELDS.work].map((field) => field.key)
        .concat(FIELDS.order, ["notes", "starred"])
        .map((key) => [key, valueText(key, card[key])])
        .filter(([, text]) => text);
      const button = el("button", {
        type: "button",
        class: "primary",
        onclick: async () => {
          button.disabled = true;
          try {
            const result = await api("/merge", {
              method: "POST",
              body: {
                target: { id: main.id, etag: main.etag },
                sources: others.map((contact) => ({ id: contact.id, etag: contact.etag })),
                card,
              },
            });
            state.contacts.set(result.contact.id, result.contact);
            result.merged.forEach((id) => {
              state.contacts.delete(id);
              state.drafts.delete(id);
            });
            state.drafts.delete(result.contact.id);
            await loadGroups();
            toast(t("dup.merged", { count: result.merged.length + 1 }));
            view.close();
            renderList();
            go("#/c/" + result.contact.id);
          } catch (error) {
            toast(error.message);
            button.disabled = false;
            if (error.code === "changed" || error.code === "deleted") {
              view.close();
              await reload();
            }
          }
        },
      }, t("dup.mergeButton", { count: contacts.length }));
      view.fill([
        el("p", { class: "muted small" }, t("dup.mergeHint")),
        el("fieldset", { class: "group" }, [
          el("legend", {}, t("dup.keep")),
          ...contacts.map((contact) => el("label", { class: "check" }, [
            el("input", {
              type: "radio",
              name: "keep",
              checked: contact.id === mainId,
              onchange: () => {
                mainId = contact.id;
                draw();
              },
            }),
            avatar(contact, "small"),
            el("span", {}, [displayName(contact), el("span", { class: "muted small" }, " · " + t("dup.stamp", { date: when(contact.created) }))]),
          ])),
        ]),
        el("h3", {}, t("dup.result")),
        el("dl", { class: "values merge-preview" }, rows.flatMap(([key, text]) => [el("dt", {}, fieldName(key)), el("dd", { class: "address" }, text)])),
        el("div", { class: "actions" }, [el("button", { type: "button", class: "ghost", onclick: () => view.close() }, t("editor.cancel")), button]),
      ]);
    };
    draw();
  }

  // Deleted cards ------------------------------------------------------------------

  async function showDeleted() {
    fill(detailNode, el("article", { class: "card" }, [backButton(), el("h2", {}, t("deleted.title")), el("p", { class: "muted" }, t("loading"))]));
    let items;
    try {
      items = (await api("/deleted")).items || [];
    } catch (error) {
      fill(detailNode, el("article", { class: "card" }, [backButton(), el("p", { class: "error" }, error.message)]));
      return;
    }
    if (route().view !== "deleted") return;
    fill(detailNode, el("article", { class: "card" }, [
      backButton(),
      el("h2", {}, t("deleted.title")),
      el("p", { class: "muted small" }, t("deleted.hint")),
      items.length ? el("ul", { class: "deleted" }, items.map((item) => el("li", {}, [
        avatar({ first: item.name, photo: item.photo, phones: [], emails: [] }, "small"),
        el("span", { class: "row-main" }, [
          el("span", { class: "row-title" }, item.name || t("contact.noName")),
          el("span", { class: "row-sub muted" }, item.into && state.contacts.has(item.into)
            ? t("deleted.mergedInto", { date: when(item.at), name: displayName(state.contacts.get(item.into)) })
            : t("deleted.at", { date: when(item.at) })),
        ]),
        el("span", { class: "actions" }, [
          el("button", { type: "button", class: "ghost", onclick: () => go(`#/c/${item.id}/history`) }, t("deleted.history")),
          el("button", {
            type: "button",
            class: "danger",
            onclick: async () => {
              if (!window.confirm(t("deleted.purgeConfirm", { name: item.name }))) return;
              try {
                await api("/deleted/" + item.id, { method: "DELETE" });
                toast(t("deleted.purged"));
              } catch (error) {
                toast(error.message);
              }
              showDeleted();
            },
          }, t("deleted.purge")),
        ]),
      ]))) : el("p", { class: "muted" }, t("deleted.none")),
    ]));
  }

  // Import and export ----------------------------------------------------------------

  // The cards of the files go to the API in parts; a card matching one in the
  // book (by UID, or by name with a shared phone or email) updates it.
  function showImport() {
    const view = dialog(t("import.title"));
    const input = el("input", { type: "file", accept: ".vcf,text/vcard,text/x-vcard", multiple: true });
    const progress = el("div", { class: "import-progress" });
    const start = el("button", { type: "submit", class: "primary" }, t("import.start"));
    view.fill(el("form", {
      onsubmit: async (event) => {
        event.preventDefault();
        if (!input.files.length) return;
        start.disabled = true;
        input.disabled = true;
        const cards = [];
        for (const file of input.files) {
          const text = await file.text();
          (text.match(/BEGIN:VCARD[\s\S]*?END:VCARD/gi) || []).forEach((card) => cards.push(card));
        }
        if (!cards.length) {
          fill(progress, el("p", { class: "error" }, t("api.bad_vcard")));
          start.disabled = false;
          input.disabled = false;
          return;
        }
        const parts = [];
        let part = [];
        let size = 0;
        cards.forEach((card) => {
          const bytes = new Blob([card]).size + 2;
          if (part.length && (part.length >= IMPORT_CARDS || size + bytes > IMPORT_BYTES)) {
            parts.push(part);
            part = [];
            size = 0;
          }
          part.push(card);
          size += bytes;
        });
        if (part.length) parts.push(part);
        const total = { created: 0, updated: 0, unchanged: 0, skipped: 0, photos_skipped: 0, groups: 0, errors: [] };
        const bar = el("progress", { max: String(cards.length), value: "0" });
        const line = el("p", { class: "muted small" });
        fill(progress, [bar, line]);
        let done = 0;
        for (const chunk of parts) {
          line.textContent = t("import.progress", { done, total: cards.length });
          try {
            const result = await api("/import", { method: "POST", body: { vcf: chunk.join("\r\n") + "\r\n" } });
            ["created", "updated", "unchanged", "skipped", "photos_skipped", "groups"].forEach((key) => { total[key] += result[key] || 0; });
            total.errors.push(...(result.errors || []).map((item) => ({ ...item, index: item.index + done })));
          } catch (error) {
            total.skipped += chunk.length;
            total.errors.push({ index: done, name: "", message: error.message });
          }
          done += chunk.length;
          bar.value = done;
        }
        fill(progress, [
          el("p", {}, t("import.done", total)),
          total.groups ? el("p", { class: "muted small" }, t("import.groups", { count: total.groups })) : null,
          total.photos_skipped ? el("p", { class: "muted small" }, t("import.photosSkipped", { count: total.photos_skipped })) : null,
          total.errors.length ? el("ul", { class: "errors" }, total.errors.slice(0, 20).map((item) => el("li", {}, [
            item.name || t("import.card", { number: item.index + 1 }), ": ",
            item.message || (has("api." + item.code) ? t("api." + item.code) : item.code) + (item.detail && has("field." + item.detail) ? " (" + fieldName(item.detail) + ")" : ""),
          ]))) : null,
        ]);
        start.remove();
        await reload();
      },
    }, [
      el("p", { class: "muted small" }, t("import.hint")),
      input,
      progress,
      el("div", { class: "actions" }, [start]),
    ]));
  }

  // The API writes the file and gives a link that works for five minutes.
  async function exportCards(ids) {
    try {
      const data = await api("/export", { method: "POST", body: ids ? { ids } : {} });
      if (!data.count) {
        toast(t("export.empty"));
        return;
      }
      const anchor = el("a", { href: data.url, download: data.name });
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      toast(t("export.done", { count: data.count }));
    } catch (error) {
      toast(error.message);
    }
  }

  // Settings ---------------------------------------------------------------

  function showAccount(section) {
    // Visitors of the demo share one account: none may change its password or
    // lock it behind their own authenticator app.
    const sections = config.demo ? [] : [["password", t("password.title")], ["mfa", t("mfa.title")]];
    sections.push(["language", t("language.title")], ["display", t("display.title")], ["sync", t("sync.title")]);
    if (state.me.admin) sections.push(["users", t("users.title")]);
    sections.push(["version", t("version.title")]);
    let current = sections.some(([key]) => key === section) ? section : sections[0][0];
    const body = el("div", { class: "settings-body" });
    const tabs = el("div", { class: "tabs", role: "tablist" });
    const close = () => {
      wrap.remove();
      document.removeEventListener("keydown", onKey);
    };
    const onKey = (event) => {
      if (event.key === "Escape") close();
    };
    const wrap = el("div", { class: "dialog-wrap", onclick: (event) => { if (event.target === wrap) close(); } }, [
      el("section", { class: "dialog wide", role: "dialog", "aria-modal": "true", "aria-label": t("account.title") }, [
        el("header", { class: "dialog-head" }, [
          el("h2", {}, t("account.title")),
          el("button", { type: "button", class: "icon-button", onclick: close }, t("close")),
        ]),
        tabs,
        body,
      ]),
    ]);
    function draw() {
      tabs.replaceChildren(...sections.map(([key, label]) => el("button", {
        type: "button",
        role: "tab",
        class: key === current ? "tab active" : "tab",
        "aria-selected": String(key === current),
        onclick: () => {
          current = key;
          draw();
        },
      }, label)));
      body.replaceChildren();
      if (current === "password") body.append(passwordForm());
      else if (current === "mfa") showMfa(body);
      else if (current === "display") showDisplay(body);
      else if (current === "sync") showSync(body);
      else if (current === "users") showUsers(body);
      else if (current === "version") showVersion(body);
      else {
        body.append(languagePicker(async (code) => {
          await saveLanguage(code);
          // Everything on screen is redrawn in the new language.
          close();
          render();
          showAccount("language");
        }));
      }
    }
    document.querySelector(".dialog-wrap")?.remove();
    document.addEventListener("keydown", onKey);
    document.body.append(wrap);
    draw();
  }

  // How names are shown; kept in this browser. The sort is above the list.
  function showDisplay(part) {
    const radios = (key, value, options) => el("fieldset", { class: "group" }, [
      el("legend", {}, t("display." + key)),
      ...options.map((option) => el("label", { class: "check" }, [
        el("input", {
          type: "radio",
          name: key,
          checked: value === option,
          onchange: () => {
            store(ORDER_KEY, option);
            renderList();
            renderDetail();
          },
        }),
        t(`display.${key}.${option}`),
      ])),
    ]);
    fill(part, [
      radios("order", nameOrder(), ["first", "last"]),
      el("label", { class: "check" }, [
        el("input", {
          type: "checkbox",
          checked: onlyPhones(),
          onchange: (event) => {
            store(PHONES_KEY, event.target.checked ? "1" : "");
            renderList();
          },
        }),
        t("display.onlyPhones"),
      ]),
      el("p", { class: "muted small" }, t("display.hint")),
    ]);
  }

  // Administrators: the accounts. A new user gets an invitation from Cognito;
  // the temporary password is also shown here once.
  function showUsers(part) {
    const note = el("div");
    const list = el("div", { class: "table-wrap" }, el("p", { class: "muted" }, t("loading")));
    const email = el("input", { type: "email", required: true, placeholder: "name@example.com", "aria-label": t("users.newEmail") });
    const admin = el("input", { type: "checkbox" });
    const invite = el("form", {
      class: "invite",
      onsubmit: async (event) => {
        event.preventDefault();
        try {
          const created = await api("/admin/users", { method: "POST", body: { email: email.value.trim(), admin: admin.checked } });
          note.replaceChildren(secretNote(created.email, created.temporary_password, true));
          email.value = "";
          admin.checked = false;
          await draw();
        } catch (error) {
          toast(error.message);
        }
      },
    }, [
      email,
      el("label", { class: "check" }, [admin, t("users.admin")]),
      el("button", { type: "submit", class: "primary" }, t("users.invite")),
    ]);
    part.replaceChildren(el("p", { class: "muted small" }, t("users.hint")), invite, note, list);

    async function update(user, changes, question) {
      if (question && !window.confirm(question)) return;
      try {
        const result = await api("/admin/users", { method: "PUT", body: { email: user.email, ...changes } });
        if (result.temporary_password) note.replaceChildren(secretNote(user.email, result.temporary_password, false));
        else toast(t("users.saved"));
      } catch (error) {
        toast(error.message);
      }
      await draw();
    }

    async function remove(user) {
      if (!window.confirm(t("users.deleteConfirm", { email: user.email, count: user.contacts }))) return;
      try {
        await api("/admin/users?email=" + encodeURIComponent(user.email), { method: "DELETE" });
        toast(t("users.deleted"));
      } catch (error) {
        toast(error.message);
      }
      await draw();
    }

    async function draw() {
      let users;
      try {
        users = (await api("/admin/users")).items || [];
      } catch (error) {
        list.replaceChildren(el("p", { class: "error" }, error.message));
        return;
      }
      list.replaceChildren(el("table", { class: "users" }, [
        el("thead", {}, el("tr", {}, [t("users.email"), t("users.status"), "2FA", t("users.contacts"), t("users.admin"), ""].map((text) => el("th", {}, text)))),
        el("tbody", {}, users.map((user) => el("tr", { class: user.enabled ? "" : "disabled" }, [
          el("td", {}, [user.email, user.self ? el("span", { class: "muted" }, " " + t("users.you")) : null]),
          el("td", {}, user.enabled ? t("users.state." + user.status) : t("users.disabled")),
          el("td", { class: user.mfa ? "" : "muted" }, [el("span", { class: "narrow-only" }, "2FA: "), t(user.mfa ? "users.mfaOn" : "users.mfaOff")]),
          el("td", {}, [el("span", { class: "narrow-only" }, t("users.contacts") + ": "), String(user.contacts)]),
          el("td", {}, el("label", { class: "admin-toggle" }, [
            el("input", {
              type: "checkbox",
              checked: user.admin,
              disabled: user.self,
              "aria-label": t("users.adminOf", { email: user.email }),
              onchange: (event) => update(user, { admin: event.target.checked }),
            }),
            el("span", { class: "narrow-only" }, t("users.admin")),
          ])),
          el("td", { class: "row-actions" }, user.self ? null : [
            el("button", { type: "button", onclick: () => update(user, { reset_password: true }, t("users.resetPasswordConfirm", { email: user.email })) }, t("users.resetPassword")),
            user.mfa ? el("button", { type: "button", onclick: () => update(user, { reset_mfa: true }, t("users.resetMfaConfirm", { email: user.email })) }, t("users.resetMfa")) : null,
            el("button", { type: "button", onclick: () => update(user, { enabled: !user.enabled }) }, t(user.enabled ? "users.disable" : "users.enable")),
            el("button", { type: "button", class: "danger", onclick: () => remove(user) }, t("users.delete")),
          ]),
        ]))),
      ]));
    }
    draw();
    email.focus();
  }

  // CardDAV: the address of the server and the app passwords that Apple
  // Contacts, Thunderbird and the rest sign in with (lambda/carddav.py).
  async function showSync(part, created) {
    part.replaceChildren(el("p", { class: "muted" }, t("loading")));
    let items;
    try {
      items = (await api("/app-passwords")).items || [];
    } catch (error) {
      part.replaceChildren(el("p", { class: "error" }, error.message));
      return;
    }
    const host = (state.me.dav_url || "").replace(/^https:\/\//, "").replace(/\/$/, "");
    const name = el("input", { type: "text", maxlength: "100", required: true, placeholder: t("sync.deviceHint"), "aria-label": t("sync.device") });
    const facts = el("dl", { class: "version-facts" }, [
      el("dt", {}, t("sync.server")), el("dd", {}, el("code", { class: "mono" }, host)),
      el("dt", {}, t("sync.user")), el("dd", {}, el("code", { class: "mono" }, state.me.email)),
    ]);
    const google = el("section", { class: "sync-section" });
    const icloud = el("section", { class: "sync-section" });
    const log = el("section", { class: "sync-section" });
    fill(part, [
      state.me.demo ? el("p", { class: "demo-note" }, t("sync.demo")) : null,
      google,
      icloud,
      el("h3", { class: "sync-title" }, t("sync.dav")),
      el("p", { class: "muted small" }, t("sync.hint")),
      facts,
      el("form", {
        class: "invite",
        onsubmit: async (event) => {
          event.preventDefault();
          try {
            await showSync(part, await api("/app-passwords", { method: "POST", body: { name: name.value.trim() } }));
          } catch (error) {
            toast(error.message);
          }
        },
      }, [name, el("button", { type: "submit", class: "primary" }, t("sync.create"))]),
      created ? passwordNote(created, host) : null,
      items.length ? el("div", { class: "table-wrap" }, el("table", { class: "users" }, [
        el("thead", {}, el("tr", {}, [t("sync.device"), t("sync.createdAt"), t("sync.used"), ""].map((text) => el("th", {}, text)))),
        el("tbody", {}, items.map((item) => el("tr", {}, [
          el("td", {}, item.name),
          el("td", {}, when(item.created)),
          el("td", { class: item.used ? "" : "muted" }, item.used ? when(item.used) : t("sync.never")),
          el("td", { class: "row-actions" }, el("button", {
            type: "button",
            class: "danger",
            onclick: async () => {
              if (!window.confirm(t("sync.revokeConfirm", { name: item.name }))) return;
              try {
                await api("/app-passwords/" + item.id, { method: "DELETE" });
                toast(t("sync.revoked"));
              } catch (error) {
                toast(error.message);
              }
              showSync(part);
            },
          }, t("sync.revoke"))),
        ]))),
      ])) : el("p", { class: "muted small" }, t("sync.none")),
      el("details", { class: "howto" }, [
        el("summary", {}, t("sync.howto")),
        el("ul", {}, ["apple", "mac", "thunderbird", "android", "outlook"].map((client) => el("li", {}, [el("strong", {}, t("sync.client." + client)), " ", t("sync.steps." + client, { host, email: state.me.email })]))),
      ]),
    ]);
    // A sync runs in the background: the cards, the state and the log are read
    // again a few seconds later.
    const refresh = () => [4000, 15000].forEach((delay) => setTimeout(async () => {
      if (!log.isConnected) return;
      await reload();
      showGoogle(google, refresh);
      showICloud(icloud, refresh);
      showSyncLog(log);
    }, delay));
    showGoogle(google, refresh);
    showICloud(icloud, refresh);
    showSyncLog(log);
    part.append(log);
  }

  // Google Contacts: connect, the state of the last sync, sync now, disconnect.
  async function showGoogle(part, refresh) {
    fill(part, [el("h3", { class: "sync-title" }, "Google"), el("p", { class: "muted small" }, t("loading"))]);
    let info;
    try {
      info = await api("/google");
    } catch (error) {
      fill(part, [el("h3", { class: "sync-title" }, "Google"), el("p", { class: "error" }, error.message)]);
      return;
    }
    const act = async (call, done) => {
      try {
        await call();
        if (done) toast(done);
      } catch (error) {
        toast(error.message);
      }
      showGoogle(part, refresh);
      if (refresh) refresh();
    };
    const head = el("h3", { class: "sync-title" }, "Google");
    if (!info.enabled) {
      fill(part, [head, el("p", { class: "muted small" }, t("google.off"))]);
      return;
    }
    if (!info.connected) {
      fill(part, [
        head,
        el("p", { class: "muted small" }, t("google.hint")),
        el("div", { class: "actions start" }, el("button", {
          type: "button",
          class: "primary",
          onclick: async (event) => {
            event.target.disabled = true;
            try {
              location.href = (await api("/google/start", { method: "POST" })).url;
            } catch (error) {
              toast(error.message);
              event.target.disabled = false;
            }
          },
        }, t("google.connect"))),
      ]);
      return;
    }
    const status = info.status === "ok" ? ["ok", t("google.status.ok")]
      : info.status === "reauth" ? ["warn", t("google.status.reauth")]
        : info.status === "error" ? ["warn", t("google.status.error")] : ["ok", t("google.status.connected")];
    fill(part, [
      head,
      el("p", { class: "muted small" }, t("google.connectedHint")),
      el("dl", { class: "version-facts" }, [
        el("dt", {}, t("google.account")), el("dd", {}, [info.email || "—", " ", el("span", { class: "badge " + status[0] }, status[1])]),
        el("dt", {}, t("google.lastSync")), el("dd", {}, info.last_sync ? when(info.last_sync) : t("sync.never")),
        el("dt", {}, t("google.linked")), el("dd", {}, String(info.linked || 0)),
        info.last_error ? el("dt", {}, t("google.error")) : null,
        info.last_error ? el("dd", { class: "error small" }, info.last_error) : null,
      ]),
      el("div", { class: "actions start" }, [
        info.status === "reauth" ? el("button", { type: "button", class: "primary", onclick: async () => { location.href = (await api("/google/start", { method: "POST" })).url; } }, t("google.reconnect")) : null,
        el("button", { type: "button", class: "ghost", onclick: () => act(() => api("/google/sync", { method: "POST" }), t("google.started")) }, t("google.syncNow")),
        el("button", {
          type: "button",
          class: "danger",
          onclick: () => {
            if (window.confirm(t("google.disconnectConfirm"))) act(() => api("/google", { method: "DELETE" }), t("google.disconnected"));
          },
        }, t("google.disconnect")),
      ]),
    ]);
  }

  // iCloud Contacts: connect with the Apple ID and an app-specific password,
  // the state of the last sync, sync now, disconnect.
  async function showICloud(part, refresh) {
    const head = el("h3", { class: "sync-title" }, "iCloud");
    fill(part, [head, el("p", { class: "muted small" }, t("loading"))]);
    let info;
    try {
      info = await api("/icloud");
    } catch (error) {
      fill(part, [head, el("p", { class: "error" }, error.message)]);
      return;
    }
    const act = async (call, done) => {
      try {
        await call();
        if (done) toast(done);
      } catch (error) {
        toast(error.message);
      }
      showICloud(part, refresh);
      if (refresh) refresh();
    };
    if (!info.enabled) {
      fill(part, [head, el("p", { class: "muted small" }, t("icloud.off"))]);
      return;
    }
    const form = () => {
      const appleId = el("input", { type: "email", required: true, autocomplete: "off", value: info.apple_id || "", placeholder: "name@icloud.com", "aria-label": t("icloud.appleId") });
      const password = el("input", { type: "password", required: true, autocomplete: "off", placeholder: "xxxx-xxxx-xxxx-xxxx", "aria-label": t("icloud.password") });
      const submit = el("button", { type: "submit", class: "primary" }, t("icloud.connect"));
      return el("form", {
        class: "icloud-form",
        onsubmit: (event) => {
          event.preventDefault();
          submit.disabled = true;
          act(() => api("/icloud", { method: "POST", body: { apple_id: appleId.value.trim(), password: password.value.trim() } }), t("icloud.connected"));
        },
      }, [
        el("label", {}, [t("icloud.appleId"), appleId]),
        el("label", {}, [t("icloud.password"), password]),
        el("p", { class: "muted small" }, [t("icloud.passwordHint") + " ", el("a", { href: "https://account.apple.com/account/manage", target: "_blank", rel: "noopener noreferrer" }, "account.apple.com")]),
        el("div", { class: "actions start" }, submit),
      ]);
    };
    if (!info.connected) {
      fill(part, [head, el("p", { class: "muted small" }, t("icloud.hint")), form()]);
      return;
    }
    const status = info.status === "ok" ? ["ok", t("google.status.ok")]
      : info.status === "reauth" ? ["warn", t("google.status.reauth")]
        : info.status === "error" ? ["warn", t("google.status.error")] : ["ok", t("google.status.connected")];
    fill(part, [
      head,
      el("p", { class: "muted small" }, t("icloud.connectedHint")),
      el("dl", { class: "version-facts" }, [
        el("dt", {}, t("icloud.appleId")), el("dd", {}, [info.apple_id || "—", " ", el("span", { class: "badge " + status[0] }, status[1])]),
        el("dt", {}, t("google.lastSync")), el("dd", {}, info.last_sync ? when(info.last_sync) : t("sync.never")),
        el("dt", {}, t("google.linked")), el("dd", {}, String(info.linked || 0)),
        info.last_error ? el("dt", {}, t("google.error")) : null,
        info.last_error ? el("dd", { class: "error small" }, info.last_error) : null,
      ]),
      info.status === "reauth" ? form() : null,
      el("div", { class: "actions start" }, [
        el("button", { type: "button", class: "ghost", onclick: () => act(() => api("/icloud/sync", { method: "POST" }), t("google.started")) }, t("google.syncNow")),
        el("button", {
          type: "button",
          class: "danger",
          onclick: () => {
            if (window.confirm(t("icloud.disconnectConfirm"))) act(() => api("/icloud", { method: "DELETE" }), t("icloud.disconnected"));
          },
        }, t("google.disconnect")),
      ]),
    ]);
  }

  // What every sync changed and where, newest first.
  async function showSyncLog(part, cursor, list) {
    if (!list) {
      list = el("ul", { class: "sync-log" });
      fill(part, [el("h3", { class: "sync-title" }, t("log.title")), el("p", { class: "muted small" }, t("log.hint")), list]);
    }
    let data;
    try {
      data = await api("/sync-log" + (cursor ? "?cursor=" + encodeURIComponent(cursor) : ""));
    } catch (error) {
      list.append(el("li", { class: "error" }, error.message));
      return;
    }
    part.querySelector(".log-more")?.remove();
    if (!data.items.length && !cursor) list.append(el("li", { class: "muted small" }, t("log.none")));
    data.items.forEach((line) => list.append(logLine(line)));
    if (data.next_cursor) {
      part.append(el("button", { type: "button", class: "ghost log-more", onclick: () => showSyncLog(part, data.next_cursor, list) }, t("log.more")));
    }
  }

  function logLine(line) {
    const source = line.source || "";
    const where = source === "to_google" ? t("log.toGoogle") : source === "google" ? t("log.fromGoogle")
      : source === "to_icloud" ? t("log.toICloud") : source === "icloud" ? t("log.fromICloud")
        : source.startsWith("dav:") ? t("log.fromDevice", { device: source.slice(4) }) : source;
    const card = line.contact && state.contacts.get(line.contact);
    const name = line.name ? (card ? el("a", {
      href: "#/c/" + card.id,
      onclick: (event) => {
        event.preventDefault();
        document.querySelector(".dialog-wrap")?.remove();
        go("#/c/" + card.id);
      },
    }, line.name) : line.name) : null;
    return el("li", { class: "log-line" + (line.action === "conflict" || line.action === "error" ? " warn" : "") }, [
      el("span", { class: "log-when muted small" }, when(line.at)),
      el("span", { class: "log-where" }, where),
      el("span", { class: "log-what" }, [
        el("strong", {}, has("log.action." + line.action) ? t("log.action." + line.action) : line.action),
        name ? " " : null, name,
        line.fields && line.fields.length ? el("span", { class: "muted" }, ": " + line.fields.map((key) => has("field." + key) ? fieldName(key) : key).join(", ")) : null,
        line.detail ? el("span", { class: "muted small log-detail" }, " — " + (has("log.detail." + line.detail) ? t("log.detail." + line.detail) : line.detail)) : null,
      ]),
    ]);
  }

  // The new password is shown once, with a profile that sets up iPhone, iPad
  // or Mac in one step.
  function passwordNote(created, host) {
    return el("div", { class: "secret-note" }, [
      el("p", {}, t("sync.created.text", { name: created.name })),
      el("p", {}, [t("sync.password") + " ", el("code", {}, created.password)]),
      el("div", { class: "actions start" }, el("button", {
        type: "button",
        class: "ghost",
        onclick: () => {
          const blob = new Blob([appleProfile(host, state.me.email, created.password, created.name)], { type: "application/x-apple-aspen-config" });
          const link = el("a", { href: URL.createObjectURL(blob), download: "contacts-" + created.name.replace(/[^\p{L}\p{N}]+/gu, "-") + ".mobileconfig" });
          document.body.append(link);
          link.click();
          link.remove();
          setTimeout(() => URL.revokeObjectURL(link.href), 10000);
        },
      }, t("sync.profile"))),
      el("p", { class: "muted small" }, t("sync.profileHint")),
    ]);
  }

  // A configuration profile of Apple with a CardDAV account
  // (com.apple.carddav.account). It is not signed, so the device says so.
  function appleProfile(host, email, password, device) {
    const esc = (text) => String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())).toUpperCase();
    const title = esc(t("brand") + " · " + host);
    return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadContent</key>
  <array>
    <dict>
      <key>CardDAVAccountDescription</key><string>${title}</string>
      <key>CardDAVHostName</key><string>${esc(host)}</string>
      <key>CardDAVPort</key><integer>443</integer>
      <key>CardDAVPrincipalURL</key><string>/dav/principal/</string>
      <key>CardDAVUseSSL</key><true/>
      <key>CardDAVUsername</key><string>${esc(email)}</string>
      <key>CardDAVPassword</key><string>${esc(password)}</string>
      <key>PayloadDisplayName</key><string>CardDAV</string>
      <key>PayloadIdentifier</key><string>${PROFILE_ID}.carddav.${uuid()}</string>
      <key>PayloadType</key><string>com.apple.carddav.account</string>
      <key>PayloadUUID</key><string>${uuid()}</string>
      <key>PayloadVersion</key><integer>1</integer>
    </dict>
  </array>
  <key>PayloadDescription</key><string>${esc(device)}</string>
  <key>PayloadDisplayName</key><string>${title}</string>
  <key>PayloadIdentifier</key><string>${PROFILE_ID}.${uuid()}</string>
  <key>PayloadType</key><string>Configuration</string>
  <key>PayloadUUID</key><string>${uuid()}</string>
  <key>PayloadVersion</key><integer>1</integer>
</dict>
</plist>
`;
  }

  function secretNote(email, password, invited) {
    return el("div", { class: "secret-note" }, [
      el("p", {}, t(invited ? "users.invited" : "users.newPassword", { email })),
      el("p", {}, [t("users.temporaryPassword") + " ", el("code", {}, password)]),
    ]);
  }

  // The version of the site and the releases of the demo repository that came
  // after it, with their notes. The API reads them from GitHub and keeps them
  // an hour.
  async function showVersion(part) {
    part.replaceChildren(el("p", { class: "muted" }, t("loading")));
    let info;
    try {
      info = await api("/version");
    } catch (error) {
      part.replaceChildren(el("p", { class: "error" }, error.message));
      return;
    }
    const anchor = (href, text) => el("a", { href, target: "_blank", rel: "noopener" }, text);
    let status;
    if (info.error) status = el("p", { class: "error" }, t("version.unavailable"));
    else if (!info.repository) status = el("p", { class: "muted" }, t("version.off"));
    else if (!info.latest) status = el("p", { class: "muted" }, t("version.none"));
    else if (!info.newer.length) status = el("p", { class: "totp-status" }, [el("span", { class: "badge ok" }, t("version.current")), " ", t("version.newest")]);
    else status = el("p", { class: "totp-status" }, [el("span", { class: "badge warn" }, t("version.behind")), " ", t("version.newer", { count: info.newer.length })]);
    fill(part, [
      el("dl", { class: "version-facts" }, [
        el("dt", {}, t("version.title")),
        el("dd", {}, info.version && info.url ? anchor(info.url + "/tag/" + encodeURIComponent(info.version), info.version) : info.version || "—"),
        el("dt", {}, t("version.repository")),
        el("dd", {}, info.repository ? anchor(info.url, info.repository) : "—"),
      ]),
      status,
      info.newer.length ? el("ul", { class: "releases" }, info.newer.map((release) => el("li", {}, [
        el("strong", {}, anchor(release.url, release.title)),
        release.date ? el("span", { class: "muted" }, " · " + release.date) : null,
        release.notes ? el("p", { class: "release-notes" }, release.notes) : null,
      ]))) : null,
    ]);
  }

  function passwordForm() {
    const error = el("p", { class: "error", role: "alert" });
    const current = el("input", { type: "password", autocomplete: "current-password", required: true });
    const next = el("input", { type: "password", autocomplete: "new-password", required: true, minlength: "12" });
    const again = el("input", { type: "password", autocomplete: "new-password", required: true, minlength: "12" });
    const button = el("button", { class: "primary", type: "submit" }, t("password.submit"));
    const form = el("form", {
      onsubmit: async (event) => {
        event.preventDefault();
        error.textContent = "";
        if (next.value !== again.value) {
          error.textContent = t("password.mismatch");
          return;
        }
        button.disabled = true;
        try {
          const { user } = await currentUser();
          await cognito((done) => user.changePassword(current.value, next.value, done));
          form.reset();
          toast(t("password.changed"));
        } catch (failure) {
          error.textContent = authMessage(failure);
        }
        button.disabled = false;
      },
    }, [
      el("label", {}, [t("password.current"), current]),
      el("label", {}, [t("password.new"), next]),
      el("label", {}, [t("password.again"), again]),
      el("p", { class: "muted small" }, t("password.rules")),
      el("div", { class: "actions" }, [button]),
      error,
    ]);
    return form;
  }

  // Two-factor sign-in with an authenticator app (TOTP): Cognito gives a
  // secret, the app shows codes, one code confirms the pairing.
  async function showMfa(part) {
    part.replaceChildren(el("p", { class: "muted" }, t("loading")));
    let enabled;
    try {
      const { user } = await currentUser();
      const data = await cognito((done) => user.getUserData(done, { bypassCache: true }));
      enabled = (data.UserMFASettingList || []).includes("SOFTWARE_TOKEN_MFA");
    } catch (error) {
      part.replaceChildren(el("p", { class: "error" }, authMessage(error)));
      return;
    }
    if (enabled) {
      part.replaceChildren(
        el("p", {}, [el("span", { class: "badge ok" }, t("mfa.on")), t("mfa.onText")]),
        el("div", { class: "actions" }, el("button", {
          type: "button",
          class: "danger",
          onclick: async () => {
            if (!window.confirm(t("mfa.disableConfirm"))) return;
            try {
              const { user } = await currentUser();
              await cognito((done) => user.setUserMfaPreference(null, { PreferredMfa: false, Enabled: false }, done));
              toast(t("mfa.disabled"));
            } catch (error) {
              toast(authMessage(error));
            }
            showMfa(part);
          },
        }, t("mfa.disable"))));
      return;
    }
    part.replaceChildren(
      el("p", { class: "muted" }, t("mfa.offText")),
      el("div", { class: "actions" }, el("button", { type: "button", class: "primary", onclick: () => pairApp(part) }, t("mfa.enable"))));
  }

  async function pairApp(part) {
    let user;
    let secret;
    try {
      user = (await currentUser()).user;
      secret = await new Promise((resolve, reject) => user.associateSoftwareToken({
        associateSecretCode: resolve,
        onFailure: reject,
      }));
    } catch (error) {
      toast(authMessage(error));
      return;
    }
    const uri = `otpauth://totp/${encodeURIComponent(ISSUER + ":" + state.me.email)}?secret=${secret}&issuer=${encodeURIComponent(ISSUER)}`;
    const qr = window.qrcode(0, "M");
    qr.addData(uri);
    qr.make();
    const error = el("p", { class: "error", role: "alert" });
    const code = el("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", pattern: "[0-9]{6}", maxlength: "6", required: true });
    const button = el("button", { class: "primary", type: "submit" }, t("mfa.submit"));
    part.replaceChildren(el("form", {
      class: "pair",
      onsubmit: async (event) => {
        event.preventDefault();
        error.textContent = "";
        button.disabled = true;
        try {
          await new Promise((resolve, reject) => user.verifySoftwareToken(code.value.trim(), ISSUER, { onSuccess: resolve, onFailure: reject }));
          await cognito((done) => user.setUserMfaPreference(null, { PreferredMfa: true, Enabled: true }, done));
          toast(t("mfa.enabled"));
          showMfa(part);
        } catch (failure) {
          error.textContent = authMessage(failure);
          button.disabled = false;
        }
      },
    }, [
      el("p", {}, t("mfa.step1")),
      el("img", { class: "qr", src: qr.createDataURL(5, 2), alt: t("mfa.qrAlt"), width: "205", height: "205" }),
      el("p", { class: "mono secret" }, secret),
      el("label", {}, [t("mfa.step2"), code]),
      el("div", { class: "actions" }, [button]),
      error,
    ]));
    code.focus();
  }

  async function boot() {
    if (!window.AmazonCognitoIdentity || !config.userPoolId || !config.clientId) {
      root.textContent = t("noAuth");
      return;
    }
    try {
      await currentSession();
      await loadLanguage();
      // The answer also sets the cookie that opens the photos.
      state.me = await api("/me");
      // The text about the app is for the page before the sign-in.
      const about = document.getElementById("about");
      if (about) about.remove();
      state.inHistory = stored(HISTORY_KEY) === "1";
      render();
      await loadContacts();
      renderList();
      renderDetail();
    } catch (error) {
      if (!document.querySelector(".login")) showLogin();
    }
  }

  window.addEventListener("hashchange", () => {
    // The browser's Back leaves the editor without asking.
    if (state.unsaved && !["edit", "new"].includes(route().view)) state.unsaved.flush();
    if (state.me) renderDetail();
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.unsaved) {
      state.unsaved.flush();
      event.preventDefault();
    }
  });

  setLanguage(known(stored(LANGUAGE_KEY)) ? stored(LANGUAGE_KEY) : browserLanguage());
  boot();
})();
