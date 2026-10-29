---
description: "Reference for mailcellar dedup: every option of index, match, plan and apply, for finding and removing duplicate email."
---

# Removing duplicates: dedup

```bash
mailcellar dedup index                     # catalogue the backups
mailcellar dedup match                     # report duplicate groups
mailcellar dedup plan --keep origin        # across accounts: drop forwarded copies
mailcellar dedup plan --scope within       # within each account
mailcellar dedup apply                     # dry run against the live mailbox
mailcellar dedup apply --label --limit 5   # label 5 removals for review
mailcellar dedup apply --trash --limit 5   # move labelled ones to Trash
```

Review the plan (`<data root>/dedup/dedup-plan.csv`) and the labelled
messages in the mailbox before trashing; removing the label vetoes a message.
Each run is logged in `<data root>/dedup/dedup-apply-log.csv`. How
copies are matched and which one is kept:
[docs/architecture.md](../architecture.md#deduplication).

On an IMAP account (one the server lets mailcellar move mail on), the
label is a `$mailcellar-` keyword, or with `setup --review folder` a move
into `mailcellar/review/<label>`: clearing the keyword, or moving the
message out, vetoes it. Trash is the folder the server marks `\Trash`.
See [Backing up an IMAP account](../guide/imap.md#3-offload-and-remove-duplicates).

## Options

The index, plan and log default to files in `<data root>/dedup/`.

### `dedup index`

| Option | What it does |
| --- | --- |
| `FOLDER ...` | Backup folders to catalogue (default: every backup in the data root) |
| `--db DB` | Index database path (default `<data root>/dedup/dedup.sqlite`) |

### `dedup match`

| Option | What it does |
| --- | --- |
| `--csv CSV` | Also write every matched copy to this file |
| `--show N` | Within-account groups to list in the report (default 20) |
| `--db DB` | Index database path |

### `dedup plan`

| Option | What it does |
| --- | --- |
| `--scope {cross,within,both}` | Duplicates across accounts (default), within one account, or both |
| `--keep ACCOUNT` | The account whose copies are kept: a full address or a unique part of one. Required unless `--scope within` |
| `--keep origin` | Remove only copies that were auto-forwarded from another account; keep the one in the account they came from |
| `--ignore-label LABEL` | A user label that may be lost on removal (repeatable) |
| `--out OUT` | Plan CSV path (default `<data root>/dedup/dedup-plan.csv`) |
| `--db DB` | Index database path |

### `dedup apply`

A dry run unless `--label` or `--trash` is given.

| Option | What it does |
| --- | --- |
| `--label [NAME]` | Add the label NAME (default `dedup-redundant`) to removals that pass the checks; it is created if missing |
| `--trash [NAME]` | Move removals that a `--label` run labelled NAME, that pass the checks and still carry the label, to Trash; the others, and those an earlier `--trash` run trashed, aren't read |
| `--limit N` | Only consider the first N removals in the plan (with `--trash`, of those a label run labelled and no trash run trashed) |
| `--yes` | Don't ask for confirmation before trashing |
| `--plan PLAN` | Plan CSV to apply (default `<data root>/dedup/dedup-plan.csv`) |
| `--log LOG` | Apply log, appended to (default `<data root>/dedup/dedup-apply-log.csv`) |
| `--config CONFIG` | Config folder |

`dedup auth` still works, as an alias of [`auth`](configuration.md#auth).
