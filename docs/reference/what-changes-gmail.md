# What changes Gmail

Everything is read-only except these, and each one asks for confirmation
or stages its changes:

| Command | What it changes | Access |
|---|---|---|
| `dedup apply --label` / `offload apply --label` | adds a review label to the planned messages | modify |
| `dedup apply --trash` / `offload apply --trash` | moves labelled messages to Gmail's Trash | modify |
| `auth ACCOUNT --scope modify` | with `keep_modify_token = true`, grants and stores modify access (changes nothing in the mailbox); otherwise only says that the commands above sign in when they run | modify |
| `restore ACCOUNT … --yes` | moves offloaded mail out of Trash, or uploads backed-up mail Gmail no longer has (into this account or, with `--to`, another), or mail from a pruned batch, an mbox file or another gyb folder; adds a `restored/<date>` label | modify |
| `backup --prune` | the *backup*: moves mail permanently deleted from Gmail into `<data root>/_pruned/` | read-only |

- **Nothing is ever permanently deleted from Gmail.** Removals go to Trash,
  after a review label. Removing the label from a message vetoes it.
  Gmail empties its Trash after 30 days; until then, a message can be moved
  back from Trash in Gmail.
- **Backups only add.** `backup`, `count` and `meta` never delete anything.
  The one exception is `--prune`, and it moves files to `_pruned/` rather
  than deleting them, behind a confirmation and a 10% guard.
- **Offloaded mail stays in the backup.** Once Gmail empties its Trash, the
  backup is the only copy, and `restore` uploads it from there.
  `offload apply --trash` reports first whether Time Machine covers the
  data root. Keep a second copy of the data root.
- **Restore only adds.** It deletes nothing, in Gmail or in the backup,
  and skips what Gmail already has.

Backups need only read access. `dedup apply`, `offload apply` and
`restore --yes` ask for modify access to the account they change.
