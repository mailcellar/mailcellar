# Configuration

The config folder holds credentials, so keep it private (`chmod 700
~/.config/mailcellar`; token files are written owner-only) and out of any
dotfile manager or sync:

```
~/.config/mailcellar/
  accounts.toml            # config name → address
  settings.toml            # optional: data_dir, token_warn_days
  <name>/client_secrets.json
  <name>/token-readonly.json, token-modify.json   # written on first use
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
  `$MAILCELLAR_DATA`, else `data_dir` in `settings.toml`, else
  `~/mailcellar`. (It used to default to `./data` in the folder you ran the
  command from; a `./data` that already holds mailcellar's data is still
  used until `~/mailcellar` exists, and `mailcellar config` says so.)

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

## Commands

### `setup`

Adds an account from a downloaded OAuth client: copies the client into the
config folder, lists the account in `accounts.toml`, and signs in read-only.
It asks for what it isn't given and changes nothing in Gmail. Running it
again for the same account is harmless.

| Option | What it does |
| --- | --- |
| `ADDRESS` | The Gmail or Workspace address to back up |
| `--client FILE` | The OAuth client JSON downloaded from Google (a Desktop app client) |
| `--name NAME` | Config name for the account (default: from the address) |
| `--no-auth` | Don't sign in now |
| `--force` | Replace the account's client if it holds another |
| `--config CONFIG` | Config folder |

### `auth`

Signs in, or renews consent. It changes nothing in the mailbox.

| Option | What it does |
| --- | --- |
| `ACCOUNT ...` | Accounts (default: every account in `accounts.toml`) |
| `--scope {readonly,modify}` | Required. `readonly` for backups, counts and reports; `modify` for `dedup apply`, `offload apply` and `restore --yes` |
| `--renew` | Ask for consent again even if the token still works, which starts Google's 7 days afresh for a consent screen in *Testing* status |
| `--config CONFIG` | Config folder |

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

`-v` shows Gmail's retries and rate-limit waits, `-q` only warnings and
errors, and `--help` the command's options. `-v` and `-q` can go anywhere on
the line.
