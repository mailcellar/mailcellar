# Architecture

One command, `mailcellar`, over one Python package, `src/mailcellar/`:

- **`mailcellar setup`** adds an account from a downloaded OAuth client:
  checks the file, copies it into the config folder, lists the account in
  `accounts.toml` and signs in read-only.
- **`mailcellar backup | count | meta`** keeps a local backup of each Gmail
  account, in Got-Your-Back's (gyb) on-disk format; **`verify`** checks,
  read-only, that its files are still intact; **`schedule`** runs backups
  regularly, unattended.
- **`mailcellar dedup`** finds duplicate messages within and across the
  backed-up accounts, decides which copies to remove, and removes them
  from Gmail — in stages, with checks, never permanently.
- **`mailcellar offload`** takes old mail out of Gmail by rule, keeping it
  in the backup, in the same stages.
- **`mailcellar restore`** puts mail back: offloaded mail out of Gmail's
  Trash within 30 days, otherwise uploaded from the backup; a whole
  backup, into its own account or another one.
- **`mailcellar export`** copies a selection of the backup out, read-only,
  as an mbox file for a mail client or a zip of the `.eml` files with a CSV
  index.
- **`mailcellar search`** finds mail in the backups, offloaded mail included,
  by words in the sender, recipients, subject and body, from a full-text
  index in the data root.
- **`mailcellar report`** (storage, bulk mail, volume, threads, labels,
  correspondents, backup health, senders) reports on the backups from the
  per-message sidecars.

This page describes the system as it is. The maintainers' design notes
and real-data measurements are kept outside the repository.

## Data flow

Gmail is read from one place and changed from another, and the backup sits
between them. Everything else reads the backup, not Gmail.

```
   Gmail
     │  ▲
     │  └─────────────────────────────── changes Gmail (gmail.modify) ──────┐
     │ reads (gmail.readonly)                                               │
     ▼                                                                      │
   backup · count · meta                                                    │
     │ add to                                                               │
     ▼                                                                      │
   ┌───────────────────────────────────────┐                                │
   │ THE BACKUP   <data>/<email>/          │ ◄── verify · export · report   │
   │   msg-db.sqlite + .eml   (gyb format) │     (read only)                │
   │   meta.sqlite            (sidecar)    │                                │
   └───────────────────┬───────────────────┘                                │
                       │ read by                                            │
                       ▼                                                    │
   dedup index · match · plan      ──► dedup-plan.csv ──┐                   │
   offload plan                    ──► offload-plan.csv ┤                   │
                                                        ▼                   │
                      dedup apply · offload apply · restore --yes ──────────┘
                      (labels, Trash, untrash, messages.import)
                                  │
                                  ▼
               logs and records: dedup-apply-log.csv, offload-apply-log.csv,
               restore-log.csv, and meta.sqlite (`offloaded`, `restored`)
```

Reading the picture from the top: `backup` copies Gmail into the backup and
touches nothing in Gmail. The planning commands only read the backup and write
their own plan file. Only the three steps at the bottom change Gmail, and only
when run for real (`--label`/`--trash`, or `--yes`), with the broader
`gmail.modify` permission. Their dry runs still read Gmail, to check each
message, but change nothing. Each takes its instructions from a plan or a
selection you can see first. The `apply` steps record what they did in a
log and in `meta.sqlite`, and `restore` also indexes the messages it uploads
in the backup of the account they went into.

Who touches what:

| Command | Reads | Writes | Gmail |
|---|---|---|---|
| `setup` | the client JSON you name; `accounts.toml` | the account's `client_secrets.json`, `accounts.toml`, a readonly token | read-only sign-in |
| `backup` | Gmail | the backup | read-only |
| `schedule install`, `remove` | the config folder | the launchd agent (macOS) or nothing (a cron line is printed); `logs/backup.log` when it runs | none |
| `schedule status` | the backups' `runs`, the token files | nothing | none |
| `count` | Gmail, the backup | nothing (a line in `runs`) | read-only |
| `meta` | the `.eml` files; Gmail, for label and thread details | `meta.sqlite` | read-only |
| `verify`, `export`, `report` | the backup | `export`: the file you name; the others, nothing | none |
| `search` | the backups | `search.sqlite` | none |
| `dedup index` | the backups | `dedup.sqlite` | none |
| `dedup match`, `plan` | `dedup.sqlite` | `dedup.sqlite` (groups); `dedup-plan.csv` | none |
| `offload plan` | the backup, `rules.toml` | `offload-plan.csv`, `plans/` | none |
| `dedup apply`, `offload apply` | the plan; Gmail, to check each message | with `--label`/`--trash`: Gmail labels and Trash; logs; `meta.sqlite` (offload) | read-only for a dry run, modify with `--label`/`--trash` |
| `restore` | the backup; Gmail, to look each message up | with `--yes`: Gmail; `restore-log.csv`; `meta.sqlite` | read-only for a dry run, modify with `--yes` |

