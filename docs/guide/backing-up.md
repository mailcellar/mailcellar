# Backing up, and keeping the backup safe

mailcellar's backup is a copy of Gmail on your disk. It is what lets you
take old mail out of Gmail, so it matters more than any other part of the
setup. This page covers how to run it, how to check it, and how to keep a
second copy of it. The README has the reference for each command
([Backups](../reference/backup.md)).

## What the backup does, and what it never does

- **It only adds.** `backup`, `count` and `meta` never delete anything from
  the backup. Mail you delete from Gmail stays in the backup, and
  `backup` says how many such messages there are.
- **It can move, never delete, with `--prune`.** `backup --prune` moves mail
  that is permanently gone from Gmail out of the backup into
  `<data root>/_pruned/<email>/<batch>/`. It asks first, refuses to move
  more than a tenth of the backup unless you add `--force`, and never
  touches mail you have offloaded. The files move; nothing is erased.
- **It reads Gmail, never changes it.** A backup needs read-only access.

Once you offload mail and Gmail empties its Trash (30 days later), the
backup holds the only copy of it. Everything below is about not losing that.

## The routine

A backup you set up once and check now and then:

| When | Command | What it tells you |
| --- | --- | --- |
| Once | `mailcellar schedule install` | a weekly unattended backup |
| After the first backup | `mailcellar count` | Gmail's message count against the backup's |
| Every month or so | `mailcellar report health` | the last run of each account, gaps, tokens that need signing in |
| Every month or so | `mailcellar verify --sample 500` | 500 random files are intact |
| Before and after an offload | `mailcellar verify --offloaded` | the copies with no other source are intact |

`backup` catches up from Gmail's change history, which Gmail keeps for about
four weeks. A longer gap still works but lists the whole mailbox again, so
schedule it: see [Scheduled backups](scheduling.md).

`count` compares what Gmail has with what the backup has. Mail missing from
the backup makes it exit 1; run `backup` again and it fetches what failed.
Chats are skipped, so they never show as missing
([Troubleshooting](troubleshooting.md)).

`verify` checks the backup against itself: every message has its `.eml`
file, the file parses, and its content hash matches the one recorded in
`meta.sqlite`. It needs no Gmail access and changes nothing. It is what
catches a failing disk, a sync tool that rewrote files, or an accidental
delete. Run the full `mailcellar verify` occasionally: it parses every
message, so on a large backup it takes a while.

## The second copy

The backup is on one disk. Keep a second copy of the **data root**, the
folder `mailcellar config` shows. It holds:

- one folder per account, with the `.eml` files and the two databases,
  `msg-db.sqlite` and `meta.sqlite`;
- `offload/` (rules, plans and the batch manifests), `restore/`, `_pruned/`
  and `logs/`.

`meta.sqlite` records which mail was offloaded and which was restored, and
nothing rebuilds that from the `.eml` files. Copy the whole folder, not just
the mail.

Any of these works:

- **Time Machine** on a Mac, with the data root included. `offload apply
  --trash` checks that it has run since the last backup and tells you if it
  has not.
- **A copy to another disk** with a tool that preserves the folder as it is,
  such as `rsync -a`.
- **A sync or cloud-backup folder** you trust.

Things to watch:

- **Copy while nothing is running.** The databases are written during a
  `backup`, `offload apply` or `restore`. A file copied half-way through a
  write can be inconsistent. Run the copy after the scheduled backup has
  finished, not during it.
- **Do not point a two-way sync tool at the live data root.** A tool that
  rewrites files or resolves conflicts can damage the databases.
  One-directional copies are safe; so are snapshots.
- **Keep the copy somewhere a single failure cannot take with the original**:
  another disk at least, another place if you can.
- **Do not copy the config folder to a shared place.** It holds OAuth
  tokens. The data root does not.

### Checking the copy

A copy you have never opened is a hope. To check one, point mailcellar at it
and verify:

```bash
MAILCELLAR_DATA=/Volumes/Spare/mailcellar mailcellar verify --offloaded
MAILCELLAR_DATA=/Volumes/Spare/mailcellar mailcellar verify --sample 500
```

`verify` reads the backup in that folder and changes nothing in it. Do this
once after setting the copy up, and now and then after.

### If you lose the data root

Restore the whole folder from the copy, then run `backup`: it catches up
with what Gmail gained since. See
[Recovering](recovery.md#a-damaged-or-lost-data-root). Mail offloaded after
the copy was made is the exception, and that page says what to do about it.

An `export` is a different kind of second copy: a standard mbox or zip
that any mail client can read, without mailcellar
([Exporting mail](exporting.md)). It is useful for what you have offloaded,
but it does not replace a copy of the data root, which can be restored.

## Mail deleted from Gmail: `--prune`

Mail you delete in Gmail and then empty from Trash is permanently gone from
Gmail but stays in the backup, so you can undo a mistake. `backup` counts
those messages. To tidy them out of the backup:

```bash
mailcellar backup work --prune
```

It lists how many messages it would move and asks. It stops at 10% of the
backup unless you add `--force`, because a large number usually means
something else is wrong: the wrong account, or an old copy of the data root
that does not know about your offloads.

The moved mail is in `<data root>/_pruned/<email>/<batch>/`, with a
`pruned.csv` of its labels. To put it back into Gmail:

```bash
mailcellar restore work --from-pruned 2026-03-01-120000        # dry run
mailcellar restore work --from-pruned 2026-03-01-120000 --yes
```

Most people never need `--prune`. Mail stays in the backup if you leave it
alone, at the cost of disk space.

## Adding an account, or moving the backup

- **Another account:** `mailcellar setup` ([Setting up Google Cloud](google-cloud.md)),
  then `mailcellar backup ACCOUNT`. The first backup downloads everything;
  if it stops, run it again and it carries on.
- **Another disk:** quit anything running, copy the data root, set `data_dir`
  in `settings.toml` to the new place, run `mailcellar config` to check that
  each account's state looks right, then `mailcellar verify --sample 500`.
  Keep the old copy until that passes.
- **From gyb:** see [Migrating from gyb](from-gyb.md).
