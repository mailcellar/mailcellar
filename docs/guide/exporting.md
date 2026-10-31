---
description: "Export mail from the backup to an mbox file, a maildir or a zip of .eml files, or just its attachments, to read in a desktop mail client or hand to an accountant or a lawyer. Read-only."
---

# Exporting mail

`mailcellar export` copies a selection of the backup out of mailcellar, to a
file you can open elsewhere. Use it to read old mail in a desktop mail client,
to hand mail to an accountant or a lawyer, or to keep a second copy of
offloaded mail. It changes nothing in Gmail or the backup, and needs no Gmail
sign-in. The README has the reference
([Exporting mail](../reference/export.md)).

## Which format

The format comes from the name: one ending `.mbox` or `.zip`, or one
ending `/` for a maildir folder.

| | `.mbox` | maildir (`name/`) | `.zip` |
| --- | --- | --- | --- |
| Good for | reading the mail in a mail client | reading it in a tool that reads a maildir | handing it to a person; a spreadsheet of what is in it |
| Holds | all the messages in one file, each with its Gmail labels | one file per message, each with its Gmail labels, and `index.csv` | one `.eml` file per message, and `index.csv` |
| Opens with | a mail client's import | mutt or neomutt, aerc, mu, notmuch, or Dovecot | any unzip tool, then a mail client or a text editor for the `.eml` files; a spreadsheet for the index |

All three hold the messages as the backup stores them, attachments
included; a maildir's files have LF line endings, as maildir readers
expect.

## Choosing the mail

The selection is the same as for `restore`: say which messages (`--batch`,
`--rule`, `--id`, `--offloaded` or `--all`), then narrow them. Values for one
narrowing option are alternatives, and every option you give must hold.

```bash
# everything from 2024
mailcellar export personal --all --after 2024-01-01 --before 2025-01-01 --to ~/mail-2024.zip

# what one offload batch took out of Gmail
mailcellar export personal --batch 2026-01-15-093000 --to ~/batch.mbox

# one company's mail, and the mail with one label
mailcellar export personal --all --sender @example.com --to ~/example-com.zip
mailcellar export personal --all --has-label Invoices --to ~/invoices.zip
```

`--after` is inclusive and `--before` is not. Try a selection on a small scale
first with `--limit 20`, which takes the oldest first. If nothing matches,
it says so and writes no file.

## What you get

The file or folder is written under another name and renamed only when
complete, so an interrupted export leaves nothing that looks finished. It will
not overwrite a file or write into a folder that exists, so choose a new name
each time, and it will not write inside the backup. If a file or folder
of the same name ending `.part` is there, perhaps left by an interrupted
export, delete it or choose another name.

When it finishes it says how many messages it wrote and their size.

**In a mail client.** Most clients can import an mbox file; look for an import
option for "mbox" or "Unix mailbox" files. Each message carries an
`X-Gmail-Labels` header with its labels, which some clients show and most
ignore. This has not been tried in every client, so check a small export
first (`--limit 20`) before exporting a lot.

**In a maildir reader.** Point the reader at the folder, for example
`mutt -f ~/old-mail/` or a mu or notmuch configuration whose mail root is
it. Messages read in Gmail are marked read, starred ones flagged. Each
message carries its labels twice: `X-Gmail-Labels`, as in an mbox, and
`X-Keywords`, the header OfflineIMAP reads, with Gmail's system labels by
their IMAP names (`\Inbox`, `\Important`). mu takes `X-Keywords` as its
tags: `mu find tag:work`, or `mu find 'tag:\\Inbox'` for a system
label. As with mbox, check a small export first (`--limit 20`).

**The index.** In a zip or a maildir, `index.csv` has one row per message:
date (UTC), sender, subject, labels (separated by `;`), ID and the file's
path inside the zip or folder. A subject or sender that begins with `=`,
`+`, `-` or `@` is written with a `'` in front. Mail is written by other people, and without the `'` a
spreadsheet could run such a subject as a formula. If you see a `'`, that is
why.

## Just the attachments

An accountant who wants every invoice doesn't want the emails they came
in. `--attachments` writes the attached files themselves, with an index
of the message each came from:

```bash
# every PDF example.com sent since 2020, in one zip
mailcellar export personal --all --after 2020-01-01 --sender @example.com \
    --attachments --type pdf --to ~/invoices.zip
```

Each file is named by its date, its sender and its own name
(`2024-03-01 billing@example.com invoice-0042.pdf`), so a folder of them
sorts by date. The same file sent twice is written once, and `index.csv`
lists both messages. `--type` can be repeated (`--type pdf --type xlsx`)
and takes a MIME type, a short one or an extension. Images in the text
of a message (logos, signatures) are left out; `--inline` takes them
too. With a name ending `/` instead of `.zip`, the files go in a folder.

## Browsing and searching your mail

mailcellar has no mail viewer: `mailcellar search` finds mail and says
where its file is. Two free tools can show and search what you keep.

**[Bichon](https://github.com/rustmailer/bichon)**, a self-hosted archive
with a web interface, can import an mbox export:

```bash
mailcellar export personal --all --to ~/archive.mbox
```

In Bichon, add a "local account", then run `bichon-cli`, choose Import,
then MBOX, and take the folders from the labels in the mail headers
(`X-Gmail-Labels`). Each message goes into one folder: the first of your
own labels, or else Inbox, Sent or its category. Its other labels are
not kept, and mail with no labels at all lands in INBOX. Bichon keeps
its own compressed copy of the mail, so look after it as you would the
export. Its first sign-in is a default password: change it, and if you
run it in Docker, publish its port only on this machine
(`-p 127.0.0.1:15630:15630`).

**[notmuch](https://notmuch.org/)** indexes a backup folder directly, with
no export. Give it a configuration of its own, with its database outside
the data root and file renaming turned off, so it never writes to the
backup or renames a file in it:

```ini
[database]
path=/home/you/.notmuch/personal
mail_root=/home/you/mailcellar/you@example.com

[new]
tags=
ignore=msg-db.sqlite;meta.sqlite;chain.jsonl

[maildir]
synchronize_flags=false
```

`mail_root` is one account's backup folder (`mailcellar config` shows the
data root). Then `NOTMUCH_CONFIG=<that file> notmuch new` indexes it, in
seconds after the first time, so run it after each backup. Search text,
subjects, senders, dates and attachment names, and read whole threads,
in notmuch or a client built on it (alot, neomutt, Emacs). It doesn't
show Gmail's labels, which the backup keeps apart from the `.eml` files.

## When it reports missing files

```
N messages have no .eml file in the backup and were left out, e.g. <ID>. Run mailcellar verify to see what is missing.
```

The rest of the export is complete, but the command ends with exit status 1 so
a script notices. `mailcellar verify` lists the missing files; for mail still
in Gmail, `mailcellar backup` downloads them again (see
[Recovering](recovery.md#a-damaged-or-lost-data-root)). Then export again
under a new name.

## Looking after the copy

An export is a copy of your mail, outside the backup, and nothing tracks it.
Keep it somewhere as private as the data root, do not send it by an unencrypted
channel, and delete it when the person has what they need. Deleting an export
affects nothing else.
