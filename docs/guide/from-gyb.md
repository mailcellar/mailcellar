# Migrating from gyb

mailcellar writes [Got Your Back](https://github.com/GAM-team/got-your-back)
(gyb)'s on-disk format byte for byte, so a gyb backup is continued, not
started again: mailcellar adds to the folder you have. Nothing is converted
and nothing is downloaded twice.

This page assumes mailcellar is installed and the account has an OAuth
client (see [Setting up Google Cloud](google-cloud.md)).

## What you need to know first

- **The folder is used in place.** gyb's `msg-db.sqlite` and the
  `<year>/<month>/<day>/<id>.eml` files stay as they are. mailcellar adds
  `meta.sqlite` beside them, which gyb ignores.
- **Same format, so gyb still reads it.** You can go back to gyb at any
  time. Do not run both on the same folder at once.
- **Only gyb's current format (`db_version` 6) is accepted.** A folder
  made by an older gyb is refused with a message naming the version.
- **One account per folder.** mailcellar checks the address in the folder's
  `msg-db.sqlite` against the account you name, and refuses a mismatch.

## 1. Copy the folder into the data root

gyb names its folder `GYB-GMail-Backup-<email>`. mailcellar looks for
`<data root>/<email>/`, with the plain address as the name, so the folder is
renamed on the way in. `mailcellar config` shows where the data root is.

```bash
mailcellar config        # the data root, and each account's state
cp -a /path/to/GYB-GMail-Backup-alex@example.com \
      "<data root>/alex@example.com"
```

Copy first, rather than move, and keep the original until `count` (step 4)
agrees with Gmail. A move works the same way once you trust the result.

If you would rather leave the folder where it is, `backup --folder PATH`
uses it for one run, but only folders inside the data root are seen by
`dedup`, `offload`, `restore` and the reports.

## 2. Configure the account

gyb's `<email>.cfg` and credential files are **not read**. mailcellar signs
in with an OAuth client of the same kind gyb uses, but stores its own
tokens in its own config folder. List the account in
`~/.config/mailcellar/accounts.toml` and put the client where
[the README](../reference/configuration.md) says:

```toml
personal = "alex@example.com"
```

```
~/.config/mailcellar/personal/client_secrets.json
```

You can reuse the client you made for gyb, if you still have its JSON, or
make a new one. The first run opens a browser for consent; see step 6 of
the Google Cloud guide. Your gyb files can stay where they are, or be
deleted once you have stopped using gyb.

## 3. The first run

```bash
mailcellar backup personal
mailcellar meta personal
```

**`backup`** has no history to start from on its first run in the folder
(that is stored in `meta.sqlite`, which is new), so it lists every message
in Gmail and downloads those the folder does not already have, judged by
Gmail ID and the file being present. The listing is the slow part on a large
mailbox; the downloads are only the mail since gyb's last run. From then on
it reads Gmail's history instead, as it does for any other account.

**`meta`** fills `meta.sqlite` for the mail gyb backed up: it parses each
`.eml` once, then asks Gmail for thread IDs, label IDs and sizes. `dedup`,
`offload` and the reports read that file rather than the messages. Mail
that has gone from Gmail keeps no thread or label details; that is
reported, not an error. Run `meta` after the first `backup` so one pass
covers old and new mail; the order does not change the result.

If you will use `dedup`, run `mailcellar dedup index` afterwards, so it
knows the folder.

## 4. Check

```bash
mailcellar count personal
```

`count` compares Gmail with the folder and changes nothing. Mail that
arrived since the last `backup` is reported as missing until the next
`backup`. Anything still missing after a fresh `backup` is worth a look
before you delete the original gyb folder.

## What stays the same

- The folder layout, the database schema and the `.eml` files.
- Chats (the `CHAT` and `CHATS` labels) are skipped, as in gyb.
- Labels are stored by name in `msg-db.sqlite`, as gyb does.

## What differs

- **Nothing is removed unless you ask.** Mail permanently deleted from Gmail
  stays in the backup, and `backup` says how many such messages there are.
  `backup --prune` moves them into `<data root>/_pruned/`, after asking, and
  never touches mail you have offloaded.
- **Restore.** `mailcellar restore` replaces `gyb --action restore`. It only
  adds, skips what Gmail already has, and takes a batch, rule, ID or
  everything; see [Restoring mail](../reference/restore.md). A
  restored message is indexed in the folder under its new Gmail ID, so gyb's
  own view of the folder stays valid.
- **Per-message data.** Parsed fields, attachments and recipients live in
  `meta.sqlite`; gyb has nowhere for them and never reads it.
- **One command for every account.** `mailcellar backup` with no account
  runs them all; failures in one do not stop the others.
