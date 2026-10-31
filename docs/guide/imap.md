---
description: "Back up iCloud Mail, Yahoo Mail, Fastmail or a self-hosted Dovecot server over IMAP, read-only, then search, offload, deduplicate and restore it as with Gmail."
---

# Backing up an IMAP account

mailcellar can back up an account on another provider's server, or your
own, over IMAP: Fastmail, iCloud, a self-hosted Dovecot. The backup sits
next to your Gmail backups, so `search`, `export`, the reports, `verify`
and legal holds cover it too.

**Backing up is read-only.** Folders are opened with IMAP's `EXAMINE` and
messages fetched with `BODY.PEEK[]`, so nothing on the server changes,
not even a message's read state. **Offloading and removing duplicates**
also work, on servers that can move mail to Trash safely (step 3 below),
and so does **restoring** it (step 4), also between Gmail and IMAP
accounts.

## 1. Add the account

```bash
mailcellar setup alex@example.org --imap imap.example.org
```

`setup` asks for the password, signs in, lists the folders it will back
up (and Junk and Trash, which it reads but leaves out), and says whether
the server could take part in offloading later. Only then does it write
anything (the account is named `alex`, from the address; `--name`
chooses another):

- `<config>/<name>/imap.toml`: the server (`host`, and `port`, `username`,
  `cafile` or `review` if you gave them);
- `<config>/<name>/imap-password`: the password, readable only by you;
- the address in `accounts.toml`.

If the login is refused, nothing is written. Options:

| Option | When you need it |
| --- | --- |
| `--port PORT` | The server doesn't use 993 (IMAP over TLS) |
| `--username NAME` | You log in with something other than the address |
| `--cafile FILE` | Your own server has a certificate from a private CA |
| `--review folder` | For offload and dedup: your mail client doesn't show keywords, or the server doesn't keep them (setup says which), so mark mail for review by moving it to a `mailcellar/review/` folder instead |
| `--name NAME` | You want another config name than the one from the address |
| `--no-auth` | Write the account without signing in now |

Many providers want an **app password** for IMAP rather than your
account's own: Fastmail, iCloud and Yahoo all do. Make one in the
provider's account settings and give that to `setup`.

| Provider | Server (`--imap`) | Note |
| --- | --- | --- |
| iCloud Mail | `imap.mail.me.com` | `--username` with the part of the address before `@` (Apple: the full address if that fails); it has no MOVE, so offload and dedup are refused and restore only uploads |
| Yahoo Mail | `imap.mail.yahoo.com` | it keeps no custom keywords: add `--review folder` to offload |
| Fastmail | `imap.fastmail.com` | not yet tried with mailcellar |

Outlook.com and Microsoft 365 want OAuth over IMAP (XOAUTH2), which
mailcellar doesn't do yet, so they aren't supported: `setup` refuses an
Outlook.com, Hotmail, Live or MSN address, and Microsoft's servers, before
asking for a password.

To change the review mode later, without the password:

```bash
mailcellar setup alex --review folder
```

Mail already marked for review the other way stays marked.

The connection is always TLS, with the server's certificate checked.
There is no option to send the password unencrypted.

## 2. Back it up

```bash
mailcellar backup alex
```

The first backup reads every folder and downloads every message; later
ones read each folder's list of messages and their flags, and download
only what is new. A scheduled backup (`mailcellar schedule install`)
includes the account like any other, with no token to renew.

What the backup keeps, in gyb's layout, as for Gmail:

- **Folders become labels.** INBOX, and the server's Sent and Drafts
  folders, become `INBOX`, `SENT` and `DRAFT`; other folders keep their
  names, with `/` for nesting (`Projects/2026`). A message in two
  folders is backed up once, with both labels.
- **Flags:** an unread message gets `UNREAD`, a flagged one `STARRED`.
- **Junk and Trash** are left out, as Spam and Trash are for Gmail. A
  message already backed up that moves to Trash stays in the backup,
  labelled `TRASH`.
- **Moves keep the message's identity.** Each message is known by its
  Message-ID, date and size, which a move doesn't change, so filing mail
  into another folder doesn't download it again.

`count` compares the server with the backup; `verify` and
`verify --chain` check the backup itself, as for Gmail.

## 3. Offload and remove duplicates

