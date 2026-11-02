# Security

mailcellar holds credentials that can read a mailbox, and some that can
change it: OAuth tokens for Gmail, a service-account key for a Google
Workspace domain, and passwords for IMAP accounts. It also keeps a full
copy of each account's mail. Problems with either are security problems.

## Reporting a vulnerability

Report it privately, through GitHub's
[private vulnerability reporting](https://github.com/mailcellar/mailcellar/security/advisories/new),
not as a public issue or pull request. Say what is affected, how to
reproduce it, and what an attacker could do with it.

Never include, in a report or anywhere else:
- tokens (`token-*.json`, or `dedup-token-*.json` from 0.9 and earlier) or `client_secrets.json`;
- an IMAP password (`imap-password`) or a service-account key
  (`service-accounts/*.json`);
- real mail: addresses, names, subjects, message content or attachments;
- the paths or names of your own accounts.

If a message is needed to reproduce the problem, build a synthetic one
with `example.com` addresses that shows the same structure.

Reports are acknowledged on a best-effort basis, with no guaranteed
response time. A fix is released as soon as it is ready, and the advisory
is published with it.

## Supported versions

Only the latest release, and the `main` branch, receive fixes.

## If a credential may have leaked

`mailcellar config` shows where each account's config folder is.

**A token:**
1. Revoke mailcellar's access to the account at
   <https://myaccount.google.com/permissions>. That stops every token
   issued to your OAuth client, for this account.
2. Delete the token files in the account's config folder. Only the
   read-only token is stored, unless `keep_modify_token = true` is set.
3. Authorise again: `mailcellar auth ACCOUNT --scope readonly`.

If `client_secrets.json` leaked too, delete the OAuth client in the Google
Cloud console and create a new one.

**An IMAP password:** it opens the whole mailbox, to read and to change.
1. Revoke it with the provider: for a Google app password, at
   <https://myaccount.google.com/apppasswords>; for an account password,
   change it.
2. Make a new one and give it to `mailcellar setup ADDRESS --imap HOST`,
   which replaces `imap-password`.

**A service-account key:** it reads every mailbox in the domain.
1. Delete the key in the Google Cloud console (the service account's
   **Keys** tab), or the client's entry in the Admin console's **Manage
   Domain Wide Delegation**, which stops it too.
2. Make a new key, run `mailcellar setup ADDRESS --service-account NEWKEY
   --force`, and delete the downloaded copy.

## What mailcellar does to limit the damage

- Through the Gmail API, read-only access unless a command needs to
  change the mailbox; modify access is asked for only by `dedup apply`,
  `offload apply` and `restore --yes`, and held in memory while that
  command runs, never written to disk (`keep_modify_token = true` in
  `settings.toml` stores it). A copy of the config folder can read the
  mailbox, but not change it. A service-account key is read-only:
  mailcellar refuses any other scope through it.
- An IMAP password has no read-only form: anyone with the config folder
  of an IMAP account can change its mailbox. mailcellar itself changes
  it only in the commands above.
- Tokens, IMAP passwords and service-account keys are written owner-only
  (mode 600), and mailcellar refuses a password or key file others can
  read. `setup` creates the config folder, and mailcellar the data root,
  owner-only (mode 700); one made before, or by hand, keeps its own mode
  (`chmod 700` it).
- The data root holds every backed-up message, unencrypted: keep it on
  an encrypted disk (FileVault on a Mac), and out of shared or synced
  folders that others can read.
- Nothing is ever permanently deleted from the mailbox: mailcellar only
  moves mail to Trash, and never expunges over IMAP.
- Each backup keeps a hash chain of what was added and removed, with its
  last link's hash in the config folder, so `verify --chain` shows a
  backup changed by anything that can't also reach the config folder
  ([the hash chain](https://mailcellar.github.io/mailcellar/reference/backup/#the-hash-chain)).
- No third party is involved: the OAuth client is your own, and the mail
  stays on your machine.

## Where mailcellar comes from

Only from the source at <https://github.com/mailcellar/mailcellar>, the
package `mailcellar` on PyPI, and the docs at
<https://mailcellar.github.io/mailcellar/>. It has no app or `.dmg` to
download, and never asks you to bypass macOS's warnings or remove its
quarantine flag. A paid macOS app sold as "Mailcellar" on newmac.app is
unrelated to this project
([how to tell](https://mailcellar.github.io/mailcellar/guide/troubleshooting/#is-this-the-mac-app-called-mailcellar)).
