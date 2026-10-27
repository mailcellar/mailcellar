# Troubleshooting and FAQ

Short answers to questions that come up. For something that has gone wrong
and needs steps to put right (a lost data root, an interrupted run, a
leaked token), see [Recovering when something goes wrong](recovery.md).

## A run is slow or seems stuck

Gmail limits how fast one user can make requests, and mailcellar slows
down when Gmail says so: it shrinks its batches and retries after a wait
that doubles from one second up to about a minute. A large mailbox, a first
backup, `offload apply` and `restore` all meet this.

To see what it is doing, add `-v` to the command:

```bash
mailcellar backup work -v
```

`-v` shows every retry and wait. Waits of 10 seconds or more are shown
without it, as an INFO line naming the account and the reason, so a run
held up by Gmail's quota says so. Progress lines go to stderr; `-q` turns
them off and leaves only warnings and errors.

If the retries are exhausted, the run reports the messages it could not
handle and exits with status 1; running it again carries on. Nothing is
lost, and the next run does not repeat what is already done. For work that
takes more than a few minutes (trashing is about 2,000 messages a minute,
uploading about six messages a second), run it in the background.

## `count` reports mail missing from the backup

```bash
mailcellar count work
```

The line `Missing from backup` is mail Gmail has that the backup lacks. Most
often it is mail that arrived since the last backup. Run `backup`, then
`count` again: the number should be zero. It also appears after a failed
or interrupted backup, for the same reason.

`count` prints the other lines only when they are not zero:

| Line | Meaning |
| --- | --- |
| `Chats (never backed up)` | Gmail chats, which are skipped on purpose (below). |
| `Only in backup (not in mailbox)` | The backup holds mail Gmail does not list: deleted for good, or in Trash or Spam. Offloaded mail is counted apart. |
| `  of which permanently deleted` | Gone from Gmail altogether. Only `backup --prune` moves these out of the backup. |
| `Offloaded (kept in backup)` | Mail you offloaded on purpose. |
| `Indexed but .eml missing` | The backup's index lists a message whose file is gone. The next `backup` downloads it again if Gmail still has it. |

## Chats are never backed up

Neither mailcellar nor gyb, whose format it keeps, stores Hangouts chats
(the `CHAT` and `CHATS` labels). `count` lists them on their own line so
they do not look like missing mail.

## Storage is still full after offloading

Gmail shares its storage with Google Drive and Google Photos: the free
15 GB, or a Google One plan, covers all three. If most of it is Drive or
Photos, offloading mail frees only Gmail's part. Google's
[storage page](https://one.google.com/storage) shows how it is split.

Offloaded mail also counts until it leaves Gmail's Trash, which Gmail
empties 30 days after the move, and Spam counts too.
`mailcellar report storage work` shows how much each batch has freed so
far and how much is still in Trash ([Offloading, step 8](offloading.md#8-check)).

## Signing in again every week

The Google Cloud project's consent screen is in *Testing* status, where
Google expires tokens after 7 days. Either accept the weekly sign-in (and
renew ahead of it with `mailcellar auth ACCOUNT --scope readonly --renew`), or
publish the app. See
[Setting up Google Cloud](google-cloud.md#testing-or-in-production).
The error you see in the meantime, and how to sign in ahead of time, is in
[the recovery guide](recovery.md#a-token-expired-or-was-revoked).

## Why not IMAP, or Google Takeout?

Both can copy mail out of Gmail. mailcellar uses the Gmail API because it
keeps what makes a Gmail mailbox a Gmail mailbox:

- **IMAP** presents labels as folders. A message with three labels is one
  message in Gmail but appears in three folders, so a backup over IMAP
  downloads it three times and has no single record of its labels.
- **Takeout** is a full export each time, so every backup copies the whole
  mailbox again, and the labels are kept only in a header inside each
  message.

The API gives each message once, with its labels and a change history,
so a later backup reads only what changed. It also lets mailcellar later
check a backup against Gmail, and put mail back with its labels.

## Is anything ever deleted?

- **From Gmail:** never permanently. The commands that change Gmail
  (`dedup apply`, `offload apply`, `restore --yes`) are dry runs unless you
  add a flag, and the strongest thing any of them does is move mail to
  Trash, which Gmail empties after 30 days. `restore` only adds.
- **From the backup:** nothing, unless you ask. `backup --prune` moves mail
  that is permanently gone from Gmail into `<data root>/_pruned/`, after
  asking, and refuses to move more than a tenth of the backup unless
  forced. Offloaded mail is never moved, and `--prune` moves files rather
  than deleting them.

Because of that, once mail has been offloaded and Gmail has emptied its
Trash, the backup holds the only copy. Keep a second copy of the data
root.
