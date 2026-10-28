# Changelog

What changed for users in each release. Releases and their dates are on the
[releases page](https://github.com/mailcellar/mailcellar/releases); the format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). mailcellar is
in beta, so commands and options may still change before 1.0.

## Unreleased

### Fixed
- `restore` skipped a message it had restored before even when the mailbox
  had lost it since, so `restore --all` after the mailbox was emptied left
  some of the backup out. A restored message now counts as done only while
  the mailbox still has the copy it went back as; one gone since comes back
  like any other, and one still there shows as "already in the mailbox"
  (restored before). `count` reports such mail under "Only in backup", as
  "restored, then deleted", where it hid it before.
- `restore` into an IMAP account shows how far its uploads have got: the
  "Importing" line counts each message as it is appended, and the folder
  reads around each batch keep that line moving rather than replacing it
  with their own, which looked stalled.

## 0.17.0 (2026-10-28)

**Upgrading from 0.16.0:** each `meta.sqlite` moves from schema 11 to 13
on first use, adding two tables that only IMAP accounts use, with a copy
kept beside it as `meta.sqlite.pre-v13`; no message is parsed again.
Scripts that read `search --format json`, an `export` zip's `index.csv`
or `report --csv` need the new names under Changed (`gmail_id` is now
`id`). Offload rules with `waive`, `protect` or `except` keep working:
`offload plan` warns and shows each in the new keys.

### Added
- Offload rules say what they keep: `keep = ["mine", "starred",
  "conversations", "labelled"]` (the default; `"correspondents"` adds mail
  from anyone you have written to, `keep = []` keeps nothing), and
  `keep_if`, a table of scope keys that must all match, or a list of them
  of which any one keeps a message: `keep_if = [{ sender =
  "@insurer.example" }, { subject = "invoice", larger_than_mb = 1 }]`. The
  plan's Rules table has a Keeps column in place of Waived and Added, and
  kept mail is counted per `keep` name, a conversation once however many
  reasons keep it.
- Offload rules have four more keys. `bulk = true` selects mail sent in
  bulk, by the same signal `report bulk` uses. `to` selects by recipient
  (To, Cc or Bcc), an address or `@domain`. `attachment` selects by
  attachment kind: `any`, `image`, `pdf`, `video`, `audio`, `office` or
  `archive`. `after` sets the oldest date a rule takes, as `before` sets
  the newest; the plan's Rules table shows it. The first three work in
  `keep_if` too.
- `offload plan --try RULE` plans one more rule without editing the rules
  file, written as the inside of a TOML inline table: `--try 'bulk = true,
  before = "2017-01-01"'`. It runs after the file's rules (`--rules none`:
  alone), shows only its own results, writes no plan unless `--plan FILE`
  is given, and ends with the rule as a `[[rule]]` table to paste in.
- `offload suggest` drafts rules from the backup, per account: mailing
  lists and bulk senders the bulk report calls candidates, automated
  senders (`noreply@` and the like) never engaged with, and large mail.
  Each is printed as a `[[rule]]` table, planned after the rules file's
  rules, with what it would take and one line per source; `--append` adds
  them to the rules file, commented out. It never sets `forget_after`.
- `report bulk --unsubscribe` lists where to unsubscribe from each
  candidate, read from its most recent message: the List-Unsubscribe link
  (`https:` only) and address (`mailto:`), whether it offers one-click
  unsubscribe, and a note when the link's domain isn't the sender's.
  Nothing is opened or sent.
- IMAP accounts can be backed up: `mailcellar setup ADDRESS --imap HOST`
  asks for the password, signs in, and keeps the server in the account's
  `imap.toml` and the password owner-only beside it. `backup`, `count`,
  `meta`, `verify`, `schedule`, `search`, `export`, holds and the reports
  work on it as on Gmail. Read-only: nothing on the server changes, not
  even a message's read state. Folders and flags become labels; Junk and
  Trash are left out, as Spam and Trash are for Gmail. A new guide
  covers it.
