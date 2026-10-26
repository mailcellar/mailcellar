# mailcellar

[![CI](https://github.com/mailcellar/mailcellar/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mailcellar/mailcellar/actions/workflows/ci.yml?query=branch%3Amain)

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

It runs on your own computer with your own Google Cloud project: there is no
service in between, and your mail goes nowhere but between Gmail and your
disk. Setting up the project takes about ten minutes
([guide](https://github.com/mailcellar/mailcellar/blob/main/docs/guide/google-cloud.md)).
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

How it fits together: [docs/architecture.md](https://github.com/mailcellar/mailcellar/blob/main/docs/architecture.md).

## Guides

Also on the documentation site, <https://mailcellar.github.io/mailcellar/>.

- [Setting up Google Cloud](https://github.com/mailcellar/mailcellar/blob/main/docs/guide/google-cloud.md): the project and
  OAuth client mailcellar signs in with, and the first consent screen.
- [Backing up, and keeping the backup safe](https://github.com/mailcellar/mailcellar/blob/main/docs/guide/backing-up.md): the routine
  (`count`, `verify`, `report health`), a second copy of the data root and
  checking it, and `backup --sync`.
- [Scheduled backups](https://github.com/mailcellar/mailcellar/blob/main/docs/guide/scheduling.md): run backups regularly, unattended,
  and what happens when a token needs signing in.
- [Migrating from gyb](https://github.com/mailcellar/mailcellar/blob/main/docs/guide/from-gyb.md): continue an existing Got Your Back
  backup instead of starting again.
- [Offloading old mail safely](https://github.com/mailcellar/mailcellar/blob/main/docs/guide/offloading.md): a worked example, from
  writing a rule to restoring.
- [Exporting mail](https://github.com/mailcellar/mailcellar/blob/main/docs/guide/exporting.md): copy mail out as an mbox or a zip, for a
  mail client or for someone else.
- [Recovering when something goes wrong](https://github.com/mailcellar/mailcellar/blob/main/docs/guide/recovery.md): an expired token,
  a long gap between backups, an interrupted run, a lost data root.
- [Troubleshooting and FAQ](https://github.com/mailcellar/mailcellar/blob/main/docs/guide/troubleshooting.md): slow runs, `count` results,
  chats, weekly sign-in, why not IMAP or Takeout.

## Quick start

You need [uv](https://docs.astral.sh/uv/) (it provides Python 3.14) and a
Google account.

1. **Install.**
   ```bash
   uv tool install mailcellar    # from PyPI; puts the mailcellar command on your PATH
   ```
   `pipx install mailcellar` works too, with Python 3.14. To run it from a
   clone instead, see [Development](#development).
2. **Create an OAuth client.** In the [Google Cloud
   console](https://console.cloud.google.com/), create a project, enable the
   **Gmail API**, set up the OAuth consent screen with yourself as a test
   user, and create an OAuth client of type **Desktop app**. Download its
   JSON. One client can serve several accounts: copy the same JSON into each
   account's folder. The step-by-step version, including *Testing* versus
   *In production* (Testing tokens expire after 7 days), is
   [Setting up Google Cloud](https://github.com/mailcellar/mailcellar/blob/main/docs/guide/google-cloud.md).
3. **Add the account.** `mailcellar setup` takes the downloaded JSON,
   checks that it is a Desktop app client, saves it as
   `~/.config/mailcellar/<name>/client_secrets.json`, lists the account in
   `accounts.toml` and signs in, read-only. It asks for what you don't give:
   ```bash
   mailcellar setup alex@example.org --client ~/Downloads/client_secret_*.json
   ```
   It changes nothing in Gmail, and a second run for the same account is
   harmless (`--name` chooses the config name, `--no-auth` skips the
   sign-in, `--force` replaces a different client). To do it by hand,
   save the JSON and list the account yourself (see
   [Configuration](https://mailcellar.github.io/mailcellar/reference/configuration/)):
   ```toml
   work = "alex@example.org"
   ```
   The backups go in `~/mailcellar`. To keep them somewhere else, such as
   a larger disk, set it in `~/.config/mailcellar/settings.toml`:
   ```toml
   data_dir = "/Volumes/Archive/mailcellar"
   ```
4. **Check, then back up.** (After `setup` you are already signed in.)
   ```bash
   mailcellar config         # the config and data folders; the account's client and tokens
   mailcellar backup work    # opens a browser once for Google's consent screen, if not yet
   mailcellar count work     # what Gmail has against what the backup has
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
mailcellar schedule install     # macOS: a launchd agent, weekly; elsewhere: a cron line
mailcellar schedule status      # installed? each account's last backup and token
mailcellar schedule remove
```

The job runs `backup --unattended`, which never opens a browser: a token that
needs signing in fails that account's run, with the command to fix it, and is
recorded for `report health`. See [Scheduled backups](https://github.com/mailcellar/mailcellar/blob/main/docs/guide/scheduling.md).

**Tokens and the consent screen.** If the consent screen's publishing
status is *Testing*, Google expires its tokens after 7 days, and the next
backup asks you to sign in again. Either set the status to *In production*
or renew access before it runs out:

```bash
mailcellar auth work --scope readonly --renew   # or --scope modify for apply and restore
```

`auth` asks for consent again (`--renew`), which starts the 7 days afresh;
without `--renew` it only signs in if the token no longer works. mailcellar
records when consent was given, shows it in `mailcellar config` and
`report health`, and warns when a token is 5 days old or more, printing this
command. It can't see the consent screen's status, so the warning goes by
the time since consent; if your tokens don't expire, turn it off with
`token_warn_days = 0` in `settings.toml`. (`mailcellar dedup auth` still
works, as an alias.)

## Command reference

Every command and option, on the [documentation site](https://mailcellar.github.io/mailcellar/reference/):

- [Backups](https://mailcellar.github.io/mailcellar/reference/backup/): `backup`, `count`, `meta`, `verify`
- [Configuration](https://mailcellar.github.io/mailcellar/reference/configuration/): the config folder, `accounts.toml`, `settings.toml`
- [What changes Gmail](https://mailcellar.github.io/mailcellar/reference/what-changes-gmail/): the few commands that write to Gmail
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
[docs/architecture.md](https://github.com/mailcellar/mailcellar/blob/main/docs/architecture.md).

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
