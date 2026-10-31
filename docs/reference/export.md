---
description: "Reference for mailcellar export: every option for copying mail from the backup to an mbox file, a maildir or a zip of .eml files, or just its attachments."
---

# Exporting mail: export

Sometimes mail needs to go somewhere other than back into its mailbox: to an
accountant or a lawyer, or into a desktop mail client to read a year of
offloaded mail. `export` copies a selection out of the backup. It is
read-only (nothing changes in the mailbox or the backup) and needs no access
to the mailbox.

```bash
mailcellar export personal --offloaded --to ~/old-mail.mbox      # for a mail client
mailcellar export personal --offloaded --to ~/old-mail/          # a maildir, for mutt, aerc, mu
mailcellar export personal --all --after 2025-01-01 --before 2026-01-01 \
    --sender @example.com --to ~/for-the-accountant.zip                 # the .eml files and an index
mailcellar export personal --all --after 2020-01-01 --sender @example.com \
    --attachments --type pdf --to ~/invoices.zip                       # just the attached PDFs
```

The selection is `restore`'s: `--batch`, `--rule`, `--id`, `--offloaded` or
`--all`, narrowed with `--before`, `--after`, `--has-label` and `--sender`
(see [Restoring mail](restore.md)), and `--limit` takes the oldest first.
`--hold NAME` selects the mail a [legal hold](hold.md) keeps, for handing
over; `--id` then narrows it.
Mail that has been restored is included, once.

- **`.mbox`** holds one message after another, each preceded by an
  `X-Gmail-Labels` header with its labels (comma-separated, as Google Takeout
  writes it, a label with a comma or a quote in it in double quotes): yours
  first, then Gmail's system labels by Takeout's English names (`Inbox`,
  `Important`, `Category Updates`), so a reader that keeps one label as the
  folder keeps yours. The usual mboxrd form: a line
  starting with `From ` is written with a `>` before it. Messages are
  otherwise as stored. `index.csv` names labels the same way.
- **A name ending `/`** is a maildir: a folder with `cur/`, `new/` and
  `tmp/`, every message in `cur/` with LF line endings, and `index.csv` at
  the top. Each message carries `X-Gmail-Labels`, as in an mbox, and
  `X-Keywords`, as OfflineIMAP writes it: comma-separated, unquoted (so a
  label with a comma in it reads as two there), sorted, Gmail's system
  labels by their IMAP names (`\Inbox`, `\Important`, `\Starred`) and
  labels IMAP lacks (categories, unread) left out. The file name's flags
  say draft (`D`), starred (`F`) and read (`S`).
- **`.zip`** holds the `.eml` files under their paths in the backup
  (`<year>/<month>/<day>/<id>.eml`) and `index.csv` with each message's date
  (UTC), sender, subject, labels, ID (column `id`, as in plans, apply logs
  and manifests; the restore log calls it `source_id`) and file. A cell
  that begins with
  `=`, `+`, `-` or `@` gets a leading `'`, so a spreadsheet doesn't run a
  subject as a formula.

**`--attachments`** writes the attached files themselves rather than the
messages, to a `.zip` or a folder (a name ending `/`), with `index.csv`:

- Each file is named `<date> <sender's address> <its filename>`, made safe
  for Windows, macOS and Linux and cut to 150 bytes with its extension
  kept. A part with no filename is named `attachment-<n>` with an
  extension from its type; a name taken already gets ` (2)` (so it can
  be a few bytes longer).
- Only parts sent as attachments are taken. **`--inline`** adds the inline
  ones too: images shown in the text, logos, signatures.
- **`--type TYPE`** keeps only parts of that type, by MIME type
  (`application/pdf`), subtype (`pdf`) or the filename's extension; it can
  be repeated.
- A file with the same content as one already written (the same PDF sent
  twice) is written once.
- `index.csv` has a row for every message and attachment, so a file sent
  twice has two: date (UTC), sender, subject, ID, the attachment's
  filename, its type and size, and the file it is in. Cells are guarded
  against spreadsheet formulas as in a `.zip`.

The file or folder is written under another name (ending `.part`) and
renamed when it is complete, so an interrupted export leaves nothing that
looks finished. It refuses a file or folder that already exists, a
`.part` file or folder of the same name (one an interrupted export left,
or one of yours), and a place inside the backup. A message whose `.eml`
file is missing from the backup is left out and reported, and the exit status is 1:
`mailcellar verify` shows what is missing.

An export is a copy of your mail, outside the backup: keep it, and delete it,
with the same care. The [Exporting mail](../guide/exporting.md) guide has
worked selections and what to do with the result.
