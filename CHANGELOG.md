# Changelog

What changed for users in each release. Releases and their dates are on the
[releases page](https://github.com/mailcellar/mailcellar/releases); the format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). mailcellar is
in beta, so commands and options may still change before 1.0.

## Unreleased

### Added
- A command reference on the documentation site, one page per command, with
  every option listed. A test fails when `--help` lists an option the
  reference doesn't name.
- Guides for backing up and keeping the backup safe, removing duplicate mail,
  restoring mail, and finding mail and deciding what to offload.
- `search --format tsv|json` prints one line per result, with the account and
  Gmail ID, for scripts and pickers such as fzf; the finding-mail guide has
  a recipe.

### Changed
- `restore --id` and `export --id` find a message anywhere in the backup,
  not only among offloaded mail; before, a message that was never offloaded
  needed `--all` as well. `--offloaded --id` still looks only at offloaded
  mail. Mail Gmail still has is left alone, as with `--all`.
- The README is now a landing page: a short quick start, with the guides and
  the command reference on the documentation site.
- `report --help` lists `senders`, which it left out before.
- `report senders --help` no longer lists `--folder`, which the command fills in
  from the account.

### Removed
- `run.sh`, a wrapper around `mailcellar backup`, `count` and `meta`. Run
  `mailcellar` directly.

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
