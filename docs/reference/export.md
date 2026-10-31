---
description: "Reference for mailcellar export: every option for copying mail from the backup to an mbox file or a zip of .eml files."
---

# Exporting mail: export

Sometimes mail needs to go somewhere other than back into its mailbox: to an
accountant or a lawyer, or into a desktop mail client to read a year of
offloaded mail. `export` copies a selection out of the backup. It is
read-only (nothing changes in the mailbox or the backup) and needs no access
to the mailbox.

```bash
mailcellar export personal --offloaded --to ~/old-mail.mbox      # for a mail client
mailcellar export personal --all --after 2025-01-01 --before 2026-01-01 \
    --sender @example.com --to ~/for-the-accountant.zip                 # the .eml files and an index
```

The selection is `restore`'s: `--batch`, `--rule`, `--id`, `--offloaded` or
`--all`, narrowed with `--before`, `--after`, `--has-label` and `--sender`
(see [Restoring mail](restore.md)), and `--limit` takes the oldest first.
`--hold NAME` selects the mail a [legal hold](hold.md) keeps, for handing
over; `--id` then narrows it.
Mail that has been restored is included, once.

- **`.mbox`** holds one message after another, each preceded by an
  `X-Gmail-Labels` header with its labels (comma-separated, as Google Takeout
  writes it): yours first, then Gmail's system labels by Takeout's English
  names (`Inbox`, `Important`, `Category Updates`), so a reader that keeps
  one label as the folder keeps yours. The usual mboxrd form: a line
  starting with `From ` is written with a `>` before it. Messages are
  otherwise as stored. `index.csv` names labels the same way.
- **`.zip`** holds the `.eml` files under their paths in the backup
  (`<year>/<month>/<day>/<id>.eml`) and `index.csv` with each message's date
  (UTC), sender, subject, labels, ID (column `id`, as in plans, apply logs
  and manifests; the restore log calls it `source_id`) and file. A cell
  that begins with
  `=`, `+`, `-` or `@` gets a leading `'`, so a spreadsheet doesn't run a
  subject as a formula.

The file is written under another name and renamed when it is complete, so an
interrupted export leaves nothing that looks finished. It refuses a file that
already exists, and a place inside the backup. A message whose `.eml` file is
missing from the backup is left out and reported, and the exit status is 1:
`mailcellar verify` shows what is missing.

An export is a copy of your mail, outside the backup: keep it, and delete it,
with the same care. The [Exporting mail](../guide/exporting.md) guide has
worked selections and what to do with the result.
