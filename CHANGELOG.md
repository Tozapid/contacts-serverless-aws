# Changelog

## v0.2.0

### Added

- A mail through SES to a user whose sync with Google or iCloud fails: at once when the sign-in is lost, after three failures in a row otherwise; one mail per run of failures, in the language picked on the page, else in English. The stack makes `domain_name` an SES identity with DKIM, MAIL FROM and DMARC for it (`sender_address`, production only; the demo sends nothing).

## v0.1.0

### Added

- Address books of their own per user, with every field of Apple Contacts and Google Contacts: name with pronunciation, maiden name, nickname, "file as", pronouns, work, phones, emails, addresses, websites, a birthday with or without a year, dates, related people, messengers, social profiles, custom fields, favourites, notes and a photo, taken from a file or pasted from the clipboard (a button, or ⌘V / Ctrl+V in the editor) and opened full size on a click.
- The history of every card with the differences between versions and a restore of any version; deleted cards to restore or purge.
- Instant search across every field, phone numbers by their digits, and in the other keyboard layout (`Jkmuf` finds "Ольга"); a switch searches the history of changes and the deleted cards too.
- Drafts: an edit not saved yet is kept on the server and comes back after a reload or on another device.
- Duplicates by a shared phone, a shared email or the same name, merged with a choice of the card to keep; a merge can be undone.
- Groups as Apple keeps them: create, rename, delete, a page per group with its members; the smart group "No group" lists the cards in no group.
- Family relations between cards in their own words, the cards that list a card, and a family graph two steps out.
- A birthday kept out of the calendars of Google and Apple: it goes out as a plain labelled date and stays on the card.
- Import and export of vCard 2.1, 3.0 and 4.0 with the labels of Apple and the photos.
- Sync over CardDAV with Apple Contacts, Thunderbird, Android (DAVx⁵) and Outlook, with app passwords and a profile for Apple devices; sync both ways with Google Contacts (People API) and with iCloud Contacts (CardDAV with an app-specific password). The address book is the source of truth, and the sync log tells what changed where.
- Settings: password, two-factor authentication, language (English, Russian, French, Italian), name order, hiding the cards without a phone, sync, users for administrators, version.
- The public demo: the sign-in published and filled in, the sync with Google, iCloud and devices shown but not run, the administration read-only, and an hourly reset to the examples of `seed/seed.json`.
