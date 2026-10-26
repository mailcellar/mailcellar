# Recovering when something goes wrong

What to do, case by case, when a run fails or something is lost. Each case
says what you see, what it means, and what to do. None of the first three
needs more than re-running a command; the fourth is about the data root
itself, and the fifth about a leaked credential.

The commands below are dry runs or only add to the backup unless they say
otherwise. A failed run leaves the backup as it was, apart from what it had
already saved.

## A token expired or was revoked

**What you see.** A command that talks to Gmail prints, then opens your
browser:

```
Stored readonly token for work@example.org was rejected (...); authorising again.
```

(`modify` in place of `readonly` for `dedup apply`, `offload apply` and
`restore --yes`.)

**What it means.** Google no longer accepts the saved token. Either you
revoked access, or the Google Cloud project's consent screen is in
*Testing* status, where Google expires tokens after 7 days. See
[Setting up Google Cloud](google-cloud.md#testing-or-in-production).

**What to do.** Nothing is lost. Sign in as the account in the browser and
tick the Gmail permission; the command then carries on. On a machine with
no browser, or from a scheduled job, authorise ahead of time:

```bash
mailcellar auth ACCOUNT --scope readonly   # for backup, count, meta, reports
```

Apply and `restore --yes` sign in for modify access each time they run, so
they need a browser; with `keep_modify_token = true` in `settings.toml`,
`mailcellar auth ACCOUNT --scope modify` stores that access ahead of time
instead.

`auth` changes nothing in the mailbox and takes every account if none is
named. (It used to be `mailcellar dedup auth`, which still works.) To avoid
the error, renew a token that is getting old before it runs out: add
`--renew`, which asks for consent again and starts Google's 7 days afresh.
mailcellar warns when a token is 5 days past consent, and `mailcellar config`
and `report health` show how old each one is. If it ends with `Google did not grant gmail.…`, the Gmail
permission box was not ticked; delete the token file it names, in the
account's folder in the config folder, and run it again. To stop the weekly
sign-in, publish the app (*In production*) as the Google Cloud guide
describes.

## More than four weeks since the last backup

**What you see.**

```
Gmail no longer has history back to the last run; listing every message instead.
```

**What it means.** Gmail keeps its change history for about four weeks. A
backup normally reads only the changes since the last run. After a longer
gap, that history is gone, so the run lists every message in the account
instead. It is slower, mostly the listing, but it is safe: it downloads
only the messages the backup does not already hold, and it deletes nothing.
The same listing happens on the first run in a folder and with `--full`.

**What to do.** Let it finish. If it stops partway, run it again; it
carries on, since messages already saved are skipped. The history position
is stored only after a run with no failed downloads, so a failed run is
repeated in full next time, not skipped. A scheduled backup at least every
four weeks avoids all of this: `mailcellar schedule install` sets one up (see
[Scheduled backups](scheduling.md)).

## An interrupted offload or restore

Both record each message as they go, so an interruption (a crash, a lost
connection, closing the terminal) is safe to follow with the same command.

**`offload apply --trash`.** Run it again, with the same options. Messages
the interrupted run had already trashed but not recorded are found (they are
in Trash, still carry the label, and are not yet recorded) and recorded
after the same backup checks, and the rest of the plan carries on. It says
`Recorded N messages an earlier run trashed without recording`. A dry run
(`offload apply`) reports them without recording. The labelling stage,
`--label`, can simply be repeated.

**`restore --yes`.** Run it again. Each message is recorded in the backup's
`meta.sqlite` as it succeeds, and the next run skips what is already back,
looking each message up in Gmail again. Gmail matches an upload to the
message that already holds its Message-ID, so a retried upload returns the
same message, not a second copy. The one exception is a message with no
Message-ID header; the dry run flags those, because a crash during their
upload can leave a duplicate. Mail restored but not yet indexed when the run
died is picked up by the next `backup`, which downloads it.

**`backup`.** Run it again; see the previous case.

To check afterwards, use `mailcellar count ACCOUNT` and, for an offload,
the batch's manifest in `<data root>/offload/batches/`.

## A damaged or lost data root

The data root holds the backups (`<email>/msg-db.sqlite`, `meta.sqlite` and
the `.eml` files), mailcellar's plans and logs, and the dedup index. What
you can rebuild depends on what is left.

**You have a copy of the data root** (a second disk, Time Machine,
whatever you set up). Restore the whole folder from it, as it was, to where
`mailcellar config` says the data root is; do not mix files from the copy
with files from the damaged folder. Then:

```bash
mailcellar config               # the folder is found; each account's state looks right
mailcellar backup               # catches up with what Gmail gained since the copy
mailcellar meta                 # fills meta.sqlite for anything it lacks
mailcellar dedup index          # rebuilds the dedup index
mailcellar count                # compare Gmail with the backup
```

`backup` after an older copy reads Gmail's history from where the copy
stopped, or lists everything if that is more than four weeks ago; either
way it catches up. `dedup.sqlite` is only an index, so `dedup index`
rebuilds it from the backups.

One thing an older copy cannot know: mail you offloaded **after** the copy
was made. Those messages are in the backup (they were downloaded earlier)
but not recorded as offloaded, so once Gmail has emptied its Trash,
`backup --sync` would offer to move them out of the backup as if Gmail had
lost them. Do not confirm that. Read what
`backup` and `count` say, and compare against the batch manifests if you
kept them, before using `--sync`.

**Only some files are damaged.** `mailcellar verify` lists them: files that
are missing, do not parse or no longer match their recorded content hash,
and offloaded mail missing from the index (`--offloaded` checks just that
mail, which has no other source). It changes nothing.

- A missing or unreadable `.eml` file: `backup` downloads it again while
  the message is in Gmail; the backup counts a message as present only if
  its file exists.
- A damaged or deleted `meta.sqlite`: `mailcellar meta` rebuilds the parsed
  fields from the `.eml` files. It cannot rebuild the records of what was
  offloaded or restored (the batch manifests in `offload/batches/` keep a
  readable copy of the offloads, but nothing reads them back in). Restore
  the file from a copy if you have one.
- A damaged `msg-db.sqlite`, the gyb index, is the hardest case. Restore it
  from a copy together with its `.eml` files.
- Mail that `backup --sync` moved out is in `<data root>/_pruned/`, one
  folder per run; `mailcellar restore ACCOUNT --from-pruned BATCH` puts it
  back into Gmail. A Google Takeout export, or another gyb folder, can be
  restored the same way with `--from-mbox` or `--from-folder` (see
  [Restoring mail](../reference/restore.md)).
- Plans and logs are only working files. Make a new plan with `dedup plan`
  or `offload plan`.

**What is lost without a copy.** Mail that is still in Gmail is not lost: a
new `backup` downloads it again. Mail that was **offloaded** exists only in
the backup once Gmail has emptied its Trash, 30 days after the offload. If
the backup is lost and there was no other copy, that mail is gone. Within
the 30 days, open Gmail's Trash and move the messages back by hand. This is
the reason `offload apply --trash` reminds you to keep a second copy of the
data root, and why you should.

`mailcellar export` is one way to keep such a copy: it writes a selection
(for example `--offloaded`) as an mbox file or a zip of `.eml` files, without
touching the backup. It is a copy of your mail, so store it as carefully as
the data root. See [Exporting mail](exporting.md).

## A leaked token or `client_secrets.json`

Revoke access, delete the token files and, if the client's secrets leaked
too, delete the OAuth client and make a new one. The steps are in
[SECURITY.md](https://github.com/mailcellar/mailcellar/blob/main/SECURITY.md#if-a-token-may-have-leaked), together with
how to report a vulnerability. Nothing in the backup needs changing.