- `offload apply` and `dedup apply` work on IMAP accounts whose server has
  MOVE and a special-use Trash: `--label` marks each candidate with a
  `$mailcellar-` keyword (clear it in a mail client to keep the message),
  `--trash` moves what is still marked to Trash and confirms it there.
  Nothing is expunged; other servers are refused, saying what's missing.
  A folder that keeps no keywords fails its own mail; a server that keeps
  none is told to review by folder.
- `restore` works on IMAP accounts: an IMAP backup goes back into its
  account, or another IMAP account. Mail still in Trash is moved back to
  the folders it lived in; mail the server no longer has is appended with
  its flags and date, under the same ID where the server stores it
  unchanged; restore reads each message's ID back from what the server
  stored. `restore --to` also goes between
  Gmail and IMAP accounts: Gmail labels become folders (archived mail
  into Archive, Important and the categories left out), IMAP folders
  become labels.
- `setup --imap --review keyword|folder` chooses how offload and dedup
  mark an IMAP account's mail for review: a `$mailcellar-` keyword, or a
  move into a `mailcellar/review/` folder (moving it out keeps it), the
  folders it came from recorded in the backup. Setting an IMAP account up again
  keeps its port, username, CA file and review mode unless given.

### Changed
- Report columns that named Gmail name the mailbox, for IMAP accounts:
  `report health`'s "Deleted in Gmail" and "Not yet gone from Gmail" are
  "Deleted in mailbox" and "Not yet gone from mailbox", and `report
  storage`'s "Gmail ID" is "ID". `report --csv`'s headers change with them.
- The message ID's field is `id`, not `gmail_id`, in `search --format json`
  and in an `export` zip's `index.csv`: it is the backup's ID, Gmail's or
  an IMAP account's. Plans, apply logs and manifests, which mailcellar
  reads back, keep calling it `gmail_id`, and the restore log `source_id`.
- Offload rules' `waive`, `protect` and `except` are deprecated, to be
  removed at 1.0. They still work, can't be mixed with `keep` and `keep_if`
  in one rule, and `offload plan` warns about each rule that uses them with
  the rule in the new keys: `except = { a = …, b = … }` is `keep_if = [{ a
  = … }, { b = … }]`. A rule that waives one of `thread` and `replied` but
  not the other can't be written with `keep`; it keeps working, and its
  warning says so. The plan's kept rows say "kept: yours", "kept: a
  conversation you were in", "kept: labelled", "kept: keep_if", and
  `--csv`'s rules table has Keeps in place of Waived and Added; the plan
  CSV's `waived` column is unchanged.

### Fixed
- A new data root is created owner-only (mode 700), as the config folder
  is: it used to take the umask's mode, usually 755, so other users on the
  machine could read the backed-up mail. A root that already exists keeps
  its mode; `chmod 700` it to close it.
- `restore`'s notes reach its log. A restored message whose restore label
  couldn't be added (or, on an IMAP account, whose offload mark couldn't be
  taken off) gets a second row with the note; the note used to be lost,
  the message's row having been written before it.
- Output names the account's mailbox rather than always Gmail: on an IMAP
  account, offload, dedup and restore speak of "the server". Reasons that
  hold for either kind of account now say "its mailbox": a skipped
  message is "no longer in its mailbox" or "couldn't read it from its
  mailbox", and restore's outcome "already in the mailbox" (they were
  "… Gmail"). Commands' help, the reports' notes and the
  docs cover IMAP accounts.

## 0.16.0 (2026-10-27)

**Upgrading from 0.15.2:** nothing to do: no database's schema changes,
and accounts set up before keep signing in as they did; the service
account applies only to an address set up with `--service-account`.

### Added
- Workspace accounts can be backed up through a service account with
  domain-wide delegation, so backups run unattended with no sign-in and
  no token to renew: `mailcellar setup ADDRESS --service-account KEY`
  installs the key once for the domain, owner-only, marks that address
  to read through it (others in the domain keep their own sign-in), and
  checks it.
  Read-only, and only that: anything that changes Gmail still needs the
  user's own sign-in. `config` and `schedule status` show it, and a
  scheduled backup the key is refused for is recorded as a failed run
  (`access-refused`). A new guide covers the Admin console and Google
  Cloud steps.

