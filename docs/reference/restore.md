# Restoring mail: restore

`restore` puts backed-up mail back into its mailbox, Gmail or IMAP:
offloaded messages, or a whole backup, into its own account or another
configured one. Without `--yes` it is a dry run, which checks the backup
and the mailbox and says what it would do. IMAP accounts restore the same
way, and mail goes between
Gmail and IMAP accounts with `--to`, labels and folders mapped by the
target; see [Backing up an IMAP account](../guide/imap.md#4-restore).

```bash
mailcellar restore personal --batch 2026-01-15-093000   # dry run of one offload batch
mailcellar restore personal --batch 2026-01-15-093000 --yes
mailcellar restore personal --rule "old work mail" --limit 100 --yes
mailcellar restore personal --id 18c0ffee0000abcd --yes
mailcellar restore personal --offloaded                 # everything offloaded
mailcellar restore personal --all                       # the whole backup
mailcellar restore work --all --to personal --label-prefix work   # into another account
```

Batch names are the folders in `<data root>/offload/batches/`. `--limit`
takes the oldest messages first, and repeated runs carry on where the
last one stopped.

**Narrowing a selection.** Any of the selections above can be narrowed, so
"last year's mail from one sender" needs no ID lookup first:
{: #narrowing-a-selection }

```bash
mailcellar restore personal --all --after 2025-01-01 --before 2026-01-01 --sender @example.com
mailcellar restore personal --offloaded --has-label projects --sender alex@example.org
```

- `--before DATE` keeps mail older than the date, `--after DATE` mail from
  that date on. A date is `YYYY-MM-DD` (UTC) or a number of years ago.
- `--has-label NAME` keeps mail with that user label, by name, ignoring case
  (system labels such as `INBOX` don't count). It is not `--label`, which
  names the label that restored mail gets.
- `--sender` keeps mail from an address, or from any address at a `@domain`,
  ignoring case; it goes by the From address, so a relayed message counts as
  from its relay. `--has-label` and `--sender` can be repeated: mail matching
  any one of the values stays.

Everything given has to hold at once, and none of them is a selection by
itself: name `--all`, `--offloaded`, `--batch`, `--rule` or `--id` too.

**How each message comes back** (the first that applies):
- **still in Gmail's Trash** (within 30 days of the offload): moved out,
  keeping its ID, labels and thread;
- **offloaded, but already out of Trash** (moved by hand): recorded and
  relabelled;
- **never offloaded and still in Gmail** (with `--all` or `--id`): left
  alone;
- **gone, but Gmail has the same Message-ID and content** under another
  ID: recorded as that message;
- **gone:** uploaded from the backup with its labels, apart from those
  Gmail won't take (Trash, Spam, chat, superstars) and the offload and
  dedup labels. The upload keeps the original date; Gmail threads it by
  its headers.

Mail deleted from the backup by `offload forget` can't be restored; a
selection that includes some says so.

Restored messages lose the offload label and gain `restored/<date>`
(`--label NAME` to choose another), so a restore can be found and undone.
A user label whose name Gmail reserves (such as `Muted`, `Archive` or
`Bin`) is created as `_Muted` and so on, as gyb does; a label the account
already has is used as it is. A message with no Date header, or one Gmail
can't read, is given one from the date the backup recorded, since Gmail
would otherwise date it at the moment of the restore; the backup's file is
left as it is, and the log says so (not for `--from-mbox`, which records
no date). Before uploading, each backup copy is checked against the SHA-256 its
hash chain recorded (for pruned mail, the one the prune recorded), so a
copy changed or damaged since it was backed up, headers and links
included, is skipped and logged; a backup from before the chain is checked
by its content hash only. The backup indexes each new Gmail ID, sharing the existing `.eml`, so the
next backup doesn't download it again. A message whose Message-ID Gmail
already holds with *other* content is skipped: Gmail would match the
upload to that message instead of storing this one. A message with no
Message-ID is uploaded again after an error only if Gmail refused it for
its rate limit: after a server error or a timeout Gmail may have stored
it, and a second upload would store a second copy. Such a message is
reported as failed, "it may have been imported"; look for it in Gmail
before running the restore again, which would upload it again.

**Into another account** (`--to`): every message is looked up by
Message-ID there, and new IDs go into that account's backup. It needs
modify access to that account (`mailcellar auth ACCOUNT --scope
modify`). `--label-prefix P` nests the backed-up user labels under `P/`;
`--strip-labels` restores none, so the messages arrive archived with only
the `restored/` label.

**How long it takes:** the dry run checks every `.eml` file and looks each
message up in Gmail, so on a large backup it takes minutes. Messages still
in Trash come out a thousand per Gmail call, then each is read back to
confirm it left Trash, so thousands take about a minute. Uploads run at
about 6 a second, so the dry run's estimate for 4,500 messages is about 13
minutes and for 50,000 over two hours. Progress lines go to stderr (`-q` turns them off); run
large restores in the background.

Runs are logged in `<data root>/restore/restore-log.csv`, a row per message
as it is done, so an interrupted run's log shows how far it got; each
restored message is also recorded in its backup's `meta.sqlite`. A message
restored whose marks then went wrong (its offload mark left on, the
restore label not added) gets a second row with the note; its last row
is the one that counts. How it works:
[docs/architecture.md](../architecture.md#restore).

**From elsewhere.** Three other sources hold mail that isn't in a backup's
index. Each restores every message it holds, narrowed by `--id`, `--before`,
`--after`, `--has-label`, `--sender` and `--limit`, with the same dry run
and `--yes`:

```bash
mailcellar restore personal --from-pruned 2026-03-01-120000   # what backup --prune moved out
mailcellar restore personal --from-mbox ~/Takeout/Mail/All.mbox --yes
mailcellar restore personal --from-folder /Volumes/old/GYB-GMail-Backup-old@example.org
```

- `--from-pruned BATCH`: mail that `backup --prune` moved into
  `<data root>/_pruned/<email>/BATCH/`, with the labels in its `pruned.csv`.
  It goes back into that account, or another with `--to`.
- `--from-mbox FILE`: a Google Takeout export, or a file `mailcellar export`
  wrote, into ACCOUNT. Labels come from each message's `X-Gmail-Labels`
  header (Takeout's names such as `Inbox` or `Category Updates` become
  Gmail's own; its `Opened` and `Archived` markers are not labels), and that
  header and `X-GM-THRID` are taken off before upload. In the log the
  messages are `mbox-1`, `mbox-2`, … in file order.
- `--from-folder DIR`: a gyb backup folder from elsewhere (another
  machine, an old account), into ACCOUNT. It is only read.

None of these has Gmail IDs that mean anything in the account restored
into, so each message is looked up there by Message-ID and content, as
with `--to`: recorded if Gmail has it, uploaded otherwise. Nothing is
recorded in a backup or indexed: a run again finds what was restored by
its Message-ID, and the next `backup` downloads it.

## Options

`restore ACCOUNT` takes the backup to read from: an address, config name or
a unique part of an address. With `--from-mbox` or `--from-folder` it is the
account restored into. Without `--yes` it is a dry run.

**Which messages** (at least one, from the backup):

| Option | What it selects |
| --- | --- |
| `--batch BATCH` | An offload batch (repeatable) |
| `--rule RULE` | An offload rule (repeatable) |
| `--id ID` | A message's ID in the backup, offloaded or not (repeatable); with `--offloaded`, only if it was offloaded |
| `--offloaded` | Every offloaded message |
| `--all` | Every message in the backup; those still in the mailbox are left alone |

**Narrowing**, combined with any of the above; all must hold:

| Option | What it keeps |
| --- | --- |
| `--before DATE`, `--after DATE` | Mail older than, or from, the date: `YYYY-MM-DD` (UTC) or a number of years ago |
| `--has-label NAME` | Mail with this user label, ignoring case (repeatable: any of them) |
| `--sender ADDRESS`, `--sender @DOMAIN` | Mail from this From address, or any address at the domain (repeatable: any of them) |

**From elsewhere**, instead of the backup (one of):

| Option | What it reads |
| --- | --- |
| `--from-pruned BATCH` | Mail `backup --prune` moved into `<data root>/_pruned/<email>/BATCH/` |
| `--from-mbox FILE` | An mbox file: a Google Takeout export, or one `export` wrote |
| `--from-folder DIR` | A gyb backup folder from elsewhere, read as it is |

**Where it goes and what it is labelled:**

| Option | What it does |
| --- | --- |
| `--to ACCOUNT` | Restore into this configured account instead; its backup gets the new IDs (a first backup of it starts with them). Refused if that account has been backed up before and its backup isn't there, as `backup` refuses it |
| `--label NAME` | The label restored messages get (default `restored/<today>`) |
| `--label-prefix P` | Nest the backed-up user labels under `P/` |
| `--strip-labels` | Restore no backed-up labels, only the `restored/` label: the messages arrive archived |

**Other:**

| Option | What it does |
| --- | --- |
| `--limit N` | Only the first N, oldest first |
| `--yes` | Restore; without it, a dry run |
| `--config CONFIG` | Config folder |
| `--log LOG` | Restore log, appended to (default `<data root>/restore/restore-log.csv`) |
