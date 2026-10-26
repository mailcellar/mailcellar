# Changelog

What changed for users in each release. Releases and their dates are on the
[releases page](https://github.com/mailcellar/mailcellar/releases); the format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). mailcellar is
in beta, so commands and options may still change before 1.0.

## Unreleased

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