### Fixed
- `restore` no longer uploads a message with no Message-ID a second time
  after a server error or a timeout, when Gmail may have stored the first
  upload: it reports it as possibly imported instead, so a second copy
  isn't made. It is still retried after a rate limit.
- `setup` creates a new config folder readable only by you; it was left
  open to other users (listing the account names) when `setup` made it.

## 0.15.2 (2026-10-27)

**Upgrading from 0.15.1:** nothing to do: no database's schema changes.

### Changed
- `offload plan` and `offload forget --due` warn about a rule whose
  `forget_after` is no longer than its `before`, which would make all it
  offloads due to be forgotten as soon as Gmail has deleted it.

### Fixed
- `restore --from-folder` reads only files inside the gyb folder: an entry
  in its index naming a file outside it (an absolute path, or one through
  `..`) is skipped with a warning, where it would have been read and
  uploaded into Gmail. `backup --prune` likewise moves nothing outside the
  backup.
- The crontab line `schedule install` prints escapes `%`, which cron reads
  as the end of the command, so a data root or log path holding one no
  longer stops the scheduled backup.
- A token or OAuth client written by two commands at once (the scheduled
  backup and one typed by hand) is no longer left broken: each write has
  a temporary file of its own.
- `restore --to ACCOUNT` refuses an account that has been backed up before
  when its backup isn't there (a volume not mounted), as `backup` does,
  where it started a new backup in its place holding only the restored
  messages, which then kept `backup` from noticing.
- `dedup apply` and `offload apply` ask the legal-hold check about every
  planned message, so one the sidecar doesn't know (a gyb-era backup that
  hasn't been through `mailcellar meta`) counts as held where a hold
  could cover it, where it was never looked at and could be trashed.
- The hash chain's file is locked while a link is appended, so two
  commands running at once (the scheduled backup and one typed by hand)
  no longer write links to the same previous link, which `verify --chain`
  reported as a broken chain; a second genesis appends nothing.
- `backup --prune` and `offload forget` change the backup's index only
  when they finish, so one stopped part-way (Ctrl-C, a crash) leaves it as
  it was, and the next run finds the files already moved and lists them
  as moved, so `restore --from-pruned` can bring them back, where it said
  "was missing".
- `backup --prune --force` counts the messages by year in chunks, so it
  no longer fails with "too many SQL variables" above about 32,000.
- A legal hold naming parties now holds a message whose addresses weren't
  read (a parse that failed), as it does other mail whose facts aren't
  known, where it let such a message go.
- `report health` and `schedule status` warn when the offload rules can't
  be read, where they showed nothing due under retention.
- A number of years (`--before 5`, `forget_after`) counted from 29 February
  lands on the 28th in a year without one, where it failed.
- Removing the `offload-candidate` label after a restore finds a label
  whose name differs only in case, as adding it does.

## 0.15.1 (2026-10-27)

**Upgrading from 0.15.0:** nothing to do: no database's schema changes.

### Fixed
- A full backup (`--full`, `--refresh-labels`, or a history too old to
  use) no longer downloads every chat only to skip it: it lists the chats
  by their label and leaves them out. Chats are still not backed up.
- `export` writes Gmail's system labels by Takeout's English names
  (`Inbox`, `Important`, `Category Updates`) rather than their IDs
  (`INBOX`, `IMPORTANT`, `CATEGORY_UPDATES`), in the mbox's
  `X-Gmail-Labels` header and in `index.csv`, and puts your own labels
  first. Readers of Takeout's mbox didn't know the IDs: Bichon filed
  every message under a system label and dropped the user labels.
  `restore --from-mbox` reads both forms, so older exports still restore.

## 0.15.0 (2026-10-27)

**Upgrading from 0.14.0:** nothing to do: no database's schema changes.

