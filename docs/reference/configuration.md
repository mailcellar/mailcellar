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
