---
description: "Answers to common questions about mailcellar: slow runs, mail missing from the backup, storage still full, weekly sign-ins, deletion, how it compares with other tools, and the Mac app of the same name."
---

# Troubleshooting and FAQ

Short answers to questions that come up. For something that has gone wrong
and needs steps to put right (a lost data root, an interrupted run, a
leaked token), see [Recovering when something goes wrong](recovery.md).

## A run is slow or seems stuck

Gmail limits how fast one user can make requests, and mailcellar slows
down when Gmail says so: it shrinks its batches and retries after a wait
that doubles from one second up to about a minute. A large mailbox, a first
backup, `offload apply` and `restore` all meet this.

To see what it is doing, add `-v` to the command:

```bash
mailcellar backup work -v
```

`-v` shows every retry and wait. Waits of 10 seconds or more are shown
without it, as an INFO line naming the account and the reason, so a run
held up by Gmail's quota says so. Progress lines go to stderr; `-q` turns
them off and leaves only warnings and errors.

If the retries are exhausted, the run reports the messages it could not
handle and exits with status 1; running it again carries on. Nothing is
lost, and the next run does not repeat what is already done. For work that
takes more than a few minutes (trashing is about 2,000 messages a minute,
uploading about six messages a second), run it in the background.

## `count` reports mail missing from the backup

```bash
mailcellar count work
```

The line `Missing from backup` is mail Gmail has that the backup lacks. Most
often it is mail that arrived since the last backup. Run `backup`, then
`count` again: the number should be zero. It also appears after a failed
or interrupted backup, for the same reason.

`count` prints the other lines only when they are not zero:

| Line | Meaning |
| --- | --- |
| `Chats (never backed up)` | Gmail chats, which are skipped on purpose (below). |
| `Only in backup (not in mailbox)` | The backup holds mail Gmail does not list: deleted for good, or in Trash or Spam. Offloaded mail is counted apart. |
| `  of which permanently deleted` | Gone from Gmail altogether. Only `forget --deleted` takes these out of the backup. |
| `Offloaded (kept in backup)` | Mail you offloaded on purpose. |
| `Indexed but .eml missing` | The backup's index lists a message whose file is gone. The next `backup` downloads it again if Gmail still has it. |

## Chats are never backed up

Neither mailcellar nor gyb, whose format it keeps, stores Hangouts chats
(the `CHAT` and `CHATS` labels). `count` lists them on their own line so
they do not look like missing mail.

## Storage is still full after offloading

