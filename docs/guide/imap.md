# Backing up an IMAP account

mailcellar can back up an account on another provider's server, or your
own, over IMAP: Fastmail, iCloud, a self-hosted Dovecot. The backup sits
next to your Gmail backups, so `search`, `export`, the reports, `verify`
and legal holds cover it too.

It is **backup only**, and **read-only**. Folders are opened with IMAP's
`EXAMINE` and messages fetched with `BODY.PEEK[]`, so nothing on the
server changes, not even a message's read state. Offloading, removing
duplicates and restoring work on Gmail accounts only, for now
([#108](https://github.com/mailcellar/mailcellar/issues/108)).

## 1. Add the account

```bash
mailcellar setup alex@example.org --imap imap.example.org
```

`setup` asks for the password, signs in, lists the folders it will back
up (and Junk and Trash, which it reads but leaves out), and says whether
the server could take part in offloading later. Only then does it write
anything (the account is named `alex`, from the address; `--name`
chooses another):

- `<config>/<name>/imap.toml`: the server (`host`, and `port`, `username`
  or `cafile` if you gave them);
- `<config>/<name>/imap-password`: the password, readable only by you;
- the address in `accounts.toml`.

If the login is refused, nothing is written. Options:

| Option | When you need it |
| --- | --- |
| `--port PORT` | The server doesn't use 993 (IMAP over TLS) |
| `--username NAME` | You log in with something other than the address |
| `--cafile FILE` | Your own server has a certificate from a private CA |
| `--name NAME` | You want another config name than the one from the address |
| `--no-auth` | Write the account without signing in now |

Many providers want an **app password** for IMAP rather than your
account's own: Fastmail, iCloud and Yahoo all do. Make one in the
provider's account settings and give that to `setup`.

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

## What it doesn't do

- Change anything on the server.
- Offload, remove duplicates or restore: those rely on Gmail's labels and
  30-day Trash, and come to IMAP later, on servers that can do them
  safely.
- Gmail searches (`query` in offload rules): an IMAP account has none.
- Keep IMAP keywords other than read and flagged (`$Forwarded` and the
  like).

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
shared folder you can't read, say). The rest is backed up, and the
skipped folder's mail isn't taken as deleted.

**"certificate verify failed":** your server's certificate isn't one the
system trusts. Give its CA with `--cafile`.