`offload apply` and `dedup apply` work on an IMAP account as on Gmail,
with two steps, on a server that has **MOVE** and a **special-use Trash**
(setup's report says "offload and dedup: possible"). On any other server
they're refused, saying what's missing: without MOVE, moving mail can
delete it for good.

1. `--label` marks each candidate for review, one of two ways, set by
   `setup --review`:
   - **keyword** (the default): `$mailcellar-` and the label
     (`$mailcellar-offload-candidate`) on every copy. Most mail clients
     show keywords as tags; **clear it on a message to keep it.** A server
     that keeps no custom keywords is refused here: review by folder
     (`setup` and the dry run say so beforehand). One
     that keeps none in some folders only fails those folders' mail, saying
     which, and marks the rest.
   - **folder**: every copy is moved into `mailcellar/review/<label>`
     (`mailcellar/review/offload-candidate`), and the folders it came from
     are recorded in the backup, so its labels there don't change while
     it's reviewed. **Move a message out of that folder to keep it.**
     Copies in Junk or Trash stay where they are, and a message already
     under another review (dedup's, say) is left to it and reported.
2. `--trash` checks what is still marked against the backup, moves every
   copy to the server's Trash with MOVE, and reads the mailbox again to
   confirm. Nothing is ever expunged or deleted.

The server, or a mail client, may empty its Trash at any time, not after
30 days as Gmail does: the check against the backup before each move is
what keeps offloading safe, not the Trash.

## 4. Restore

`restore` puts an IMAP backup's mail back into its account, or into
another account (`--to`), IMAP or Gmail, as it does for Gmail:

- a message still in Trash is moved back to the folders it lived in (a
  folder review's record, else its labels in the backup; the archive folder if none),
  and its review mark taken off;
- a message the server no longer has is appended with its flags and the
  date the server first received it (as the backup recorded it), into
  each of its folders (created if missing). Where the server stores it
  unchanged, it comes back with the same ID, so the next backup has
  nothing new to fetch; some servers add a header to appended mail (Yahoo
  adds `Content-Length`), so restore reads each message's ID back from what
  the server stored (by the UID the server gives it, else by its
  Message-ID and date among what arrived during the restore), and the
  next backup downloads it once;
- restored mail gets a `$mailcellar-restored_<date>` keyword, where the
  server keeps keywords (otherwise restore says so and carries on; in a
  folder that keeps none, the message is restored with a note in the log).

On a server without MOVE (iCloud), restore only uploads: a message still
in the server's Trash can't be moved out, so it is skipped, saying so,
and left unrecorded; once the Trash is emptied, the next restore appends
it. Setup's report says "restore: possible (uploads only …)" there.

**Between Gmail and IMAP accounts** the target decides: a Gmail backup
restored into an IMAP account has its labels made folders (INBOX and the
server's Sent and Drafts for Gmail's own, a folder per user label,
matched whatever its case and nested at `/` only; archived mail into the
folder the server marks as its archive, else Archive), unread
and starred as flags, and Important and the categories left out; an
IMAP backup restored into Gmail has its folders made labels. A message
filed under several labels goes into each of those folders.

The message goes back byte for byte: no Date header is added, as it is
for Gmail, since an IMAP server keeps the date apart. mailcellar's own
review marks (`$mailcellar-` keywords, `mailcellar/review/` folders) aren't
put back, and Gmail-only labels (Important, the categories) make no
folders when restoring a Takeout mbox.

## What it doesn't do

- Gmail searches (`query` in offload rules): an IMAP account has none.
- Keep IMAP keywords other than read, flagged and mailcellar's own
  (`$Forwarded` and the like).

## Problems

**"refused the login":** the password, or the username, is wrong; many
providers need an app password for IMAP. Run `setup` again: nothing was
written, and a later run replaces the password. Only a different host,
port, username or CA file needs `--force`, since it changes `imap.toml`.

**"set up as a Gmail account":** the address already has an OAuth client
or a token, and its backup holds Gmail's message IDs. An address is
backed up one way only, Gmail's API or IMAP.

**"can be read by others":** the password file's permissions were widened,
perhaps by copying the config folder. Run the `chmod 600` the message
gives.

**A folder is "skipped":** the server listed it but wouldn't open it (a
shared folder you can't read, say), or wouldn't list all its messages.
The rest is backed up, the skipped folder is left as it was in the
backup (its mail isn't taken as deleted), and the run ends with an
error, so a scheduled backup records it. While a folder is skipped,
`restore` appends nothing (the message may be in that folder, unseen) and
says so; it still moves back what it finds in Trash.

**"certificate verify failed":** your server's certificate isn't one the
system trusts. Give its CA with `--cafile`.

**`offload plan` selects nothing on an account backed up before 0.17.0:**
its older messages have no thread ID yet, so the plan takes them for gone
from the server. Run `mailcellar meta ACCOUNT` once to fill them in.