Gmail shares its storage with Google Drive and Google Photos: the free
15 GB, or a Google One plan, covers all three. If most of it is Drive or
Photos, offloading mail frees only Gmail's part. Google's
[storage page](https://one.google.com/storage) shows how it is split.

Offloaded mail also counts until it leaves Gmail's Trash, which Gmail
empties 30 days after the move, and Spam counts too.
`mailcellar report storage work` shows how much each batch has freed so
far and how much is still in Trash ([Offloading, step 8](offloading.md#8-check)).

## Signing in again every week

The Google Cloud project's consent screen is in *Testing* status, where
Google expires tokens after 7 days. Either accept the weekly sign-in (and
renew ahead of it with `mailcellar auth ACCOUNT --scope readonly --renew`), or
publish the app. See
[Setting up Google Cloud](google-cloud.md#testing-or-in-production).
The error you see in the meantime, and how to sign in ahead of time, is in
[the recovery guide](recovery.md#a-token-expired-or-was-revoked).

## Why not IMAP, or Google Takeout?

Both can copy mail out of Gmail. mailcellar uses the Gmail API because it
keeps what makes a Gmail mailbox a Gmail mailbox:

- **IMAP** presents labels as folders. A message with three labels is one
  message in Gmail but appears in three folders, so a backup over IMAP
  downloads it three times and has no single record of its labels.
- **Takeout** is a full export each time, so every backup copies the whole
  mailbox again, and the labels are kept only in a header inside each
  message.

The API gives each message once, with its labels and a change history,
so a later backup reads only what changed. It also lets mailcellar later
check a backup against Gmail, and put mail back with its labels.

That is about Gmail. Another provider's mailbox, which has only IMAP, is
backed up over IMAP: [Backing up an IMAP account](imap.md).

## How does it compare with other tools?

Other tools back up, sync or archive mail. mailcellar backs up, frees
space by rules you review, and keeps the way back; each tool below covers
part of that or a different job, and several run alongside it. As each
describes itself, in its README or on its site, in October 2026:

- **[Got Your Back](https://github.com/GAM-team/got-your-back) (gyb)**
  backs up Gmail over the Gmail API and restores it, also into a Google
  Group, and works with Workspace service accounts. mailcellar reads and
  writes gyb's format byte for byte, so it continues a gyb backup, and gyb
  can still read it: see [Migrating from gyb](from-gyb.md).
- **[Gmvault](https://github.com/gaubert/gmvault)** backs up a Gmail
  account, in full or incrementally, and restores it to Gmail. Its
  repository was last changed in May 2022. Its backup is a folder of its
  own (`gmvault-db`), which mailcellar doesn't read.
- **[mbsync](https://isync.sourceforge.io/) (isync) and
  [OfflineIMAP](https://github.com/OfflineIMAP/offlineimap3)** keep an IMAP
  mailbox and a local Maildir in step, so a Maildir mail client can read
  the mail offline. Changes go both ways, deletions included (as mbsync's
  page puts it), which suits reading mail; mailcellar keeps what the
  mailbox deletes until you forget it. Over IMAP, Gmail's labels are
  folders (see the answer above). Either can run alongside mailcellar.
- **[Thunderbird](https://www.thunderbird.net/)** is a desktop mail
  client for Windows, macOS and Linux. It keeps an offline copy of an IMAP
  account, Gmail included, for reading, which mailcellar leaves to mail
  clients by design. The copy follows the server, so mail deleted in
  Gmail goes from it too. Mozilla's Gmail article says each label is a
  folder with its own copy, and that only moving a message to Trash
  removes all its labels: dragging mail into Local Folders takes away a
  label, not the message, so it frees no space in Gmail by itself. Its
  ImportExportTools NG add-on imports mbox, which `mailcellar export`
  writes.
- **[imapsync](https://github.com/imapsync/imapsync)** copies one IMAP
  account to another, one way and without duplicates, with an option for
  Gmail, for moving to a new provider. It works server to server, with
  no copy of its own; mailcellar's `restore --to` copies between accounts
  from the backup, so the copy stays.
- **[MailStore Home](https://www.mailstore.com/en/products/mailstore-home/)**
  is a Windows program, free for private use, that archives mail from
  Gmail, other IMAP or POP3 accounts, Outlook, Exchange and Thunderbird
  into one searchable archive; MailStore Server is its edition for
  businesses.
- **Google Takeout:** see [Why not IMAP, or Google
  Takeout?](#why-not-imap-or-google-takeout) above.

## Is anything ever deleted?

- **From Gmail:** never permanently. The commands that change Gmail
  (`dedup apply`, `offload apply`, `restore --yes`) are dry runs unless you
  add a flag, and the strongest thing any of them does is move mail to
  Trash, which Gmail empties after 30 days. `restore` only adds.
- **From the backup:** nothing, unless you ask. `forget` deletes mail that
  Gmail no longer has, offloaded mail or (`--deleted`) mail you deleted
  there, after you type the count. It holds it, and `--undo` can put it
  back until `--empty`, which waits 30 days, deletes it for good;
  `--deleted` refuses to take more than a tenth of the backup unless
  forced.
  Offloaded mail is never taken as deleted.

Because of that, once mail has been offloaded and Gmail has emptied its
Trash, the backup holds the only copy. Keep a second copy of the data
root.

## Is this the Mac app called Mailcellar?

No. A paid macOS app is sold under the name "Mailcellar" on newmac.app.
It isn't made by this project and has no connection to it.

As its own site described it on 29 October 2026, that app isn't signed
with an Apple Developer ID or notarized by Apple. Its install guide asks
you to override macOS's warning, or to run a script that removes the
quarantine flag so the warning never appears. Its terms and privacy
policy name no company or address. And it asks for an app password to
your email account. Whatever app you use, don't give a password for your
email to software you can't verify, and don't bypass macOS's warnings to
open it.

mailcellar is free and open source (Apache-2.0), and it comes only from
these places:

- the source at [github.com/mailcellar/mailcellar](https://github.com/mailcellar/mailcellar);
- the package `mailcellar` on [PyPI](https://pypi.org/project/mailcellar/),
  installed with `uv tool install mailcellar` or `pipx install mailcellar`;
- these docs, at [mailcellar.github.io/mailcellar](https://mailcellar.github.io/mailcellar/).

It has no paid edition, no license key, no `.dmg` or app to download, and
no install script to pipe into a shell. Nothing in these docs asks you to
open an app macOS warns about, or to remove macOS's quarantine flag. An
IMAP account's password is kept in your own config folder, readable only
by you, and sent only to that account's mail server.
