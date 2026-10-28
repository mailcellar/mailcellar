# What changes your mailbox

Everything is read-only except these, and each one asks for confirmation
or stages its changes:

| Command | What it changes | Access |
|---|---|---|
| `dedup apply --label` / `offload apply --label` | adds a review label to the planned messages | modify |
| `dedup apply --trash` / `offload apply --trash` | moves labelled messages to the mailbox's Trash | modify |
| `auth ACCOUNT --scope modify` | with `keep_modify_token = true`, grants and stores modify access (changes nothing in the mailbox); otherwise only says that the commands above sign in when they run | modify |
| `restore ACCOUNT … --yes` | moves offloaded mail out of Trash, or uploads backed-up mail the mailbox no longer has (into this account or, with `--to`, another), or mail from a pruned batch, an mbox file or another gyb folder; adds a `restored/<date>` label | modify |
| `backup --prune` | the *backup*: moves mail permanently deleted from the mailbox into `<data root>/_pruned/` | read-only |

- **Nothing is ever permanently deleted from the mailbox.** Removals go to
  Trash, after a review label. Removing the label from a message vetoes it.
  Gmail empties its Trash after 30 days (an IMAP server when it or a mail
  client chooses); until then, a message can be moved back from Trash.
- **Backups only add.** `backup`, `count` and `meta` never delete anything.
  Two commands take mail out of the backup, and only when you ask:
  `backup --prune` moves mail the mailbox has deleted to `_pruned/`, behind
  a confirmation and a 10% guard; `offload forget` deletes offloaded mail
  the mailbox has deleted too, after you type the count, holding it 30 days in
  `_forgotten/` before `--empty` deletes it for good.
- **Offloaded mail stays in the backup**, unless you forget it. Once the
  mailbox empties its Trash, the backup is the only copy, and `restore` uploads it
  from there.
  `offload apply --trash` reports first whether Time Machine covers the
  data root. Keep a second copy of the data root.
- **Restore only adds.** It deletes nothing, in the mailbox or in the
  backup, and skips what the mailbox already has.

On Gmail, backups need only read access; `dedup apply`, `offload apply`
and `restore --yes` ask for modify access to the account they change (the
Access column).

## On an IMAP account

The same commands, and only they, change an IMAP account's server, and
only one that has MOVE and a special-use Trash; any other is refused,
saying what's missing. Backing up opens folders read-only (`EXAMINE`) and
leaves even the read state alone.

- **`--label`** adds a `$mailcellar-` keyword, or, with review by folder,
  moves the message into a `mailcellar/review/` folder.
- **`--trash`** moves it to the folder the server marks `\Trash`.
- **`restore --yes`** moves it back out of Trash, or appends it where the
  server no longer has it, with its flags and date.
- **Nothing is expunged**, ever: when Trash is emptied is up to the server
  or a mail client, so the backup check before each move is what keeps
  offloading safe.

There are no scopes: the password allows reading and writing, so the
server's abilities are the check. Details:
[Backing up an IMAP account](../guide/imap.md).
