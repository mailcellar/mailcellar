# Security

mailcellar holds OAuth tokens that can read, and with modify access
change, a Gmail account, and it keeps a full copy of that account's mail.
Problems with either are security problems.

## Reporting a vulnerability

Report it privately, through GitHub's
[private vulnerability reporting](https://github.com/mailcellar/mailcellar/security/advisories/new),
not as a public issue or pull request. Say what is affected, how to
reproduce it, and what an attacker could do with it.

Never include, in a report or anywhere else:
- tokens (`token-*.json`, or `dedup-token-*.json` from 0.9 and earlier) or `client_secrets.json`;
- real mail: addresses, names, subjects, message content or attachments;
- the paths or names of your own accounts.

If a message is needed to reproduce the problem, build a synthetic one
with `example.com` addresses that shows the same structure.

Reports are acknowledged on a best-effort basis, with no guaranteed
response time. A fix is released as soon as it is ready, and the advisory
is published with it.

## Supported versions

Only the latest release, and the `main` branch, receive fixes.

## If a token may have leaked

1. Revoke mailcellar's access to the account at
   <https://myaccount.google.com/permissions>. That stops every token
   issued to your OAuth client, for this account.
2. Delete the token files in the account's config folder
   (`mailcellar config` shows where). Only the read-only token is stored,
   unless `keep_modify_token = true` is set.
3. Authorise again: `mailcellar auth ACCOUNT --scope readonly`.

If `client_secrets.json` leaked too, delete the OAuth client in the Google
Cloud console and create a new one.

## What mailcellar does to limit the damage

- Read-only access unless a command needs to change Gmail; modify access
  is asked for only by `dedup apply`, `offload apply` and `restore --yes`,
  and held in memory while that command runs, never written to disk
  (`keep_modify_token = true` in `settings.toml` stores it). A copy of
  the config folder can read the mailbox, but not change it.
- Token files are written owner-only (mode 600). Make the config folder
  owner-only too (`chmod 700 ~/.config/mailcellar`); mailcellar doesn't
  create it.
- Nothing is ever permanently deleted from Gmail.
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