### Added
- Retention: an offload rule's `forget_after = N` forgets the mail it
  offloaded once it is more than N years old (from the message's date),
  with `offload forget --due` (`--rule` to narrow it), through forget's
  checks and 30-day hold; mail still in Gmail and mail under a legal hold
  are never forgotten. `offload plan`, the review pages, `report health`
  and `schedule status` show the policy and what is due, and the hash
  chain's forget link names the rules.

### Fixed
- `backup` refuses an account it has backed up before (its hash chain
  anchor is in the config folder) when the backup isn't there, creating
  nothing, where it started a new full backup in its place: on a data
  root whose volume wasn't mounted, that meant the wrong disk, and
  unencrypted. The other accounts still run, and the exit status is 1.

## 0.14.0 (2026-10-27)

**Upgrading from 0.13.1:** each backup's `meta.sqlite` moves to schema 11
the first time a command writes to it (`backup`, `meta`, `hold add`); until
then the reports ask you to run `mailcellar meta`. 0.13.1 can't read schema
11: mailcellar copies the old file to `meta.sqlite.pre-v11` next to it first,
so to go back, put that copy back.

### Added
- Legal holds: `mailcellar hold add NAME --reason TEXT` with `--party`,
  `--after`, `--before`, `--label`, `--id` and `--until` keeps the mail it
  covers, now and as more arrives: `offload apply` and `dedup apply` leave
  it in Gmail, and `backup --prune`, `offload forget` and `--empty` leave it
  in the backup. `hold list`, `show` and `release`; placing and releasing
  are recorded in the hash chain. `export --hold NAME` copies the mail out,
  and `report health` lists the holds, warning when backups lapse.

## 0.13.1 (2026-10-27)

**Upgrading from 0.13.0:** nothing to do: no database's schema changes.

### Fixed
- `report health` prints each hash chain's head hash in full, so it can be
  noted down; the text report cut it at 42 characters.
- `auth --scope readonly` makes a read-only token when the account has a
  stored modify token, where it reported "OK (via gmail.modify)" and made
  none, so backups kept running with modify access.
- `verify` counts messages with no text or attachments to hash (a body of
  only links, images or nothing) apart, as having nothing to compare,
  instead of as unchecked with advice to run `mailcellar meta`, which
  changed nothing; one recorded empty that has text now is reported as
  changed.
- `restore` checks each backup copy's bytes against the SHA-256 its hash
  chain recorded before uploading (for `--from-pruned`, the one the prune
  recorded; for `--from-folder`, that folder's own chain), and skips one
  that differs. It compared only the content hash, which leaves out
  headers and links and is empty for a message of only links or images,
  so such changes went through.
- `verify --help` and the reference say that plain `verify` checks a
  message's content, not every byte, and that `--chain` checks the bytes.

## 0.13.0 (2026-10-27)

**Upgrading from 0.12.0:** no database's schema changes. The first `backup`
of each account starts its hash chain by reading every `.eml` once, which
adds a little time to that run; `verify --chain` can check it from then on.

### Added
- Tamper evidence ([#70](https://github.com/mailcellar/mailcellar/issues/70)):
  each backup folder keeps a hash chain, `chain.jsonl`, recording what
  every `backup`, `restore`, `--prune` and `offload forget` added or
  removed, with each file's SHA-256, and its latest link is kept in the
  config folder too. `verify --chain` checks every file against it and
  fails on a change, a loss or an addition no command made, even if
  `meta.sqlite` was changed to agree. `report health` shows each chain's
  head. The first `backup` after upgrading starts the chain by hashing
  every file once.

### Fixed
- `restore` creates a user label whose name Gmail reserves (`Muted`,
  `Archive`, `Bin` and others) as `_Muted` and so on, as gyb does;
  Gmail refuses those names (HTTP 400), as gyb's users found.
- `restore --from-mbox` maps a Takeout's system label names in any of the
  73 interface languages to Gmail's own (from gyb's list), where only
  English names were mapped and others became user labels such as
  "Posteingang".
- `restore` uses an existing label whose name differs only in case
  (`Furniture` for `furniture`), as Gmail treats them as one, where it
  stopped with "Label name exists or conflicts". A label Gmail still
  refuses now costs that message the label, noted in the log, instead of
  stopping the restore.
- `restore --from-mbox` reads non-ASCII label names written as plain UTF-8
  in `X-Gmail-Labels`, where they became labels of replacement characters.
- `restore`'s summary names the label restored mail carries (`--label`),
  not always `restored/`.
- `restore --from-mbox` finds the end of a message's headers when a header
  line ends in LF and the blank line after it in CRLF, where it took the
  whole message for headers and could drop a body line that looked like
  `X-Gmail-Labels`.
- `restore` gives a message with no Date header, or an unreadable one, a
  Date from the backup's recorded date, where Gmail dated it at the moment
  of the restore (and dropped an unreadable header). The backup's file is
  unchanged; `--from-mbox` is left as it was.
- `restore` writes its log a row per message as it goes, so a run that is
  interrupted or fails leaves a log of what it did; it wrote nothing until
  the end.

## 0.12.0 (2026-10-26)

**Upgrading from 0.11.0:** nothing to do: no database's schema changes.

### Added
- Offload rules take `query`, a Gmail search written as in Gmail's search
  box (`"has:attachment filename:pdf"`). `offload plan` asks Gmail which
  messages it finds, read-only, and selects those the backup has; the
  rule's cut-off, exceptions and protections still apply. `offload plan`
  takes `--config` for it ([#73](https://github.com/mailcellar/mailcellar/issues/73)).
- Each `offload plan --review` page opens with what its rule selects, in
  one line: its scope keys, cut-off and exception.
- `report health` lists the forget runs held in `_forgotten/`, with when
  each hold ends and which are due for `offload forget --empty`; `offload
  forget` says when runs are due, so held mail isn't kept by oversight.

## 0.11.0 (2026-10-26)

**Upgrading from 0.10.0:** each backup's `meta.sqlite` moves to schema 10
the first time any command opens it, and 0.10.0 can't read schema 10.
mailcellar copies the old file to `meta.sqlite.pre-v10` next to it first;
to go back to 0.10.0, put that copy back.

### Added
- `offload forget` deletes offloaded mail from the backup once Gmail has
  deleted it too, for mail that shouldn't be kept at all. It takes only
  messages a backup recorded gone and Gmail confirms gone, holds their
  files 30 days in `_forgotten/` (`--undo` puts them back, `--empty`
  deletes them), and removes every trace from the backup's databases,
  the search index, the dedup catalogue, plans, logs and manifests. A dry
  run unless `--yes`, which asks for the count to be typed.
- A command reference on the documentation site, one page per command, with
  every option listed. A test fails when `--help` lists an option the
  reference doesn't name.
- Guides for backing up and keeping the backup safe, removing duplicate mail,
  restoring mail, and finding mail and deciding what to offload.
- `search --format tsv|json` prints one line per result, with the account and
  Gmail ID, for scripts and pickers such as fzf; the finding-mail guide has
  a recipe.

### Changed
- Before a schema migration, `meta.sqlite` is copied to `meta.sqlite.pre-vN`
  next to it, untouched, so the old version survives a migration that goes
  wrong. An existing copy is never overwritten.
- `backup --sync` is now `backup --prune`, which says what it does: move mail
  permanently deleted from Gmail out of the backup, into `_pruned/`, the name
  `restore --from-pruned` already used. `--sync` is no longer accepted.
- Long steps show their progress the same way everywhere: how many of how
  many, the rate, the time so far and about how long is left, on one line
  that a terminal rewrites in place, or logged about once a minute when
  stderr isn't a terminal. Reading Gmail before an apply and trashing,
  which showed nothing before, now show it too; `-q` turns it off. The
  estimate waits for ten seconds of work, and leaves out time spent waiting
  for Gmail's rate limit.
- `restore --id` and `export --id` find a message anywhere in the backup,
  not only among offloaded mail; before, a message that was never offloaded
  needed `--all` as well. `--offloaded --id` still looks only at offloaded
  mail. Mail Gmail still has is left alone, as with `--all`.
- The README is now a landing page: a short quick start, with the guides and
  the command reference on the documentation site.
- `report --help` lists `senders`, which it left out before.
- `report senders --help` no longer lists `--folder`, which the command fills in
  from the account.
- `offload apply --trash` and `dedup apply --trash` move messages to Trash a
  thousand per Gmail call (adding the `TRASH` label with `batchModify`)
  instead of one call per message, which Gmail held to about 5 a second.
  Each message is then read back, and only one Gmail shows in Trash is
  recorded as trashed. `restore --yes` moves offloaded messages out of Trash
  the same way, removing the label and reading each back.
- `backup --refresh-labels`, and a backup that finds labels changed on
  thousands of messages, read labels by listing the mailbox label by label,
  500 messages per Gmail call, instead of one lookup per message: about a
  minute for 50,000 messages instead of about 14.

### Removed
- `run.sh`, a wrapper around `mailcellar backup`, `count` and `meta`. Run
  `mailcellar` directly.

### Fixed
- `search` no longer stops with an SQLite error on a query containing a NUL
  character; the NUL counts as a space. Found by the new property tests.
- `offload apply` and `dedup apply` no longer stop the whole run when Gmail
  won't answer for a few messages: those are skipped, with the reason in the
  log, and the next run tries them again. Gmail's transient 400
  "Precondition check failed" is now retried like a rate limit.
- `offload apply --trash` reads from Gmail only the messages a `--label` run
  labelled, not every remaining plan row; with nothing labelled, it says so
  at once instead of reading the whole plan.

### Security
- mailcellar requires certifi 2026.10.19 or later, the CA bundle its
  connections to Google trust. Earlier versions trust emSign root
  certificates whose intermediates' keys were compromised
  ([GHSA-6q48-6c7w-9gw2](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2)),
  which would let someone on the network impersonate Google. Upgrade an
  existing install with `uv tool upgrade mailcellar`.
- The `gmail.modify` token, which can label and trash mail, is no longer
  stored. `dedup apply`, `offload apply` and `restore --yes` sign in when
  they run and hold the token in memory until they end, so a copy of the
  config folder can read the mailbox but not change it. A stored
  `token-modify.json` is used once more by the next of those commands, then
  deleted; if the account has no read-only token, it says to run
  `mailcellar auth ACCOUNT --scope readonly` before the next unattended
  backup. `keep_modify_token = true` in `settings.toml` stores it as before,
  and `auth --scope modify` only stores a token with that setting.
- Token files and `client_secrets.json` are owner-only from the moment they
  are written. Before, a token was written with the umask's permissions,
  often readable by other users, and made owner-only just after; replacing
  a client kept the old file's permissions until then. Each is now written
  to a new owner-only file and renamed into place, so an interrupted write
  also leaves the old file whole.

## 0.10.0 (2026-10-26)

### Added
- `mailcellar setup` adds an account from a downloaded OAuth client: it
  copies the client into the config folder, lists the account in
  `accounts.toml` and signs in, read-only.
- A documentation site on GitHub Pages, built from `docs/`.

### Changed
- Token files are named `token-<scope>.json`, not `dedup-token-<scope>.json`.
  mailcellar renames existing ones on first use.

## 0.9.0 (2026-10-25)

The first release, classified beta. It brings together:

- **Backup** in gyb's format, incremental by Gmail's change history, for
  several accounts, with `count`, `meta` and `verify`, and `backup --sync` to
  move mail permanently deleted from Gmail into `_pruned/`.
- **Scheduled backups** (`schedule install`, `status`, `remove`) and
  `backup --unattended`; a warning before Testing-status OAuth tokens expire,
  and `auth --renew`.
- **Deduplication** within and across accounts (`dedup index`, `match`,
  `plan`, `apply`), staged through a review label and Trash.
- **Offloading** old mail by rule (`offload plan`, `apply`), with review
  pages per rule and the space each batch freed in `report storage`.
- **Restore** of offloaded mail, whole backups, mail into another account,
  and mail from a pruned batch, an mbox file or another gyb folder; selections
  narrowed by date, label or sender.
- **Search** of the backups, offloaded mail included, and **export** as an
  mbox file or a zip.
- **Reports**: storage, bulk, volume, threads, labels, correspondents,
  health and senders.
- Guides for setting up Google Cloud, migrating from gyb, offloading,
  recovery and troubleshooting.
- The data root defaults to `~/mailcellar`, not `./data`.
