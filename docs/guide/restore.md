# Restoring mail: putting it back into Gmail

`restore` puts mail from the backup back into Gmail: mail you offloaded,
mail that was lost, a whole backup into another account, or mail from a
Google Takeout file or an older backup folder. It only adds. It deletes
nothing in Gmail or in the backup, and it skips what Gmail already has, so
running it twice is safe.

This page covers the cases people actually have. Every option is in the
[reference](../reference/restore.md); the mail and addresses below are
invented.

| You want to | Use |
| --- | --- |
| Undo an offload, or part of one | `--batch` or `--rule`, then narrow it |
| Get one message back | `--id`, usually from `search` |
| Put back everything offloaded | `--offloaded` |
| Rebuild an account that lost its mail | `--all` |
| Copy an account's mail into another | `--all --to ACCOUNT` |
| Recover mail `backup --prune` moved out | `--from-pruned` |
| Load a Takeout export or an old gyb folder | `--from-mbox`, `--from-folder` |

Restoring needs modify access to the account the mail goes into. A
restore with `--yes` asks you to sign in in the browser when it starts,
and keeps that access only until it ends.

## The pattern: dry run, small, then the rest

Every restore starts with a dry run, which is what you get without `--yes`:

```bash
mailcellar restore personal --batch 2026-01-15-093000
```

It checks each `.eml` file in the backup, looks each message up in Gmail,
and says how many messages would come back and how: out of Trash,
re-recorded, or uploaded. On a large backup that takes minutes. Read the
counts; if they aren't what you expected, stop and look at the selection.

Then do a few:

```bash
mailcellar restore personal --batch 2026-01-15-093000 --limit 20 --yes
```

Open Gmail and look at them. Restored messages carry the label
`restored/<date>` (`--label NAME` to choose another), so
`label:restored` finds everything a restore brought back. When they look
right, run without `--limit`. `--limit` takes the oldest messages first, and
the next run carries on where the last one stopped.

Messages still in Trash come back quickly, a thousand per Gmail call.
Uploads are slower, about six a second, so 4,500 messages take about 13
minutes and 50,000 over two hours. Run large restores in the background; progress
lines go to stderr (`-q` turns them off).

## Undo an offload

Offloads are stored in batches. `mailcellar report storage` lists them with
how much each freed, and the folders in `<data root>/offload/batches/` are
the same names.

```bash
mailcellar restore work --batch 2026-01-15-093000            # dry run
mailcellar restore work --rule "old build notifications" --yes
```

`--rule` selects by the rule's name. Within 30 days of the
offload the messages are still in Gmail's Trash, and restore moves them out
with their IDs, labels and threads intact. After that Gmail has emptied the
Trash and restore uploads them from the backup, with the labels they had
(apart from the ones Gmail won't accept: Trash, Spam and chats).

To restore only part of a batch, narrow it by date, sender or label. The
options and their rules are in the
[reference](../reference/restore.md#narrowing-a-selection):

```bash
mailcellar restore work --offloaded --sender @ci.example.com --after 2022-01-01
```

## Get one message back

Gmail can't search offloaded mail, but `search` can. It prints a ready-made
`restore` command for offloaded results:

```bash
mailcellar search "quarterly figures" --offloaded
mailcellar restore work --id 18c0ffee0000abcd --yes
```

If you only want to read it, you don't need to restore. The `.eml` file
`search` shows opens in any mail client, and
[Exporting mail](exporting.md) copies a selection out as an mbox.

## Rebuild an account, or copy one into another

```bash
mailcellar restore personal --all                      # dry run: the whole backup
mailcellar restore work --all --to personal --label-prefix work
```

`--all` considers the whole backup. Messages Gmail already has are left
alone, so after a lost mailbox it uploads only what is missing.

With `--to`, the mail goes into a different account, which needs modify
access too. Each message is looked up there by Message-ID first, so nothing
is duplicated. Two options keep it tidy:

- `--label-prefix work` nests the backed-up user labels under `work/`, so
  the copy doesn't mix with the other account's labels;
- `--strip-labels` restores no labels at all: the messages arrive archived,
  with only the `restored/` label.

The new messages are indexed in the target account's backup too, so the next
`backup` doesn't download them again.

## Mail from somewhere that isn't in the index

Three sources hold mail that no backup lists. Each restores every message it
holds, narrowed by the same options, with the same dry run and `--yes`.

**Pruned mail** is what `backup --prune` moved out of the backup
([Backing up](backing-up.md#mail-deleted-from-gmail-prune)). The batch name is
the folder under `<data root>/_pruned/<email>/`:

```bash
mailcellar restore work --from-pruned 2026-03-01-120000
```

**A Takeout export or an mbox file**, including one `mailcellar export`
wrote:

```bash
mailcellar restore personal --from-mbox ~/Takeout/Mail/All.mbox
```

Labels come from each message's `X-Gmail-Labels` header, with Takeout's
display names mapped to Gmail's own, in any of the 73 languages Gmail's
interface uses ("Posteingang" is the inbox). **A gyb backup folder from elsewhere**,
such as an old machine, is read but never changed:

```bash
mailcellar restore personal --from-folder /Volumes/old/GYB-GMail-Backup-old@example.org
```

None of these has Gmail IDs that mean anything in the account the mail goes
into, so each message is looked up there by Message-ID and content: recorded
if Gmail has it, uploaded if not. Nothing is added to a backup's index by
these three. Run `backup` afterwards and it downloads the restored mail as
new messages. Running the same restore again finds what it restored by
Message-ID and skips it.

## What to expect

- **Some messages are skipped.** A message whose Message-ID Gmail already
  holds with *different* content is skipped, since Gmail would match the
  upload to the existing message instead of storing this one. A backup copy
  that doesn't parse, or no longer matches its recorded hash, is skipped too;
  `mailcellar verify` lists those. The run exits 1 when anything was skipped
  or failed.
- **Dates are kept.** An uploaded message keeps its original date, and Gmail
  threads it by its headers, so threads may not look exactly as they did.
- **Everything is logged:** `<data root>/restore/restore-log.csv`, one row per
  message.

## Undoing a restore

Restore adds mail, so undoing it means removing what it added. In Gmail,
search `label:restored/2026-10-26` (the date of the run), check what comes
up, and delete it. Nothing in the backup needs to change.

## Check

```bash
mailcellar backup work      # picks up the restored messages
mailcellar count work       # Gmail against the backup
```

For an offload you restored fully, `report storage` shows how much was
restored since.
