---
description: "Reference for mailcellar backup, count, meta and verify: incremental Gmail and IMAP backups, comparing a mailbox with its backup, and checking its integrity."
---

# Backups: backup, count, meta, verify

```bash
mailcellar backup                  # every account, incremental
mailcellar backup personal         # one account
mailcellar count                   # compare each mailbox with its backup
mailcellar backup personal --full  # list every message instead of using history
mailcellar backup --unattended     # for scheduled runs: never opens a browser
mailcellar backup personal --verify 0   # without checking the backup's files afterwards
mailcellar meta                    # fill meta.sqlite for older mail
mailcellar verify                  # are the backup's files still intact?
```

**Exit status** (for scripts and schedulers): 0 when everything worked.
Otherwise 1, in any of these cases:

- an account failed (with several accounts, the others still run);
- downloads failed (the next run fetches them), or the check after the
  backup found a damaged file;
- `count` found mail missing from the backup;
- `verify` found a problem.

`restore` exits 1 when any message was skipped or failed.

**Checking the backup.** Once mail is offloaded and the mailbox has emptied
its Trash, the backup holds the only copy, so `verify` looks for damage that
would otherwise go unnoticed: a failing disk, a sync tool rewriting files,
an accidental delete. It needs no access to the mailbox and changes nothing (both
databases are opened read-only).

Each `backup` also checks part of the backup, as `verify --chain` does
file by file: an eighth of its messages by default, at least 1,000,
starting where the last backup's check stopped, so a weekly backup has
checked every message after eight runs, two months. A damaged or
changed file makes `backup` exit 1, and `report health` lists each check
as a `verify` run, with the problems it found as Failed. `--verify N`
checks N instead, and `--verify 0` none. Only the full `verify` also
checks that the two databases agree, and only `verify --chain` checks
every file in one run.

```bash
mailcellar verify                  # every backup, every message
mailcellar verify work --offloaded # only offloaded mail: the copies with no other source
mailcellar verify --sample 500     # 500 random messages: quick enough for a routine check
```

For each message it checks that the `.eml` exists and parses, and that its
content hash matches the one `meta.sqlite` recorded. The content hash covers
what a reader sees, the visible text and attachments, and leaves out headers,
links and addresses (it was made for matching duplicates), so plain `verify`
catches damage to a message's content but not every changed byte;
`verify --chain` checks the bytes. It also checks that
`msg-db.sqlite` and `meta.sqlite` agree on which messages exist, and, with
`--offloaded`, that every offloaded message is in the backup. Problems are
listed (`--top N` sets how many) and the exit status is 1 if there are any.
A message with no current `meta.sqlite` row has nothing to compare with: it
is counted as unchecked, with a warning, and `mailcellar meta` fills it in;
the check after each backup and `verify --chain` still compare its bytes.
A message with no visible text or attachments (a body that is only a link,
images or nothing) has no content hash, by design; it is counted apart, as
having nothing to compare, and `verify --chain` checks its bytes instead.
A full run parses every message, so it takes a while on a large backup;
progress goes to stderr.

