# Exporting mail

`mailcellar export` copies a selection of the backup out of mailcellar, to a
file you can open elsewhere. Use it to read old mail in a desktop mail client,
to hand mail to an accountant or a lawyer, or to keep a second copy of
offloaded mail. It changes nothing in Gmail or the backup, and needs no Gmail
sign-in. The README has the reference
([Exporting mail](../reference/export.md)).

## Which format

The format comes from the file name, which must end `.mbox` or `.zip`.

| | `.mbox` | `.zip` |
| --- | --- | --- |
| Good for | reading the mail in a mail client | handing it to a person; a spreadsheet of what is in it |
| Holds | all the messages in one file, each with its Gmail labels | one `.eml` file per message, and `index.csv` |
| Opens with | a mail client's import | any unzip tool, then a mail client or a text editor for the `.eml` files; a spreadsheet for the index |

Both hold the messages exactly as the backup stores them, attachments
included.

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

The file is written under another name and renamed only when complete, so an
interrupted export leaves nothing that looks finished. It will not overwrite a
file, so choose a new name each time, and it will not write inside the backup.

When it finishes it says how many messages it wrote and their size.

**In a mail client.** Most clients can import an mbox file; look for an import
option for "mbox" or "Unix mailbox" files. Each message carries an
`X-Gmail-Labels` header with its labels, which some clients show and most
ignore. This has not been tried in every client, so check a small export
first (`--limit 20`) before exporting a lot.

**The zip's index.** `index.csv` has one row per message: date (UTC), sender,
subject, labels (separated by `;`), Gmail ID and the file's path inside the
zip. A subject or sender that begins with `=`, `+`, `-` or `@` is written with
a `'` in front. Mail is written by other people, and without the `'` a
spreadsheet could run such a subject as a formula. If you see a `'`, that is
why.

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
