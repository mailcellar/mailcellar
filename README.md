# mailcellar

[![CI](https://github.com/mailcellar/mailcellar/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mailcellar/mailcellar/actions/workflows/ci.yml?query=branch%3Amain)
[![Docs](https://github.com/mailcellar/mailcellar/actions/workflows/docs.yml/badge.svg?branch=main)](https://mailcellar.github.io/mailcellar/)
[![PyPI](https://img.shields.io/pypi/v/mailcellar)](https://pypi.org/project/mailcellar/)
[![Python](https://img.shields.io/pypi/pyversions/mailcellar)](https://pypi.org/project/mailcellar/)
[![License](https://img.shields.io/github/license/mailcellar/mailcellar)](https://github.com/mailcellar/mailcellar/blob/main/LICENSE)

**Your own checked copy of your email, and room in your mailbox.**

Your mail lives on one server you don't control: a deletion, a
compromised account or a lock-out can take it, and it fills up.
mailcellar keeps an incremental, checked backup on your own disk, then
takes old mail out of the mailbox by rules you review, for space or a
tidier mailbox, keeping it searchable and restorable. It works with
Gmail, Google Workspace and any IMAP mailbox.

## Why mailcellar

- **Your mailbox is full, or just cluttered.** Offload old mail by rule
  (older than five years, newsletters, large attachments): it leaves the
  mailbox, so there's less to wade through and less for anyone who gets
  into the account to read, and `search` and `restore` still reach it
  ([Offloading old mail](https://mailcellar.github.io/mailcellar/guide/offloading/)).
- **You have no backup of your own.** A provider's redundancy doesn't
  cover a deletion, a compromised account or a lock-out, and its export
  starts from scratch each time. mailcellar fetches only what changed and
  checks the backup against the mailbox (`count`) and itself (`verify`)
  ([Scheduled backups](https://mailcellar.github.io/mailcellar/guide/scheduling/)).
- **Several providers, or moving.** One backup, search and set of reports
  across Gmail and IMAP; `restore --to` copies mail between accounts,
  labels to folders and back ([Restoring mail](https://mailcellar.github.io/mailcellar/guide/restore/)).
- **You use gyb.** Your folders work as they are, and gain offloading,
  search, scoped restore and checks ([Migrating from gyb](https://mailcellar.github.io/mailcellar/guide/from-gyb/)).
- **You run a Workspace domain.** Unattended, read-only backups of every
  mailbox through a service account ([Workspace](https://mailcellar.github.io/mailcellar/guide/workspace-service-account/)).
- **A dispute or a records request.** A legal hold keeps the
  correspondence out of reach of offloading and forgetting,
  `verify --chain` shows it unchanged, and `export` hands it over, or just its
  attachments ([Holds](https://mailcellar.github.io/mailcellar/guide/legal-holds/), [Exporting](https://mailcellar.github.io/mailcellar/guide/exporting/)).
- **Mail you mustn't keep.** `forget` removes it from the backup, and a
  rule's `forget_after` does so on a schedule ([Forgetting mail](https://mailcellar.github.io/mailcellar/reference/forget/)).

## What it does

- **Back up** every account incrementally, in
  [Got Your Back](https://github.com/GAM-team/got-your-back)'s format; an
  existing gyb backup carries on where it is.
- **Offload** old mail by rule, in stages you check: plan, review, mark,
  look in your mail client, then trash. The backup is checked for every
  message before it goes, and nothing is ever deleted permanently.
- **Find and restore**: search the backups, offloaded mail included, and
  put mail back into the same account or another.
- **Deduplicate, export and report**: remove duplicate copies within and
  across accounts, copy mail out (an mbox, a zip, a maildir, or just the
  attachments), and see what takes up the space.
- **Keep mail as it is**: a hash chain shows the backup hasn't changed
  since each message was saved (`verify --chain`), and a legal hold keeps
  the mail a dispute or an audit needs out of reach of offloading,
  forgetting and dedup, in the mailbox and in the backup.

**What changes your mailbox.** Only `offload apply`, `dedup apply` and
`restore --yes`, and each is a dry run until you add a flag. Offload and
dedup mark mail for review, then move it to Trash: nothing is ever
permanently deleted, and Gmail keeps Trash for 30 days. On an IMAP server
this happens only where the server can move mail safely; elsewhere
mailcellar backs up only. `restore` only puts mail back: out of Trash, or
as an upload. Everything else, `backup` included, only reads
([details](https://mailcellar.github.io/mailcellar/reference/what-changes-gmail/)).

**Your mail stays yours.** mailcellar runs on your own computer: there is
no service in between, and your mail goes nowhere but between the mailbox
and your disk. Gmail is read through your own Google Cloud project (about
ten minutes to set up,
[guide](https://mailcellar.github.io/mailcellar/guide/google-cloud/)); an
IMAP account needs only its password (an app password, where the provider
asks for one). mailcellar is in beta: it is used
on real mailboxes, and its backups are in gyb's fixed format, but commands
and options may still change before 1.0. It runs on macOS and Linux and
needs Python 3.14, which uv installs for you.

**Not for everyone.** It's a command-line tool for people who can set up a
Google Cloud project or an app password. It has no mail reader (export a
maildir or an mbox for one), doesn't run on Windows, and is one person's
backup, not an organization's archive
([how it compares](https://mailcellar.github.io/mailcellar/guide/troubleshooting/#how-does-it-compare-with-other-tools)).

**Not the Mac app of the same name.** mailcellar is free and open source,
installed from PyPI, with no paid edition and nothing else to download. A
paid macOS app sold as "Mailcellar" on newmac.app is unrelated to this
project
([how to tell](https://mailcellar.github.io/mailcellar/guide/troubleshooting/#is-this-the-mac-app-called-mailcellar)).

## Commands

```
mailcellar setup   [ADDRESS]       add a Gmail account (OAuth client) or an IMAP one (--imap)
mailcellar backup  [ACCOUNT ...]   incremental backups in gyb's format
mailcellar count   [ACCOUNT ...]   compare the mailbox with the backup
mailcellar meta    [ACCOUNT ...]   fill the per-message sidecar for older mail
mailcellar verify  [ACCOUNT ...]   check that the backup's files are still intact
mailcellar schedule install|status|remove   run backups regularly, unattended
mailcellar dedup   …               find duplicates and remove them from the mailbox, in stages
mailcellar offload …               take old mail out of the mailbox, keeping it in the backup
mailcellar restore ACCOUNT …       put backed-up mail back, into its account or another
mailcellar export  ACCOUNT …       copy mail out: an mbox, a zip, a maildir, or the attachments
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
anywhere on the line to also see the server's retries and rate-limit waits
(waits of 10 seconds or more show without it), or `-q` to see only warnings
and errors.
`search --format json` (or `tsv`) prints one result per line, for
scripts and pickers such as fzf.

How it fits together: [How it works](https://mailcellar.github.io/mailcellar/architecture/).

## Quick start

You need [uv](https://docs.astral.sh/uv/) (it provides Python 3.14).

1. **Install.**
   ```bash
   uv tool install mailcellar    # from PyPI; puts the mailcellar command on your PATH
   ```
   `pipx install mailcellar` works too, with Python 3.14. To run it from a
   clone instead, see [Development](#development).
2. **Add an account**, one of these two ways.

   **Gmail or Google Workspace, through the Gmail API.** Create an OAuth client in the
   [Google Cloud console](https://console.cloud.google.com/): a project with
   the **Gmail API** enabled, a consent screen with yourself as a test user,
   and an OAuth client of type **Desktop app**. Download its JSON. The step
   by step version, including *Testing* versus *In production* (Testing
   tokens expire after 7 days), is
   [Setting up Google Cloud](https://mailcellar.github.io/mailcellar/guide/google-cloud/).
   Then:
   ```bash
   mailcellar setup alex@example.org --client ~/Downloads/client_secret_*.json
   ```

   **Any mailbox over IMAP** (Gmail without a Cloud project, iCloud Mail,
   Yahoo Mail, Fastmail, your own server). Most providers want an app
   password, made in their account settings; give `setup` the server, and
   it asks for the password:
   ```bash
   mailcellar setup alex@example.org --imap imap.mail.yahoo.com
   ```

   | Provider | Server | Note |
   | --- | --- | --- |
   | iCloud Mail | `imap.mail.me.com` | `--username` with the part before `@`; no offload or dedup, restore by upload only (no IMAP MOVE) |
   | Yahoo Mail | `imap.mail.yahoo.com` | add `--review folder` to offload: Yahoo keeps no custom keywords |
   | Fastmail | `imap.fastmail.com` | not yet tried with mailcellar |
   | Gmail | `imap.gmail.com` | the quickest start: no Cloud project, only an app password (needs 2-Step Verification); [compared with the API](https://mailcellar.github.io/mailcellar/guide/imap/#gmail-over-imap-or-through-the-api) |

   More in [Backing up an IMAP account](https://mailcellar.github.io/mailcellar/guide/imap/).

   Either way, `setup` signs in read-only, changes nothing in the mailbox,
   and names the account after the address (`alex`; `--name` chooses
   another). The backups go in `~/mailcellar`; to keep them elsewhere, set
   `data_dir` in `~/.config/mailcellar/settings.toml`
   ([Configuration](https://mailcellar.github.io/mailcellar/reference/configuration/)).
3. **Back up, then check.**
   ```bash
   mailcellar config         # the config and data folders; each account's state
   mailcellar backup alex    # the first backup; resumes where it left off if stopped
   mailcellar count alex     # what the mailbox has against what the backup has
   ```
4. **Schedule it.**
   ```bash
   mailcellar schedule install     # macOS: a launchd agent, weekly; elsewhere: a cron line
   ```
   For Gmail, back up at least every four weeks: Gmail keeps change history
   about that long, and a longer gap lists the whole mailbox again. If its
   consent screen is in *Testing*, tokens expire after 7 days: publish it,
   or renew with `mailcellar auth alex --scope readonly --renew`. An IMAP
   account has no token to renew. See
   [Scheduled backups](https://mailcellar.github.io/mailcellar/guide/scheduling/).

Then [keep the backup safe](https://mailcellar.github.io/mailcellar/guide/backing-up/), and when you want old
mail out of the mailbox, [offload it](https://mailcellar.github.io/mailcellar/guide/offloading/).

## Documentation

The [documentation site](https://mailcellar.github.io/mailcellar/) has the guides and the reference.

**Guides**, in the order you will want them:

- [Setting up Google Cloud](https://mailcellar.github.io/mailcellar/guide/google-cloud/): the project and OAuth
  client mailcellar signs in with, and the first consent screen.
- [Backing up an IMAP account](https://mailcellar.github.io/mailcellar/guide/imap/): back up
  mail on another provider's server, or your own; offload, dedup and restore
  there too, where the server can move mail safely.
- [Workspace: backing up through a service account](https://mailcellar.github.io/mailcellar/guide/workspace-service-account/):
  unattended, read-only backups of a domain's mailboxes without each person signing in.
- [Backing up, and keeping the backup safe](https://mailcellar.github.io/mailcellar/guide/backing-up/): the
  routine, a second copy of the data root and checking it, `forget --deleted`.
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
- [Exporting mail](https://mailcellar.github.io/mailcellar/guide/exporting/): copy mail out as an mbox, a zip or a
  maildir, or just the attachments.
- [Keeping mail on hold](https://mailcellar.github.io/mailcellar/guide/legal-holds/): keep mail for a dispute or an
  audit, in Gmail and the backup, and hand it over.
- [Recovering when something goes wrong](https://mailcellar.github.io/mailcellar/guide/recovery/): an expired
  token, a long gap, an interrupted run, a lost data root.
- [Troubleshooting and FAQ](https://mailcellar.github.io/mailcellar/guide/troubleshooting/): slow runs, `count`
  results, chats, why not Takeout.

**Command reference**, every command and option:

- [Backups](https://mailcellar.github.io/mailcellar/reference/backup/): `backup`, `count`, `meta`, `verify`
- [Configuration](https://mailcellar.github.io/mailcellar/reference/configuration/): the config folder, `accounts.toml`, `settings.toml`; `setup`, `auth`, `schedule`, `config`
- [What changes your mailbox](https://mailcellar.github.io/mailcellar/reference/what-changes-gmail/): the commands that write to Gmail, or to an IMAP server
- [Removing duplicates](https://mailcellar.github.io/mailcellar/reference/dedup/): `dedup`
- [Offloading old mail](https://mailcellar.github.io/mailcellar/reference/offload/): `offload`, and the rule keys
- [Forgetting mail](https://mailcellar.github.io/mailcellar/reference/forget/): `forget`, and retention
- [Restoring mail](https://mailcellar.github.io/mailcellar/reference/restore/): `restore`
- [Exporting mail](https://mailcellar.github.io/mailcellar/reference/export/): `export`
- [Searching mail](https://mailcellar.github.io/mailcellar/reference/search/): `search`
- [Reports](https://mailcellar.github.io/mailcellar/reference/report/): `report`
- [What stays stable](https://mailcellar.github.io/mailcellar/reference/stability/): what 1.0
  promises not to break, and how old names are retired

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