Backups live in `<data root>/<email>/`, in the format of
[gyb](https://github.com/GAM-team/got-your-back), so other tools can read
them too. [`mailcellar restore`](restore.md) puts mail back into its mailbox.

## Options

Accounts are named by address, config name or a unique part of an address;
with none named, every account in `accounts.toml`.

### `backup`

| Option | What it does |
| --- | --- |
| `--full` | List every message instead of reading Gmail's history since the last run (an IMAP account reads every folder in full, not only what changed since the last backup) |
| `--unattended` | For scheduled runs: never open a browser. A token that needs signing in fails that account's run, with the command to fix it |
| `--refresh-labels` | Also re-sync the labels of messages already backed up. Gmail lists them label by label, 500 messages per call, so about a minute for 50,000 messages. An IMAP account's labels are refreshed on every run; with this option it reads every folder, as with `--full` |
| `--search QUERY` | Only consider messages matching this Gmail search (Gmail accounts only, through the API or over IMAP) |
| `--verify N` | Then check the files of the next N messages, from where the last backup's check stopped (default: an eighth of the backup, at least 1,000; `0`: none) |
| `--folder FOLDER` | Backup folder (default `<data root>/<email>`) |
| `--config CONFIG` | Config folder (default `~/.config/mailcellar`, or `$MAILCELLAR_CONFIG`) |

An account that has been backed up before (its hash chain anchor is in the
config folder) but has no backup in the folder is refused, and nothing is
created there: most likely the data root is on a volume that isn't
mounted. To start a new backup in that place on purpose, move
`<config>/anchors/<email>.json` aside first.

### `count`

Takes `--folder` and `--config`, as `backup` does.

### `meta`

| Option | What it does |
| --- | --- |
| `--offline` | Only parse the `.eml` files; don't ask the mailbox for thread IDs, labels and sizes |
| `--folder FOLDER`, `--config CONFIG` | As for `backup` |

### `verify`

| Option | What it does |
| --- | --- |
| `--offloaded` | Only offloaded mail, the copies with no other source |
| `--sample N` | Check N messages chosen at random (of the offloaded ones, with `--offloaded`) |
| `--chain` | Also check the backup against its hash chain and the anchor in the config folder (not with `--sample` or `--offloaded`) |
| `--top N` | Problems listed per account (default 50) |

`verify` takes account names and finds the backups in the data root.

See [The hash chain](#the-hash-chain) for what `--chain` adds.

## The hash chain

`verify` alone compares each `.eml` with the content hash in
`meta.sqlite`, which sits beside it in the data root: whatever can change
one file can change the other to agree. The hash chain is tamper
evidence: a record of what the backup should hold that a change in the
data root can't rewrite unnoticed.

**What it records.** Each backup folder has a `chain.jsonl`, with a line,
or link, for each change: the messages `backup`, `restore` or `forget`
(with its `--undo` and `--empty`) added to or removed from the backup,
with the SHA-256 of each `.eml`. `hold` and `hold release` add a link
with the hold's details. Each link names the one before it by its hash,
so none can be edited, removed or reordered without breaking the rest.
The first `backup` starts the chain from the files the backup already
holds, taking them as they are. Labels aren't recorded: they change all
the time and aren't the mail.

**The anchor.** The latest link's number and hash are also kept outside
the data root, in the config folder (`anchors/<email>.json`). A chain
rewritten to match a changed file is whole in itself, but it no longer
ends at the link the anchor names.

**What protects what.** The chain shows changes made by anything that
can reach the data root but not the config folder: a sync tool, a disk
fault, an edit by hand, or a program with access to that disk only.
Something running as you can rewrite the chain and the anchor together;
against that, note the head hash `report health` shows somewhere else
now and then, and compare.

**What each check sees:**

| Check | Runs | Compares | Catches |
| --- | --- | --- | --- |
| After each `backup` | Every run, a slice of the backup (`--verify N`) | Files with `meta.sqlite` and with the chain | Damage, and any file changed or added outside those commands, a slice at a time |
| `verify` | When you run it | Every file with `meta.sqlite`, and the two databases | Damage, and databases out of step |
| `verify --chain` | When you run it | Every file with the chain, and the chain with the anchor | Any file changed, missing or added outside those commands, and a rewritten chain |
| `report health` | When you run it | The chain with the anchor, reading no file | A rewritten or edited chain |
| `restore` | Each message it uploads | The copy with the chain | A changed copy, which it doesn't upload |
| A command that adds a link | Every time | The chain's end with the anchor | A rewritten chain, which it refuses |
| `schedule status` | When you run it | The chain's end with the anchor | A backup the next run will refuse |

`verify --chain` fails on a file changed since it was recorded, a
recorded message missing from the backup, a message in the backup no
link added, a chain that was edited, or an anchor that names another
link. It warns, without failing, when there is no anchor (a copy of the
backup on another machine) or the anchor is behind the chain.

**When a command refuses the chain.** A command that would add a link
(`backup`, `restore`, `forget`, `hold`) refuses a chain that doesn't end
at the link its anchor names, and changes nothing: the chain was
rewritten, cut short or added to, or the backup was put back from an
older copy. Anchoring it would make it check clean from then on, so
there is no option to override this. Run `verify --chain` to see what
differs:

- **Only the anchor differs, and you put the backup back from an older
  copy** (Time Machine, restic, a second disk): move
  `<config>/anchors/<email>.json` aside and keep it, as the record of
  how far the chain had got. The next link a command adds anchors the
  copy's chain. [Recovering](../guide/recovery.md#a-damaged-or-lost-data-root)
  has the steps.
- **Files differ too, or you didn't put anything back:** something
  changed the backup. Leave the anchor where it is, and compare the
  backup with a copy you trust before running anything that changes it.
