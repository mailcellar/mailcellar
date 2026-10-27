# mailcellar

[![CI](https://github.com/mailcellar/mailcellar/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mailcellar/mailcellar/actions/workflows/ci.yml?query=branch%3Amain)
[![Docs](https://github.com/mailcellar/mailcellar/actions/workflows/docs.yml/badge.svg?branch=main)](https://mailcellar.github.io/mailcellar/)
[![PyPI](https://img.shields.io/pypi/v/mailcellar)](https://pypi.org/project/mailcellar/)
[![Python](https://img.shields.io/pypi/pyversions/mailcellar)](https://pypi.org/project/mailcellar/)
[![License](https://img.shields.io/github/license/mailcellar/mailcellar)](https://github.com/mailcellar/mailcellar/blob/main/LICENSE)

**Gmail storage full? Move old mail out of Gmail without losing it.**
mailcellar keeps a local backup of your Gmail accounts. You can then take old
mail out of Gmail by rule while the backup keeps it, and it stays searchable,
exportable and restorable whenever you need it.

- **Back up** every account incrementally, in
  [Got Your Back](https://github.com/GAM-team/got-your-back)'s format; an
  existing gyb backup carries on where it is.
- **Offload** old mail by rule, in stages you check: plan, review, label,
  look in Gmail, then trash. Nothing is ever permanently deleted from Gmail,
  and the backup is checked for every message before it goes.
- **Find and restore** it later: `mailcellar search` searches the backups,
  offloaded mail included, and `mailcellar restore` puts mail back into
  Gmail, into the same account or another.
- **Deduplicate, export and report**: remove duplicate copies within and
  across accounts, copy mail out as an mbox or a zip, and see what takes up
  the space.
- **Keep mail as it is**: a hash chain shows the backup hasn't changed
  since each message was saved (`verify --chain`), and a legal hold keeps
  the mail a dispute or an audit needs out of reach of offloading, pruning
  and dedup, in Gmail and in the backup.

**What changes Gmail.** Only `dedup apply`, `offload apply` and
`restore --yes`, and each is a dry run until you add a flag. The first two
label mail, then move it to Trash, which Gmail empties after 30 days;
nothing is ever permanently deleted from Gmail. `restore` only adds mail.
Everything else, including `backup`, only reads
([details](https://mailcellar.github.io/mailcellar/reference/what-changes-gmail/)).

It runs on your own computer with your own Google Cloud project: there is no
service in between, and your mail goes nowhere but between Gmail and your
disk. Setting up the project takes about ten minutes
([guide](https://mailcellar.github.io/mailcellar/guide/google-cloud/)).
mailcellar is in beta: it is used on real mailboxes, and its backups are in
gyb's fixed format, but commands and options may still change before 1.0.
It runs on macOS and Linux and needs Python 3.14, which uv installs for you.

```
mailcellar setup   [ADDRESS]       add an account from a downloaded OAuth client
mailcellar backup  [ACCOUNT ...]   incremental backups in gyb's format
mailcellar count   [ACCOUNT ...]   compare Gmail with the backup
mailcellar meta    [ACCOUNT ...]   fill the per-message sidecar for older mail
mailcellar verify  [ACCOUNT ...]   check that the backup's files are still intact
mailcellar schedule install|status|remove   run backups regularly, unattended
mailcellar dedup   …               find duplicates and remove them from Gmail, in stages
mailcellar offload …               take old mail out of Gmail, keeping it in the backup
mailcellar restore ACCOUNT …       put backed-up mail back into Gmail
mailcellar export  ACCOUNT …       copy mail out as an mbox file or a zip of .eml files
mailcellar search  QUERY …         find mail in the backups, offloaded mail included
mailcellar hold    add|list|show|release   keep mail for a dispute or an audit
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

How it fits together: [How it works](https://mailcellar.github.io/mailcellar/architecture/).

## Quick start

You need [uv](https://docs.astral.sh/uv/) (it provides Python 3.14) and a
Google account.

1. **Install.**
   ```bash
   uv tool install mailcellar    # from PyPI; puts the mailcellar command on your PATH
   ```
   `pipx install mailcellar` works too, with Python 3.14. To run it from a
   clone instead, see [Development](#development).
2. **Create an OAuth client** in the [Google Cloud
   console](https://console.cloud.google.com/): a project with the **Gmail
   API** enabled, a consent screen with yourself as a test user, and an OAuth
   client of type **Desktop app**. Download its JSON. The step-by-step
   version, including *Testing* versus *In production* (Testing tokens
   expire after 7 days), is [Setting up Google Cloud](https://mailcellar.github.io/mailcellar/guide/google-cloud/).
3. **Add the account.** `mailcellar setup` takes the downloaded JSON, saves
   it in the config folder, lists the account in `accounts.toml` and signs
   in, read-only. It asks for what you don't give and changes nothing in
   Gmail:
   ```bash
   mailcellar setup alex@example.org --client ~/Downloads/client_secret_*.json
   ```
   The backups go in `~/mailcellar`. To keep them somewhere else, set it in
   `~/.config/mailcellar/settings.toml`:
   ```toml
   data_dir = "/Volumes/Archive/mailcellar"
   ```
   The files and their keys are in [Configuration](https://mailcellar.github.io/mailcellar/reference/configuration/).
4. **Back up, then check.**
   ```bash
   mailcellar config         # the config and data folders; the account's client and tokens
   mailcellar backup work    # the first backup; resumes where it left off if stopped
   mailcellar count work     # what Gmail has against what the backup has
   ```
5. **Schedule it.** Gmail keeps change history for about four weeks, so back
   up at least that often. A longer gap still works, but lists the whole
   mailbox again.
   ```bash
   mailcellar schedule install     # macOS: a launchd agent, weekly; elsewhere: a cron line
   ```
   If the consent screen is in *Testing*, tokens expire after 7 days:
   publish it, or renew with `mailcellar auth work --scope readonly --renew`.
   See [Scheduled backups](https://mailcellar.github.io/mailcellar/guide/scheduling/).

Then [keep the backup safe](https://mailcellar.github.io/mailcellar/guide/backing-up/), and when you are ready
to free space, [offload old mail](https://mailcellar.github.io/mailcellar/guide/offloading/).

## Documentation

The [documentation site](https://mailcellar.github.io/mailcellar/) has the guides and the reference.

**Guides**, in the order you will want them:

- [Setting up Google Cloud](https://mailcellar.github.io/mailcellar/guide/google-cloud/): the project and OAuth
  client mailcellar signs in with, and the first consent screen.
- [Workspace: backing up through a service account](https://mailcellar.github.io/mailcellar/guide/workspace-service-account/):
  unattended, read-only backups of a domain's mailboxes without each person signing in.
- [Backing up an IMAP account](https://mailcellar.github.io/mailcellar/guide/imap/): back up
  mail on another provider's server, or your own, read-only.
- [Backing up, and keeping the backup safe](https://mailcellar.github.io/mailcellar/guide/backing-up/): the
  routine, a second copy of the data root and checking it, `backup --prune`.
- [Scheduled backups](https://mailcellar.github.io/mailcellar/guide/scheduling/): run backups unattended, and
  what happens when a token needs signing in.
- [Migrating from gyb](https://mailcellar.github.io/mailcellar/guide/from-gyb/): continue an existing Got Your
  Back backup.
- [Finding mail, and deciding what to offload](https://mailcellar.github.io/mailcellar/guide/finding-and-sizing/):
  `search`, and which `report` answers what.
- [Offloading old mail safely](https://mailcellar.github.io/mailcellar/guide/offloading/): a worked example,
  from writing a rule to restoring.
- [Removing duplicate mail](https://mailcellar.github.io/mailcellar/guide/dedup/): a worked example, from
  matching to trashing.
- [Restoring mail](https://mailcellar.github.io/mailcellar/guide/restore/): undo an offload, recover one
  message, rebuild or copy an account, load a Takeout file.
- [Exporting mail](https://mailcellar.github.io/mailcellar/guide/exporting/): copy mail out as an mbox or a zip.
- [Keeping mail on hold](https://mailcellar.github.io/mailcellar/guide/legal-holds/): keep mail for a dispute or an
  audit, in Gmail and the backup, and hand it over.
- [Recovering when something goes wrong](https://mailcellar.github.io/mailcellar/guide/recovery/): an expired
  token, a long gap, an interrupted run, a lost data root.
- [Troubleshooting and FAQ](https://mailcellar.github.io/mailcellar/guide/troubleshooting/): slow runs, `count`
  results, chats, why not IMAP or Takeout.

**Command reference**, every command and option:

- [Backups](https://mailcellar.github.io/mailcellar/reference/backup/): `backup`, `count`, `meta`, `verify`
- [Configuration](https://mailcellar.github.io/mailcellar/reference/configuration/): the config folder, `accounts.toml`, `settings.toml`; `setup`, `auth`, `schedule`, `config`
- [What changes Gmail](https://mailcellar.github.io/mailcellar/reference/what-changes-gmail/): the commands that write to Gmail
- [Removing duplicates](https://mailcellar.github.io/mailcellar/reference/dedup/): `dedup`
- [Offloading old mail](https://mailcellar.github.io/mailcellar/reference/offload/): `offload`, and the rule keys
- [Restoring mail](https://mailcellar.github.io/mailcellar/reference/restore/): `restore`
- [Exporting mail](https://mailcellar.github.io/mailcellar/reference/export/): `export`
- [Searching mail](https://mailcellar.github.io/mailcellar/reference/search/): `search`
- [Reports](https://mailcellar.github.io/mailcellar/reference/report/): `report`

## Development

To run mailcellar from a clone, with the tests and tools:

```bash
git clone https://github.com/mailcellar/mailcellar.git
cd mailcellar
uv sync                                    # the dependencies, and mailcellar in editable mode
uv run mailcellar config                   # every command, run from the clone
uv run pytest                              # all tests; no network, never the real config or data
uv run pytest --cov                        # the same, with a branch-coverage report
uv run ruff format && uv run ruff check
```

The code is one package, `src/mailcellar/`: `cli.py` dispatches to
`backup/`, `dedup/`, `offload/`, `restore/` and `report/`, which share `paths.py`,
`gmailapi.py` and `message.py`. `uv sync` installs it in editable mode;
`python -m mailcellar` works too. See
[How it works](https://mailcellar.github.io/mailcellar/architecture/).

How to contribute, and the rules changes follow:
[CONTRIBUTING.md](https://github.com/mailcellar/mailcellar/blob/main/CONTRIBUTING.md).

## Acknowledgements

mailcellar's backups use the on-disk format of
[Got Your Back](https://github.com/GAM-team/got-your-back) (gyb) by the
GAM team, so backups made by either tool can be read by the other.
mailcellar is a separate project and shares no code with gyb.

mailcellar is not affiliated with or endorsed by Google or the gyb
project. Gmail and Google Workspace are trademarks of Google LLC.

## License

Apache License 2.0: see [LICENSE](https://github.com/mailcellar/mailcellar/blob/main/LICENSE) and [NOTICE](https://github.com/mailcellar/mailcellar/blob/main/NOTICE).
