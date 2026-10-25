# mailcellar

Local backups of Gmail accounts, kept as an archive: remove duplicate
messages within and across accounts, move old mail out of Gmail while the
backup keeps it, put it back when it's needed, and report on what's there.

```
mailcellar backup  [ACCOUNT ...]   incremental backups in gyb's format
mailcellar count   [ACCOUNT ...]   compare Gmail with the backup
mailcellar meta    [ACCOUNT ...]   fill the per-message sidecar for older mail
mailcellar verify  [ACCOUNT ...]   check that the backup's files are still intact
mailcellar schedule install|status|remove   run backups regularly, unattended
mailcellar dedup   …               find duplicates and remove them from Gmail, in stages
mailcellar offload …               take old mail out of Gmail, keeping it in the backup
mailcellar restore ACCOUNT …       put backed-up mail back into Gmail
mailcellar report  …               storage, bulk mail, volume, threads, labels, correspondents,
                                   backup health, top senders
mailcellar config                  where the config and data live; each account's state
```

`backup`, `count` and `meta` take every configured account unless given
some (an address, a config name like `work`, or a unique part of an
address). Each command takes `--help`.

Results go to stdout; progress, warnings and errors go to stderr. Add `-v`
anywhere on the line to also see Gmail's retries and rate-limit waits (waits
of 10 seconds or more show without it), or `-q` to see only warnings and
errors.

How it fits together: [docs/architecture.md](docs/architecture.md).

## Guides

- [Setting up Google Cloud](docs/guide/google-cloud.md): the project and
  OAuth client mailcellar signs in with, and the first consent screen.
- [Scheduled backups](docs/guide/scheduling.md): run backups regularly, unattended,
  and what happens when a token needs signing in.
- [Migrating from gyb](docs/guide/from-gyb.md): continue an existing Got Your Back
  backup instead of starting again.
- [Offloading old mail safely](docs/guide/offloading.md): a worked example, from
  writing a rule to restoring.
- [Recovering when something goes wrong](docs/guide/recovery.md): an expired token,
  a long gap between backups, an interrupted run, a lost data root.
- [Troubleshooting and FAQ](docs/guide/troubleshooting.md): slow runs, `count` results,
  chats, weekly sign-in, why not IMAP or Takeout.

## Quick start

You need [uv](https://docs.astral.sh/uv/) (it provides Python 3.14, as set
in `.python-version`) and a Google account.

1. **Install.**
   ```bash
   git clone https://github.com/mailcellar/mailcellar.git
   cd mailcellar
   uv sync                  # the dependencies, and the mailcellar command
   ```
