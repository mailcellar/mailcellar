---
description: "Reference for mailcellar's config folder, accounts.toml and settings.toml, and the setup, auth, schedule and config commands."
---

# Configuration

The config folder holds credentials, so keep it private (`chmod 700
~/.config/mailcellar`; token files are written owner-only) and out of any
dotfile manager or sync:

```
~/.config/mailcellar/
  accounts.toml            # config name → address
  settings.toml            # optional: data_dir, token_warn_days, keep_modify_token
  <name>/client_secrets.json
  <name>/token-readonly.json   # written on first use
  <name>/token-modify.json     # only with keep_modify_token = true
  anchors/<email>.json         # each backup's latest hash-chain link (verify --chain)
```

```toml
# accounts.toml: config name = the account's address (Gmail, Google Workspace or IMAP).
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
  dedup and offload plans and logs, and mail `forget` holds. It is
  `$MAILCELLAR_DATA`, else `data_dir` in `settings.toml`, else
  `~/mailcellar`. (It used to default to `./data` in the folder you ran the
  command from; a `./data` that already holds mailcellar's data is still
  used until `~/mailcellar` exists, and `mailcellar config` says so.)

`~` is expanded; a relative path is taken from the working directory.

```toml
# settings.toml
data_dir = "~/data/mailcellar"
token_warn_days = 5     # warn when a token is this many days past consent; 0 = never
keep_modify_token = false   # true stores the modify token too, as earlier versions did
```

**The modify token isn't stored.** On a Gmail account, `dedup apply`,
`offload apply` and `restore --yes` need `gmail.modify`, which can label and trash mail. Each
signs in when it runs and holds the token in memory until it ends, so no
file in the config folder can change your mailbox; the read-only token,
which unattended backups need, is stored as before. A `token-modify.json`
from an earlier version is used once more by the next command that needs
it, then deleted. mailcellar doesn't revoke it at Google: that would
revoke the read-only token too. `keep_modify_token = true` stores the
modify token like the read-only one, for when signing in on every run is
a burden.

To move existing backups, move the whole folder and set the new path. Then
run `mailcellar dedup index` (it records each backup's folder) and re-run
`mailcellar offload plan` (its plan holds `.eml` paths). `backup --folder`
still overrides the location for one account, but only the data root is
seen by every tool.

## Commands

### `setup`

Adds an account from a downloaded OAuth client: copies the client into the
config folder, lists the account in `accounts.toml`, and signs in read-only.
It asks for what it isn't given and changes nothing in the mailbox. Running it
again for the same account is harmless.

A Workspace address can be read through a service account instead
(`--service-account KEY`): the key is installed once for the domain, at
`<config>/service-accounts/<domain>.json`, owner-only, and the address is
marked to read through it (`<config>/<name>/service-account`), so its
backups need no sign-in; other addresses in the domain keep their own.
Read-only only; see
[Workspace: backing up through a service account](../guide/workspace-service-account.md).

An account on another provider's IMAP server (`--imap HOST`): setup asks
for its password, signs in, and says what the server allows, then writes
`<config>/<name>/imap.toml` (the server) and `<config>/<name>/imap-password`,
owner-only. Nothing on the server changes when it is backed up; offload and
dedup apply work where the server has MOVE and a special-use Trash, and
restore works into it too, by upload only where it has no MOVE
([Backing up an IMAP account](../guide/imap.md)).
Setting an account up again (for a new password, say) keeps the port, username, CA
file and review mode its `imap.toml` holds, unless an option changes them.
Where the server would allow offloading, setup also says how mail will be
marked for review, and whether the server keeps keywords in INBOX (a
SELECT that changes nothing); where it keeps none, it says to review by
folder, without choosing that for you. Microsoft's servers (Outlook.com,
Hotmail, Live, Microsoft 365) take only OAuth over IMAP, so setup refuses
them before asking for a password.

`setup ACCOUNT --review MODE`, without `--imap`, changes an IMAP account's
review mode alone: it changes the review line in `imap.toml`, keeping the
rest, asks for no password and doesn't connect. `ACCOUNT` is its config name or address. Mail already
marked the other way stays marked: offload and dedup read keywords and
review folders whatever the mode.

| Option | What it does |
| --- | --- |
| `ADDRESS` | The address to back up: Gmail or Workspace, or with `--imap` an IMAP account |
| `--client FILE` | The OAuth client JSON downloaded from Google (a Desktop app client) |
| `--service-account KEY` | A Workspace address: read it through this service-account key, granted domain-wide delegation for `gmail.readonly` (no sign-in) |
| `--imap HOST` | An IMAP account on this server (port 993, TLS): backed up read-only; offload, dedup and restore where the server can move mail safely |
| `--port PORT` | With `--imap`: the server's port (default 993) |
| `--username NAME` | With `--imap`: the login name (default: the address) |
| `--cafile FILE` | With `--imap`: a CA certificate to trust besides the system's, for a private server |
| `--review MODE` | How an IMAP account's offload and dedup mark mail for review, `keyword` (default) or `folder` (a `mailcellar/review/` folder); without `--imap`, changes an existing account's mode alone |
| `--name NAME` | Config name for the account (default: from the address) |
| `--no-auth` | Don't sign in now |
| `--force` | Replace the account's client, the domain's service-account key, or an IMAP account's `imap.toml`, if it holds another |
| `--config CONFIG` | Config folder |

### `auth`

Signs in, or renews consent. It changes nothing in the mailbox.

| Option | What it does |
| --- | --- |
| `ACCOUNT ...` | Accounts (default: every account in `accounts.toml`) |
| `--scope {readonly,modify}` | Required. `readonly` for backups, counts and reports; `modify` for `dedup apply`, `offload apply` and `restore --yes`, which only stores a token with `keep_modify_token = true` (without it, those commands sign in when they run) |
| `--renew` | Ask for consent again even if the token still works, which starts Google's 7 days afresh for a consent screen in *Testing* status |
| `--config CONFIG` | Config folder |

Without `--renew`, `auth` signs in only if the token no longer works.
Other commands that only read (backups, `count`, the reports) use a stored
modify token when there is no read-only one; `auth --scope readonly` always
makes a read-only token of its own, which they use from then on.
mailcellar records when consent was given, shows it in `config` and
`report health`, and warns when a token is `token_warn_days` (5) or more days
past consent, printing the `auth --renew` command. It can't see the consent
screen's publishing status, so the warning goes by the time since consent;
if your tokens don't expire, set `token_warn_days = 0` in `settings.toml`.
`dedup auth` still works, as an alias.

### `schedule`

`schedule install`, `schedule status` and `schedule remove`; see
[Scheduled backups](../guide/scheduling.md).

| Option (`install`) | What it does |
| --- | --- |
| `ACCOUNT ...` | Accounts to back up (default: every account in `accounts.toml`, including ones added later) |
| `--daily`, `--weekly` | How often; weekly is the default |
| `--at HH:MM` | Time of day (default 03:30) |
| `--weekday {sun,...,sat}` | With `--weekly`: the day (default `sun`) |
| `--print` | Show what would be installed, and install nothing |
| `--config CONFIG` | Config folder |

`schedule status` takes `--config`. `schedule remove` takes no options.

### `config`

Shows where the config and data live and each account's state. Changes
nothing and prints no credentials. It takes no options.

### Options every command takes

`-v` shows the mailbox's retries and Gmail's rate-limit waits, `-q` only warnings and
errors, and `--help` the command's options. `-v` and `-q` can go anywhere on
the line.

**Progress.** A step that takes more than a couple of seconds (downloading,
reading the mailbox's state before an apply, trashing, verifying, indexing for
`search`, exporting, restoring) shows how far it has got on stderr:

```
work@example.org: checking: 1,610 of 56,136 (2%), 201/s, 0:08 so far, about 4:31 left
```

On a terminal the line is rewritten in place and ends with how long the
step took. When stderr isn't a terminal (a scheduled run, or output sent to
a file) the line is logged about once a minute instead. The rate and the
time left go by the last half minute of work, since a mailbox's pace changes;
they appear after ten seconds, and time spent waiting for Gmail's rate
limit doesn't count as slowness. `-q` turns progress off.
