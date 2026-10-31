# What stays stable

From 1.0, mailcellar follows [semantic versioning](https://semver.org/):
what this page lists changes incompatibly only in a new major version
(2.0). A minor version (1.1) adds; a patch version (1.0.1) fixes. Until
1.0, minor versions may still change what is listed here, and the
[changelog](https://github.com/mailcellar/mailcellar/blob/main/CHANGELOG.md)
says so under **Changed** or **Removed**.

## Stable

- **Commands and options.** Their names, what they do and the values they
  take, as the [reference](index.md) describes them: a date is
  `YYYY-MM-DD`, a time ago `90d`, `3w`, `6m` or `2y`. A command that changes your mailbox
  keeps its safeguards: a dry run unless told otherwise, and the checks
  listed in [What changes your mailbox](what-changes-gmail.md).
- **Exit status.** 0 when everything worked; 1 when something failed or
  was skipped, as each command's page says; 2 for a command line
  mailcellar can't read.
- **Configuration.** `accounts.toml`, `settings.toml` and their keys, an
  IMAP account's `imap.toml` and its keys, where a Workspace domain's
  service-account key goes (`service-accounts/<domain>.json`), the
  offload rules file and its keys, `$MAILCELLAR_CONFIG` and
  `$MAILCELLAR_DATA`, and where the config folder and the data root are
  found.
- **The backup.** Each account's backup stays in
  [gyb](https://github.com/GAM-team/got-your-back)'s format, byte for byte:
  gyb can read it, and mailcellar reads gyb's. An IMAP account's backup
  is in the same format, with message IDs of mailcellar's own. Each
  backup's hash chain (`chain.jsonl`) stays readable, so a later version's
  `verify --chain` checks the links an earlier one wrote.
- **The data root's layout.** Where backups, plans, logs, manifests and
  `_forgotten/` live.
- **Upgrades are automatic.** A new version migrates the backup's
  databases (`meta.sqlite`, the dedup catalogue and the search index) the
  first time it opens them, keeps a copy of each `meta.sqlite` beside it
  (`meta.sqlite.pre-vN`), and never needs a step from you. A migration only
  goes forward: an older version won't open a database a newer one has
  migrated, so go back to the copy if you need to.
- **Files meant for scripts.** `search --format tsv` and `json`, an
  `export` zip's `index.csv`, the offload and dedup plans, the apply and
  restore logs, and the batch manifests keep their columns. A new column is
  added at the end and never changes what the others mean.

## Not stable

- The wording and layout of what is meant for people to read: reports
  (their `--csv` columns included), notes, progress lines, warnings and the
  review pages. Scripts should read the files above, not these.
- mailcellar's Python modules, the `Mailbox` interface a provider
  implements among them. Use the `mailcellar` command; importing the
  package isn't supported.

## How things are retired

Something stable that is renamed or replaced keeps working under its old
name for at least one minor version after its replacement ships, and says
so when it is used. It is removed only in the next major version, with a
**Removed** entry in the changelog that names the replacement. Before 1.0,
`dedup auth` became `auth` this way: it was kept from 0.9.0 to 0.21.0 and
removed in a minor version, as 0.x allowed.

A fix may refuse a value that was accepted but did something other than
it says: 0.21.0 refused a decimal number of years, which it had cut to
whole years (`before = "0.5"` meant now). That is a **Fixed** entry,
saying what to write instead, not a breaking change: keeping the value
working would keep the harm.
