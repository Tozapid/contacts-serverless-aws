# Changelog

## v0.5.0

### Changed

- The demo is reset six hours after the first change instead of every hour, and not at all while nobody changes anything: most hours of a demo cost nothing now. The first change through the API (any request but a read that succeeds) makes a one-off schedule `<name>-reset` of EventBridge Scheduler, which calls the reset function `reset_delay_hours` later with the role `<name>-reset-scheduler` and then deletes itself; while it waits, further changes make none. The EventBridge rule of the hourly reset is gone. `reset_schedule` became `reset_delay_hours`, a number of hours (`0`, the default, no reset; `6` in the demo).
- The sign-in page of the demo says when it goes back to the start: six hours after a change.

## v0.4.1

### Removed

- `demo/moved.tf`: the state of the demo already keeps its resources under `module.contacts`. A site that still has the resources at the top of its state moves them in its own root module before taking this version.

## v0.4.0

### Changed

- The repository is a Terraform module: the root makes no providers and no backend (the caller passes `aws` and `aws.global`), and a site is a module call pinned to a release tag, `git::https://github.com/Tozapid/contacts-serverless-aws.git?ref=vX.Y.Z`; a newer version of a site is a newer `ref`. The public demo is a call of the module in `demo/`, which the CI deploys (`working_directory: demo`); `demo/moved.tf` moves its resources under `module.contacts` in the state. The defaults are those of a production site (`demo = false`, no reset); `name` and `admin_email` have none. New input `ses_identity` (false when the caller manages the SES identity of the domain). The region comes from the provider, the tags from its `default_tags`; the zips of the functions are built in `.build/` of the root module. `examples/production.tfvars` became `examples/production/main.tf`.
- On an iPhone the page no longer zooms in on a tap into a field: fields on touch screens are 16px (Safari zooms into smaller text), and the text size does not change on rotation. A dialog is as high as the visible screen (`100dvh`), not as the screen behind the bars of the browser.

### Fixed

- A preview could not be deployed once the account held ten CloudFront public keys (`TooManyPublicKeys`), a limit that is not raised. A preview (`<name>-pr<N>`) now signs photo cookies with the key of the main site, which publishes its ids in the SSM parameter `/<name>/cloudfront-key-ids`; only the main site makes a key. `photos_key_from` names another stack to borrow from, or `""` for a key of its own. The main site has to be deployed with this version before the first such preview.

## v0.3.0

### Added

- CloudFront access logs, optional: with `access_logs_bucket` set, the standard logs (v2) of the distribution go to that bucket under `cloudfront/<name>/` as JSON lines, and the S3 server access logs of the photos bucket under `s3/`, for an overview of the account such as [account-serverless-aws](https://github.com/Tozapid/account-serverless-aws) to read. The CI passes the repository variable `ACCESS_LOGS_BUCKET`; unset, there are none.

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