## Modules

All under `src/mailcellar/`; installed by `uv sync` (editable), so `uv run
mailcellar` and `python -m mailcellar` both work.

| Module | Role |
|---|---|
| `cli.py` | The `mailcellar` command: dispatches `setup`, `backup`, `count`, `meta` (every configured account by default), `verify`, `auth`, `schedule`, `dedup`, `offload`, `restore`, `export`, `search`, `report` (plus `senders`), and `config`, which shows paths and each account's state without printing secrets; sets up logging (stderr; `-v`/`-q` anywhere on the line) |
| `paths.py` | Where things are: `config_root()`, `data_root()` and where each was set, `backup_folder()`, `find_backups()`, `pruned_folder()` |
| `gmailapi.py` | Accounts (`<config>/accounts.toml`), OAuth tokens and scopes, `GmailAccount`: adaptive batching, retries with backoff, listing, history, labels, trash and untrash, batched Message-ID searches, `messages.import` |
| `message.py` | Message parsing: `parse_message()`, `content_hash()`, `header_key()`, `extract_address()`, `PARSER_VERSION` |
| `setup/cli.py` | `setup`: from a downloaded OAuth client to a configured, signed-in account. `check_client`, `accounts_entries`, `pick_name`, `add_account`, `latest_download` are pure; `accounts.toml` is only appended to, the client is written owner-only, and the sign-in is `mailcellar auth`'s read-only one |
| `backup/commands.py` | `backup` (history-based or full listing), `count`, `meta` (sidecar backfill), `sync`; the runs log |
| `backup/gyb.py` | gyb's format: writes `msg-db.sqlite` and `<y>/<m>/<d>/<id>.eml`, reads them back (`iter_backup`, `backup_account`), `prune` for `--sync` |
| `backup/schedule.py` | `schedule`: installs `backup --unattended` as a launchd agent on macOS (a cron line elsewhere), `status` from the sidecars' runs and the token files, `remove`. The decisions (`describe`, `launchd_plist`, `cron_line`, `overdue`) are pure |
| `backup/verify.py` | `verify`: for each backup, every indexed message's `.eml` exists, parses and matches the content hash in `meta.sqlite`; the two databases agree on which messages exist (`--offloaded`, `--sample`). Both are opened read-only; the decisions (`judge`, `select`, `disagreements`) are pure |
| `backup/sidecar.py` | The per-account sidecar `meta.sqlite`: schema, migrations, runs, the offload record |
| `dedup/cli.py` | argparse and one `cmd_*` per subcommand (`index`, `match`, `plan`, `apply`, `auth`); `mailcellar auth` is its `auth`, since authorisation serves every command, and `dedup auth` stays as an alias |
| `dedup/db.py` | `dedup.sqlite` schema and migrations |
| `dedup/index.py` | Catalogue backups into `dedup.sqlite`, from the sidecars where possible |
| `dedup/match.py` | Group copies of the same message |
| `dedup/rules.py`, `dedup/plan.py` | Keep / remove / review decisions; the plan CSV |
| `dedup/apply.py` | Live checks against Gmail, then label or trash; the audit log. Shared with offload. `apply_plan()` reads Gmail's state, `check_row()`/`checked()` decide, and `act()` carries out one account's rows; each returns new frozen `Result`s (the plan rows are never changed) |
| `dedup/report.py`, `dedup/paths.py` | Text reports; default output locations |
| `offload/` | `rules.py` (TOML rules), `plan.py` (selection, plan CSV, summary; the messages each protection keeps), `review.py` (`plan --review`: a local HTML page per rule from the plan's results; samples, largest messages, busiest threads and the page itself are pure), `apply.py` (backup checks, the offload record, batch manifests on top of dedup's label → trash loop), `cli.py` |
| `restore/` | `sources.py` and `mbox.py` (`--from-pruned`, `--from-mbox`, `--from-folder`: candidates from a pruned batch, an mbox file or another gyb folder), `apply.py` (which offloaded messages, narrowed by date, label and sender; backup checks; each message's way back: untrash, already in Gmail, found by Message-ID, or import; indexing a new ID in the backup), `cli.py` (the command, and the selection options `export` shares), `export.py` (`export`: the same selection copied out read-only as an mbox file or a zip of `.eml` files with an index; the mbox entry, label header and index row are pure) |
| `search/` | `index.py` (each message's sender, recipients, subject and readable body into the index, incrementally: `document()` and `changes()` are pure), `query.py` (the query language translated to FTS5 by `fts_query()`; `keep()` for the offload filters and `render()` for a result are pure; `find()` reads), `db.py` (`search.sqlite` schema and migrations), `cli.py` (the command) |
| `report/` | Reports over all accounts' sidecars, attached read-only (`db.py`): `storage` (with each offload batch's space freed, from `offloaded.gone_ms`), `bulk`, `volume`, `threads`, `labels`, `correspondents`, `health`; `senders.py` for one backup; `output.py` formats tables |

## Files on disk

```
<config>/                    credentials, owner-only: $MAILCELLAR_CONFIG, else
                             ~/.config/mailcellar, else ./config (the old place,
                             gitignored); paths.config_root()
  accounts.toml              folder name → address (work = "alex@example.org")
  <name>/client_secrets.json the account's OAuth client (Desktop app)
  <name>/token-readonly.json, token-modify.json   # + authorised_at: when consent was last given
  settings.toml              data_dir: the data root; token_warn_days (optional)
<data root>/                 everything derived from mail: $MAILCELLAR_DATA, else
                             data_dir in <config>/settings.toml, else ~/mailcellar
                             (or an existing ./data, the old default); paths.data_root()
  <email>/
    msg-db.sqlite            gyb index (db_version 6)
    <year>/<month>/<day>/<gmail_id>.eml
    meta.sqlite              sidecar (schema v9)
  _pruned/<email>/<timestamp>/  messages moved out by backup --sync, + pruned.csv
  logs/
    backup.log               output of the scheduled backup (schedule install); not rotated
  offload/
    rules.toml               offload rules (user-written)
    offload-plan.csv         the latest offload plan, input of offload apply
    plans/<utc time>.csv     every plan made, named by its planned time
    offload-apply-log.csv    one row per plan row per apply run, with the plan's time
    batches/<batch>/offloaded.csv  manifest of each trash batch: plan row + batch + trashed
    review/<rule>.html       offload plan --review: a page per rule (senders, subjects)
  restore/
    restore-log.csv          one row per message per restore run (dry run or --yes)
  search/
    search.sqlite            full-text index (schema v1); rebuilt if deleted
  dedup/
    dedup.sqlite             dedup index (schema v5)
    dedup-plan.csv           output of plan, input of apply
    dedup-apply-log.csv      one row per planned removal per apply run; keep it
```

### Databases

| Database | Owner | Contents | Versioning |
|---|---|---|---|
| `msg-db.sqlite` | `backup/gyb.py` | gyb's exact schema: `messages`, `uids`, `labels` (names), `settings` | fixed (gyb `db_version` 6) — never extended |
| `meta.sqlite` | `backup/sidecar.py` | per message: parsed fields (incl. sender name, Reply-To, content hash, header key, list headers, In-Reply-To), thread ID, UTC date, size, `parser_version`; attachments (name, type, size, hash) and recipients; label IDs; label catalogue; `history_id`; a `runs` log of every backup, count and sync; `offloaded`, mail removed from Gmail on purpose; `restored`, mail put back and its ID now | `SCHEMA_VERSION` + `MIGRATIONS`; rows go stale by `parser_version` |
| `search.sqlite` | `search/db.py` | per message: account, Gmail ID, path, size, date, sender, subject, `text_version`; an FTS5 index of sender, recipients, subject and body that stores no text (`contentless_delete`, SQLite 3.43+) | `SCHEMA_VERSION` + `MIGRATIONS`; `index.TEXT_VERSION` forces a re-read, independent of `PARSER_VERSION` |
| `dedup.sqlite` | `dedup/db.py` | all accounts' messages (paths relative to `backups.folder`), labels, match groups | `SCHEMA_VERSION` + `MIGRATIONS`; `meta.parser_version` forces a re-parse |

The gyb format is kept exactly (verified byte-for-byte against gyb
1.97), so gyb and anything else that reads it can still use the backups;
`mailcellar restore` replaced gyb's restore (2026-10-25). Anything gyb has
no room for goes in `meta.sqlite`.

## Backup

- **First run** (or `--full`, `--search`, `--refresh-labels`, or expired
  history): list every message, download what's missing (`format=raw`),
  and store the mailbox `historyId` taken before the listing.
- **Later runs:** read Gmail's history since that ID (kept ~4 weeks), look
  up the current state of each changed message, and download new mail,
  update labels, and skip Spam/Trash/Chat — from current state, so a
  repeated run is harmless. The new `historyId` is stored only after a run
  with no failures.
- **Parse once:** each downloaded message is parsed and written to
  `meta.sqlite` in the same pass; `meta` backfills older mail.
- **Never deletes**, except `backup --sync`: messages permanently gone from
  Gmail (Trash and Spam count as present) are moved to
  `<data root>/_pruned/<email>/`, behind confirmation and a 10% guard. Messages
  recorded in `offloaded` always stay, and count neither toward the guard
  nor as deleted in `count` or a backup run.

## Deduplication

**Matching** (`dedup/match.py`): two copies are the same message when they
have the same **content hash** — the visible text (HTML tags, URLs and
addresses removed) plus every attachment's hash — **and** either the same
**Message-ID** or the same **header key** (From, Date, Subject). Groups are
connected components; each is `cross` (spans accounts) or `within`.
Message-IDs reused for different content are reported, not grouped.
The rule was chosen by checking candidates against real backups: sender,
date and subject alone grouped different messages (two invoices, two
tickets); a hash of the whole body missed re-sends that differ only in
tracking links; and identical visible text is common across genuinely
separate mail, so the content hash is never used on its own.

**Planning** (`dedup/rules.py`), per group, `keep` / `remove` / `review`:
- cross-account: `--keep <account>` keeps that account's copies, or
  `--keep origin` removes only copies auto-forwarded from another account
  (`X-Forwarded-For`), leaving independent deliveries alone;
- within an account: keep one copy from the largest set with the same
  labels; copies with other labels go to review;
- safety rules on every removal: sent/draft copies, a keeper in
  Trash/Spam, stars or user labels the keeper lacks → review.

**Applying** (`dedup/apply.py`): dry run by default. `--label [NAME]` adds
a review label in Gmail; `--trash [NAME]` moves labelled copies to Trash
(recoverable for 30 days). Before touching a message it checks that the
local backup copy exists, the message and its keeper are still in Gmail,
the keeper isn't in Trash/Spam, and the message's labels haven't changed
since the plan. Removing the label in Gmail vetoes a message.

## Restore

`restore/apply.py`, in two steps, passing frozen values (`Candidate` →
`Decision` → `Result`):

1. **`plan()`** (read-only) checks each backup copy (the `.eml` parses and
   matches the sidecar's content hash), reads Gmail's state (one lookup
   per message, or two ID listings above 1,000 messages) and searches for
   matching Message-IDs. Then **`decide()`**, a pure function of what was
   read, picks each message's way back: `untrash`, `in gmail`
   (offloaded, already out of Trash), `present` (never offloaded, still
   there), `found` (same Message-ID and content under another ID, from a
   batched `rfc822msgid:` search), `import`, or `skipped` (unusable
   backup copy, or the Message-ID held by other content).
2. **`apply()`** (or, for the dry run, the pure `dry_run()`) returns a
   `Result` per decision. It untrashes, then uploads with `messages.import` (Date
   header as the internal date, spam classifier off) 200 messages at a
   time. Each success is recorded in the source backup's `restored` table
   at once, so a rerun carries on. Imported messages are indexed under
   their new IDs with `gyb.add_copy()` (a hard link to the old `.eml`) in
   the backup of the account restored into.

Gmail matches an import to the message already holding its Message-ID, so
a retried upload returns the same message rather than a copy. The old ID
of an imported message stays in the backup: `sidecar.kept_ids()` protects
it from `--sync`, and `superseded_ids()` keeps it out of reports and
dedup. With `--to`, restored rows carry the target account and leave the
source's offload record alone.

**Other sources** (`restore/sources.py`, `restore/mbox.py`): `--from-pruned`,
`--from-mbox` and `--from-folder` build the same `Candidate`s from a pruned
batch's `pruned.csv`, an mbox file (a candidate's `span` is where its entry
is; `read_raw()` reads it back) or another gyb folder, parsed once for the
sender, date and content hash. `plan()` and `apply()` are unchanged apart
from that: every message takes the other-account way back (`found` by
Message-ID and content, or `import`), and `apply()` with no backup folder
records and indexes nothing. The mbox reader splits at `From ` lines at the
start of the file or after a blank line, undoes mboxrd escaping, drops the
headers Takeout adds, and maps Takeout's label names to Gmail's IDs; all of
that is pure.

## Safety model

- Read-only by default: backups, `count`, `meta`, `index`, `match`,
  `plan` and dry-run `apply` use `gmail.readonly` or no API at all.
- `gmail.modify` is requested only for the account `apply --label/--trash`
  or `restore --yes` changes; a modify token also serves read-only requests.
- Nothing is ever permanently deleted from Gmail (no `batchDelete`), and
  the backup keeps everything unless `--sync` is asked for. Restore only
  adds, and skips what Gmail already has.
- Every account call checks it reached the expected mailbox.

## Testing

`uv run pytest` — no network. Gmail is replaced by fakes:
`FakeGmail` (account level, with history computed from mailbox snapshots)
and `FakeService` (the googleapiclient surface, for batching and retries);
`FakeBackup` builds gyb folders. Changes are also checked against real
backups, read-only, before they are relied on.
