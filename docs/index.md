---
title: "Back up Gmail and IMAP email to your own disk"
description: "mailcellar backs up Gmail, Google Workspace and any IMAP mailbox to your own disk, takes old mail out by rule to free space, and keeps it searchable and restorable."
---

# mailcellar

**Your own checked copy of your email, and room in your mailbox.**

Your mail lives on one server you don't control: a deletion, a
compromised account or a lock-out can take it, and it fills up.
mailcellar keeps an incremental, checked backup on your own disk, then
frees space by taking old mail out of the mailbox by rules you review,
keeping it searchable and restorable. It works with Gmail, Google
Workspace and any IMAP mailbox.

```bash
uv tool install mailcellar      # or: pipx install mailcellar
mailcellar setup alex@example.org --client client_secret.json   # Gmail; or --imap HOST
mailcellar backup alex          # the first backup; resumes where it left off if stopped
mailcellar count alex           # what the mailbox has against what the backup has
```

## Why mailcellar

- **Your storage is full.** Offload old mail by rule (older than five
  years, newsletters, large attachments); `search` and `restore` still
  reach it ([Offloading old mail](guide/offloading.md)).
- **You have no backup of your own.** A provider's redundancy doesn't
  cover a deletion, a compromised account or a lock-out, and its export
  starts from scratch each time. mailcellar fetches only what changed and
  checks the backup against the mailbox (`count`) and itself (`verify`)
  ([Scheduled backups](guide/scheduling.md)).
- **Several providers, or moving.** One backup, search and set of reports
  across Gmail and IMAP; `restore --to` copies mail between accounts,
  labels to folders and back ([Restoring mail](guide/restore.md)).
- **You use gyb.** Your folders work as they are, and gain offloading,
  search, scoped restore and checks ([Migrating from gyb](guide/from-gyb.md)).
- **You run a Workspace domain.** Unattended, read-only backups of every
  mailbox through a service account ([Workspace](guide/workspace-service-account.md)).
- **A dispute or a records request.** A legal hold keeps the
  correspondence out of reach of offloading and forgetting,
  `verify --chain` shows it unchanged, and `export` hands it over, or just its
  attachments ([Holds](guide/legal-holds.md), [Exporting](guide/exporting.md)).
- **Mail you mustn't keep.** `forget` removes it from the backup, and a
  rule's `forget_after` does so on a schedule ([Forgetting mail](reference/forget.md)).

## What it does

- **Back up** every account incrementally, in Got Your Back's format; an
  existing gyb backup carries on where it is.
- **Offload** old mail by rule, in stages you check: plan, review, mark,
  look in your mail client, then trash. The backup is checked for every
  message before it goes, and nothing is ever deleted permanently.
- **Find and restore**: search the backups, offloaded mail included, and
  put mail back into the same account or another.
- **Deduplicate, export and report**: remove duplicate copies within and
  across accounts, copy mail out (an mbox, a zip, a maildir, or just the
  attachments), and see what takes up the space.
- **Keep mail as it is**: a hash chain shows the backup hasn't changed
  since each message was saved (`verify --chain`), and a legal hold keeps
  the mail a dispute or an audit needs out of reach of offloading,
  forgetting and dedup, in the mailbox and in the backup.

Only `offload apply`, `dedup apply` and `restore --yes` change a mailbox,
each a dry run until you add a flag; on an IMAP server, only where the
server can move mail safely ([What changes your mailbox](reference/what-changes-gmail.md)).

**Your mail stays yours.** mailcellar runs on your own computer: there is
no service in between. Gmail is read through your own Google Cloud project;
an IMAP account needs only its password (an app password, where the
provider asks for one). mailcellar is in beta; it runs
on macOS and Linux and needs Python 3.14, which
[uv](https://docs.astral.sh/uv/) installs for you.

**Not for everyone.** It's a command-line tool for people who can set up a
Google Cloud project or an app password. It has no mail reader (export a
maildir or an mbox for one), doesn't run on Windows, and is one person's
backup, not an organization's archive
([how it compares](guide/troubleshooting.md#how-does-it-compare-with-other-tools)).

## Where to start

1. **Add an account**, one of two ways:
    - **Gmail or Google Workspace:** [set up Google Cloud](guide/google-cloud.md),
      the project and OAuth client mailcellar signs in with (about ten
      minutes, once), then run `mailcellar setup` with the client's JSON.
    - **iCloud Mail, Yahoo Mail, Fastmail or your own server:** run
      `mailcellar setup ADDRESS --imap HOST` with the account's password (an
      app password, where the provider asks for one)
      ([Backing up an IMAP account](guide/imap.md) has the servers and
      what each one allows).
2. Follow the [quick start](https://github.com/mailcellar/mailcellar#quick-start)
   to make the first backup and check it.
3. [Schedule backups](guide/scheduling.md); for Gmail, at least every four
   weeks. Then [keep the backup safe](guide/backing-up.md): check it, and
   keep a second copy of it.
4. When you are ready to free space, [offload old mail](guide/offloading.md),
   one stage at a time.

Coming from gyb? [Migrating from gyb](guide/from-gyb.md) continues your
existing backup in place. Something went wrong?
[Recovering](guide/recovery.md) and [Troubleshooting](guide/troubleshooting.md)
cover the usual cases.

Every command and option is described in the
[command reference](reference/index.md), and
[How it works](architecture.md) explains the design.