2. **Create an OAuth client.** In the [Google Cloud
   console](https://console.cloud.google.com/), create a project, enable the
   **Gmail API**, set up the OAuth consent screen with yourself as a test
   user, and create an OAuth client of type **Desktop app**. Download its
   JSON. One client can serve several accounts: copy the same JSON into each
   account's folder. The step-by-step version, including *Testing* versus
   *In production* (Testing tokens expire after 7 days), is
   [Setting up Google Cloud](docs/guide/google-cloud.md).
3. **Configure the account.** Save the JSON as
   `~/.config/mailcellar/<name>/client_secrets.json` and list the account
   in `~/.config/mailcellar/accounts.toml` (see
   [Configuration](#configuration)):
   ```toml
   work = "alex@example.org"
   ```
4. **Check, then back up.**
   ```bash
   uv run mailcellar config         # the config and data folders; the account's client and tokens
   uv run mailcellar backup work    # opens a browser once for Google's consent screen
   uv run mailcellar count work     # what Gmail has against what the backup has
   ```

The first backup lists every message and downloads it. For a large
mailbox that takes a while; if it stops, re-running carries on where it
left off. Later backups read
Gmail's change history and take seconds.

**Back up at least every four weeks.** Gmail keeps change history for
about four weeks. A backup after a longer gap still works, but it lists the
whole mailbox again instead of reading the changes. A monthly or weekly
scheduled job is enough, and mailcellar can set one up:

```bash
uv run mailcellar schedule install     # macOS: a launchd agent, weekly; elsewhere: a cron line
uv run mailcellar schedule status      # installed? each account's last backup and token
uv run mailcellar schedule remove
```

The job runs `backup --unattended`, which never opens a browser: a token that
needs signing in fails that account's run, with the command to fix it, and is
recorded for `report health`. See [Scheduled backups](docs/guide/scheduling.md).

**Tokens and the consent screen.** If the consent screen's publishing
status is *Testing*, Google expires its tokens after 7 days, and the next
backup asks you to sign in again. Either set the status to *In production*
or renew access before it runs out:

```bash
uv run mailcellar auth work --scope readonly --renew   # or --scope modify for apply and restore
```

`auth` asks for consent again (`--renew`), which starts the 7 days afresh;
without `--renew` it only signs in if the token no longer works. mailcellar
records when consent was given, shows it in `mailcellar config` and
`report health`, and warns when a token is 5 days old or more, printing this
command. It can't see the consent screen's status, so the warning goes by
the time since consent; if your tokens don't expire, turn it off with
`token_warn_days = 0` in `settings.toml`. (`mailcellar dedup auth` still
works, as an alias.)

## What changes Gmail

Everything is read-only except these, and each one asks for confirmation
or stages its changes:

| Command | What it changes | Access |
|---|---|---|
| `dedup apply --label` / `offload apply --label` | adds a review label to the planned messages | modify |
| `dedup apply --trash` / `offload apply --trash` | moves labelled messages to Gmail's Trash | modify |
| `auth ACCOUNT --scope modify` | grants modify access (changes nothing in the mailbox) | modify |
| `restore ACCOUNT … --yes` | moves offloaded mail out of Trash, or uploads backed-up mail Gmail no longer has (into this account or, with `--to`, another); adds a `restored/<date>` label | modify |
| `backup --sync` | the *backup*: moves mail permanently deleted from Gmail into `<data root>/_pruned/` | read-only |

- **Nothing is ever permanently deleted from Gmail.** Removals go to Trash,
  after a review label. Removing the label from a message vetoes it.
  Gmail empties its Trash after 30 days; until then, a message can be moved
  back from Trash in Gmail.
- **Backups only add.** `backup`, `count` and `meta` never delete anything.
  The one exception is `--sync`, and it moves files to `_pruned/` rather
  than deleting them, behind a confirmation and a 10% guard.
- **Offloaded mail stays in the backup.** Once Gmail empties its Trash, the
  backup is the only copy, and `restore` uploads it from there.
  `offload apply --trash` reports first whether Time Machine covers the
  data root. Keep a second copy of the data root.
- **Restore only adds.** It deletes nothing, in Gmail or in the backup,
  and skips what Gmail already has.

Backups need only read access. `dedup apply`, `offload apply` and
`restore --yes` ask for modify access to the account they change.

## Configuration

The config folder holds credentials, so keep it private (`chmod 700
~/.config/mailcellar`; token files are written owner-only) and out of any
dotfile manager or sync:

```
~/.config/mailcellar/
  accounts.toml            # config name → address
  settings.toml            # optional: data_dir, token_warn_days
  <name>/client_secrets.json
  <name>/dedup-token-readonly.json, dedup-token-modify.json   # written on first use
```

```toml
# accounts.toml: config name = the account's Gmail or Google Workspace address.
# The name is the folder in the config folder holding that account's OAuth
# files, and what you type on the command line (`mailcellar backup work`).
personal = "alex@example.com"
work = "alex.lee@example.org"
```

**Where things are.** `mailcellar config` shows both folders, where each
setting came from, and every account's OAuth client, tokens, message
count and last backup. It never prints a credential.

- **Config folder:** `$MAILCELLAR_CONFIG`, else `~/.config/mailcellar`
  (`$XDG_CONFIG_HOME` is honoured), else `./config` in the project, the old
  place.
- **Data root:** everything derived from mail, that is the backups, the
  dedup and offload plans and logs, and `_pruned/`. It is
  `$MAILCELLAR_DATA`, else `data_dir` in `settings.toml`, else `./data`.

`~` is expanded; a relative path is taken from the working directory.

```toml
# settings.toml
data_dir = "~/data/mailcellar"
token_warn_days = 5     # warn when a token is this many days past consent; 0 = never
```

To move existing backups, move the whole folder and set the new path. Then
run `mailcellar dedup index` (it records each backup's folder) and re-run
`mailcellar offload plan` (its plan holds `.eml` paths). `backup --folder`
still overrides the location for one account, but only the data root is
seen by every tool.

## Backups

```bash
uv run mailcellar backup                  # every account, incremental
uv run mailcellar backup personal         # one account
uv run mailcellar count                   # compare Gmail with the backups
uv run mailcellar backup personal --full  # list every message instead of using history
uv run mailcellar backup --unattended     # for scheduled runs: never opens a browser
uv run mailcellar backup personal --sync  # also move mail permanently deleted from Gmail
                                          # (not offloaded mail) into <data root>/_pruned/
uv run mailcellar meta                    # fill meta.sqlite for older mail
uv run mailcellar verify                  # are the backup's files still intact?
```

**Exit status** (for scripts and schedulers): 0 when everything worked.
Otherwise 1, in any of these cases:
- an account failed (with several accounts, the others still run);
- downloads failed (the next run fetches them), or `--sync` stopped at its
  guard;
- `count` found mail missing from the backup;
- `verify` found a problem.

`restore` exits 1 when any message was skipped or failed.

**Checking the backup.** Once mail is offloaded and Gmail has emptied its
Trash, the backup holds the only copy, so `verify` looks for damage that
would otherwise go unnoticed: a failing disk, a sync tool rewriting files,
an accidental delete. It needs no Gmail access and changes nothing (both
databases are opened read-only).

```bash
uv run mailcellar verify                  # every backup, every message
uv run mailcellar verify work --offloaded # only offloaded mail: the copies with no other source
uv run mailcellar verify --sample 500     # 500 random messages: quick enough for a routine check
```

For each message it checks that the `.eml` exists and parses, and that its
content hash matches the one `meta.sqlite` recorded. It also checks that
`msg-db.sqlite` and `meta.sqlite` agree on which messages exist, and, with
`--offloaded`, that every offloaded message is in the backup. Problems are
listed (`--top N` sets how many) and the exit status is 1 if there are any.
A message with no current `meta.sqlite` row has nothing to compare with: it
is counted as unchecked, with a warning, and `mailcellar meta` fills it in.
A full run parses every message, so it takes a while on a large backup;
progress goes to stderr.

Backups live in `<data root>/<email>/`, in the format of
[gyb](https://github.com/GAM-team/got-your-back), so other tools can read
them too. [`mailcellar restore`](#restoring-mail) puts mail back into Gmail.

## Removing duplicates

```bash
uv run mailcellar dedup index                     # catalogue the backups
uv run mailcellar dedup match                     # report duplicate groups
uv run mailcellar dedup plan --keep origin        # across accounts: drop forwarded copies
uv run mailcellar dedup plan --scope within       # within each account
uv run mailcellar dedup apply                     # dry run against live Gmail
uv run mailcellar dedup apply --label --limit 5   # label 5 removals for review
uv run mailcellar dedup apply --trash --limit 5   # move labelled ones to Trash
```

Review the plan (`<data root>/dedup/dedup-plan.csv`) and the labelled
messages in Gmail before trashing; removing the label vetoes a message.
Each run is logged in `<data root>/dedup/dedup-apply-log.csv`. How
copies are matched and which one is kept:
[docs/architecture.md](docs/architecture.md#deduplication).

## Offloading old mail

Rules go in `<data root>/offload/rules.toml`, one `[[rule]]` table each.
The keys are documented in `src/mailcellar/offload/rules.py`:

```toml
[[rule]]
name = "old work mail"
account = "work"
label = "work"           # or sender, list, forwarded_from, category, subject, to_me,
                         # larger_than_mb; different keys must all match
before = "2022-01-01"    # or a number of years ago, e.g. "5"
# except = { subject = "contract", to_me = true }   # keep what matches any of these
# waive = ["thread"]     # drop default protections (sent, starred, thread, replied, labels)
# protect = ["correspondents"]
```

```bash
uv run mailcellar offload plan                    # all accounts → <data root>/offload/offload-plan.csv
uv run mailcellar offload plan personal --top 30 --csv ~/reports/offload
```

`plan` changes nothing. It prints what each rule would offload (by year and
sender) and what its protections keep. To keep particular messages, add a
user label in Gmail and run a backup.

```bash
uv run mailcellar offload apply                       # dry run: Gmail and backup checks
uv run mailcellar offload apply --label --limit 2000  # label the next 2,000 offload-candidate
uv run mailcellar backup personal                     # trash needs a backup newer than the plan
uv run mailcellar offload apply --trash --limit 2000  # trash the next 2,000 labelled ones
```

Repeated runs with `--limit` move on through the plan. Removing the label
in Gmail vetoes a message for good. Trashed messages are recorded as
offloaded, so `backup --sync` keeps them in the backup after Gmail empties
its Trash.

**How long it takes:**
- labelling is fast: thousands of messages a minute;
- trashing takes one Gmail call per message, about 4 a second, so 4,500
  messages take about 17 minutes;
- run large batches in the background, or in batches with `--limit`.

Each trash run writes a manifest,
`<data root>/offload/batches/<batch>/offloaded.csv`: every message it
offloaded, with its `.eml` path, date, sender, subject and rule. Every plan
is also kept in `offload/plans/`, and the apply log names the plan each run
used. To put offloaded mail back, see [Restoring mail](#restoring-mail).

## Restoring mail

`restore` puts backed-up mail back into Gmail: offloaded messages, or a
whole backup, into its own account or another configured one. Without
`--yes` it is a dry run, which checks the backup and Gmail and says what
it would do.

```bash
uv run mailcellar restore personal --batch 2026-01-15-093000   # dry run of one offload batch
uv run mailcellar restore personal --batch 2026-01-15-093000 --yes
uv run mailcellar restore personal --rule "old work mail" --limit 100 --yes
uv run mailcellar restore personal --id 18c0ffee0000abcd --yes
uv run mailcellar restore personal --offloaded                 # everything offloaded
uv run mailcellar restore personal --all                       # the whole backup
uv run mailcellar restore work --all --to personal --label-prefix work   # into another account
```

Batch names are the folders in `<data root>/offload/batches/`. `--limit`
takes the oldest messages first, and repeated runs carry on where the
last one stopped.

**Narrowing a selection.** Any of the selections above can be narrowed, so
"last year's mail from one sender" needs no ID lookup first:

```bash
uv run mailcellar restore personal --all --after 2025-01-01 --before 2026-01-01 --sender @example.com
uv run mailcellar restore personal --offloaded --has-label projects --sender alex@example.org
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
- **never offloaded and still in Gmail** (with `--all`): left alone;
- **gone, but Gmail has the same Message-ID and content** under another
  ID: recorded as that message;
- **gone:** uploaded from the backup with its labels, apart from those
  Gmail won't take (Trash, Spam, chat, superstars) and the offload and
  dedup labels. The upload keeps the original date; Gmail threads it by
  its headers.

Restored messages lose the offload label and gain `restored/<date>`
(`--label NAME` to choose another), so a restore can be found and undone.
The backup indexes each new Gmail ID, sharing the existing `.eml`, so the
next backup doesn't download it again. A message whose Message-ID Gmail
already holds with *other* content is skipped: Gmail would match the
upload to that message instead of storing this one.

**Into another account** (`--to`): every message is looked up by
Message-ID there, and new IDs go into that account's backup. It needs
modify access to that account (`mailcellar auth ACCOUNT --scope
modify`). `--label-prefix P` nests the backed-up user labels under `P/`;
`--strip-labels` restores none, so the messages arrive archived with only
the `restored/` label.

**How long it takes:** the dry run of a whole 56,000-message backup took
about 6 minutes, mostly checking the `.eml` files; 8,000 messages into
another account took about 3. Uploads run at about 6 a second, so the dry
run's estimate for 4,500 messages is about 13 minutes and for 50,000 over
two hours. Progress lines go to stderr (`-q` turns them off); run
large restores in the background.

Runs are logged in `<data root>/restore/restore-log.csv`, and each
restored message is recorded in its backup's `meta.sqlite`. How it works:
[docs/architecture.md](docs/architecture.md#restore).

## Reports

```bash
uv run mailcellar report storage                 # what takes up space, every account
uv run mailcellar report storage personal --csv ~/reports/storage
uv run mailcellar report bulk                    # mailing lists and bulk senders; unsubscribe candidates
uv run mailcellar report volume                  # received, sent and forwarded mail over time
uv run mailcellar report threads                 # thread sizes; threads in both accounts
uv run mailcellar report labels                  # user labels per account; unlabelled mail
uv run mailcellar report correspondents          # who you write to and hear from; reply rates
uv run mailcellar report health                  # backup completeness, runs, gaps, pruned batches
uv run mailcellar report senders personal --top 30  # top senders and domains in one backup
```

Reports read only the local backups, never Gmail. Each takes `--help`,
and prints notes on what its columns mean.

## Development

```bash
uv run pytest                              # all tests; no network, never the real config or data
uv run pytest --cov                        # the same, with a branch-coverage report
uv run ruff format && uv run ruff check
```

The code is one package, `src/mailcellar/`: `cli.py` dispatches to
`backup/`, `dedup/`, `offload/`, `restore/` and `report/`, which share `paths.py`,
`gmailapi.py` and `message.py`. `uv sync` installs it in editable mode;
`python -m mailcellar` works too. See
[docs/architecture.md](docs/architecture.md).

How to contribute, and the rules changes follow:
[CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgements

mailcellar's backups use the on-disk format of
[Got Your Back](https://github.com/GAM-team/got-your-back) (gyb) by the
GAM team, so backups made by either tool can be read by the other.
mailcellar is a separate project and shares no code with gyb.

mailcellar is not affiliated with or endorsed by Google or the gyb
project. Gmail and Google Workspace are trademarks of Google LLC.

## License

Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE).
